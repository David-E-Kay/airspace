# Session Board — design (v1)

Date: 2026-09-05
Status: approved, building

## Problem

Multiple Claude Code sessions run at once across worktrees of the same project,
and across different projects. The desktop app shows that sessions exist. It does
not show, in one place, which folder and branch each is on, whether it is busy,
or what it last did. The recurring failure is starting a new session without
knowing another one is already live in that worktree.

## Existing coverage

`~/.claude/hooks/git-workspace-brief.py` detects concurrent sessions by counting
recently-touched transcripts in **one** project slug directory. Worktrees are
separate directories and therefore separate slugs, so cross-worktree concurrency
is invisible to it. Same-folder concurrency (the dangerous case) is covered.

## Findings that shaped this design

- `~/.claude/sessions/<pid>.json` is a live process registry: `pid`, `sessionId`,
  `cwd`, `startedAt`, `entrypoint`, `kind`, `name`. Cross-checking `pid` against
  running processes gives exact liveness — no freshness heuristic needed.
- `~/.claude/projects/<slug>/<sessionId>.jsonl` carries per-line `gitBranch`,
  `cwd`, `timestamp`, tool calls with arguments, assistant text, and
  `type: last-prompt` records holding the user's most recent prompt.
- Slug = `re.sub(r'[^A-Za-z0-9]', '-', abspath(cwd))` — same rule as the hook.
- **Subagent internals are not persisted.** Zero `isSidechain` records exist
  across every transcript on disk. `~/.claude/tasks/` holds todo lists, not
  subagent state. Only the launch (`Agent` tool call) and completion are visible.
- `git rev-parse --git-common-dir` returns the same path for a main worktree and
  all its linked worktrees, making it the correct grouping key.

## Scope

Read-only. The board observes; it never plans, spawns, or writes to any session.

### Shows

Live sessions only, grouped by repository (main folder and its worktrees
together). Per session: folder, worktree name, branch, uncommitted file count,
minutes since last activity, and one line describing the last action.

Two warning badges, both facts already present in the data:

- **Same folder** — two live sessions share a `cwd`. This is the case where they
  can overwrite each other's uncommitted work.
- **Same branch** — two live sessions in one repository are on the same branch.

### Does not show

- Subagent internals (not recorded anywhere).
- Finished or historical sessions.
- Predicted or future collisions.
- Token or cost figures (`tare` and the token dashboards cover this).

## Delivery

A local HTTP server built on Python's standard library. The page is rebuilt on
each request, so it is never stale, and it refreshes itself every 10 seconds.

Launched as a stripped-down browser window (`--app=`), giving it its own taskbar
icon and no browser chrome. Chrome preferred, Edge fallback, `webbrowser.open`
last resort. Both Chrome and Edge confirmed present.

## Implementation

Single file `dashboard.py`, plus `test_dashboard.py`. Standard library only.
Estimated ~200 lines. No package structure at this size.

Transcripts reach 1.6 MB; only the final 64 KB of each is read.

### Test coverage

Four pieces of logic that could fail silently, each proven to fail before being
trusted:

1. cwd → project slug derivation
2. transcript tail → branch, last activity, one-line summary
3. registry entries filtered by process liveness
4. worktree grouping and the two warning conditions

## Deferred

- Recording subagent activity via a `PostToolUse` hook. Revisit only if the
  missing detail proves to matter in practice.
- Short history of recently-finished sessions.
- UI refinement, deliberately postponed until the board can be seen running.
