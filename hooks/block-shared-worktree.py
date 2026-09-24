#!/usr/bin/env python3
"""PreToolUse hook: one session per working folder. The later arrival yields.

Before Edit/Write/MultiEdit/NotebookEdit, find the git working folder the target
file lives in (the main checkout or a worktree - each counts separately). If
another live Claude or Codex session is working in that same folder AND started
before this one, block. The first session keeps the desk; the newcomer is told
to use a worktree.

The session-start brief warns once, and only the newcomer, and only as advice.
This is the enforcing half: it runs on every edit, so a session that arrives
after this one started is still caught.

Who is live comes from dashboard.py, the same code the board renders and the
brief calls, so all three agree.

Known, accepted limitations:
- Edits made through Bash/PowerShell (sed -i, Set-Content, ...) are not caught.
- Codex does not run Claude hooks: Codex is never blocked, only Claude sessions
  that arrive after it.
- A finished session left open still holds its folder until it is closed.

Fails open: any error (unreadable input, dashboard.py missing, git absent)
exits 0, so a broken hook never blocks unrelated work.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def toplevel(path):
    """The working folder (checkout or worktree) holding `path`, or None."""
    d = os.path.abspath(path)
    while d and not os.path.isdir(d):  # Write may target a folder not made yet
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    try:
        r = subprocess.run(['git', '-C', d, 'rev-parse', '--show-toplevel'],
                           capture_output=True, text=True, timeout=10,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except Exception:
        return None
    return os.path.normcase(os.path.abspath(r.stdout.strip())) if r.returncode == 0 else None


def blockers(target_top, my_started, others, top_of=toplevel):
    """Other sessions in the same working folder that got there first.

    Worktrees sit physically inside the main checkout (.claude/worktrees/...),
    so folders are compared by what git says they are, never by path prefix.
    Timestamps are parsed, not compared as text: Claude and Codex write
    different ISO formats. Unknown start on either side counts as "they were
    first" - when in doubt the newcomer yields, never the session already
    working.
    """
    import dashboard
    mine = dashboard.moment(my_started)
    out = []
    for o in others:
        if top_of(o['cwd']) != target_top:
            continue
        theirs = dashboard.moment(o.get('started'))
        if mine is None or theirs is None or theirs <= mine:
            out.append(o)
    return out


def main():
    try:
        data = json.load(sys.stdin)
        tool_input = data.get('tool_input') or {}
        raw = tool_input.get('file_path') or tool_input.get('notebook_path')
        if not raw:
            return 0
        target_top = toplevel(os.path.join(data.get('cwd') or os.getcwd(), raw))
        if not target_top:
            return 0  # not in a git repo

        import dashboard

        sid = data.get('session_id') or ''
        # Claude hands every child this. The registry is keyed by pid and lags
        # a cleared session by a few seconds, so the id alone can miss "me".
        pid = os.environ.get('CLAUDE_PID', '')
        pid = int(pid) if pid.isdigit() else None

        def is_me(sid_, pid_):
            return (sid and sid_ == sid) or (pid is not None and pid_ == pid)

        me = next((s for s in dashboard.live_sessions()
                   if is_me(s.get('sessionId'), s.get('pid'))), None)
        my_started = dashboard.iso_from_ms(me.get('startedAt')) if me else None
        others = [r for r in dashboard.session_rows()
                  if not is_me(r['sid'], r.get('pid'))]
        found = blockers(target_top, my_started, others)
    except Exception:
        return 0

    if found:
        who = '; '.join(f'{o.get("agent", "claude")} session '
                        f'"{o.get("title") or o["sid"][:8]}"' for o in found)
        sys.stderr.write(
            f'Blocked: another session is already working in {target_top} ({who}). '
            "Two sessions editing one folder overwrite each other's work. "
            'Do this in a separate worktree, or ask the user to close the other '
            'session first.\n'
        )
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
