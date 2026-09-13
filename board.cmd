@echo off
rem Start the board with no console window hanging around.
rem pythonw is Python without a console; `start` returns immediately.

rem Optional: one-line summaries of each turn from a local model. Needs the
rem model pulled first (`ollama pull qwen2.5:1.5b-instruct`); Ollama itself
rem is started below.
rem Comment the line out to turn summaries off. See docs/summaries.md for
rem which model fits how much memory. Setting this in a terminal instead only
rem lasts for that window, and never reaches a double-clicked board.
set BOARD_SUMMARY_MODEL=qwen2.5:1.5b-instruct

rem Ollama is a separate program and does not start with Windows, so the
rem board starts it - the server on its own, not the Ollama desktop app. The
rem desktop app puts its chat window on your screen every time it opens, which
rem is not what you asked for by starting a dashboard.
rem
rem Started through PowerShell because batch has no way to launch a program
rem with no window at all: `start` would leave a console sitting in the
rem taskbar, and `start /b` would tie Ollama to this window and kill it when
rem this window closes a moment later.
rem
rem `ollama` is found on the PATH, which its installer sets up, so no install
rem folder is written down here. A second copy cannot take the port and quits
rem by itself, so this is safe to run when Ollama is already up. The `catch`
rem covers Ollama not being installed: the board still starts, summaries stay
rem blank, the footer says so, and it keeps asking - so starting Ollama later
rem is enough on its own. BOARD_OLLAMA_URL means Ollama is somewhere else,
rem usually another machine, so nothing is started locally.
if not defined BOARD_SUMMARY_MODEL goto :board
if defined BOARD_OLLAMA_URL goto :board
powershell -NoProfile -WindowStyle Hidden -Command "try { Start-Process -FilePath ollama -ArgumentList serve -WindowStyle Hidden } catch {}"

:board
start "" pythonw "%~dp0dashboard.py"
