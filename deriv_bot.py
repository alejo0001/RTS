import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import threading
import time
from typing import List
import ssl

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

    def buy(self, symbol: str, direction: str, duration: int, amount: float):
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
        self.safe_send(req)
        return json.loads(self.ws.recv())

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

    def _run(self):
        self.api.connect()
        try:
            for sig in list(self.signals):
                if not self.running:
                    break
                now = datetime.now()
                wait_time = (sig.time - timedelta(seconds=self.delay) - now).total_seconds()
                if wait_time > 0:
                    time.sleep(wait_time)
                amount = self.current_stake
                if self.percent:
                    # This is a placeholder for balance retrieval
                    balance = 1000
                    amount = balance * self.current_stake / 100
                print(f"Executing {sig.symbol} {sig.direction} {sig.timeframe} at {datetime.now()} amount {amount}")
                try:
                    self.api.buy(sig.symbol, sig.direction, int(sig.timeframe[1:]), amount)
                except Exception as e:
                    print(f"Error placing trade: {e}")
                # Here we would check result to update win/loss
                # Placeholder win assumption
                self.win_amount += amount
                if self.stop_win and self.win_amount >= self.stop_win:
                    print("Stop win reached")
                    break
                if self.stop_loss and self.loss_amount >= self.stop_loss:
                    print("Stop loss reached")
                    break
                if self.martingale:
                    self.current_stake *= 2
        finally:
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
        self._build()

    def _build(self):
        frm = ttk.Frame(self.root, padding=10)
        frm.grid()
        ttk.Label(frm, text="Token:").grid(column=0, row=0, sticky="w")
        self.token_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.token_var, width=40).grid(column=1, row=0)

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
        self.signals_text = tk.Text(frm, width=60, height=10)
        self.signals_text.grid(column=1, row=6, columnspan=2)

        self.start_button = ttk.Button(frm, text="Start", command=self.start_bot)
        self.start_button.grid(column=1, row=7, pady=5)

        self.bot = None

    def start_bot(self):
        token = self.token_var.get().strip()
        if not token:
            messagebox.showerror("Error", "Token required")
            return
        self.bot = DerivBot(token=token,
                            delay=self.delay_var.get(),
                            stake=self.stake_var.get(),
                            martingale=self.martingale_var.get(),
                            stop_win=self.stop_win_var.get(),
                            stop_loss=self.stop_loss_var.get(),
                            percent=self.percent_var.get())
        signal_lines = self.signals_text.get("1.0", tk.END).strip().splitlines()
        for line in signal_lines:
            if not line.strip():
                continue
            try:
                sig = parse_signal(line)
                self.bot.add_signal(sig)
            except Exception as e:
                messagebox.showerror("Error", str(e))
                return
        self.bot.start()
        messagebox.showinfo("Bot", "Bot started")

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    if tk is None:
        print("Tkinter not available. Exiting.")
    else:
        ui = BotUI()
        ui.run()