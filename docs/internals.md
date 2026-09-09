# Internals, and how to add a harness

The README is for using the board. This is for changing it — mainly for the
one change most likely to be wanted: teaching it to see a coding agent it does
not currently know about.

`dashboard.py` is a single stdlib-only file. There is no package, no
framework, no dependency to install. `test_dashboard.py` sits beside it and is
run directly. Keep it that way unless the file stops fitting in your head.

## The shape of the thing

Four stages, in this order:

1. **Discover** — ask each harness which of its sessions are running *right
   now*. Liveness is established from the operating system, never from "this
   file was touched recently".
2. **Describe** — read the tail of each live session's own log to work out what
   it is doing and what it last said.
3. **Enrich** — ask git about each folder once, and cache it per folder.
4. **Flag and render** — mark collisions, group by repository, emit one page.

`session_rows()` does 1–3. `flag_clashes()` does the collision half of 4.
`collect()` is the board's own entry point; `clashes_for()` is the hook's.
Both go through the same detector on purpose, so the board and the
session-start warning can never disagree with each other.

The board is read-only by design. It never writes to a session, sends a
session anything, or starts a process. Several features would be easy if it
did; that line is deliberate.

## The row contract

Everything downstream — collision detection, grouping, rendering — consumes a
list of plain dictionaries. A harness adapter produces them. This is the whole
interface between "how do I find sessions" and "how do they get shown".

Filled in by the harness adapter:

| key | meaning |
|---|---|
| `agent` | short lowercase name, e.g. `claude`, `codex`. Becomes the badge on the card. |
| `sid` | the session's own id, unique within that harness |
| `cwd` | absolute working directory. Everything else keys off this. |
| `pid` | process id, or `None` if the harness does not expose one |
| `title` | what to call this session on the card |
| `app` | which window it is running in, e.g. `desktop`, `terminal`. `''` if unknown. |
| `started` | ISO timestamp the session opened, for the "open 3h" chip |
| `state` | one of `working`, `thinking`, `asking`, `done` |
| `detail` | the action in progress, e.g. `editing timer.py`. Empty when the turn has ended. |
| `says` | the last thing the agent said, one line |
| `trail` | up to six recent tool calls, oldest first |
| `since` | ISO timestamp the current state began |
| `last_ts` | ISO timestamp of the last entry seen. Also drives the "silent 14m" note. |
| `model` | raw model id; `pretty_model()` trims it for display |

Added afterwards by `session_rows()`, so an adapter must not set them:
`repo`, `label`, `branch`, `dirty`, `is_main`, `committed` (all from
`git_info()`),
`folder`, and an empty `warnings` list. `collect()` later adds `pulse` (the
glow) and `summary` (the optional local-model line).

`read_activity()` and `read_codex_activity()` both return the `state` / `detail`
/ `says` / `trail` / `since` / `last_ts` / `model` block, so an adapter usually
ends with `**your_read_activity(...)` and never spells those keys out.

Two of these are worth a warning. `app` should be the window a person would go
and open, not an internal channel name — `pretty_app()` exists to trim the ids
each harness happens to use. And `model` must be whatever the harness actually
last ran with, read from the log rather than from configuration, or the card
will confidently show a model the session is not using.

## Adding a harness

Write one function, `<name>_rows()`, and add it to the `+` in `session_rows()`.
That is the entire wiring. Answer four questions:

**Which of its sessions are alive?** This is the part worth getting right, and
the part that is different for every harness. Two working examples:

- *Claude* keeps a live process registry at `~/.claude/sessions/<pid>.json`.
  Each entry carries `pid` and `procStart`, the process's own start time.
  `is_alive()` compares that against the running process. The start time
  matters: Windows hands a dead process's id straight to the next program, and
  "something holds this pid" resurrects finished sessions carrying their old
  folder, which then trips a false collision warning.
- *Codex* holds an exclusive file lock per thread in
  `~/.codex/thread-writer-locks/`. `codex_lock_held()` tries to open the lock;
  failing to open it means a live process still owns it. No registry, no pid —
  hence `pid: None` in `codex_rows()`.

If a harness offers neither, look for a lock, a socket, a pid file, or a
process name — in that order. **Do not fall back on file modification times.**
The original version of the session-start hook did exactly that, called a live
session idle after fourteen minutes, and could not see a second folder at all.

**Where is each session working?** Some harnesses record the folder in the
registry entry (Claude); some record it in the first line of the session log
(Codex, via `codex_meta()`). Rows without a folder are dropped — the folder is
what every later stage keys off.

**What is it doing?** Read the tail of the session's log, not the whole thing;
these files reach megabytes. `tail_entries()` seeks to the last 64 KB and
throws away the fragment left by cutting mid-line. Codex uses a larger window
because its turn markers sit at the start of a turn.

The state rule, if the harness's log has a similar shape: an agent turn ending
on a tool call is `working`; ending on text is `done`; a turn carrying a tool
result the agent has not answered yet is `thinking`; and a tool whose entire
job is handing control back — asking a question, submitting a plan — is
`asking` even though the log shows a tool call mid-flight.

**What is it called?** Any stable human-readable label. Claude prefers the
chat's own title and falls back to the registry's derived name; Codex reads
its thread titles. `sid[:8]` is an acceptable last resort.

Then add a colour for the new badge next to `.agent.claude` and `.agent.codex`
in the stylesheet, and a test that proves the adapter reads a real log
correctly.

## Collisions

`flag_clashes()` marks two things, both facts already present in the data
rather than predictions:

- **Same worktree** — two live sessions share a folder, so whichever saves last
  wins.
- **Same branch** — two live sessions in one repository, in *different*
  folders, are on the same branch, so their changes will mix.

Worktrees are grouped by `git rev-parse --git-common-dir`, which returns an
identical path for a main worktree and every linked worktree of it. That is
what makes "the same project in two folders" detectable at all.

`clashes_for(cwd, session_id, pid)` is the hook's door into the same code. It
adds a stand-in row for the session doing the asking, because at session start
that session has not written a transcript yet and a lone neighbour would
otherwise read as no collision. It then has to recognise its own row and drop
it, by session id **and** by pid. The pid rule is not redundant: the registry
is filed under the process id and is rewritten several seconds *after* the
session-start hook fires, so a session started by clearing the previous one
finds its own registry entry still naming its predecessor — same process, same
folder, different session id. Without the pid rule every cleared session opens
by warning about the session it replaced.

## Click-through, if the flag ever flips

Opening a session from the board is one small change away, and the missing
piece is not ours to supply.

The desktop app writes one record per session to
`%APPDATA%/Claude/claude-code-sessions/<a>/<b>/local_<uuid>.json`. Each holds
`sessionId` (the `local_...` form the deep link expects) alongside
`cliSessionId`, which is the uuid already used as the transcript filename and
in the process registry. Glob the tree once, build the reverse map, and every
row can carry the id the app answers to. The files also carry the app's own
`title`, `model`, `effort` and `permissionMode` - none of which are in a
transcript.

The routes are `claude://code/continue?session=<local id>` and
`claude://code/needs-input`, the second described in the bundle as opening the
session that has waited longest for a permission answer. Both are guarded by a
server-side feature gate that is currently off for this account. The bundle's
own override, `CLAUDE_DEV_FORCE_GATES`, returns an empty set when
`app.isPackaged` is true, so a released build cannot be talked into it locally.
Verified on app 1.46388.4.0 with CLI 2.1.260 on 2026-09-08; firing the link
changed nothing, including the record's own `lastFocusedAt`.

So: do not build the link until a fired deep link is observed to work. A link
that silently does nothing is worse than no link.

## Things that are deliberately absent

- **Subagent internals.** Nothing on disk records them. Only the launch and the
  completion of a subagent are visible in a transcript.
- **Finished sessions.** The board is a picture of now.
- **Token and cost figures.** Other tools already do this well.
- **Anything predictive.** Every warning corresponds to a state that exists at
  the moment the page was built.

## Two traps

The board serves on `127.0.0.1`, never `localhost` — the name costs about two
seconds per page while the network stack tries IPv6 first and gives up.

Editing `dashboard.py` with a search-and-replace script whose anchor text
contains a backslash tends to fail silently, because the backslash is consumed
somewhere on the way. Anchor on a fragment without one.

## Regenerating the README screenshot and the icon

`assets/demo_board.py` renders the real page from invented sessions and writes
`assets/demo.html`. Shoot that rather than a live board — a live one is full of
whatever you happen to be working on. Re-run it after changing the page, so the
picture stops being a lie.

```
python assets/demo_board.py
chrome --headless=new --hide-scrollbars --force-device-scale-factor=2 ^
       --window-size=900,1206 --screenshot=assets\screenshot.png ^
       "file:///C:/path/to/assets/demo.html"
```

The two numbers are the point. **900 CSS pixels wide at a scale factor of 2**
produces an 1800px image whose text is still legible after GitHub scales it
down to the README column; capturing at a native window width of ~1800 does
not, and that is how the first screenshot ended up unreadable. **1206** is the
page's full height at that width — headless Chrome captures the window, not the
document, so a short window silently crops. Re-measure with
`document.documentElement.scrollHeight` whenever the demo gains a card.

`demo_board.py` sets `BOARD_SUMMARY_MODEL` before importing `dashboard`,
because its invented rows carry summary lines and the footer would otherwise
say summaries were off.

`assets/make_icon.py` produces `icon.ico` and `icon-256.png`. Both scripts need
Pillow. The board itself does not — nothing in `dashboard.py` imports anything
outside the standard library.
