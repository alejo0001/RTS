# RTS

This repository contains a prototype trading bot for Deriv.com written in Python.
The script `deriv_bot.py` provides a simple Tkinter interface to load manual
signals and execute them automatically using the Deriv WebSocket API. It now
includes basic reconnection logic so that trades continue even if the
WebSocket connection drops.