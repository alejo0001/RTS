# RTS

This repository contains a prototype trading bot for Deriv.com written in Python.
The script `deriv_bot.py` provides a simple Tkinter interface to load manual
signals and execute them automatically using the Deriv WebSocket API. It now
includes basic reconnection logic so that trades continue even if the
WebSocket connection drops.
The martingale feature listens for trade results via WebSocket and, when
enabled, will continue doubling the stake **only** on losing trades. It will
repeat this process indefinitely until a trade finally returns a profit or a
stop condition is reached.