# Five ideas taken from AOS — design

Date: 2026-10-19. Source of the ideas: <https://github.com/tomtomsatnav/agent-orchestration-standard>
(AOS, a hook-based live monitor for Claude Code). Only its *watching* ideas
fit Airspace; its orchestrator, graph view, replay and cost readout do not and
are out of scope.

## Why

Airspace answers "which session wants me next?". Two of its known blind spots
send the wrong answer today, and two small security gaps exist in the page
server:

- A Claude session waiting on a **permission prompt** reads as `working`, so it
  never reaches the "stopped for you" strip. (README, "Two honest limits".)
- A Claude session that **stopped on an API error** (hit the usage limit, got
  signed out) reads as `done — your turn`, as if it had finished.
- The page server answers any request that reaches 127.0.0.1:8765, whatever
  `Host` it names (DNS-rebinding exposure of titles, file names, summaries).
- Command text on cards is shown raw, so a key or token in a command appears on
  screen and in screenshots.
- Two sessions in **different worktrees of one repo** that change the same file
  are invisible until they are merged, when they clash.

## Goals

1. **Waiting for approval** (item 1). A Claude session with an unanswered
   permission prompt shows state `approval`, label **"needs your approval"**,
   in the strip and glowing exactly like `asking`.
2. **Stopped on an error** (item 2). A Claude session whose last turn ended on
   an API error shows state `error`, with a label naming the cause:
   - `rate_limit` → "stopped: usage limit" plus "resets HH:MM" (local time)
     when `quotaLimits.resetsAt` is present.
   - `authentication_failed` → "stopped: signed out".
   - anything else → "stopped: error (<code>)".
   It is in the strip and glows like `done`.
3. **Address check** (item 4a). The server answers only requests whose `Host`
   hostname is `127.0.0.1` or `localhost`, and whose `Origin`, when present,
   is too. Anything else gets 403.
4. **Hide secrets** (item 4b). Command text shown on a card (`summarise_tool`,
   `short_tool`) has key-like strings replaced with `[redacted]`.
5. **Same file, different worktrees** (item 5). When two live sessions sit in
   different worktrees of the same repository and both worktrees have changed
   the same file relative to the trunk, both cards turn red with the existing
   warning format, head **"same file"**, text along the lines of:
   "a {who} session in another worktree has also changed <file>, so the two
   will clash when merged". Several shared files: name the first, then
   "and N more".

## Non-goals

- No new pop-up / desktop notifications (item 3 dropped: Claude desktop already
  notifies and the card already glows).
- No status line, MCP server, graph view, replay, cost or token readouts.
- No Codex support for items 1 and 2. Codex runs no Claude hooks, and no Codex
  error record exists on this machine to design against (only
  `turn_aborted` / `interrupted`, which is the user pressing stop). Do not
  invent a Codex error shape.
- No subagent tracking.

## Anti-goals (failure even if it "works")

- **Airspace writing into a session.** The new hook writes only to a file
  Airspace owns. It never prints output the session sees, never blocks, never
  changes a decision.
- **A hook that can break or slow a session.** It must fail open: any error →
  exit 0 silently. It must be fast (no `dashboard.py` import, no git).
- **A stale "needs your approval"** that stays up after the prompt is answered.
  Wrongly claiming the session is stuck is worse than the current blind spot.
- **Slowing the session-start hook or the edit guard.** Item 5 runs only in the
  board's own `collect()`, never in `flag_clashes()` (which both hooks share).
- **Adding a dependency.** Stdlib only, single `dashboard.py`, as today.

## Constraints

- Windows, Python 3 stdlib. Match the house style: plain functions in
  `dashboard.py`, docstrings that explain *why*, tests in
  `tests/test_dashboard.py` as plain `test_*` functions with bare `assert`s,
  each new test proven able to fail.
- The live folder is the main checkout (`C:\Users\david\repos\Multi Agent
  Dashboard`) — the global hooks run from it. All work happens in this
  worktree and branch; nothing reaches main until approved.
- Installing the new hook means one addition to `~/.claude/settings.json`.
  That is persistent configuration, so David does it (or explicitly approves
  it); the build only documents it in the README, like the two existing hooks.

## How — decided

### Item 1: waiting for approval

- New hook script `hooks/note-permission-prompt.py`, registered for Claude
  Code's **`PermissionRequest`** hook event. On each call it writes
  `{"ts": <now, ISO UTC>}` to `~/.claude/airspace/approval/<session_id>.json`
  (one small file per session, overwritten each time — no log, no rotation).
- The board decides, per Claude row: state is `approval` when
  1. `read_activity` says `working`, and
  2. that session's note exists, and
  3. the note's `ts` is **later than the last user/assistant entry's
     timestamp** — not `last_ts`, which also moves on `hook_success`,
     `queue-operation` and similar bookkeeping lines and would hide the state
     (builder check; my call).
  OPEN — see "Open question" below: a tool that is approved but still running
  writes nothing until it finishes, so this rule alone keeps saying "needs
  your approval" for the whole run.
  Why this clears itself: the transcript records the tool call *before* the
  prompt appears, and records the tool's result (approved or denied) *after*
  it is answered. So once answered, the transcript is newer than the note and
  the state falls back to whatever the transcript says. No second hook, no
  cleanup step.
- `approval` joins `PULSE_STATES` between `asking` and `done`:
  `('asking', 'approval', 'error', 'done')`. Order decided: a question first,
  then an approval, then an error stop, then a finished turn.
- Notes for dead sessions are harmless (only live rows are looked up). No
  cleanup.

### Item 2: stopped on an error

- In `read_activity`, if the last `convo` entry is an assistant entry with
  `isApiErrorMessage: true`, state is `error`, `detail` is the label above.
  Real shape seen on this machine (version 2.1.275):
  `{"type":"assistant","isApiErrorMessage":true,"error":"rate_limit",
  "apiErrorStatus":429,"quotaLimits":{"resetsAt":1789938000,...},
  "message":{"model":"<synthetic>",...}}`. Ten such records exist locally:
  six `rate_limit`, four `authentication_failed`.
- The model picker must skip `<synthetic>`; otherwise a card that stopped on an
  error names its model "<synthetic>". (Likely an existing bug — verify with a
  test first.)

### Item 4a: address check

- In `Handler.do_GET`, before anything else: parse the `Host` header's
  hostname; if not `127.0.0.1`/`localhost`, or an `Origin` header is present
  with a different hostname, `send_error(403)` and return. A missing `Host`
  is refused too. Must not refresh `_LAST_SEEN` (a refused request is not
  the window being open).

### Item 4b: hide secrets

- One `redact()` helper, applied to command text in `summarise_tool` and
  `short_tool`. Patterns taken from AOS: known key prefixes
  (`sk`, `pk`, `ghp`, `gho`, `xox?`, `AKIA`, `eyJ`) followed by 8+ token
  characters, and any run of 32+ characters from `[A-Za-z0-9+_=-]` (no `/`,
  no `.`) that mixes upper case, lower case and digits. Narrower than AOS on
  purpose (my call, from the builder check): AOS's rule blanks out branch
  names, long paths and commit ids; this one leaves those alone (commit ids
  are lower-case hex) at the cost of missing an all-lower-case key.
  Also applied to Codex command text in `codex_call_label` (my call). The text sent
  to the local summary model stays unredacted (it never leaves the machine);
  the summary it writes back is shown on the card, so that line is redacted
  too.

### Item 5: same file, different worktrees

- Uses git, not transcripts: covers Claude and Codex, edits made by shell
  commands, and committed-but-unmerged work.
- For each repository with live sessions in two or more *different* worktree
  folders: per folder, the changed-file set is
  `git diff --name-only <merge-base of HEAD and trunk>` (committed + staged +
  unstaged tracked changes) plus untracked files from `git status --porcelain`.
  Trunk is found the way `git-workspace-brief.py` does it
  (`origin/HEAD`, else `main`). Paths are repo-relative, so they compare
  across worktrees.
- Two folders sharing any path → both get the "same file" warning. Folders on
  the same branch are already warned "same branch"; still add "same file"
  only when it names something new (builder's call on de-duplication — see
  below).
- Runs in `collect()` after `flag_clashes(rows)`, once per folder per page
  build, only for repos that qualify. Repos with one live folder cost nothing.

## Left to the builder

- Exact wording of labels and warnings, within the meaning above.
- The card colour for `approval` and `error` (reuse `asking`'s and `done`'s
  look, or pick close neighbours that pass the existing theme's contrast).
- Whether "same branch" and "same file" both show when both are true.
- The test breakdown, so long as each goal has a test proven to fail first.

## Risks to check first

1. **Does `PermissionRequest` fire for desktop-app sessions?** The desktop app
   shows its own permission UI. If the hook does not fire there, item 1 does
   nothing for most of David's sessions. The plan's first step is a manual
   check: install a logging-only version of the hook, trigger a permission
   prompt in a desktop session, see if the file appears. If it does not fire,
   try the `Notification` hook (`notification_type` `permission_prompt`) the
   same way. If neither fires, stop item 1 and report back — do not build it
   on hope. The same check must also cover: (a) a call **auto mode approves
   on its own** — the note must NOT appear, or item 1 stops; (b) whether
   anything on disk changes **at the moment of approval** (transcript lines,
   the desktop app's `claude-code-sessions/.../local_<id>.json` record,
   which may hold pending-permission state given the app's
   `claude://code/needs-input` route); (c) which transcript lines land
   between the note and the answer.

## Open question (David's)

- What should the card say while an **approved** command is still running?
  **Answered by David, 2026-10-19: build item 1 only if a fix turns up.** The
  first step's hands-on check must find something on disk that changes at
  the moment of approval (check (b) above). If nothing does, drop item 1;
  items 2, 4a, 4b and 5 go ahead regardless.
- **How AOS handles it (checked 2026-10-19, `monitor.mjs` ~lines 333-372): it
  doesn't.** `PermissionRequest` and `Notification` set `wait = true`; the
  flag clears only on the next `PreToolUse`, `PostToolUse`,
  `PostToolUseFailure`, `Stop`, `StopFailure` or `UserPromptSubmit`. An
  approved command fires no event until it finishes, so AOS shows "waiting"
  for the whole run — the same false alarm. It also sets `wait` on every
  `Notification` type, including `idle_prompt` (its ignore list only
  silences the alert), and its ROADMAP says firing in the desktop app was
  never verified. Nothing to copy here; the check above stands.
2. **Item 5 cost.** Two extra git calls per qualifying folder per page build.
   Measure the page build before and after; if it grows noticeably, cache per
   folder for a short time.

## Task 1 results (2026-10-20)

Desktop-app session in ask mode, Claude Code v2.1.x, command
`powershell -c "Start-Sleep 20; 'done'"`, David clicked Allow ~5 s after the
prompt appeared. Case B (auto mode) was not run: it only mattered if (iii)
found a signal.

- (i) **Fires.** `PermissionRequest` fired 0.23 s after the `tool_use` line
  was written (00:16:23.675 → 00:16:23.906 UTC), carrying `session_id`,
  `tool_name`, `tool_input`, `permission_mode`. `Notification` with
  `notification_type: "permission_prompt"` followed 6 s later.
- (iii) **No signal at approval.** Between the `tool_use` line (00:16:23) and
  the `tool_result` (00:16:55, i.e. after the 20 s sleep) the transcript got
  no lines at all, and the desktop record (`local_<id>.json`) did not change.
  The record's first change was at turn end (`completedTurns`,
  `postTurnSummary`, `lastTurnReport`).
- (iv) Nothing lands between the note and the answer.
- Side finding: the hook's `transcript_path` was 267 characters long, and
  Python's plain `open()` failed on it (no long-path support on this machine);
  `\?\` prefix works. `tail_entries` would read such a session as empty.

**Decision rule outcome:** item 1 dropped unless a non-disk signal is accepted.
Untested candidate: an approved shell command runs as a child process of the
session's process, which exists only after approval.

**Decided 2026-10-20: item 1 dropped** (David: "we can drop it"). The
child-process idea was not tried. The README's "Two honest limits" paragraph
stays as it is.

AOS re-checked the same day against its current code (commit `cc4ed67`,
`plugin/monitor.mjs` ~328-372). It has not solved this. `PermissionRequest`
and `Notification` set `wait = true`. Only the next `PreToolUse`,
`PostToolUse`, `PostToolUseFailure`, `UserPromptSubmit`, `Stop` or
`StopFailure` clears it. After Allow, the next of those is `PostToolUse` when
the command finishes. AOS mostly uses `wait` to mute its "stuck" alarm, so a
late clear costs it little. Not measured here: when `PreToolUse` fires
relative to the prompt. Claude Code's docs put it before the permission
dialog, and our test did not log it.
