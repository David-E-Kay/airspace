# Multi-agent session toolkit

Support for running several Claude Code and Codex sessions at once without
them treading on each other.

Read-only throughout. It reads the files those apps already write about
themselves and asks git about each folder. It never writes to a session,
starts anything, or plans work.

Two pieces, one detector between them:

- **The board** (`dashboard.py`) - one page showing what every live session
  is doing, refreshing itself.
- **The session-start hook** (`hooks/git-workspace-brief.py`) - tells a
  starting session where it is and who else is already there.

Both call `session_rows()` and `flag_clashes()` in `dashboard.py`, so the
page and the hook can never disagree about who is running.

## Running the board

Double-click the **Session Board** icon on the Desktop, or `board.cmd` in this
folder, or:

```
python dashboard.py
```

Close the window and the board stops about a minute later, giving back the
memory it was using.

If the Desktop icon is ever lost: right-click `board.cmd`, **Show more
options**, **Send to**, **Desktop (create shortcut)**.

It opens at <http://127.0.0.1:8765>. Use the numeric address rather than
`localhost` — the server is IPv4-only and Windows tries IPv6 first, which
costs about two seconds a page.

Stdlib only. No install step, no dependencies.

## Reading a card

The **left edge** is what the session is doing:

| State | Means |
|---|---|
| working | mid-tool-call |
| thinking | a tool finished, no reply yet |
| needs your answer | asked a question, or a plan is up for approval |
| done — your turn | finished its turn and stopped |
| no recent activity | nothing in the transcript to go on |

A card **glows** for 25 seconds when it moves into "needs your answer" or
"done". Replying puts the session back to work and the glow stops.

Under the state are up to three rows: the action it is on, **turn summary**
(the local model's read of the turn, if summaries are on) and **last message**
(what the session actually wrote).

The **right edge** names the agent, the app it is running in, and the model
underneath — "claude / desktop / Opus 5". The board cannot open a session for
you, so telling you which window to go and find it in is the next best thing.

Along the top of a card: the branch, how many files are uncommitted, and how
long the session has been open. If files have been sitting uncommitted while
nothing has been saved to that branch for over two hours, the chip says so —
that is the state where a crash or a careless branch switch costs you work.

At the very top of the page, a strip lists every session that has stopped —
one per line, each saying which kind of stopped it is. A session that **asked
a question** is holding a task open waiting for your answer; a session that is
**done** simply finished its turn. Both mean nothing moves until you act,
which is why they share the strip, but they are not the same thing and the
strip no longer pretends they are. Questions sort above finished turns, and
the projects holding them sort above the rest.

The count also goes in the window title, so the taskbar button reads
"2 waiting" without you opening the board at all.

A **red card** is a collision, with one of two warnings:

- *same worktree* — another session is editing the same files. Whoever saves
  last wins.
- *same branch* — another session in a different folder saves to this branch
  too. The two sets of changes will mix.

### Two things it cannot see

A session parked on a permission prompt reads as `working`. The transcript
records the tool call whether or not you have approved it yet, so there is
nothing to tell them apart without writing into the session, which this board
deliberately does not do.

What the board *can* say is that a busy session has written nothing for a
while: after ten minutes a working card reads "silent 14m". That is a fact,
not a diagnosis. It is equally consistent with a session waiting for you to
approve a command and one running a slow test suite, and the card does not
pretend to know which.

There is no click-through to a session. Claude registers a `claude://` handler
and the routes exist, but the whole `code/` family is gated off in this build.

## Optional: one-line summaries from a local model

The line on each card is picked out of what the session wrote — first line,
markdown stripped, cut at a sentence. A rule can only pick words that are
already there; it cannot compress meaning. A small local model can.

The board reads `BOARD_SUMMARY_MODEL` once, at startup. `board.cmd` sets it to
`qwen2.5:1.5b-instruct`; comment that line out to turn summaries off, or change
it to any model you have pulled. Either way, restart the board - a running one
will not pick up a change.

Pull the model first, or the line does nothing:

```
ollama pull qwen2.5:1.5b-instruct
```

The footer of the page names the model in use, or says `summaries: off`, so
you can tell at a glance whether the setting reached the board.

Setting the variable in a terminal only lasts for that window, and a board
started by double-clicking `board.cmd` never sees it. Put it in `board.cmd`,
or use `setx BOARD_SUMMARY_MODEL <model>` to make it permanent for new
processes.

A model named here but not installed is not an error: the request fails, the
card keeps its plain-text line, and the page is unaffected.

### It gives the GPU back

The model is held for five minutes after the last summary, then Ollama drops
it. Closing the board window drops it straight away — the page asks the board
for a fresh copy of itself every ten seconds, so a minute of silence means the
window is gone, and the board unloads the model and shuts down. Nothing is
left sitting on video memory.

`ollama ps` shows what is loaded, if you want to check.

### Choosing a model

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

`BOARD_OLLAMA_URL` points at a different Ollama if yours is not on
`http://127.0.0.1:11434`.

## The session-start hook

`hooks/git-workspace-brief.py` runs when a Claude Code session starts, in any
repository. It reports the branch, the trunk, how many files are uncommitted,
the worktree list, and any collision with a session already running.

Wire it up in `~/.claude/settings.json`:

```json
"SessionStart": [
  { "hooks": [ { "type": "command", "timeout": 15,
      "command": "python \"<path to this repo>/hooks/git-workspace-brief.py\"" } ] }
]
```

It takes about a second, against a fifteen-second budget, and the cost is
paid once per session rather than per turn.

The collision check used to be a guess: it counted session log files touched
in the last fifteen minutes, so a session idle for fourteen looked live, it
could not see Codex at all, and two folders sharing one branch were invisible
to it. It now asks the board's own code, which checks whether the process is
genuinely running.

If `dashboard.py` cannot be loaded, the brief says so out loud rather than
reporting no collisions. Silence would read as "nobody else is here", which
is the one wrong answer that costs you work.

## Tests

```
python test_dashboard.py
```

Every check was proven able to fail before being trusted: the logic it guards
was broken on purpose and the check watched to go red. A test that has never
failed is not a test.

## Changing it

`docs/internals.md` covers how the board is put together and, in particular,
how to teach it about a coding agent it does not yet know: write one function
returning rows in a documented shape, and add it to one list.
