# RTS

This repository contains a prototype trading bot for Deriv.com written in Python.
The script `deriv_bot.py` provides a Tkinter interface to load manual
signals and execute them automatically using the Deriv WebSocket API. It now
includes basic reconnection logic so that trades continue even if the
WebSocket connection drops.
The martingale feature listens for trade results via WebSocket and, when
enabled, will continue doubling the stake **only** on losing trades. It will
repeat this process indefinitely until a trade finally returns a profit or a
stop condition is reached.

Multiple trading accounts can be stored in a `keys.json` file. The dropdown
labelled **Cuenta** lets you choose which account to use and the **+** button
opens a dialog to add new accounts. The first account in the file is selected
by default when the interface starts.

Signals can be loaded with the new **Load** button which parses the text area
and displays each entry in a table. The table lets you edit `Stop Win`,
`Stop Loss` and whether `Martingale` is used for every individual signal.
Tick the checkbox of a row if you want the global values to overwrite that
signal when the bot starts.