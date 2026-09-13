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
rem board starts it. This is its tray app rather than `ollama serve`: the
rem tray app allows only one of itself, so starting it when it is already
rem running does nothing, and it leaves no console window behind. If it is
rem installed somewhere else the line is skipped and summaries stay blank
rem until Ollama is up - the board says so in its footer and keeps asking.
if defined BOARD_SUMMARY_MODEL (
  if exist "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe" (
    start "" "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe"
  )
)

start "" pythonw "%~dp0dashboard.py"
