# One-line summaries from a local model

Optional. Off unless you turn it on.

The line on each card is picked out of what the session wrote — first line,
markdown stripped, cut at a sentence. A rule can only pick words that are
already there; it cannot compress meaning. A small local model can.

## What the model reads

Three things: what you last typed, the whole of the session's last reply, and
the tools it has been running. All of it, not the trimmed line the card shows.

That last part used to be the whole input, and one trimmed sentence is not
enough to work from — the only move left to a model handed a sentence is to
reword it, which is what the lines read like. With the request and the tools
alongside it there is something to summarise, and the line can say what the
session is doing rather than restate how it said it.

Codex sessions are given the reply and the tools but not the request, because
a Codex prompt on disk arrives wrapped in pasted environment text.

## Turning it on

The board reads `BOARD_SUMMARY_MODEL` once, at startup. `board.cmd` sets it to
`qwen2.5:1.5b-instruct`; comment that line out to turn summaries off, or change
it to any model you have pulled. Either way, restart the board — a running one
will not pick up a change.

Pull the model first, or the line does nothing:

```
ollama pull qwen2.5:1.5b-instruct
```

The footer of the page names the model in use, or says `summaries: off`, so
you can tell at a glance whether the setting reached the board. If calls to
Ollama are coming back empty it says `no answer from Ollama` alongside the
name, because a named model and a working one are not the same thing.

Setting the variable in a terminal only lasts for that window, and a board
started by double-clicking `board.cmd` never sees it. Put it in `board.cmd`,
or use `setx BOARD_SUMMARY_MODEL <model>` to make it permanent for new
processes.

Ollama is a separate program that does not start with Windows, so `board.cmd`
starts it. It starts the server — `ollama serve` — and not the Ollama
desktop app, which would put its chat window on your screen every time you
opened the board. Nothing appears: no window, no taskbar button.

It goes through PowerShell because a batch file has no way to start a
program with no window at all. `start` leaves a console in the taskbar, and
`start /b` ties Ollama to the launcher's own window, which closes a moment
later and takes Ollama with it.

No install path is written down anywhere — `ollama` is found on your PATH,
which its installer sets up, so any install location works. Running it when
Ollama is already up is harmless: the second copy cannot take the port and
quits by itself. Set `BOARD_OLLAMA_URL` and nothing is started locally at
all, on the grounds that you have told the board Ollama lives somewhere
else.

If Ollama is not found the board still starts, without summaries. Start
Ollama yourself afterwards and the board picks them up on its own, no
restart needed.

A model named here but not installed is not an error: the request fails, the
card keeps its plain-text line, and the page is unaffected. A failure is not
remembered as that turn's summary either — the board stays off Ollama for a
minute and then asks again, so one that comes up late is still picked up.

`BOARD_OLLAMA_URL` points at a different Ollama if yours is not on
`http://127.0.0.1:11434`.

## It gives the GPU back

The model is held for five minutes after the last summary, then Ollama drops
it. Closing the board window drops it straight away — the page asks the board
for a fresh copy of itself every ten seconds, so two and a half minutes of
silence means the window is gone, and the board unloads the model and shuts
down. Nothing is left sitting on video memory.

The wait is that long on purpose. A browser slows a minimised window's timers
to roughly one tick a minute, so a shorter wait would read a window you had
simply clicked away from as one you had closed.

`ollama ps` shows what is loaded, if you want to check.

## Choosing a model

Budget by the graphics memory you have spare, not total. The model has to fit
alongside anything else using the card.

| Spare memory | Model | Notes |
|---|---|---|
| ~1 GB | `gemma3:1b` | ~800 MB. Works, blunter phrasing. |
| ~2 GB | `qwen2.5:1.5b-instruct` | ~1 GB on disk, ~1.2 GB loaded. The default recommendation. |
| ~4 GB | `llama3.2:3b-instruct-q4_K_M` | ~2 GB. Noticeably better lines. |
| 8 GB+ | `qwen2.5:7b-instruct` | ~4.7 GB. Diminishing returns for a 12-word line. |

Anything that does not fit falls back to the main processor and takes tens of
seconds per line. That does not break the board — the request runs on its own
thread and the card keeps its plain-text line until an answer arrives — but
you will rarely see a summary.

## While you wait

The first summary of a session is slow, because Ollama has to load the model
into the graphics card before it can answer anything. Every one after it takes
a couple of seconds.

So the summary line says what it is doing rather than sitting blank: *loading
the local model* until the first answer lands, *summarising* for every wait
after that, both in grey italics so they do not read as the model's own words.

A board with Ollama switched off shows no summary line at all. It cannot know
Ollama is down until it has tried, so the first refresh still says it is
loading; the one after that drops the line, and the footer says why.
