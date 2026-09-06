# Session board

One page showing what every live Claude Code and Codex session is doing.

Read-only. It reads the files those apps already write about themselves, asks
git about each folder, and renders a page that refreshes itself. It never
writes to a session, starts anything, or plans work.

## Running it

Double-click `board.cmd`, or:

```
python dashboard.py
```

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

The **right edge** and the corner tag say which app it is — Claude or Codex.

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

## Tests

```
python test_dashboard.py
```

Every check was proven able to fail before being trusted: the logic it guards
was broken on purpose and the check watched to go red. A test that has never
failed is not a test.
