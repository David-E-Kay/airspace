#!/usr/bin/env python3
"""Session board: what every live Claude Code session is doing, in one page.

Read-only. Reads the live process registry and each session's transcript, asks
git about each folder, and renders one self-refreshing page. It never writes to
a session, spawns anything, or plans work.

ponytail: single file, stdlib only. Split it when it stops fitting on a screen.
"""
import html
import json
import os
import re
import subprocess
import sys
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CLAUDE = Path.home() / '.claude'
REGISTRY = CLAUDE / 'sessions'
PROJECTS = CLAUDE / 'projects'
TAIL_BYTES = 64 * 1024
PORT = 8765
REFRESH_SECONDS = 10

BROWSERS = [
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
]


# --------------------------------------------------------------------------
# liveness
# --------------------------------------------------------------------------

def alive_pids():
    """PIDs currently running. Exact, not a recency guess."""
    if sys.platform == 'win32':
        out = subprocess.run(
            ['tasklist', '/fo', 'csv', '/nh'],
            capture_output=True, text=True, timeout=15,
        ).stdout
        pids = set()
        for line in out.splitlines():
            parts = line.split('","')
            if len(parts) > 1:
                try:
                    pids.add(int(parts[1].strip('" ')))
                except ValueError:
                    pass
        return pids
    return None  # POSIX: caller falls back to os.kill


def is_alive(pid, pids):
    if pids is not None:
        return pid in pids
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def live_sessions(registry=REGISTRY, pids=None):
    """Registry entries whose process is still running."""
    if pids is None:
        pids = alive_pids()
    out = []
    for f in sorted(Path(registry).glob('*.json')):
        try:
            d = json.loads(f.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        pid = d.get('pid')
        if not isinstance(pid, int) or not is_alive(pid, pids):
            continue
        if not d.get('sessionId') or not d.get('cwd'):
            continue
        out.append(d)
    return out


# --------------------------------------------------------------------------
# transcripts
# --------------------------------------------------------------------------

def slug_for(cwd):
    """Project directory name Claude derives from a working directory."""
    return re.sub(r'[^A-Za-z0-9]', '-', os.path.abspath(cwd))


def tail_entries(path, nbytes=TAIL_BYTES):
    """Parsed JSON lines from the end of a transcript. Transcripts reach
    megabytes; reading the tail keeps a page build well under a second."""
    try:
        with open(path, 'rb') as fh:
            fh.seek(max(0, os.path.getsize(path) - nbytes))
            raw = fh.read()
    except OSError:
        return []
    # A cut mid-line leaves a fragment; the JSON guard below drops it.
    out = []
    for line in raw.decode('utf-8', errors='replace').splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def summarise_tool(name, inp):
    def base(key):
        return os.path.basename(str(inp.get(key, ''))) or '?'
    if name in ('Edit', 'Write', 'NotebookEdit'):
        return f'editing {base("file_path")}'
    if name == 'Read':
        return f'reading {base("file_path")}'
    if name in ('Bash', 'PowerShell'):
        return 'running: ' + ' '.join(str(inp.get('command', '')).split())[:70]
    if name in ('Grep', 'Glob'):
        return f'searching for {str(inp.get("pattern", ""))[:40]}'
    if name == 'Agent':
        return f'subagent: {str(inp.get("description", ""))[:50]}'
    if name == 'Skill':
        return f'skill: {inp.get("skill", "")}'
    if name.startswith('mcp__'):
        return 'tool: ' + name.split('__')[-1]
    return f'tool: {name}'


def read_activity(entries):
    """Last action, last timestamp and latest user prompt from tail entries."""
    action, last_ts, prompt = '', None, ''

    for d in reversed(entries):
        if d.get('type') == 'last-prompt' and d.get('lastPrompt'):
            prompt = ' '.join(str(d['lastPrompt']).split())
            break

    for d in reversed(entries):
        ts = d.get('timestamp')
        if ts and last_ts is None:
            last_ts = ts
        if action or d.get('type') != 'assistant':
            continue
        content = (d.get('message') or {}).get('content')
        if not isinstance(content, list):
            continue
        for block in reversed(content):
            if block.get('type') == 'tool_use':
                action = summarise_tool(block.get('name', ''), block.get('input') or {})
                break
            if block.get('type') == 'text' and block.get('text', '').strip():
                action = 'saying: ' + ' '.join(block['text'].split())[:80]
                break

    return {'action': action, 'last_ts': last_ts, 'prompt': prompt}


def transcript_path(cwd, session_id, projects=PROJECTS):
    return Path(projects) / slug_for(cwd) / f'{session_id}.jsonl'


# --------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------

def git(cwd, *args, timeout=15):
    try:
        r = subprocess.run(
            ['git', '-C', str(cwd), *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def git_info(cwd):
    """Branch, uncommitted count, and the key that groups worktrees together."""
    common = git(cwd, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    if not common:
        return {'repo': '', 'label': 'Not a git repository',
                'branch': '', 'dirty': 0, 'is_main': False}
    root = os.path.dirname(common.rstrip('/\\'))
    status = git(cwd, 'status', '--porcelain') or ''
    return {
        'repo': os.path.normcase(common),
        'label': os.path.basename(root) or root,
        'branch': git(cwd, 'branch', '--show-current') or '(detached)',
        'dirty': len([l for l in status.splitlines() if l.strip()]),
        'is_main': os.path.normcase(os.path.abspath(cwd)) == os.path.normcase(root),
    }


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------

def flag_clashes(rows):
    """Mark the two collisions that are facts, not predictions: two live
    sessions in one folder, and two live sessions on one branch."""
    folders, branches = {}, {}
    for r in rows:
        folders.setdefault(os.path.normcase(r['cwd']), []).append(r)
        if r['branch']:
            branches.setdefault((r['repo'], r['branch']), []).append(r)
    for group in folders.values():
        if len(group) > 1:
            for r in group:
                r['warnings'].append('same folder as another session')
    for group in branches.values():
        if len(group) > 1 and len({os.path.normcase(r['cwd']) for r in group}) > 1:
            for r in group:
                r['warnings'].append('same branch as another session')
    return rows


def collect():
    rows, gits = [], {}
    for s in live_sessions():
        cwd = s['cwd']
        key = os.path.normcase(cwd)
        if key not in gits:
            gits[key] = git_info(cwd)
        g = gits[key]
        act = read_activity(tail_entries(transcript_path(cwd, s['sessionId'])))
        rows.append({
            'cwd': cwd,
            'folder': os.path.basename(cwd.rstrip('/\\')) or cwd,
            'name': s.get('name') or s['sessionId'][:8],
            'pid': s.get('pid'),
            'started': s.get('startedAt'),
            'warnings': [],
            **g,
            **act,
        })
    flag_clashes(rows)

    groups = {}
    for r in rows:
        groups.setdefault((r['repo'], r['label']), []).append(r)
    return [
        (label, sorted(rs, key=lambda r: (not r['is_main'], r['folder'])))
        for (_, label), rs in sorted(groups.items(), key=lambda kv: kv[0][1].lower())
    ]


def ago(ts):
    if not ts:
        return '—'
    try:
        when = datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
    except ValueError:
        return '—'
    secs = int((datetime.now(timezone.utc) - when).total_seconds())
    if secs < 60:
        return f'{max(secs, 0)}s ago'
    if secs < 3600:
        return f'{secs // 60}m ago'
    return f'{secs // 3600}h {secs % 3600 // 60}m ago'


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

CSS = """
* { box-sizing: border-box; }
body { margin:0; padding:18px; background:#14161a; color:#e6e6e6;
       font:13px/1.5 "Segoe UI",system-ui,sans-serif; }
h1 { font-size:13px; font-weight:600; color:#8b93a1; letter-spacing:.08em;
     text-transform:uppercase; margin:0 0 14px; }
h2 { font-size:14px; font-weight:600; margin:20px 0 8px; color:#cfd4dc; }
.card { background:#1c1f25; border:1px solid #282c34; border-left:3px solid #3d8bfd;
        border-radius:6px; padding:10px 12px; margin-bottom:8px; }
.card.warn { border-left-color:#e5534b; }
.top { display:flex; align-items:baseline; gap:8px; flex-wrap:wrap; }
.folder { font-weight:600; color:#fff; }
.main-tag { font-size:10px; color:#8b93a1; text-transform:uppercase;
            letter-spacing:.06em; }
.branch { font-family:Consolas,monospace; font-size:12px; color:#7ee787;
          background:#1b2b1f; padding:1px 7px; border-radius:10px; }
.dirty { font-size:11px; color:#e3b341; }
.when { margin-left:auto; font-size:11px; color:#8b93a1; }
.action { margin-top:5px; font-family:Consolas,monospace; font-size:12px;
          color:#c9d1d9; white-space:nowrap; overflow:hidden;
          text-overflow:ellipsis; }
.prompt { margin-top:3px; font-size:11px; color:#7d8590; white-space:nowrap;
          overflow:hidden; text-overflow:ellipsis; }
.warn-line { margin-top:5px; font-size:11px; color:#ff7b72; }
.empty { color:#8b93a1; padding:20px 0; }
footer { margin-top:22px; font-size:10px; color:#5b626d; }
"""


def render(groups, error=''):
    e = html.escape
    parts = [
        '<!doctype html><html><head><meta charset="utf-8">',
        f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">',
        '<title>Session Board</title>', f'<style>{CSS}</style></head><body>',
        '<h1>Live Claude sessions</h1>',
    ]
    if error:
        parts.append(f'<div class="warn-line">{e(error)}</div>')
    if not groups:
        parts.append('<div class="empty">No live sessions.</div>')

    for label, rows in groups:
        parts.append(f'<h2>{e(label)}</h2>')
        for r in rows:
            parts.append(f'<div class="card{" warn" if r["warnings"] else ""}">')
            parts.append('<div class="top">')
            parts.append(f'<span class="folder">{e(r["folder"])}</span>')
            if r['is_main']:
                parts.append('<span class="main-tag">main</span>')
            if r['branch']:
                parts.append(f'<span class="branch">{e(r["branch"])}</span>')
            if r['dirty']:
                parts.append(f'<span class="dirty">{r["dirty"]} uncommitted</span>')
            parts.append(f'<span class="when">{e(ago(r["last_ts"]))}</span>')
            parts.append('</div>')
            if r['action']:
                parts.append(f'<div class="action">{e(r["action"])}</div>')
            if r['prompt']:
                parts.append(f'<div class="prompt">{e(r["prompt"][:110])}</div>')
            for w in r['warnings']:
                parts.append(f'<div class="warn-line">! {e(w)}</div>')
            parts.append('</div>')

    parts.append(f'<footer>refreshed {time.strftime("%H:%M:%S")} '
                 f'&middot; every {REFRESH_SECONDS}s &middot; read-only</footer>')
    parts.append('</body></html>')
    return ''.join(parts)


def build_page():
    try:
        return render(collect())
    except Exception as exc:  # a broken page is worse than a page saying why
        return render([], error=f'{type(exc).__name__}: {exc}')


# --------------------------------------------------------------------------
# serve
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ('/', '/index.html'):
            self.send_error(404)
            return
        body = build_page().encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def open_window(url):
    for exe in BROWSERS:
        if os.path.exists(exe):
            subprocess.Popen([exe, f'--app={url}', '--window-size=560,760'])
            return os.path.basename(exe)
    webbrowser.open(url)
    return 'default browser'


def main():
    url = f'http://localhost:{PORT}/'
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    print(f'Session board on {url}   (Ctrl+C to stop)')
    if '--no-browser' not in sys.argv:
        print('Opened in', open_window(url))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')


if __name__ == '__main__':
    main()
