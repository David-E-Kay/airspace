#!/usr/bin/env python3
"""Airspace: what every live Claude Code and Codex session is doing, in one page.

Read-only. Reads the live process registry and each session's transcript, asks
git about each folder, and renders one self-refreshing page. It never writes to
a session, spawns anything, or plans work.

One exception, and it says so when it happens: on Windows it will name the
program that opens a claude:// or codex:// link, where that is registered with
no program behind it and clicking a card would otherwise do nothing at all.
See mend_link_type().

Single file, stdlib only. Split it when it stops fitting on a screen.
"""
import hashlib
import html
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CLAUDE = Path.home() / '.claude'
REGISTRY = CLAUDE / 'sessions'
PROJECTS = CLAUDE / 'projects'
CODEX = Path.home() / '.codex'


def desktop_sessions(home=None):
    """Where the desktop app keeps one record per session it opened, keyed
    by the id its own claude:// deep link answers to.

    A session started in a bare terminal never gets a record - not this
    app's doing, so click-through just skips it.

    %APPDATA%\Claude is the plain answer, and the right one for an ordinary
    install. A packaged (Store) install virtualises that folder: it is only
    there for processes the app itself started, and the board is normally
    started from a shortcut, which left every card unmatched. Such an
    install keeps the real folder under Packages, so fall back to it.

    That fallback searches for the folder rather than naming the package it
    sits in: the package folder is named after whoever published the build,
    which is not ours to predict on someone else's machine.
    """
    home = home or Path.home()
    plain = home / 'AppData' / 'Roaming' / 'Claude' / 'claude-code-sessions'
    if plain.is_dir():
        return plain
    for p in sorted((home / 'AppData' / 'Local' / 'Packages').glob(
            '*/LocalCache/Roaming/Claude/claude-code-sessions')):
        return p
    return plain


DESKTOP_SESSIONS = desktop_sessions()
TAIL_BYTES = 64 * 1024
# A whole Codex thread is smaller than one Claude transcript, and the turn
# markers that settle its state sit at the start of the turn, which a 64K
# window can fall behind.
CODEX_TAIL_BYTES = 256 * 1024
PORT = 8765
# assets/icon.ico is the same drawing; assets/make_icon.py regenerates it.
#
# The taskbar button for the open board looks softer than the pinned
# shortcut, and no favicon fixes that. Serving the .ico was tried: Chrome
# does fetch it (measured), and the button stayed soft, because Chrome keeps
# a window icon at 32 pixels whatever it was given. A 250% display draws
# that button at 60, so Windows is stretching 32 to 60 every time. The
# shortcut escapes it by reading the .ico itself, which carries a 64.
# The way out is installing the board as a web app, so Windows gets a real
# multi-size icon for the window - not a favicon change.
FAVICON = (
    "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' "
    "viewBox='0 0 256 256'>"
    "<rect width='256' height='256' rx='56' fill='%2315171c'/>"
    "<path d='M128 128 L128 42 A86 86 0 0 1 189 67 Z' fill='%23716141'/>"
    "<circle cx='128' cy='128' r='88' fill='none' stroke='%2334b3a6' "
    "stroke-width='13'/>"
    "<line x1='128' y1='128' x2='128' y2='42' stroke='%23f0c674' "
    "stroke-width='13' stroke-linecap='round'/>"
    "<circle cx='172' cy='94' r='19' fill='%23f0c674'/></svg>"
)
REFRESH_SECONDS = 10
# How long a card keeps glowing after its state changed. Two refreshes, so a
# change cannot slip past between glances. Only a change into a state that
# wants you glows: answering a question moves the session back to work, and
# that is the glow going away, not another one starting.
PULSE_SECONDS = 25
PULSE_STATES = ('asking', 'done')

# Both of these mean the session has stopped and nothing moves until you act,
# which is why they share the strip at the top of the page. They are not the
# same thing, though: `asking` is a turn held open mid-task waiting for an
# answer, `done` is a turn that finished. Listed in this order, most blocked
# first, and each one says which it is - the strip used to call both of them
# "waiting on you", which read as a question that was never asked.
STATE_WORDS = {'working': 'working', 'asking': 'needs your answer',
               'done': 'done — your turn', 'thinking': 'thinking',
               'idle': 'no recent activity'}

# Optional one-line summaries from a local model, off unless
# BOARD_SUMMARY_MODEL names one - the board has to work on a machine with no
# graphics card at all. On 2GB of spare memory, `qwen2.5:1.5b-instruct` fits
# with room left over; `gemma3:1b` is smaller and blunter.
#   set BOARD_SUMMARY_MODEL=qwen2.5:1.5b-instruct
SUMMARY_MODEL = os.environ.get('BOARD_SUMMARY_MODEL', '')
OLLAMA_URL = os.environ.get('BOARD_OLLAMA_URL', 'http://127.0.0.1:11434')
SUMMARY_KEEP = 400
# How long Ollama holds the model in the GPU after a summary. Short, so a
# board left open overnight is not sitting on VRAM it has stopped using.
KEEP_ALIVE = '5m'
# How long the board leaves Ollama alone after a call to it failed.
# Without this every card on the page would retry on every refresh while
# Ollama is shut down; with it the board goes quiet for a minute and then
# tries once more, so starting Ollama after the board fixes the page on
# its own rather than needing the board restarted.
RETRY_AFTER_SECONDS = 60
# What the summary line says while the model is being asked. The first answer
# of a session is slow - Ollama has to load the model into the graphics card
# first - and a blank line for that long reads as a feature that is broken
# rather than one that is starting up. Only shown while a request is actually
# in flight; a board with Ollama switched off shows no line at all, and the
# footer says why.
LOADING_MODEL = 'loading the local model…'
SUMMARISING = 'summarising…'
# The page asks for itself every REFRESH_SECONDS. Going quiet for this long
# means the window is shut, and the board has nothing left to serve. Well
# clear of a minute: a browser throttles a hidden window's timers to about one
# tick a minute, and a board that quit while you were in another window would
# be worse than a slow goodbye.
IDLE_EXIT_SECONDS = 150

# How long a session can be busy without writing anything to its log before
# the card says so. It is a fact, not a diagnosis: nothing on disk separates
# "waiting for you to approve a command" from "running a slow one".
QUIET_MINUTES = 10
# Uncommitted work becomes worth mentioning once the last commit is this old.
STALE_COMMIT_MINUTES = 120

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
    import winreg

    _K32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _K32.OpenProcess.restype = wintypes.HANDLE
    _K32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _K32.CreateFileW.restype = wintypes.HANDLE
    _K32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _INVALID_HANDLE = ctypes.c_void_p(-1).value

    # Which app owns a link type like codex:// - see app_exe().
    _SHLWAPI = ctypes.WinDLL('shlwapi')
    _SHLWAPI.AssocQueryStringW.argtypes = [
        wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]


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
    # Builds before 2.1 wrote no procStart. Trust the PID there rather than
    # hide a real session; drop this once none are left.
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


def local_ids_by_cli_session(root=DESKTOP_SESSIONS):
    """cliSessionId -> the app's own local_<uuid> id for the same session.

    That local id is what claude://code/continue?session=<id> wants; it is
    not the id Claude's own registry and transcripts use, so this is the
    reverse map docs/internals.md describes. Missing entries are normal, not
    an error - see DESKTOP_SESSIONS.
    """
    out = {}
    for f in root.rglob('local_*.json'):
        try:
            d = json.loads(f.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        if d.get('cliSessionId') and d.get('sessionId'):
            out[d['cliSessionId']] = d['sessionId']
    return out


def deep_link(row, local_ids):
    """The URL that jumps the desktop app straight to this session.

    Codex puts the thread id in the link itself, so the id already on the
    row is the one it wants. Claude needs the app's own local id instead,
    which only sessions the app opened have - see docs/internals.md.
    """
    if row.get('agent') == 'codex':
        return f'codex://threads/{row["sid"]}'
    local = local_ids.get(row['sid'])
    return f'claude://code/continue?session={local}' if local else None


def _assoc(scheme, what):
    """Ask Windows what it associates with <scheme>:// links."""
    buf = ctypes.create_unicode_buffer(1024)
    n = wintypes.DWORD(len(buf))
    # ASSOCF_IS_PROTOCOL
    if _SHLWAPI.AssocQueryStringW(0x40, what, scheme, None, buf,
                                  ctypes.byref(n)):
        return ''
    return buf.value


def app_exe(scheme):
    """The program Windows would run for a <scheme>:// link.

    Handing the link to Windows is the obvious way to open one, and it is
    what a browser does. It fails silently when the link type is registered
    with no program behind it, which is how the Codex install here arrived -
    see docs/internals.md. So the board finds the program itself and runs it,
    which needs nothing repaired and changes nothing on the machine.

    Nothing here is written down in advance. Windows is asked for the plain
    program first, which is the answer for an ordinary install. A packaged
    (Store) install has no plain answer, but Windows still names the package
    the link belongs to, and the package's own manifest names its program.
    Those answers keep working while the link type itself is broken, which is
    the whole reason this can be fixed from here.

    The package folder is opened by the name Windows just gave, never found
    by looking: WindowsApps refuses to be listed at all, and the folder name
    carries a version that changes under us anyway.
    """
    exe = _assoc(scheme, 2)  # ASSOCSTR_EXECUTABLE
    if exe and Path(exe).is_file():
        return Path(exe)
    # ASSOCSTR_DELEGATEEXECUTE, '@{<package>?ms-resource://...}'
    package = _assoc(scheme, 15).lstrip('@{').split('?')[0]
    if not package:
        return None
    folder = Path(os.environ.get('ProgramFiles', r'C:\Program Files'),
                  'WindowsApps', package)
    try:
        root = ET.parse(folder / 'AppxManifest.xml').getroot()
    except (OSError, ET.ParseError):
        return None
    # ASSOCSTR_APPID, '<package family>!<application id>'
    wanted = _assoc(scheme, 21).partition('!')[2]
    apps = [a for a in root.iter()
            if a.tag.endswith('}Application') and a.get('Executable')]
    for app in sorted(apps, key=lambda a: a.get('Id') != wanted):
        found = folder / app.get('Executable').replace('/', os.sep)
        if found.is_file():
            return found
    return None


def _named_exe(command):
    """The program a registered open command runs, quoted or not."""
    if not command:
        return None
    rest = command[1:].partition('"')[0] if command.startswith('"') \
        else command.partition(' ')[0]
    return Path(rest) if rest else None


def mend_link_type(scheme):
    """Make sure Windows knows which program opens a <scheme>:// link.

    Windows keeps one entry per link type naming the program that opens it.
    The Codex install here declared `codex` and named no program, so clicking
    a Codex card did nothing at all - no error, nowhere. See
    docs/internals.md; the app's own manifest asks for the link type, so this
    reads as an install that half finished rather than a decision.

    Opening the program ourselves is not a way round it. Handed the link as
    an argument, the app treats it as a web address and passes it to the
    browser, so the session never opens. Only Windows can deliver it, and
    Windows needs the entry. So the board writes the entry, with the program
    Windows itself named - never a guess.

    Written only when the entry is missing, or names a program that is no
    longer there, which is what a Codex update leaves behind. An entry that
    works is left alone, whatever it says. Returns what was written, or None
    if nothing needed doing.
    """
    if sys.platform != 'win32':
        return None
    key = rf'Software\Classes\{scheme}\shell\open\command'
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            named = _named_exe(winreg.QueryValueEx(k, '')[0])
    except OSError:
        named = None
    if named and named.is_file():
        return None
    exe = app_exe(scheme)
    if not exe:
        return None
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
            winreg.SetValueEx(k, '', 0, winreg.REG_SZ, f'"{exe}" "%1"')
    except OSError:
        # A locked-down machine may refuse. The board still works; the cards
        # for that app just will not jump.
        return None
    return exe


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


# --------------------------------------------------------------------------
# optional local summaries
# --------------------------------------------------------------------------
#
# A rule can only pick words the session already wrote; it cannot compress
# meaning. A small local model can. It is off by default because the board
# must not need a graphics card, and it never runs on the page's own thread -
# a card keeps its plain-text line until an answer arrives, one refresh later.

# What went in used to be one 160-character line of the reply, which left the
# model nothing to compress - a summary of a single sentence can only be a
# rewording of it. It now sees what was asked, the whole of the reply, and the
# tools, which is enough to name the work rather than restate the wording.
SUMMARY_PROMPT = (
    'Read a coding session and say what work it is doing.\n\n'
    'THEY ASKED: {asked}\n'
    'IT REPLIED: {said}\n'
    'TOOLS IT RAN: {trail}\n\n'
    'One line, at most 14 words, naming the task and how far along it is. '
    'Describe the work, not the wording of the reply. No preamble, no '
    'quotes.\n\nLINE:'
)

# A 1.5B model spends a third of the line restating the question - "This
# coding session is..." - and asking it not to made things worse: it shouted
# in capitals and echoed the prompt back instead. Small models follow "do
# this" far better than "never do that", so the tidying is a rule, not a
# request.
_ECHO = re.compile(r'^(?:line|it said)\s*:\s*', re.I)
_PREAMBLE = re.compile(
    r'^(?:the\s+|this\s+)?(?:coding\s+)?session\s+'
    r'(?:is\s+|aims\s+to\s+|appears\s+to\s+be\s+|focus(?:es|ing)\s+on\s+)?',
    re.I)


def clean_summary(line):
    s = ' '.join(str(line).split()).strip('"').rstrip('.')
    s = _PREAMBLE.sub('', _ECHO.sub('', s))
    if s.isupper():
        s = s[:1] + s[1:].lower()
    return s[:110]

_SUMMARIES = {}
_ASKED = set()
_SUMMARY_LOCK = threading.Lock()
# Caching a failure as the summary was worse than not caching at all: the
# card had an answer, the answer was nothing, and it never asked again -
# so a board opened before Ollama was running stayed blank all day. A
# failure now parks the feature for RETRY_AFTER_SECONDS instead.
_DOWN_UNTIL = 0.0


def summary_key(asked, said, trail):
    """Same turn, same answer. A new tool means the turn has moved on."""
    return hashlib.sha1(
        '|'.join([asked, said, *trail]).encode('utf-8')).hexdigest()


def _fetch_summary(key, asked, said, trail):
    body = json.dumps({
        'model': SUMMARY_MODEL, 'stream': False, 'keep_alive': KEEP_ALIVE,
        'prompt': SUMMARY_PROMPT.format(
            asked=asked[:600] or 'not recorded',
            said=said[:1500],
            trail=', '.join(trail) or 'none'),
        'options': {'num_predict': 28, 'temperature': 0.2},
    }).encode('utf-8')
    line = ''
    try:
        req = urllib.request.Request(
            OLLAMA_URL.rstrip('/') + '/api/generate', body,
            {'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=30) as r:
            line = json.loads(r.read()).get('response') or ''
    except Exception:
        line = ''  # a model that is missing or down must not break the page
    global _DOWN_UNTIL
    answer = clean_summary(line)
    with _SUMMARY_LOCK:
        _ASKED.discard(key)
        if not answer:
            _DOWN_UNTIL = time.time() + RETRY_AFTER_SECONDS
            return
        _DOWN_UNTIL = 0.0
        _SUMMARIES[key] = answer
        for stale in list(_SUMMARIES)[:-SUMMARY_KEEP]:
            _SUMMARIES.pop(stale, None)


def unload_model():
    """Drop the model out of the GPU now, instead of waiting KEEP_ALIVE."""
    if not SUMMARY_MODEL:
        return
    body = json.dumps({'model': SUMMARY_MODEL, 'prompt': '',
                       'keep_alive': 0}).encode('utf-8')
    try:
        req = urllib.request.Request(
            OLLAMA_URL.rstrip('/') + '/api/generate', body,
            {'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception:
        pass  # nothing to unload if Ollama is already gone


def summarise(asked, said, trail):
    """The model's one-line read of the turn, or '' until it arrives."""
    if not SUMMARY_MODEL or not said.strip():
        return ''
    key = summary_key(asked, said, trail)
    with _SUMMARY_LOCK:
        if key in _SUMMARIES:
            return _SUMMARIES[key]
        if time.time() < _DOWN_UNTIL:
            return ''
        # Nothing answered yet means the model is still being loaded, which is
        # the wait worth naming - every one after it is a couple of seconds.
        waiting = LOADING_MODEL if not _SUMMARIES else SUMMARISING
        if key in _ASKED:
            return waiting
        _ASKED.add(key)
    threading.Thread(target=_fetch_summary, args=(key, asked, said, trail),
                     daemon=True).start()
    return waiting


def blocks_of(entry):
    content = (entry.get('message') or {}).get('content')
    return content if isinstance(content, list) else []


def last_asked(entries):
    """The last thing you actually typed.

    A typed prompt is a user entry whose content is a plain string. A tool
    result is a user entry too, but carries a list of blocks, and `isMeta`
    marks the ones the harness wrote rather than you - so both are skipped.

    Only the summary model reads this. What was asked is half of what a turn
    means, and without it the model could describe nothing but the reply.
    """
    for d in reversed(entries):
        if d.get('type') != 'user' or d.get('isMeta'):
            continue
        content = (d.get('message') or {}).get('content')
        if isinstance(content, str) and content.strip():
            return strip_md(content)
    return ''



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

    A session parked on a permission prompt is indistinguishable from one
    mid-tool-call - the transcript records the call either way - so it reads
    as `working`. Fixing that needs a hook writing into the session, which
    this board deliberately does not do.
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
            # Nothing is running, so there is no action to name. The last
            # message below carries the whole of it.
            detail = ''
        else:
            state = 'thinking'
            detail = ('tool finished, composing a reply'
                      if any(b.get('type') == 'tool_result' for b in bs)
                      else 'instruction received')

    model = next((e['message'].get('model') for e in reversed(convo)
                  if e['type'] == 'assistant'
                  and isinstance(e.get('message'), dict)
                  and e['message'].get('model')), '')

    return {'state': state, 'detail': detail, 'since': since,
            'trail': trail[-6:], 'last_ts': last_ts, 'model': model,
            'says': headline(says, 160),
            # The card shows `says`, one trimmed line. The model gets the
            # whole reply and the request - it is reading, not displaying.
            'says_full': strip_md(says), 'asked': last_asked(entries)}


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
    # Scanning whole transcripts tripled page build to 3.2s, so the result is
    # kept for the life of the server. A rename is picked up from the tail
    # above, which is the only way a title changes.
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
        # The lock scheme is only verified on Windows. Elsewhere, fall back
        # to "written recently" rather than claim more than we know.
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
    if name == 'exec':
        # The computer-use tool wraps the real command in a JS snippet -
        # tools.exec_command({"cmd": "..."}) - instead of a plain JSON
        # `arguments` payload, so the command has to be pulled out of that
        # JS source text rather than parsed as JSON outright.
        m = re.search(r'"cmd"\s*:\s*"((?:\\.|[^"\\])*)"', payload.get('input') or '')
        try:
            cmd = json.loads('"' + m.group(1) + '"') if m else ''
        except (json.JSONDecodeError, TypeError):
            cmd = ''
        cmd = ' '.join(cmd.split())
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
    model = ''
    for d in entries:
        payload = d.get('payload')
        if not isinstance(payload, dict):
            continue
        kind, ts = payload.get('type'), d.get('timestamp')
        if d.get('type') == 'turn_context' and payload.get('model'):
            model = str(payload['model'])
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
        # Nothing is running, so there is no action to name.
        detail = ''
    # No `asked`: a Codex user message carries pasted environment blurb as
    # well as the prompt, and the full reply below is the bigger win anyway.
    return {'state': state, 'detail': detail, 'since': since,
            'trail': trail[-6:], 'last_ts': last_ts, 'model': model,
            'says': headline(says, 160),
            'says_full': strip_md(says), 'asked': ''}


def codex_rows(root=CODEX):
    titles = codex_titles(root)
    rows = []
    for sid in codex_live_ids(root):
        path = codex_rollout(sid, root)
        if path is None:
            continue
        meta = codex_meta(path)
        cwd = meta.get('cwd')
        if not cwd:
            continue
        if meta.get('thread_source') == 'guardian_review':
            # Codex's own background safety check before a risky action,
            # not a task anyone asked for - nothing on the board can act
            # on it, so it never belongs on the page.
            continue
        rows.append({
            'agent': 'codex', 'sid': sid, 'cwd': cwd, 'pid': None,
            'app': pretty_app(meta.get('originator')),
            'started': meta.get('timestamp'),
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
                'branch': '', 'dirty': 0, 'is_main': True, 'committed': None}
    root = os.path.dirname(common.rstrip('/\\'))
    status = git(cwd, 'status', '--porcelain') or ''
    # A repository with no commits yet has no answer here, and 1970 is not it.
    epoch = git(cwd, 'log', '-1', '--format=%ct')
    return {
        'repo': os.path.normcase(common),
        'label': os.path.basename(root) or root,
        'branch': git(cwd, 'branch', '--show-current') or '(detached)',
        'dirty': len([l for l in status.splitlines() if l.strip()]),
        'is_main': os.path.normcase(os.path.abspath(cwd)) == os.path.normcase(root),
        # Uncommitted work is only interesting once it is old. This is the
        # other half of that: when this branch last had anything saved to it.
        'committed': iso_from_ms(int(epoch) * 1000) if epoch else None,
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
    def warn(group, head, text):
        # Naming the other agent matters now that Claude and Codex share the
        # board: "another codex session" tells you where to go and look.
        for r in group:
            who = sorted({o.get('agent', 'claude') for o in group if o is not r})
            r['warnings'].append((head, text.format(who=' and '.join(who))))

    for group in folders.values():
        if len(group) > 1:
            warn(group, 'same worktree',
                 'a {who} session is editing these same files,'
                 ' so whoever saves last wins')
    for group in branches.values():
        if len(group) > 1 and len({os.path.normcase(r['cwd']) for r in group}) > 1:
            warn(group, 'same branch',
                 'a {who} session in another folder saves to this branch too,'
                 ' so the two sets of changes will mix')
    return rows


_PREV_STATE = {}


def note_change(sid, state, now=None):
    """Whether this session's state changed recently enough to still glow.

    The page is a full reload every few seconds and the browser remembers
    nothing across one, so the server has to hold the previous state itself.
    A session seen for the first time never glows - otherwise every card
    would light up whenever the board restarts, which is noise, not news.
    Only a move into a state that wants you glows, so answering a question
    puts the card back to work and the glow stops rather than restarting.
    """
    now = time.time() if now is None else now
    prev, changed_at = _PREV_STATE.get(sid, (None, 0.0))
    if prev != state:
        changed_at = now if prev is not None and state in PULSE_STATES else 0.0
        _PREV_STATE[sid] = (state, changed_at)
    return bool(changed_at) and now - changed_at < PULSE_SECONDS


def pretty_model(name):
    """Model ids are built for machines. Trim to the part that identifies it.

    Four rules, no lookup table. An id that matches none of them is shown as
    it is, which is worse-looking but never wrong.
    """
    n = re.sub(r'-\d{8}$', '', str(name or ''))     # drop the date stamp
    n = re.sub(r'^claude-', '', n)
    n = re.sub(r'^(opus|sonnet|haiku|fable)-(\d+)-(\d+)$', r'\1 \2.\3', n)
    n = re.sub(r'^(opus|sonnet|haiku|fable)-(\d+)$', r'\1 \2', n)
    if n.startswith('gpt'):
        return 'GPT' + n[3:]
    return n[:1].upper() + n[1:]


def pretty_app(name):
    """Which window to go and look in. The board cannot open a session, so
    naming the app it lives in is the next best thing."""
    n = str(name or '').lower().replace('_', '-')
    n = re.sub(r'^(claude|codex)[- ]', '', n)
    return {'': '', 'cli': 'terminal', 'code': 'vs code'}.get(n, n)


def iso_from_ms(ms):
    """Registry timestamps are milliseconds; everything else here is ISO."""
    try:
        return datetime.fromtimestamp(float(ms) / 1000, timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def moment(ts):
    """An ISO timestamp as a datetime, or None if it isn't one."""
    try:
        return datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None


def worked_since_opening(started, last_ts):
    """Whether this session has written anything since its process started.

    The desktop app starts a real session process the moment you click into a
    chat, so a session opened only to read something is running, and lands on
    the board. Nothing in its transcript separates it from a session that
    genuinely stopped and is waiting for you: both end on an assistant turn,
    so both read as `done`.

    The clock separates them exactly. A session you opened to read last wrote
    something BEFORE the process now holding it open - by weeks, in the case
    that prompted this. A session with real work in it always wrote something
    after. No silence threshold, so a question left hanging while you go to a
    meeting still shows, however long you are gone.

    Unknown counts as worked: hiding a session is only safe on proof it has
    done nothing, never on a timestamp that failed to parse.
    """
    opened, wrote = moment(started), moment(last_ts)
    if opened is None or wrote is None:
        return True
    return wrote > opened


def claude_rows():
    rows = []
    for s in live_sessions():
        tpath = transcript_path(s['cwd'], s['sessionId'])
        entries = tail_entries(tpath)
        activity = read_activity(entries)
        started = iso_from_ms(s.get('startedAt'))
        if not worked_since_opening(started, activity['last_ts']):
            continue
        rows.append({
            'agent': 'claude', 'sid': s['sessionId'], 'cwd': s['cwd'],
            'pid': s.get('pid'),
            'app': pretty_app(s.get('entrypoint')),
            'started': started,
            'title': (read_title(tpath, entries) or s.get('name')
                      or s['sessionId'][:8]),
            **activity,
        })
    return rows


def session_rows():
    """Every session running right now, with the git context of its folder.

    The board renders these; the session-start hook asks the same question of
    the same function. Collisions are a separate step because the hook has to
    add its own session to the list first, before its transcript exists to be
    found.
    """
    rows, gits = [], {}
    for r in claude_rows() + codex_rows():
        key = os.path.normcase(r['cwd'])
        if key not in gits:
            gits[key] = git_info(r['cwd'])
        r.update(gits[key])
        r['folder'] = os.path.basename(r['cwd'].rstrip('/\\')) or r['cwd']
        r['warnings'] = []
        rows.append(r)
    return rows


def clashes_for(cwd, session_id='', pid=None):
    """The collisions a session starting in `cwd` is walking into.

    At session start its own transcript is empty, so it is not in
    `session_rows` yet - and a lone neighbour would then read as no clash at
    all. A stand-in row goes in for it, and the ordinary rules run over that.

    Two ways to recognise the caller's own row, because the session id alone
    is not enough. The registry is keyed by pid and is rewritten a few
    seconds AFTER SessionStart fires, so a session started by clearing the
    previous one finds the registry still naming its predecessor - same
    process, same folder, different id. Measured at 8s on 2.1.260. Without
    the pid rule every cleared session warns about the one it replaced.
    """
    rows = [r for r in session_rows()
            if not (session_id and r['sid'].startswith(session_id))
            and not (pid and r.get('pid') == pid)]
    me = {'sid': session_id or 'me', 'agent': 'claude', 'cwd': cwd,
          'warnings': [], **git_info(cwd)}
    rows.append(me)
    flag_clashes(rows)
    return me['warnings']


def collect():
    rows = session_rows()
    local_ids = local_ids_by_cli_session()
    for r in rows:
        r['pulse'] = note_change(r['sid'], r['state'])
        r['deep_link'] = deep_link(r, local_ids)
        # Its own row now. It used to overwrite whichever prose line the
        # state happened to use, which left the card unable to say which of
        # the two you were reading.
        r['summary'] = summarise(r.get('asked', ''),
                                 r.get('says_full') or r['says'] or r['detail'],
                                 r['trail'])
    flag_clashes(rows)

    groups = {}
    for r in rows:
        groups.setdefault((r['repo'], r['label']), []).append(r)
    out = [
        (label, sorted(rs, key=lambda r: (urgency(r), not r['is_main'],
                                          r['folder'])))
        for (_, label), rs in groups.items()
    ]
    # The question you actually have when you glance at the board is never
    # "what is everyone doing", it is "what has stopped for me" - so a project
    # holding a stopped session sorts to the top, and within it that session
    # sorts to the front. Alphabetical is the tie-breaker, not the rule.
    return sorted(out, key=lambda g: (min(urgency(r) for r in g[1]),
                                      g[0].lower()))


def wants_you(row):
    """Whether this session has stopped and nothing moves until you act."""
    return row.get('state') in PULSE_STATES


def urgency(row):
    """Sort key. A question held open mid-task outranks a finished turn,
    which outranks everything still running."""
    state = row.get('state')
    return (PULSE_STATES.index(state) if state in PULSE_STATES
            else len(PULSE_STATES))


def minutes_since(ts, now=None):
    """Whole minutes since an ISO timestamp, or None if there isn't one."""
    if not ts:
        return None
    try:
        when = datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
    except ValueError:
        return None
    now = now or datetime.now(timezone.utc)
    return max(int((now - when).total_seconds()) // 60, 0)


def span(ts):
    """How long since `ts`, with no "ago" on it - for "open 3h 4m"."""
    mins = minutes_since(ts)
    if mins is None:
        return '—'
    if mins < 1:
        return 'under a minute'
    if mins < 60:
        return f'{mins}m'
    # Past a couple of days "245h 17m" stops being a number anyone reads.
    if mins < 60 * 48:
        return f'{mins // 60}h {mins % 60}m'
    return f'{mins // (60 * 24)} days'


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
/* Two edges carry two different facts: the left is what the session is
   doing, the right is which app it is. Grouping the page by app instead
   would split a project across two lists - and a project holding both is
   exactly the collision the board exists to show. */
.card { background:#1c1f25; border:1px solid #282c34; border-left:3px solid #3d444d;
        border-right:3px solid transparent;
        border-radius:6px; padding:9px 11px; margin-bottom:7px;
        transition:background-color .12s, border-color .12s; }
.card.a-claude { border-right-color:#8a6440; }
.card.a-codex { border-right-color:#2f7f79; }
.card.working { border-left-color:#3d8bfd; }
.card.asking { border-left-color:#e3b341; }
.card.done { border-left-color:#3fb950; }
.card.thinking { border-left-color:#8957e5; }
.card.clash { border-left-color:#e5534b; }
.card.pulse { animation:pulse 1.5s ease-in-out infinite; }
.card.clickable { cursor:pointer; }
/* Only the top/bottom border brightens - left/right carry state/app colour
   and must stay untouched. */
.card.clickable:hover, .card.clickable.hover { background:#232730; border-top-color:#4a5260;
                                              border-bottom-color:#4a5260; }
@keyframes pulse {
  0%,100% { box-shadow:0 0 0 0 rgba(230,236,255,0); }
  50%     { box-shadow:0 0 0 4px rgba(230,236,255,.30); }
}
/* Pinned right, so the eye finds it in the same place on every card. Stacked:
   which agent and which window on top, which model under it. */
.agent { margin-left:auto; font-size:10px; font-weight:700; letter-spacing:.05em;
         text-transform:uppercase; padding:2px 7px; border-radius:9px;
         background:#232936; color:#8b93a1; text-align:right; line-height:1.35; }
.agent.claude { background:#2b2119; color:#e0a06a; }
.agent.codex { background:#16292a; color:#69c6c0; }
.agent .app { font-weight:400; opacity:.75; }
.agent .model { display:block; font-weight:400; letter-spacing:0;
                text-transform:none; opacity:.8; }
/* The one number most glances at this board were ever after. */
.triage { margin:0 0 12px; padding:8px 12px; border-radius:6px; font-size:12px;
          background:#241d10; border:1px solid #4a3a16; color:#f0c674; }
/* Pulled out by the rows' side padding, so the text stays in line with the
   heading and a hovered row's highlight is even on both sides. */
.triage ul { list-style:none; margin:6px -6px 0; padding:0; }
.triage li { display:flex; gap:9px; align-items:baseline; padding:2px 6px;
             border-radius:4px; color:#e6e6e6; }
.triage li.jump { cursor:pointer; }
.triage li.jump:hover, .triage li.jump.hover { background:#33280f; }
/* Fixed width, so the titles line up and the labels read as a column. */
.triage .w { flex:0 0 auto; width:130px; font-size:10px; font-weight:700;
             letter-spacing:.06em; text-transform:uppercase; }
.triage .w.asking { color:#f0c674; }
.triage .w.done { color:#6fcf7f; }
.triage .where { margin-left:auto; padding-left:10px; font-size:10px;
                 letter-spacing:.06em; text-transform:uppercase;
                 color:#8a8f98; white-space:nowrap; }
.age { font-size:11px; color:#6f7684; }
.state .quiet { font-weight:400; letter-spacing:0; text-transform:none;
                color:#e3b341; }
.top { display:flex; align-items:baseline; gap:7px; flex-wrap:wrap; }
.title { font-weight:600; color:#fff; font-size:13px; margin-bottom:2px;
         white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.folder { font-size:11px; color:#8b93a1; }
.branch { font-family:Consolas,monospace; font-size:12px; color:#7ee787;
          background:#1b2b1f; padding:0 7px; border-radius:10px; }
.dirty { font-size:11px; color:#e3b341; }
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
.lbl { font-family:"Segoe UI",system-ui,sans-serif; font-size:9px;
       font-weight:700; letter-spacing:.07em; text-transform:uppercase;
       color:#565d68; margin-right:7px; }
.says { margin-top:4px; font-size:11px; color:#7d8590; white-space:nowrap;
        overflow:hidden; text-overflow:ellipsis; }
.says.waiting { color:#565d68; font-style:italic; }
.warn-line { margin-top:5px; font-size:11px; color:#ff7b72; }
.warn-head { font-weight:700; letter-spacing:.05em; text-transform:uppercase;
             font-size:10px; color:#ff9c94; }
.empty { color:#8b93a1; padding:20px 0; }
footer { margin-top:20px; font-size:10px; color:#5b626d; }
/* ponytail: light mode is the whole page inverted, not a second palette.
   hue-rotate puts the hues back, so green stays green. Swap for real light
   values if the washed-out look ever grates. */
html.light { filter: invert(1) hue-rotate(180deg); }
#theme { position:fixed; top:12px; right:16px; cursor:pointer; line-height:1;
         padding:5px 9px; font-size:14px; }
/* The glyph names where the click goes, not where you are. */
#theme::after { content:"☀"; }
html.light #theme::after { content:"☽"; }
"""


def render(groups, error=''):
    e = html.escape
    parts = [
        '<!doctype html><html><head><meta charset="utf-8">',
        # Refreshed by fetching a fresh copy and swapping the body in,
        # not by reloading. A reload makes Windows redraw the tab icon, and
        # the taskbar button flashes every time. Only <body> is replaced, so
        # this script is not re-run and the scroll position survives.
        # The swap also replaces the card under a still mouse, and the
        # browser only notices it is hovered a moment later - a visible
        # blink. So the page tracks the mouse and marks that card .hover
        # before the new body is drawn; the next mouse move clears it.
        '<script>var mx=-1,my=-1;'
        'document.addEventListener("mousemove",function(e){'
        'mx=e.clientX;my=e.clientY;'
        'var h=document.querySelector(".hover");'
        'if(h)h.classList.remove("hover");});'
        'document.documentElement.addEventListener("mouseleave",'
        'function(){mx=my=-1;});'
        'setInterval(function(){'
        'fetch(location.href,{cache:"no-store"})'
        '.then(function(r){return r.text()})'
        '.then(function(t){'
        'var p=new DOMParser().parseFromString(t,"text/html");'
        'document.title=p.title;'
        'document.body.innerHTML=p.body.innerHTML;'
        'var el=mx<0?null:document.elementFromPoint(mx,my);'
        'el=el&&el.closest(".clickable,.jump");'
        'if(el)el.classList.add("hover");})'
        '.catch(function(){});'
        f'}}, {REFRESH_SECONDS * 1000});</script>',
        # The tab icon, inline so the board still serves one file and
        # needs no second request. Simplified like the small .ico:
        # one ring, the sweep, one contact.
        f'<link rel="icon" href="{FAVICON}">',
        # Set before the body paints, so a refresh does not flash dark first.
        '<script>if(localStorage.theme==="light")'
        'document.documentElement.className="light"</script>',
    ]
    # The count goes in the title so Windows shows it on the taskbar button.
    # Most glances at this board only ever needed that one number.
    waiting = [r for _, rows in groups for r in rows if wants_you(r)]
    parts += [
        f'<title>{len(waiting)} waiting &middot; Airspace</title>'
        if waiting else '<title>Airspace</title>',
        f'<style>{CSS}</style></head><body>',
        '<button id="theme" class="btn" aria-label="Switch light or dark" '
        'onclick="localStorage.theme='
        "document.documentElement.classList.toggle('light')?'light':'dark'"
        '"></button>',
        '<h1>Live agent sessions</h1>',
    ]
    if waiting:
        # One per line. A comma-separated run of session titles is a wall of
        # words, and this strip only earns its place if it reads at a glance.
        parts.append(
            f'<div class="triage"><b>{len(waiting)} '
            f'session{"s" if len(waiting) > 1 else ""} stopped for you</b><ul>')
        for r in sorted(waiting, key=lambda r: PULSE_STATES.index(r['state'])):
            # Same agent/app pair the card carries, so the strip alone tells
            # you which window to go and look in.
            where = ' &middot; '.join(
                e(x) for x in (r.get('agent', 'claude'), r.get('app')) if x)
            open_attr = (f' onclick="location.href=\'{e(r["deep_link"])}\'"'
                         if r.get('deep_link') else '')
            parts.append(f'<li{" class=\"jump\"" if open_attr else ""}'
                         f'{open_attr}><span class="w {r["state"]}">'
                         f'{STATE_WORDS[r["state"]]}</span>{e(r["title"])}'
                         f'<span class="where">{where}</span></li>')
        parts.append('</ul></div>')
    if error:
        parts.append(f'<div class="warn-line">{e(error)}</div>')
    if not groups:
        parts.append('<div class="empty">No live sessions.</div>')

    for label, rows in groups:
        parts.append(f'<h2>{e(label)}</h2>')
        for r in rows:
            agent = r.get('agent', 'claude')
            cls = 'clash' if r['warnings'] else r['state']
            cls += f' a-{agent}' + (' pulse' if r.get('pulse') else '')
            # Clicking jumps the desktop app to this session via its own
            # deep link - see docs/internals.md and deep_link() above. Not
            # every session has one to jump to.
            open_attr = (f' onclick="location.href=\'{e(r["deep_link"])}\'"'
                         if r.get('deep_link') else '')
            if open_attr:
                cls += ' clickable'
            parts.append(f'<div class="card {cls}"{open_attr}>')
            parts.append(f'<div class="title">{e(r["title"])}</div>')
            parts.append('<div class="top">')
            if not r['is_main']:
                parts.append(f'<span class="folder">{e(r["folder"])}</span>')
            if r['branch']:
                parts.append(f'<span class="branch">{e(r["branch"])}</span>')
            if r['dirty']:
                # Uncommitted files only matter once they have been sitting
                # there a while, so say when anything was last saved.
                stale = minutes_since(r.get('committed'))
                risk = (f' &middot; nothing committed for {span(r["committed"])}'
                        if stale and stale >= STALE_COMMIT_MINUTES else '')
                parts.append(f'<span class="dirty">{r["dirty"]} '
                             f'uncommitted{risk}</span>')
            if minutes_since(r.get('started')) is not None:
                parts.append(f'<span class="age">open {span(r["started"])}</span>')
            # The "how long" beside the state says the same thing as a second
            # clock in the corner did, so there is only one now.
            app = f'<span class="app"> &middot; {e(r.get("app") or "")}</span>' \
                if r.get('app') else ''
            model = f'<span class="model">{e(pretty_model(r.get("model")))}</span>' \
                if r.get('model') else ''
            parts.append(f'<span class="agent {e(agent)}">{e(agent)}{app}'
                         f'{model}</span>')
            parts.append('</div>')

            words = STATE_WORDS
            # A busy session that has written nothing for a while. Stated as
            # the fact it is: the log cannot tell a session parked on a
            # permission prompt from one running a slow command, and saying
            # "stuck" would be a guess dressed up as a reading.
            quiet = minutes_since(r.get('last_ts'))
            hush = (f'<span class="quiet"> &middot; silent {span(r["last_ts"])}</span>'
                    if r['state'] in ('working', 'thinking')
                    and quiet is not None and quiet >= QUIET_MINUTES else '')
            parts.append(f'<div class="state {r["state"]}">{words[r["state"]]}'
                         f'<span class="dur"> &middot; {e(ago(r["since"]))}</span>'
                         f'{hush}</div>')
            if r['detail']:
                parts.append(f'<div class="detail">{e(r["detail"])}</div>')
            # Both rows are prose about the turn, so each says which it is.
            if r.get('summary'):
                waiting = (' waiting' if r['summary'] in
                           (LOADING_MODEL, SUMMARISING) else '')
                parts.append(f'<div class="says{waiting}">'
                             '<span class="lbl">turn summary</span>'
                             + e(r['summary']) + '</div>')
            if r['says']:
                parts.append('<div class="says"><span class="lbl">last '
                             'message</span>' + e(r['says'][:160]) + '</div>')
            if r['trail']:
                parts.append('<div class="trail"><span class="lbl">last '
                             'tools</span>' + e(' → '.join(r['trail']))
                             + '</div>')
            for head, body in r['warnings']:
                parts.append('<div class="warn-line"><span class="warn-head">'
                             f'warning: {e(head)}</span> — {e(body)}</div>')
            parts.append('</div>')

    # The model is read from the environment once, at startup, so this line
    # is the quickest way to tell whether a BOARD_SUMMARY_MODEL actually
    # reached the board or was set in a window it never saw.
    # It used to name the model and stop there, which reads as working when
    # the model is named but Ollama is not running - the one failure this
    # line exists to catch.
    if not SUMMARY_MODEL:
        summaries = 'summaries: off (set BOARD_SUMMARY_MODEL)'
    elif time.time() < _DOWN_UNTIL:
        summaries = (f'summaries: {e(SUMMARY_MODEL)}'
                     ' &mdash; no answer from Ollama')
    else:
        summaries = f'summaries: {e(SUMMARY_MODEL)}'
    parts.append(f'<footer>refreshed {time.strftime("%H:%M:%S")} '
                 f'&middot; every {REFRESH_SECONDS}s &middot; read-only '
                 f'&middot; {summaries}</footer>')
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

_LAST_SEEN = time.time()


def window_gone(now=None):
    """Has the page stopped asking for itself?

    A heartbeat, not a watched browser process. Chrome hands an --app window
    to a Chrome that is already running and the process we launched exits
    half a second later, so its exit says nothing at all.
    """
    return (now or time.time()) - _LAST_SEEN > IDLE_EXIT_SECONDS


def close_with_the_window():
    while True:
        time.sleep(REFRESH_SECONDS)
        if window_gone():
            unload_model()
            os._exit(0)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        global _LAST_SEEN
        _LAST_SEEN = time.time()
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


def already_running():
    # Windows lets a second ThreadingHTTPServer bind the same port instead of
    # erroring (SO_REUSEADDR), so a stale copy can silently eat every request
    # meant for the fresh one. Ask the port itself before trying to bind it.
    # A raw connect, not a page fetch: build_page() can be slow enough to
    # blow past a short timeout and read as "nobody's there" when someone is.
    try:
        with socket.create_connection(('127.0.0.1', PORT), timeout=1):
            return True
    except OSError:
        return False


def main():
    # 127.0.0.1, not localhost: the server is IPv4-only, and Windows resolves
    # localhost to IPv6 first, costing ~1.9s per page load waiting for that
    # to fail. Measured 3.2s via localhost against 1.3s via 127.0.0.1.
    url = f'http://127.0.0.1:{PORT}/'
    if already_running():
        print(f'Airspace already running on {url}')
        if '--no-browser' not in sys.argv:
            print('Opened in', open_window(url))
        return
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    print(f'Airspace on {url}   (Ctrl+C to stop)')
    # Never silently: this is the one thing the board changes outside itself.
    for scheme in ('claude', 'codex'):
        mended = mend_link_type(scheme)
        if mended:
            print(f'Told Windows that {scheme}:// links open {mended.name} '
                  f'- clicking a card had nothing to open before.')
    if '--no-browser' not in sys.argv:
        print('Opened in', open_window(url))
        # Close the window and the board stops, freeing the GPU with it.
        threading.Thread(target=close_with_the_window, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')


if __name__ == '__main__':
    main()
