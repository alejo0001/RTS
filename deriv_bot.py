import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import threading
import time
from typing import Dict, List
import ssl
import os
import sys

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

        if sig.martingale:
            print(f"📈 Inicio Martingala para {sig.symbol} {sig.direction} timeframe {sig.timeframe}")
        else:
            print(f"📈 Ejecutando señal simple para {sig.symbol} {sig.direction} timeframe {sig.timeframe}")

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
            if profit == 0:
                print(f"⚠️ No se obtuvo ganancia. Verificar contrato o posible error.")


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
                if profit == 0:
                    print(f"⚠️ Trade sin ganancia ni pérdida (break-even).")
                else:
                    print(f"🔁 Trade perdido. Pérdida: {-profit}")

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

        
        if sig.martingale:
            print(f"🏁 Fin Martingala para {sig.symbol}. Último profit: {profit}, stake final: {current_stake}")
        else:
            print(f"📈 fin operación simple para {sig.symbol} {sig.direction} timeframe {sig.timeframe}")

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
        self.console_visible = tk.BooleanVar(value=True)
        self.row_signals: Dict[str, Signal] = {}
        self.bot = None
        #self._build()
        self._build_ui()
        self.load_accounts()
        self.redirect_output()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def on_close(self):
        if self.bot:
            self.bot.stop()
            self.bot = None
        self.root.destroy()

    def redirect_output(self):
        sys.stdout = ConsoleRedirector(self.console_output)
        sys.stderr = ConsoleRedirector(self.console_output)

    def toggle_console(self):
        if self.console_visible.get():
            self.console_frame.grid()
        else:
            self.console_frame.grid_remove()

    def disable_all_inputs(self):
        self.account_combo["state"] = "disabled"
        self.stake_var_entry["state"] = "disabled"
        self.delay_var_entry["state"] = "disabled"
        self.percent_cb["state"] = "disabled"

        self.stop_win_entry["state"] = "disabled"
        self.stop_loss_entry["state"] = "disabled"
        self.martingale_cb["state"] = "disabled"
        self.apply_button["state"] = "disabled"

        self.start_button["state"] = "disabled"
        self.load_button["state"] = "disabled"
        self.signals_text["state"] = "disabled"
        self.tree["selectmode"] = "none"  # impedir clics

        self.stop_button["state"] = "normal"


    def enable_all_inputs(self):
        self.account_combo["state"] = "readonly"
        self.stake_var_entry["state"] = "normal"
        self.delay_var_entry["state"] = "normal"
        self.percent_cb["state"] = "normal"

        self.stop_win_entry["state"] = "normal"
        self.stop_loss_entry["state"] = "normal"
        self.martingale_cb["state"] = "normal"
        self.apply_button["state"] = "normal"

        self.start_button["state"] = "normal"
        self.load_button["state"] = "normal"
        self.signals_text["state"] = "normal"
        self.tree["selectmode"] = "browse"

        self.stop_button["state"] = "disabled"

    def set_ui_enabled(self, enabled: bool):
        
        state = "normal" if enabled else "disabled"
        # Entradas
        self.account_combo["state"] = "readonly" if enabled else "disabled"
        self.stake_var_entry["state"] = state
        self.delay_var_entry["state"] = state
        self.percent_cb["state"] = state

        # Parámetros globales
        self.stop_win_entry["state"] = state
        self.stop_loss_entry["state"] = state
        self.martingale_cb["state"] = state
        self.apply_button["state"] = state

        # Señales
        self.signals_text["state"] = state
        self.tree["selectmode"] = "browse" if enabled else "none"

        # Botones
        self.start_button["state"] = state
        self.load_button["state"] = state
        self.stop_button["state"] = "normal" if not enabled else "disabled"

    def stop_bot(self):
        if self.bot:
            self.bot.stop()
            self.bot = None
        self.set_ui_enabled(True)
        self.enable_all_inputs()
        self.stop_button["state"] = "disabled"
        messagebox.showinfo("Bot", "Bot detenido")

    def _build_ui(self):
        self.root.geometry("980x650")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        main = ttk.Frame(self.root, padding=10)
        main.grid(sticky="nsew")
        main.columnconfigure(1, weight=1)

        # Left Frame - Configuración
        left = ttk.LabelFrame(main, text="Configuración")
        left.grid(row=0, column=0, sticky="nw", padx=5, pady=5)

        ttk.Label(left, text="Cuenta:").grid(row=0, column=0, sticky="w")
        self.account_combo = ttk.Combobox(left, textvariable=self.account_var, state="readonly", width=30)
        self.account_combo.grid(row=0, column=1, sticky="w")
        ttk.Button(left, text="+", width=3, command=self.add_account_window).grid(row=0, column=2, padx=5)

        ttk.Label(left, text="Delay (s):").grid(row=1, column=0, sticky="w")
        self.delay_var = tk.IntVar(value=0)
        self.delay_var_entry = ttk.Entry(left, textvariable=self.delay_var, width=10)
        self.delay_var_entry.grid(row=1, column=1, sticky="w")

        ttk.Label(left, text="Stake:").grid(row=2, column=0, sticky="w")
        self.stake_var = tk.DoubleVar(value=1.0)
        self.stake_var_entry = ttk.Entry(left, textvariable=self.stake_var, width=10)
        self.stake_var_entry.grid(row=2, column=1, sticky="w")

        self.percent_var = tk.BooleanVar(value=False)
        self.percent_cb = ttk.Checkbutton(left, text="Stake %", variable=self.percent_var)
        self.percent_cb.grid(row=2, column=2, sticky="w")

        params = ttk.LabelFrame(left, text="Parámetros Globales")
        params.grid(row=3, column=0, columnspan=3, sticky="ew", pady=5)

        self.martingale_var = tk.BooleanVar(value=False)
        self.martingale_cb = ttk.Checkbutton(params, text="Martingale", variable=self.martingale_var)
        self.martingale_cb.grid(row=0, column=0, sticky="w")

        ttk.Label(params, text="Stop Win:").grid(row=1, column=0, sticky="w")
        self.stop_win_var = tk.DoubleVar(value=0.0)
        self.stop_win_entry = ttk.Entry(params, textvariable=self.stop_win_var, width=10)
        self.stop_win_entry.grid(row=1, column=1, sticky="w")

        ttk.Label(params, text="Stop Loss:").grid(row=2, column=0, sticky="w")
        self.stop_loss_var = tk.DoubleVar(value=0.0)
        self.stop_loss_entry = ttk.Entry(params, textvariable=self.stop_loss_var, width=10)
        self.stop_loss_entry.grid(row=2, column=1, sticky="w")

        self.apply_button = ttk.Button(params, text="Aplicar", command=self.apply_globals)
        self.apply_button.grid(row=3, column=0, columnspan=2, pady=5)

        # Right Frame
        right = ttk.Frame(main)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(3, weight=1)

        ttk.Label(right, text="Señales:").grid(row=0, column=0, sticky="w")
        self.signals_text = tk.Text(right, height=4)
        self.signals_text.grid(row=1, column=0, sticky="ew")

        btn_frame = ttk.Frame(right)
        btn_frame.grid(row=2, column=0, sticky="ew", pady=5)

        self.start_button = ttk.Button(btn_frame, text="Start", command=self.start_bot)
        self.start_button.pack(side="left", padx=2)

        self.stop_button = ttk.Button(btn_frame, text="Stop", command=self.stop_bot)
        self.stop_button.pack(side="left", padx=2)
        self.stop_button["state"] = "disabled"

        self.load_button = ttk.Button(btn_frame, text="Load", command=self.load_signals)
        self.load_button.pack(side="left", padx=2)

        # Frame para Treeview con scrollbars
        table_frame = ttk.Frame(right)
        table_frame.grid(row=3, column=0, sticky="nsew", pady=5)
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)

        xscroll = ttk.Scrollbar(table_frame, orient="horizontal")
        yscroll = ttk.Scrollbar(table_frame, orient="vertical")

        self.tree = ttk.Treeview(
            table_frame,
            columns=("use", "fecha", "hora", "symbol", "action", "tf", "sw", "sl", "mg"),
            show="headings",
            xscrollcommand=xscroll.set,
            yscrollcommand=yscroll.set,
            height=6
        )
        xscroll.config(command=self.tree.xview)
        yscroll.config(command=self.tree.yview)

        self.tree.grid(row=0, column=0, sticky="nsew")
        xscroll.grid(row=1, column=0, sticky="ew")
        yscroll.grid(row=0, column=1, sticky="ns")

        for col in ("use", "fecha", "hora", "symbol", "action", "tf", "sw", "sl", "mg"):
            self.tree.heading(col, text=col.capitalize())
            self.tree.column(col, anchor="center")

        self.tree.bind("<Button-1>", self.on_tree_click)
        self.tree.bind("<Double-1>", self.edit_signal)

        ttk.Checkbutton(right, text="Mostrar consola", variable=self.console_visible, command=self.toggle_console).grid(row=4, column=0, sticky="w")

        self.console_frame = ttk.Frame(right)
        self.console_frame.grid(row=5, column=0, sticky="nsew")

        self.console_output = tk.Text(
            self.console_frame, height=8, state="disabled",
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="white",
            font=("Consolas", 9)
        )
        self.console_output.pack(fill="both", expand=True)



    
    def get_config_path(self, filename="keys.json"):
        """Ruta persistente segura (escribible) para configuraciones."""
        base_dir = os.path.expanduser("~")  # Carpeta del usuario (segura para escritura)
        app_dir = os.path.join(base_dir, ".deriv_bot")  # Subcarpeta oculta para tu app

        if not os.path.exists(app_dir):
            os.makedirs(app_dir)

        return os.path.join(app_dir, filename)



    def load_accounts(self):
        """Load saved accounts from keys.json en una ruta segura."""
        path = self.get_config_path()
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump({}, f)

        try:
            with open(path, "r", encoding="utf-8") as f:
                self.accounts = json.load(f)
        except Exception:
            self.accounts = {}

        names = list(self.accounts.keys())
        self.account_combo["values"] = names
        if names:
            self.account_var.set(names[0])

    def save_accounts(self):
        """Save accounts to keys.json en una ruta segura."""
        path = self.get_config_path()
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


    def apply_globals(self):
        """Apply global parameters to checked signals in the table."""
        for row_id, sig in self.row_signals.items():
            if sig.use_global:
                sig.stop_win = self.stop_win_var.get()
                sig.stop_loss = self.stop_loss_var.get()
                sig.martingale = self.martingale_var.get()
                self.tree.set(row_id, "sw", sig.stop_win)
                self.tree.set(row_id, "sl", sig.stop_loss)
                self.tree.set(row_id, "mg", "Yes" if sig.martingale else "No")


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
        self.apply_globals()
        for row_id, sig in self.row_signals.items():
            # if sig.use_global:
            #     sig.stop_win = self.stop_win_var.get()
            #     sig.stop_loss = self.stop_loss_var.get()
            #     sig.martingale = self.martingale_var.get()
            #     self.tree.set(row_id, "sw", sig.stop_win)
            #     self.tree.set(row_id, "sl", sig.stop_loss)
            #     self.tree.set(row_id, "mg", "Yes" if sig.martingale else "No")
            self.bot.add_signal(sig)
        self.bot.start()

        self.set_ui_enabled(False)
        self.stop_button["state"] = "normal"

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

class ConsoleRedirector:
    def __init__(self, widget):
        self.widget = widget

    def write(self, message):
        self.widget.configure(state="normal")
        self.widget.insert("end", message)
        self.widget.see("end")
        self.widget.configure(state="disabled")

    def flush(self):
        pass

# class BotUI:
#     def __init__(self):
#         self.root = tk.Tk()
#         self.root.title("Deriv Bot")
#         self.accounts = {}
#         self.account_var = tk.StringVar()
#         self.console_visible = tk.BooleanVar(value=True)
#         self.row_signals: Dict[str, Signal] = {}
#         self.bot = None

#         self._build_ui()
#         self.load_accounts()
#         self.redirect_output()

#     def _build_ui(self):
#         self.root.geometry("980x650")
#         self.root.columnconfigure(0, weight=1)
#         self.root.rowconfigure(0, weight=1)

#         main = ttk.Frame(self.root, padding=10)
#         main.grid(sticky="nsew")
#         main.columnconfigure(1, weight=1)

#         # Left Frame - Configuration
#         left = ttk.LabelFrame(main, text="Configuración")
#         left.grid(row=0, column=0, sticky="nw", padx=5, pady=5)

#         ttk.Label(left, text="Cuenta:").grid(row=0, column=0, sticky="w")
#         self.account_combo = ttk.Combobox(left, textvariable=self.account_var, state="readonly", width=30)
#         self.account_combo.grid(row=0, column=1, sticky="w")
#         ttk.Button(left, text="+", width=3, command=self.add_account_window).grid(row=0, column=2, padx=5)

#         ttk.Label(left, text="Delay (s):").grid(row=1, column=0, sticky="w")
#         self.delay_var = tk.IntVar(value=0)
#         self.delay_var_entry = ttk.Entry(left, textvariable=self.delay_var, width=10)
#         self.delay_var_entry.grid(row=1, column=1, sticky="w")

#         ttk.Label(left, text="Stake:").grid(row=2, column=0, sticky="w")
#         self.stake_var = tk.DoubleVar(value=1.0)
#         self.stake_var_entry = ttk.Entry(left, textvariable=self.stake_var, width=10)
#         self.stake_var_entry.grid(row=2, column=1, sticky="w")

#         self.percent_var = tk.BooleanVar(value=False)
#         self.percent_cb = ttk.Checkbutton(left, text="Stake %", variable=self.percent_var)
#         self.percent_cb.grid(row=2, column=2, sticky="w")

#         params = ttk.LabelFrame(left, text="Parámetros Globales")
#         params.grid(row=3, column=0, columnspan=3, sticky="ew", pady=5)

#         self.martingale_var = tk.BooleanVar(value=False)
#         self.martingale_cb = ttk.Checkbutton(params, text="Martingale", variable=self.martingale_var)
#         self.martingale_cb.grid(row=0, column=0, sticky="w")

#         ttk.Label(params, text="Stop Win:").grid(row=1, column=0, sticky="w")
#         self.stop_win_var = tk.DoubleVar(value=0.0)
#         self.stop_win_entry = ttk.Entry(params, textvariable=self.stop_win_var, width=10)
#         self.stop_win_entry.grid(row=1, column=1, sticky="w")

#         ttk.Label(params, text="Stop Loss:").grid(row=2, column=0, sticky="w")
#         self.stop_loss_var = tk.DoubleVar(value=0.0)
#         self.stop_loss_entry = ttk.Entry(params, textvariable=self.stop_loss_var, width=10)
#         self.stop_loss_entry.grid(row=2, column=1, sticky="w")

#         self.apply_button = ttk.Button(params, text="Aplicar", command=self.apply_globals)
#         self.apply_button.grid(row=3, column=0, columnspan=2, pady=5)

#         # Right Frame - Signals, Buttons and Console
#         right = ttk.Frame(main)
#         right.grid(row=0, column=1, sticky="nsew")
#         right.columnconfigure(0, weight=1)

#         ttk.Label(right, text="Señales:").grid(row=0, column=0, sticky="w")
#         self.signals_text = tk.Text(right, height=4)
#         self.signals_text.grid(row=1, column=0, sticky="ew")

#         btn_frame = ttk.Frame(right)
#         btn_frame.grid(row=2, column=0, sticky="ew", pady=5)

#         self.start_button = ttk.Button(btn_frame, text="Start", command=self.start_bot)
#         self.start_button.pack(side="left", padx=2)

#         self.stop_button = ttk.Button(btn_frame, text="Stop", command=self.stop_bot)
#         self.stop_button.pack(side="left", padx=2)
#         self.stop_button["state"] = "disabled"

#         self.load_button = ttk.Button(btn_frame, text="Load", command=self.load_signals)
#         self.load_button.pack(side="left", padx=2)

#         self.tree = ttk.Treeview(right, columns=("use","fecha", "hora", "symbol", "action", "tf", "sw", "sl", "mg"), show="headings", height=6)
#         self.tree.grid(row=3, column=0, sticky="nsew", pady=5)
#         for col in ("use","fecha", "hora", "symbol", "action", "tf", "sw", "sl", "mg"):
#             self.tree.heading(col, text=col.capitalize())
#             self.tree.column(col, anchor="center")
#         self.tree.bind("<Button-1>", self.on_tree_click)
#         self.tree.bind("<Double-1>", self.edit_signal)

#         ttk.Checkbutton(right, text="Mostrar consola", variable=self.console_visible, command=self.toggle_console).grid(row=4, column=0, sticky="w")

#         self.console_frame = ttk.Frame(right)
#         self.console_frame.grid(row=5, column=0, sticky="nsew")

#         self.console_output = tk.Text(
#             self.console_frame, height=8, state="disabled",
#             bg="#1e1e1e", fg="#d4d4d4", insertbackground="white",
#             font=("Consolas", 9)
#         )
#         self.console_output.pack(fill="both", expand=True)

#     # Resto de métodos: redirect_output, toggle_console, disable_all_inputs,
#     # enable_all_inputs, set_ui_enabled, stop_bot, load_accounts, save_accounts,
#     # add_account_window, apply_globals, start_bot, load_signals,
#     # on_tree_click, edit_signal, run

#     # Puedes seguir aquí pegando tus métodos existentes sin necesidad de cambios lógicos

#     def redirect_output(self):
#         sys.stdout = ConsoleRedirector(self.console_output)
#         sys.stderr = ConsoleRedirector(self.console_output)

#     def toggle_console(self):
#         if self.console_visible.get():
#             self.console_frame.grid()
#         else:
#             self.console_frame.grid_remove()

#     def run(self):
#         self.root.mainloop()

if __name__ == "__main__":
    if tk is None:
        print("Tkinter not available. Exiting.")
    else:
        ui = BotUI()
        ui.run()