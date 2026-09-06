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
CODEX = Path.home() / '.codex'
TAIL_BYTES = 64 * 1024
# A whole Codex thread is smaller than one Claude transcript, and the turn
# markers that settle its state sit at the start of the turn, which a 64K
# window can fall behind.
CODEX_TAIL_BYTES = 256 * 1024
PORT = 8765
REFRESH_SECONDS = 10
# How long a card keeps glowing after its state changed. Two refreshes, so a
# change cannot slip past between glances.
PULSE_SECONDS = 25

# Launched from board.cmd there is no console, so Windows hands every console
# program we start a brand new window of its own. git runs several times a
# page, every 10 seconds, which spawns windows without end.
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

BROWSERS = [
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
]


# --------------------------------------------------------------------------
# liveness
# --------------------------------------------------------------------------

if sys.platform == 'win32':
    import ctypes
    import ctypes.wintypes as wintypes

    _K32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _K32.OpenProcess.restype = wintypes.HANDLE
    _K32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _K32.CreateFileW.restype = wintypes.HANDLE
    _K32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _INVALID_HANDLE = ctypes.c_void_p(-1).value


def proc_start(pid):
    """When the process holding this PID started, as a Windows FILETIME, or
    None if nothing holds it. Same units the registry writes to procStart."""
    handle = _K32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not _K32.GetProcessTimes(handle, *map(ctypes.byref, times)):
            return None
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        _K32.CloseHandle(handle)


def is_alive(entry):
    """Whether this registry entry's session is still running.

    Windows hands a dead process's PID straight to the next one, and a dozen
    claude.exe processes at a time is normal here, so "some process holds this
    PID" resurrects finished sessions - carrying their old cwd, which then
    trips the same-folder warning. The registry records the session's own
    start time, so comparing the two settles it exactly.
    """
    pid = entry.get('pid')
    if not isinstance(pid, int):
        return False
    if sys.platform != 'win32':
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False
    started = proc_start(pid)
    if started is None:
        return False
    # ponytail: builds before 2.1 wrote no procStart. Trust the PID there
    # rather than hide a real session; drop this once none are left.
    claimed = entry.get('procStart')
    return claimed is None or str(claimed) == str(started)


def live_sessions(registry=REGISTRY, alive=is_alive):
    """Registry entries whose process is still running."""
    out = []
    for f in sorted(Path(registry).glob('*.json')):
        try:
            d = json.loads(f.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        if not alive(d):
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


# Firing one of these ends the turn and puts the ball in the user's court,
# even though the transcript shows the assistant mid-tool-call.
HANDBACK_TOOLS = ('AskUserQuestion', 'ExitPlanMode')


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
    if name == 'AskUserQuestion':
        qs = inp.get('questions') or [{}]
        return str(qs[0].get('question') or 'asked you a question')[:110]
    if name == 'ExitPlanMode':
        return 'plan ready for your approval'
    if name.startswith('mcp__'):
        return 'tool: ' + name.split('__')[-1]
    return f'tool: {name}'


def short_tool(name, inp):
    """Compact label for the tool trail."""
    if name in ('Edit', 'Write', 'NotebookEdit', 'Read'):
        return f'{name} {os.path.basename(str(inp.get("file_path", "")))}'[:26]
    if name in ('Bash', 'PowerShell'):
        # `cd <dir> &&` and `VAR=x &&` are plumbing, and a command often
        # opens with both; the real command is whatever survives them
        cmd = str(inp.get('command', ''))
        while True:
            stripped = re.sub(
                r'^\s*(?:cd\s+|\w+=)("[^"]*"|\'[^\']*\'|\S+)\s*&&\s*', '', cmd)
            if stripped == cmd:
                break
            cmd = stripped
        # PowerShell parks a result in a variable before it does anything, so
        # the first word is `$x` and every command read the same in the trail
        cmd = re.sub(r'^\s*\$\w+\s*=\s*', '', cmd)
        # `git` alone names no action - three `sh git` in a row said
        # nothing. A bare second word is the subcommand (`git status`);
        # a flag, path or filename is noise and gets dropped.
        words = cmd.split()[:2]
        if len(words) > 1 and not re.fullmatch(r'[A-Za-z][\w-]*', words[1]):
            words = words[:1]
        return ('ps ' if name == 'PowerShell' else 'sh ') + \
            ' '.join(words)[:22]
    if name.startswith('mcp__'):
        return name.split('__')[-1][:18]
    return name


def strip_md(s):
    """Markdown read as prose: link text without the target, no punctuation
    scaffolding."""
    s = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', str(s))
    s = re.sub(r'[`*_#>]+', '', s)
    return ' '.join(s.split())


def headline(text, limit=120):
    """One readable line out of a block of markdown prose.

    The card used to show the first `limit` characters, which cut mid-word
    and often caught nothing but throat-clearing. Take the opening line -
    a heading or bold lead is the session's own summary of what follows -
    then whole sentences, and never split a word.
    """
    lines = [l for l in str(text).splitlines() if l.strip()]
    if not lines:
        return ''
    t = strip_md(lines[0])
    if len(t) <= limit:
        return t
    cut = t[:limit]
    stop = max(cut.rfind('. '), cut.rfind('? '), cut.rfind('! '))
    if stop > limit // 3:
        return cut[:stop + 1]
    return cut[:cut.rfind(' ')] + '…' if ' ' in cut else cut


def blocks_of(entry):
    content = (entry.get('message') or {}).get('content')
    return content if isinstance(content, list) else []


def read_activity(entries):
    """What the session is doing, plus the trail of tools it has been hitting.

    The shape of the last real turn settles the state. An assistant turn that
    ends on a tool call is mid-work. One that ends on text has finished its
    turn - `done`. A user turn carrying a tool result means the tool finished
    and the model has not answered yet. The exception is a tool whose whole
    job is to hand control back - asking a question, or putting a plan up for
    approval - which is `asking`.

    `asking` and `done` used to share one label, "waiting for you", which
    made a session that had simply stopped talking look as urgent as one
    holding a question. Only `asking` is genuinely blocked on you.

    ponytail: a session parked on a permission prompt is indistinguishable
    from one mid-tool-call - the transcript records the call either way - so
    it reads as `working`. Fixing that needs a hook writing into the session,
    which this board deliberately does not do.
    """
    last_ts = next((d['timestamp'] for d in reversed(entries) if d.get('timestamp')), None)
    convo = [e for e in entries
             if e.get('type') in ('user', 'assistant') and blocks_of(e)]

    trail = [short_tool(b.get('name', ''), b.get('input') or {})
             for e in convo for b in blocks_of(e) if b.get('type') == 'tool_use']

    # The last thing the session actually said. It describes the turn it is
    # in far better than the prompt that started it does.
    says = ''
    for e in reversed(convo):
        if e['type'] == 'assistant':
            said = '\n'.join(b.get('text', '') for b in blocks_of(e)
                             if b.get('type') == 'text')
            if said.strip():
                says = said
                break

    state, detail, since = 'idle', '', last_ts
    if convo:
        last = convo[-1]
        bs = blocks_of(last)
        since = last.get('timestamp') or last_ts
        if last['type'] == 'assistant' and bs[-1].get('type') == 'tool_use':
            name = bs[-1].get('name', '')
            state = 'asking' if name in HANDBACK_TOOLS else 'working'
            detail = summarise_tool(name, bs[-1].get('input') or {})
        elif last['type'] == 'assistant':
            state = 'done'
            # one line, not the same line twice
            detail, says = headline(says, 110), ''
        else:
            state = 'thinking'
            detail = ('tool finished, composing a reply'
                      if any(b.get('type') == 'tool_result' for b in bs)
                      else 'instruction received')

    return {'state': state, 'detail': detail, 'since': since,
            'trail': trail[-6:], 'last_ts': last_ts,
            'says': headline(says, 160)}


_TITLES = {}


def read_title(path, entries):
    """The chat's title, so two sessions in one project can be told apart.

    A rename lands at the end of the file, inside the tail already read. The
    original title is written near the start, so falling back to a scan of the
    whole file is what makes this work for a long-running session.
    """
    key = str(path)
    for d in reversed(entries):
        if d.get('type') == 'custom-title' and d.get('customTitle'):
            _TITLES[key] = str(d['customTitle'])
            return _TITLES[key]
    if key in _TITLES:
        return _TITLES[key]
    # ponytail: scanning whole transcripts tripled page build to 3.2s, so the
    # result is kept for the life of the server. A rename is picked up from
    # the tail above, which is the only way a title changes.
    title = ''
    try:
        with open(path, 'rb') as fh:
            for raw in fh:
                if b'"custom-title"' not in raw:
                    continue
                try:
                    d = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if d.get('customTitle'):
                    title = str(d['customTitle'])  # a later rename wins
    except OSError:
        pass
    _TITLES[key] = title
    return title


def transcript_path(cwd, session_id, projects=PROJECTS):
    return Path(projects) / slug_for(cwd) / f'{session_id}.jsonl'


# --------------------------------------------------------------------------
# codex
# --------------------------------------------------------------------------
#
# Codex logs a session the same way Claude does - one JSON line per event -
# but records more, so less has to be inferred. Every turn is bracketed by
# task_started and task_complete, the branch and repository are written down
# at session start, and each open thread holds a lock file open for as long
# as it lives. That last one is proof of life, not a guess: Windows drops the
# handle when the process dies, so a crashed session leaves a lock nobody
# holds.

CODEX_CALLS = ('function_call', 'custom_tool_call', 'web_search_call',
               'local_shell_call')


def codex_lock_held(path):
    """Whether a running process still has this thread's lock file open."""
    if sys.platform != 'win32':
        # ponytail: the lock scheme is only verified on Windows. Elsewhere,
        # fall back to "written recently" rather than claim more than we know.
        try:
            return time.time() - os.path.getmtime(path) < 900
        except OSError:
            return False
    handle = _K32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0, None)
    if handle not in (None, 0, _INVALID_HANDLE):
        _K32.CloseHandle(handle)
        return False
    # Opening can also fail because the lock was cleaned up mid-glob. Only a
    # sharing violation means somebody is holding it; anything else is gone.
    return ctypes.get_last_error() == 32  # ERROR_SHARING_VIOLATION


def codex_live_ids(root=CODEX):
    """Thread ids whose lock a running process still holds."""
    locks = Path(root) / 'thread-writer-locks'
    return [f.stem for f in sorted(locks.glob('*.lock'))
            if not f.name.startswith('.') and codex_lock_held(f)]


_CODEX_PATHS = {}


def codex_rollout(sid, root=CODEX):
    """The log file for one thread, or None if it has not written one yet.

    A blank tab in the Codex app holds a lock before it has said anything,
    and has no log at all. Only hits are kept - caching a miss would hide a
    thread that starts talking a second later.
    """
    hit = _CODEX_PATHS.get(sid)
    if hit:
        return hit
    hit = next(Path(root, 'sessions').rglob(f'*{sid}.jsonl'), None)
    if hit:
        _CODEX_PATHS[sid] = hit
    return hit


_CODEX_META = {}


def codex_meta(path):
    """The session_meta line Codex writes first: cwd, and the repo it opened
    in. Written once and never changed, so reading it once is enough."""
    key = str(path)
    if key not in _CODEX_META:
        try:
            with open(path, encoding='utf-8') as fh:
                _CODEX_META[key] = json.loads(fh.readline()).get('payload') or {}
        except (OSError, ValueError):
            _CODEX_META[key] = {}
    return _CODEX_META[key]


def codex_titles(root=CODEX):
    """Thread id to the name shown in the Codex app, so two threads in one
    repo can be told apart. Renaming rewrites this file, so it is re-read."""
    out = {}
    try:
        with open(Path(root) / 'session_index.jsonl', encoding='utf-8') as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get('id') and d.get('thread_name'):
                    out[d['id']] = str(d['thread_name'])
    except OSError:
        pass
    return out


def codex_call_label(payload, limit=26):
    """Compact label for one Codex tool call."""
    kind = payload.get('type')
    if kind == 'web_search_call':
        query = (payload.get('action') or {}).get('query')
        return ('search ' + str(query))[:limit] if query else 'web search'
    name = str(payload.get('name') or kind or '?')
    if name in ('exec_command', 'shell', 'local_shell'):
        try:
            cmd = json.loads(payload.get('arguments') or '{}').get('cmd', '')
        except (json.JSONDecodeError, TypeError):
            cmd = ''
        if isinstance(cmd, list):
            cmd = ' '.join(map(str, cmd))
        cmd = ' '.join(str(cmd).split())
        return ('run ' + cmd)[:limit] if cmd else 'run'
    return name[:limit]


def read_codex_activity(entries):
    """The same answers read_activity gives, but Codex states them outright.

    task_started and task_complete bracket every turn, so an open turn is
    work in progress and a closed one has handed back. Codex has no tool
    whose job is to ask you something, so a Codex card never reads `asking`.
    """
    last_ts = next((d['timestamp'] for d in reversed(entries)
                    if d.get('timestamp')), None)
    trail, says, state, detail, since = [], '', 'idle', '', last_ts
    for d in entries:
        payload = d.get('payload')
        if not isinstance(payload, dict):
            continue
        kind, ts = payload.get('type'), d.get('timestamp')
        if d.get('type') == 'response_item' and kind in CODEX_CALLS:
            trail.append(codex_call_label(payload))
            detail, since = codex_call_label(payload, 90), ts or since
        elif (d.get('type') == 'response_item' and kind == 'message'
                and payload.get('role') == 'assistant'):
            said = '\n'.join(b.get('text', '') for b in payload.get('content') or []
                             if isinstance(b, dict))
            if said.strip():
                says = said
        elif kind == 'task_started':
            state, detail, since = 'working', 'thinking', ts or since
        elif kind == 'task_complete':
            state, since = 'done', ts or since
            says = payload.get('last_agent_message') or says
            detail = ''

    if state == 'done':
        # one line, not the same line twice
        detail, says = headline(says, 110), ''
    return {'state': state, 'detail': detail, 'since': since,
            'trail': trail[-6:], 'last_ts': last_ts,
            'says': headline(says, 160)}


def codex_rows(root=CODEX):
    titles = codex_titles(root)
    rows = []
    for sid in codex_live_ids(root):
        path = codex_rollout(sid, root)
        if path is None:
            continue
        cwd = codex_meta(path).get('cwd')
        if not cwd:
            continue
        rows.append({
            'agent': 'codex', 'sid': sid, 'cwd': cwd, 'pid': None,
            'title': titles.get(sid) or sid[:8],
            **read_codex_activity(tail_entries(path, CODEX_TAIL_BYTES)),
        })
    return rows


# --------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------

def git(cwd, *args, timeout=15):
    try:
        r = subprocess.run(
            ['git', '-C', str(cwd), *args],
            capture_output=True, text=True, timeout=timeout,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def git_info(cwd):
    """Branch, uncommitted count, and the key that groups worktrees together."""
    common = git(cwd, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    if not common:
        # No repository to group under, so the folder is its own group and
        # its own heading. One shared "Not a git repository" heading implied
        # those sessions were related; the missing branch chip already says
        # there is no git here.
        return {'repo': os.path.normcase(os.path.abspath(cwd)),
                'label': os.path.basename(os.path.abspath(cwd)) or cwd,
                'branch': '', 'dirty': 0, 'is_main': True}
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
    def warn(group, text):
        # Naming the other agent matters now that Claude and Codex share the
        # board: "another codex session" tells you where to go and look.
        for r in group:
            who = sorted({o.get('agent', 'claude') for o in group if o is not r})
            r['warnings'].append(text.format(who=' and '.join(who)))

    for group in folders.values():
        if len(group) > 1:
            warn(group, 'another {who} session is in this same folder'
                        ' — your edits can overwrite each other')
    for group in branches.values():
        if len(group) > 1 and len({os.path.normcase(r['cwd']) for r in group}) > 1:
            warn(group, 'another {who} session is on this branch'
                        ' — your commits will interleave')
    return rows


_PREV_STATE = {}


def note_change(sid, state, now=None):
    """Whether this session's state changed recently enough to still glow.

    The page is a full reload every few seconds and the browser remembers
    nothing across one, so the server has to hold the previous state itself.
    A session seen for the first time never glows - otherwise every card
    would light up whenever the board restarts, which is noise, not news.
    """
    now = time.time() if now is None else now
    prev, changed_at = _PREV_STATE.get(sid, (None, 0.0))
    if prev != state:
        changed_at = now if prev is not None else 0.0
        _PREV_STATE[sid] = (state, changed_at)
    return bool(changed_at) and now - changed_at < PULSE_SECONDS


def claude_rows():
    rows = []
    for s in live_sessions():
        tpath = transcript_path(s['cwd'], s['sessionId'])
        entries = tail_entries(tpath)
        rows.append({
            'agent': 'claude', 'sid': s['sessionId'], 'cwd': s['cwd'],
            'pid': s.get('pid'),
            'title': (read_title(tpath, entries) or s.get('name')
                      or s['sessionId'][:8]),
            **read_activity(entries),
        })
    return rows


def collect():
    rows, gits = [], {}
    for r in claude_rows() + codex_rows():
        key = os.path.normcase(r['cwd'])
        if key not in gits:
            gits[key] = git_info(r['cwd'])
        r.update(gits[key])
        r['folder'] = os.path.basename(r['cwd'].rstrip('/\\')) or r['cwd']
        r['warnings'] = []
        r['pulse'] = note_change(r['sid'], r['state'])
        rows.append(r)
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
body { margin:0; padding:16px 18px 26px; background:#14161a; color:#e6e6e6;
       font:13px/1.5 "Segoe UI",system-ui,sans-serif; }
h1 { font-size:12px; font-weight:600; color:#8b93a1; letter-spacing:.08em;
     text-transform:uppercase; margin:0 0 10px; }
h2 { font-size:13px; font-weight:600; margin:18px 0 7px; color:#cfd4dc; }
.bar { display:flex; gap:8px; margin-bottom:14px; flex-wrap:wrap; }
.btn { display:inline-block; padding:5px 11px; border-radius:5px; font-size:12px;
       text-decoration:none; background:#22262e; border:1px solid #333944;
       color:#c9d1d9; }
.btn:hover { background:#2c313a; border-color:#4a5260; }
.btn.hot { background:#3a2e15; border-color:#7a5c1e; color:#f0c674; }
.card { background:#1c1f25; border:1px solid #282c34; border-left:3px solid #3d444d;
        border-radius:6px; padding:9px 11px; margin-bottom:7px; }
.card.working { border-left-color:#3d8bfd; }
.card.asking { border-left-color:#e3b341; }
.card.done { border-left-color:#3fb950; }
.card.thinking { border-left-color:#8957e5; }
.card.clash { border-left-color:#e5534b; }
.card.pulse { animation:pulse 1.5s ease-in-out infinite; }
@keyframes pulse {
  0%,100% { box-shadow:0 0 0 0 rgba(230,236,255,0); }
  50%     { box-shadow:0 0 0 4px rgba(230,236,255,.30); }
}
.agent { font-size:10px; font-weight:700; letter-spacing:.05em;
         text-transform:uppercase; padding:1px 6px; border-radius:9px;
         background:#232936; color:#8b93a1; }
.agent.claude { background:#2b2119; color:#e0a06a; }
.agent.codex { background:#1a2a2a; color:#69c6c0; }
.top { display:flex; align-items:baseline; gap:7px; flex-wrap:wrap; }
.title { font-weight:600; color:#fff; font-size:13px; margin-bottom:2px;
         white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.folder { font-size:11px; color:#8b93a1; }
.branch { font-family:Consolas,monospace; font-size:12px; color:#7ee787;
          background:#1b2b1f; padding:0 7px; border-radius:10px; }
.dirty { font-size:11px; color:#e3b341; }
.when { margin-left:auto; font-size:11px; color:#8b93a1; }
.state { margin-top:6px; font-size:11px; font-weight:700; letter-spacing:.06em;
         text-transform:uppercase; }
.state.working { color:#79b8ff; }
.state.asking { color:#f0c674; }
.state.done { color:#6fcf7f; }
.state.thinking { color:#c39bff; }
.state.idle { color:#8b93a1; }
.state .dur { font-weight:400; letter-spacing:0; text-transform:none;
              color:#8b93a1; }
.detail { margin-top:2px; font-family:Consolas,monospace; font-size:12px;
          color:#c9d1d9; white-space:nowrap; overflow:hidden;
          text-overflow:ellipsis; }
.trail { margin-top:5px; font-family:Consolas,monospace; font-size:11px;
         color:#6e7681; white-space:nowrap; overflow:hidden;
         text-overflow:ellipsis; }
.says { margin-top:4px; font-size:11px; color:#7d8590; white-space:nowrap;
        overflow:hidden; text-overflow:ellipsis; }
.warn-line { margin-top:5px; font-size:11px; color:#ff7b72; }
.empty { color:#8b93a1; padding:20px 0; }
footer { margin-top:20px; font-size:10px; color:#5b626d; }
"""


def render(groups, error=''):
    e = html.escape
    parts = [
        '<!doctype html><html><head><meta charset="utf-8">',
        f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">',
        '<title>Session Board</title>', f'<style>{CSS}</style></head><body>',
        '<h1>Live agent sessions</h1>',
    ]
    # No click-through to a session. The app registers claude:// and the
    # routes exist, but the whole code/ family is gated off in this build -
    # claude://code/new fires and nothing happens. Even if it were on,
    # code/continue only accepts "last" or a local_ id, and those ids are
    # not written to disk anywhere.
    if error:
        parts.append(f'<div class="warn-line">{e(error)}</div>')
    if not groups:
        parts.append('<div class="empty">No live sessions.</div>')

    for label, rows in groups:
        parts.append(f'<h2>{e(label)}</h2>')
        for r in rows:
            cls = 'clash' if r['warnings'] else r['state']
            if r.get('pulse'):
                cls += ' pulse'
            parts.append(f'<div class="card {cls}">')
            parts.append(f'<div class="title">{e(r["title"])}</div>')
            parts.append('<div class="top">')
            agent = r.get('agent', 'claude')
            parts.append(f'<span class="agent {e(agent)}">{e(agent)}</span>')
            if not r['is_main']:
                parts.append(f'<span class="folder">{e(r["folder"])}</span>')
            if r['branch']:
                parts.append(f'<span class="branch">{e(r["branch"])}</span>')
            if r['dirty']:
                parts.append(f'<span class="dirty">{r["dirty"]} uncommitted</span>')
            parts.append(f'<span class="when">{e(ago(r["last_ts"]))}</span>')
            parts.append('</div>')

            words = {'working': 'working', 'asking': 'needs your answer',
                     'done': 'done — your turn', 'thinking': 'thinking',
                     'idle': 'no recent activity'}
            parts.append(f'<div class="state {r["state"]}">{words[r["state"]]}'
                         f'<span class="dur"> &middot; {e(ago(r["since"]))}</span></div>')
            if r['detail']:
                parts.append(f'<div class="detail">{e(r["detail"])}</div>')
            if r['trail']:
                parts.append('<div class="trail">'
                             + e(' → '.join(r['trail'])) + '</div>')
            if r['says']:
                parts.append(f'<div class="says">{e(r["says"][:160])}</div>')
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
    # 127.0.0.1, not localhost: the server is IPv4-only, and Windows resolves
    # localhost to IPv6 first, costing ~1.9s per page load waiting for that
    # to fail. Measured 3.2s via localhost against 1.3s via 127.0.0.1.
    url = f'http://127.0.0.1:{PORT}/'
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
