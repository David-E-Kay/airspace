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
rem board starts it. Where it lives is asked of Windows rather than assumed:
rem the installer puts `ollama` on the PATH, and its tray app sits in the same
rem folder, so any install location works and none is written down here.
rem
rem The tray app, not `ollama serve` - it allows only one of itself, so this
rem does nothing when Ollama is already up, and it leaves no console window
rem behind. BOARD_OLLAMA_URL means Ollama is somewhere else entirely, usually
rem another machine, so nothing is started locally. If Ollama is not found the
rem board simply starts without it: summaries stay blank, the footer says so,
rem and the board keeps asking, so starting Ollama later is enough.
if not defined BOARD_SUMMARY_MODEL goto :board
if defined BOARD_OLLAMA_URL goto :board
for /f "delims=" %%I in ('where ollama 2^>nul') do set "OLLAMA_APP=%%~dpIollama app.exe"
if defined OLLAMA_APP if exist "%OLLAMA_APP%" start "" "%OLLAMA_APP%"

:board
start "" pythonw "%~dp0dashboard.py"
