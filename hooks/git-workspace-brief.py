#!/usr/bin/env python3
"""SessionStart hook: report where this session is about to do its work.

Facts only. The decision rule lives in ~/.claude/CLAUDE.md section 8 - this
just makes sure the model never has to go looking for the state.

Collisions come from dashboard.py, the same code the board renders, so the
hook and the board can never disagree. That replaced a recency heuristic over
transcript mtimes which could only see Claude, called a session idle for 14
minutes live, and had no idea two folders could share a branch.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def git(*args, cwd):
    try:
        out = subprocess.run(['git', *args], cwd=cwd, capture_output=True,
                             text=True, timeout=10)
    except Exception:
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def collisions(cwd, session_id):
    """Warnings for this folder, and a note if the board's code will not load.

    A broken import must cost the collision check, not the whole session
    start - but it must say so. Returning a silent empty list would read as
    "nobody else is here", which is the one answer that gets work clobbered.
    """
    try:
        import dashboard
        return dashboard.clashes_for(cwd, session_id or ''), ''
    except Exception as exc:
        return [], ('- collision check unavailable (%s) - dashboard.py could '
                    'not be loaded, so treat "no other session" as unknown'
                    % exc.__class__.__name__)


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    cwd = payload.get('cwd') or os.getcwd()

    if git('rev-parse', '--is-inside-work-tree', cwd=cwd) != 'true':
        return  # not a repo - nothing to advise about

    branch = git('branch', '--show-current', cwd=cwd) or '(detached)'
    dirty = git('status', '--porcelain', cwd=cwd) or ''
    dirty_n = len([l for l in dirty.splitlines() if l.strip()])
    trees = [l for l in (git('worktree', 'list', cwd=cwd) or '').splitlines()
             if l.strip()]

    # Which branch is "the trunk" here - do not assume it is called main.
    head = git('symbolic-ref', '--short', 'refs/remotes/origin/HEAD', cwd=cwd)
    trunk = head.split('/')[-1] if head else 'main'

    lines = [
        'GIT WORKSPACE (start of session):',
        f'- branch: {branch}   trunk: {trunk}   ' +
        ('clean' if dirty_n == 0 else f'{dirty_n} uncommitted file(s)'),
        f'- worktrees (separate folders sharing this history): {len(trees)}',
    ]
    if len(trees) > 1:
        lines += [f'    {t}' for t in trees]
    clashes, unavailable = collisions(cwd, payload.get('session_id'))
    if unavailable:
        lines.append(unavailable)
    for head_word, body in clashes:
        lines.append(
            f'- WARNING: {head_word.upper()} - {body}. Re-check '
            '`git worktree list` and the current branch before any checkout, '
            'merge, or commit, and raise a worktree with David before '
            'switching branches.'
        )
    lines.append(
        'Apply CLAUDE.md section 8 before the first file change: say where '
        'this work should live (trunk / new branch / new worktree) and why, '
        'in plain language.'
    )

    print(json.dumps({
        'hookSpecificOutput': {
            'hookEventName': 'SessionStart',
            'additionalContext': '\n'.join(lines),
        }
    }))


if __name__ == '__main__':
    main()
