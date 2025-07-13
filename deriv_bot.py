import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import threading
import time
from typing import List
import ssl
import os

try:
    import tkinter as tk
    from tkinter import ttk, messagebox
except ImportError:
    tk = None  # for environments without tkinter

try:
    import websocket
except ImportError:
    websocket = None

@dataclass
class Signal:
    time: datetime
    symbol: str
    direction: str
    timeframe: str
    stop_win: float = 0.0
    stop_loss: float = 0.0
    martingale: bool = False
    use_global: bool = False

class DerivAPI:
    def __init__(self, token: str, app_id: str = "1089"):
        self.token = token
        self.url = f"wss://ws.binaryws.com/websockets/v3?app_id={app_id}"
        self.ws = None

    def safe_send(self, message: dict):
        """Send a message and reconnect on failure."""
        if self.ws is None:
            self.connect()
        try:
            self.ws.send(json.dumps(message))
        except (websocket.WebSocketConnectionClosedException, ssl.SSLError, ConnectionResetError):
            print("WebSocket cerrado. Reconectando...")
            self.connect()
            self.ws.send(json.dumps(message))

    def connect(self):
        if websocket is None:
            raise RuntimeError("websocket-client is required")
        self.ws = websocket.WebSocket()
        self.ws.connect(self.url)
        self.safe_send({"authorize": self.token})
        resp = json.loads(self.ws.recv())
        if resp.get("error"):
            raise RuntimeError(resp["error"]["message"])
        
        
    def recv(self):
        """Recibe un mensaje del websocket con reconexión si hay fallas."""
        if self.ws is None:
            self.connect()
        try:
            return json.loads(self.ws.recv())
        except (websocket.WebSocketConnectionClosedException, ssl.SSLError, ConnectionResetError) as e:
            print(f"⚠️ WebSocket desconectado durante recv(). Reintentando conexión... ({e})")
            self.connect()
            try:
                return json.loads(self.ws.recv())
            except Exception as e2:
                print(f"❌ Error tras reconexión: {e2}")
                raise e2

    def subscribe_contract(self, contract_id: int):
        """Subscribe to updates for a specific contract."""
        self.safe_send({
            "proposal_open_contract": 1,
            "contract_id": int(contract_id),
            "subscribe": 1,
        })

    def forget_all(self, stream_type: str):
        """Forget all subscriptions of a given type."""
        self.safe_send({"forget_all": stream_type})


    def buy(self, symbol: str, direction: str, duration: int, amount: float, retries: int = 3, delay: float = 2.0):
        contract_type = "CALL" if direction.upper() == "CALL" else "PUT"
        req = {
            "buy": 1,
            "price": amount,
            "parameters": {
                "amount": amount,
                "basis": "stake",
                "contract_type": contract_type,
                "currency": "USD",
                "duration": duration,
                "duration_unit": "m",
                "symbol": symbol,
            },
        }
        for attempt in range(1, retries + 1):
            try:
                self.safe_send(req)

                while True:
                    response = self.recv()
                    if response.get("msg_type") == "buy":
                        return response
                    else:
                        print(f"🔄 Ignorando mensaje no relacionado al 'buy': {response}")
            except Exception as e:
                print(f"⚠️ Intento {attempt} fallido en buy(): {e}")
                time.sleep(delay)
                self.connect()

    def close(self):
        if self.ws:
            self.ws.close()

class DerivBot:
    def __init__(self, token: str, delay: int = 0, stake: float = 1.0,
                 martingale: bool = False, stop_win: float = 0.0,
                 stop_loss: float = 0.0, percent: bool = False):
        self.api = DerivAPI(token)
        self.delay = delay
        self.stake = stake
        self.martingale = martingale
        self.stop_win = stop_win
        self.stop_loss = stop_loss
        self.percent = percent
        self.signals: List[Signal] = []
        self.win_amount = 0.0
        self.loss_amount = 0.0
        self.current_stake = stake
        self.running = False
        self.thread = None

    def _start_pinger(self, interval=60):
        """Lanza un hilo para enviar pings periódicos al servidor."""
        def ping_loop():
            while self.running:
                try:
                    self.api.safe_send({"ping": 1})
                    print("📡 Ping enviado al servidor Deriv.")
                except Exception as e:
                    print(f"⚠️ Error al enviar ping: {e}")
                time.sleep(interval)
        threading.Thread(target=ping_loop, daemon=True).start()

    def _wait_result(self, contract_id: int) -> float:
        """Wait for a contract to be sold and return the profit."""
        self.api.subscribe_contract(contract_id)
        profit = 0.0
        attempts = 0
        while attempts < 300:  # máximo 300 intentos (~30s si 100ms entre cada uno)
            msg = self.api.recv()
            poc = msg.get("proposal_open_contract")
            if poc and poc.get("contract_id") == contract_id:
                if poc.get("is_sold"):
                    profit = float(poc.get("profit", 0))
                    self.api.forget_all("proposal_open_contract")
                    break
            time.sleep(0.1)
            attempts += 1
        else:
            print(f"Timeout esperando resultado para contrato {contract_id}")
        return profit

    def add_signal(self, signal: Signal):
        self.signals.append(signal)
        self.signals.sort(key=lambda s: s.time)

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join()

    def _run_signal(self, sig: Signal):
        now = datetime.now()
        wait_time = (sig.time - timedelta(seconds=self.delay) - now).total_seconds()
        if wait_time > 0:
            time.sleep(wait_time)

        win_amount = 0.0
        loss_amount = 0.0
        current_stake = self.stake
        profit = -1.0

        print(f"📈 Inicio Martingala para {sig.symbol} {sig.direction} timeframe {sig.timeframe}")

        while self.running and profit <= 0:
            amount = current_stake
            if self.percent:
                balance = 1000  # placeholder
                amount = balance * current_stake / 100

            print(f"🟡 Ejecutando {sig.symbol} {sig.direction} {sig.timeframe} @ {datetime.now()} con monto {amount}")

            try:
                result = self.api.buy(sig.symbol, sig.direction, int(sig.timeframe[1:]), amount)
            except Exception as e:
                print(f"❌ Error al ejecutar trade: {e}")
                break

            contract_id = result.get("buy", {}).get("contract_id")
            if not contract_id:
                print("❌ No se recibió contract_id. Resultado:", result)
                break

            profit = self._wait_result(contract_id)

            if profit > 0:
                print(f"✅ Trade ganado. Ganancia: {profit}")
                win_amount += profit
                current_stake = self.stake

                if sig.stop_win > 0 and win_amount >= sig.stop_win:
                    print("🎯 Stop win alcanzado para señal.")
                    break
                if self.stop_win > 0 and win_amount >= self.stop_win:
                    print("🎯 Stop win global alcanzado.")
                    self.running = False
                    break
                break  # terminó con ganancia

            else:
                print(f"🔁 Trade perdido. Perdida: {-profit}")
                loss_amount += -profit

                if sig.stop_loss > 0 and loss_amount >= sig.stop_loss:
                    print("🛑 Stop loss alcanzado para señal.")
                    break
                if self.stop_loss > 0 and loss_amount >= self.stop_loss:
                    print("🛑 Stop loss global alcanzado.")
                    self.running = False
                    break

                if not sig.martingale:
                    print("📌 Martingala desactivada para esta señal.")
                    break

                current_stake *= 2
                print(f"⚠️ Reintentando con Martingala. Nuevo stake: {current_stake}")

        print(f"🏁 Fin Martingala para {sig.symbol}. Último profit: {profit}, stake final: {current_stake}")


    def _run(self):
        self.api.connect()
        self._start_pinger()  # ✅ mantener la sesión WebSocket viva

        threads = []


        for sig in self.signals:
            t = threading.Thread(target=self._run_signal, args=(sig,))
            t.start()
            threads.append(t)

        for t in threads:
            t.join()

        self.api.close()
        self.running = False

      

PAIR_MAP = {
    "EURUSD": "frxEURUSD",
    "GBPUSD": "frxGBPUSD",
    "GBPJPY": "frxGBPJPY",
    "AUDJPY": "frxAUDJPY",
    "EURGBP": "frxEURGBP",
    "EURJPY": "frxEURJPY",
}

def parse_signal(line: str) -> Signal:
    parts = [p.strip() for p in line.split(';')]
    if len(parts) != 5:
        raise ValueError(f"Invalid signal format: {line}")
    date_part = parts[0]
    time_part = parts[1]
    dt = datetime.strptime(f"{date_part} {time_part}", "%d/%m/%Y %H:%M")
    symbol = PAIR_MAP.get(parts[2].upper(), parts[2].upper())
    direction = parts[3].upper()
    timeframe = parts[4].upper()
    return Signal(time=dt, symbol=symbol, direction=direction, timeframe=timeframe)

class BotUI:
    def __init__(self):
        if tk is None:
            raise RuntimeError("tkinter is required for the UI")
        self.root = tk.Tk()
        self.root.title("Deriv Bot")
        self.accounts = {}
        self.account_var = tk.StringVar()
        self._build()
        self.load_accounts()

    def _build(self):
        frm = ttk.Frame(self.root, padding=10)
        frm.grid()
        ttk.Label(frm, text="Cuenta:").grid(column=0, row=0, sticky="w")
        self.account_combo = ttk.Combobox(frm, textvariable=self.account_var, state="readonly", width=37)
        self.account_combo.grid(column=1, row=0, sticky="w")
        ttk.Button(frm, text="+", width=3, command=self.add_account_window).grid(column=2, row=0, sticky="w")

        ttk.Label(frm, text="Delay (s):").grid(column=0, row=1, sticky="w")
        self.delay_var = tk.IntVar(value=0)
        ttk.Entry(frm, textvariable=self.delay_var, width=10).grid(column=1, row=1, sticky="w")

        ttk.Label(frm, text="Stake:").grid(column=0, row=2, sticky="w")
        self.stake_var = tk.DoubleVar(value=1.0)
        ttk.Entry(frm, textvariable=self.stake_var, width=10).grid(column=1, row=2, sticky="w")

        self.percent_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="Stake %", variable=self.percent_var).grid(column=2, row=2, sticky="w")

        self.martingale_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="Martingale", variable=self.martingale_var).grid(column=1, row=3, sticky="w")

        ttk.Label(frm, text="Stop Win:").grid(column=0, row=4, sticky="w")
        self.stop_win_var = tk.DoubleVar(value=0.0)
        ttk.Entry(frm, textvariable=self.stop_win_var, width=10).grid(column=1, row=4, sticky="w")

        ttk.Label(frm, text="Stop Loss:").grid(column=0, row=5, sticky="w")
        self.stop_loss_var = tk.DoubleVar(value=0.0)
        ttk.Entry(frm, textvariable=self.stop_loss_var, width=10).grid(column=1, row=5, sticky="w")

        ttk.Label(frm, text="Signals:").grid(column=0, row=6, sticky="nw")
        self.signals_text = tk.Text(frm, width=60, height=5)
        self.signals_text.grid(column=1, row=6, columnspan=2)

        self.start_button = ttk.Button(frm, text="Start", command=self.start_bot)
        self.start_button.grid(column=1, row=7, pady=5, sticky="e")

        self.load_button = ttk.Button(frm, text="Load", command=self.load_signals)
        self.load_button.grid(column=2, row=7, pady=5, sticky="w")

        cols = (
            "use",
            "fecha",
            "hora",
            "symbol",
            "action",
            "tf",
            "sw",
            "sl",
            "mg",
        )
        self.tree = ttk.Treeview(frm, columns=cols, show="headings", height=6)
        self.tree.grid(column=0, row=8, columnspan=3, pady=5)
        self.tree.heading("use", text="✔")
        self.tree.heading("fecha", text="Fecha")
        self.tree.heading("hora", text="Hora")
        self.tree.heading("symbol", text="Símbolo")
        self.tree.heading("action", text="Acción")
        self.tree.heading("tf", text="Temp")
        self.tree.heading("sw", text="Stop Win")
        self.tree.heading("sl", text="Stop Loss")
        self.tree.heading("mg", text="Martingale")
        self.tree.column("use", width=40, anchor="center")
        self.tree.bind("<Button-1>", self.on_tree_click)
        self.tree.bind("<Double-1>", self.edit_signal)

        self.row_signals = {}

        self.bot = None

    def load_accounts(self):
        """Load saved accounts from keys.json."""
        path = os.path.join(os.path.dirname(__file__), "keys.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.accounts = json.load(f)
        except FileNotFoundError:
            self.accounts = {}
        except Exception:
            self.accounts = {}
        names = list(self.accounts.keys())
        self.account_combo["values"] = names
        if names:
            self.account_var.set(names[0])

    def save_accounts(self):
        """Save accounts to keys.json."""
        path = os.path.join(os.path.dirname(__file__), "keys.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.accounts, f, indent=2)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save accounts: {e}")

    def add_account_window(self):
        """Open a window to add a new account."""
        top = tk.Toplevel(self.root)
        top.title("Agregar cuenta")

        ttk.Label(top, text="Nombre:").grid(column=0, row=0, sticky="w")
        name_var = tk.StringVar()
        ttk.Entry(top, textvariable=name_var, width=30).grid(column=1, row=0)

        ttk.Label(top, text="Token:").grid(column=0, row=1, sticky="w")
        token_var = tk.StringVar()
        ttk.Entry(top, textvariable=token_var, width=30).grid(column=1, row=1)

        def save():
            name = name_var.get().strip()
            token = token_var.get().strip()
            if not name or not token:
                messagebox.showerror("Error", "Nombre y Token requeridos")
                return
            self.accounts[name] = token
            self.save_accounts()
            self.account_combo["values"] = list(self.accounts.keys())
            self.account_var.set(name)
            top.destroy()

        ttk.Button(top, text="Guardar", command=save).grid(column=0, row=2, columnspan=2, pady=5)


    def start_bot(self):
        token = self.accounts.get(self.account_var.get(), "").strip()
        if not token:
            messagebox.showerror("Error", "Cuenta requerida")
            return
        if not self.row_signals:
            messagebox.showerror("Error", "No se han cargado señales")
            return
        self.bot = DerivBot(
            token=token,
            delay=self.delay_var.get(),
            stake=self.stake_var.get(),
            martingale=self.martingale_var.get(),
            stop_win=self.stop_win_var.get(),
            stop_loss=self.stop_loss_var.get(),
            percent=self.percent_var.get(),
        )
        for row_id, sig in self.row_signals.items():
            if sig.use_global:
                sig.stop_win = self.stop_win_var.get()
                sig.stop_loss = self.stop_loss_var.get()
                sig.martingale = self.martingale_var.get()
                self.tree.set(row_id, "sw", sig.stop_win)
                self.tree.set(row_id, "sl", sig.stop_loss)
                self.tree.set(row_id, "mg", "Yes" if sig.martingale else "No")
            self.bot.add_signal(sig)
        self.bot.start()
        messagebox.showinfo("Bot", "Bot started")

    def load_signals(self):
        self.tree.delete(*self.tree.get_children())
        self.row_signals.clear()
        lines = self.signals_text.get("1.0", tk.END).strip().splitlines()
        for line in lines:
            if not line.strip():
                continue
            try:
                sig = parse_signal(line)
                sig.stop_win = self.stop_win_var.get()
                sig.stop_loss = self.stop_loss_var.get()
                sig.martingale = self.martingale_var.get()
                row_id = self.tree.insert(
                    "",
                    "end",
                    values=(
                        "",
                        sig.time.strftime("%d/%m/%Y"),
                        sig.time.strftime("%H:%M"),
                        sig.symbol,
                        sig.direction,
                        sig.timeframe,
                        sig.stop_win,
                        sig.stop_loss,
                        "Yes" if sig.martingale else "No",
                    ),
                )
                self.row_signals[row_id] = sig
            except Exception as e:
                messagebox.showerror("Error", str(e))
                return
    def on_tree_click(self, event):
        row_id = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if column == "#1" and row_id in self.row_signals:
            sig = self.row_signals[row_id]
            sig.use_global = not sig.use_global
            self.tree.set(row_id, "use", "✓" if sig.use_global else "")

    def edit_signal(self, event):
        row_id = self.tree.identify_row(event.y)
        if row_id not in self.row_signals:
            return
        sig = self.row_signals[row_id]
        top = tk.Toplevel(self.root)
        top.title("Edit Signal")
        ttk.Label(top, text="Stop Win:").grid(column=0, row=0, sticky="w")
        win_var = tk.DoubleVar(value=sig.stop_win)
        ttk.Entry(top, textvariable=win_var, width=10).grid(column=1, row=0)
        ttk.Label(top, text="Stop Loss:").grid(column=0, row=1, sticky="w")
        loss_var = tk.DoubleVar(value=sig.stop_loss)
        ttk.Entry(top, textvariable=loss_var, width=10).grid(column=1, row=1)
        mg_var = tk.BooleanVar(value=sig.martingale)
        ttk.Checkbutton(top, text="Martingale", variable=mg_var).grid(column=0, row=2, columnspan=2)

        def save():
            sig.stop_win = win_var.get()
            sig.stop_loss = loss_var.get()
            sig.martingale = mg_var.get()
            self.tree.set(row_id, "sw", sig.stop_win)
            self.tree.set(row_id, "sl", sig.stop_loss)
            self.tree.set(row_id, "mg", "Yes" if sig.martingale else "No")
            top.destroy()

        ttk.Button(top, text="OK", command=save).grid(column=0, row=3, columnspan=2, pady=5)
    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    if tk is None:
        print("Tkinter not available. Exiting.")
    else:
        ui = BotUI()
        ui.run()