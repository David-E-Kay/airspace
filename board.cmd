@echo off
rem Start the board with no console window hanging around.
rem pythonw is Python without a console; `start` returns immediately.
rem ponytail: this window still blinks once as it closes. A .lnk shortcut
rem would avoid even that, but it is a binary file full of absolute paths.

rem Optional: one-line summaries of each turn from a local model.
rem Off unless the line below is uncommented. Needs Ollama running and the
rem model pulled first (`ollama pull qwen2.5:1.5b-instruct`). See README.md
rem for which model fits how much memory. Setting this in a terminal instead
rem only lasts for that window, and never reaches a double-clicked board.
set BOARD_SUMMARY_MODEL=qwen2.5:1.5b-instruct

start "" pythonw "%~dp0dashboard.py"
