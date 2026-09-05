@echo off
rem Start the board with no console window hanging around.
rem pythonw is Python without a console; `start` returns immediately.
rem ponytail: this window still blinks once as it closes. A .lnk shortcut
rem would avoid even that, but it is a binary file full of absolute paths.
start "" pythonw "%~dp0dashboard.py"
