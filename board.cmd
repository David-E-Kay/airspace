@echo off
rem Start the board with no console window hanging around.
rem pythonw is Python without a console; `start` returns immediately.

rem Optional: one-line summaries of each turn from a local model. Needs Ollama
rem running and the model pulled first (`ollama pull qwen2.5:1.5b-instruct`).
rem Comment the line out to turn summaries off. See docs/summaries.md for
rem which model fits how much memory. Setting this in a terminal instead only
rem lasts for that window, and never reaches a double-clicked board.
set BOARD_SUMMARY_MODEL=qwen2.5:1.5b-instruct

start "" pythonw "%~dp0dashboard.py"
