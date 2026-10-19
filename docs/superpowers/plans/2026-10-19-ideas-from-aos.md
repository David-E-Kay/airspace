# Ideas from AOS Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add five things to Airspace: hide key-like strings on cards, an `error` state for API-error stops, a Host/Origin check on the page server, a "same file" warning across worktrees, and (only if Task 1 finds a signal) a "needs your approval" state.

**Architecture:** Everything except item 1's hook lands in the single `dashboard.py` as plain functions, matching the file's existing style. Item 1 adds one fail-open hook script under `hooks/`. Tests stay in `tests/test_dashboard.py` as plain `test_*` functions with bare asserts.

**Tech Stack:** Python 3 stdlib only, Windows, git CLI.

**Spec:** `docs/superpowers/specs/2026-10-19-ideas-from-aos-design.md` — read it alongside this plan.

## Global Constraints

- Stdlib only. No new dependency, no new module: `dashboard.py` stays one file.
- Work only in worktree `C:\Users\david\repos\Multi Agent Dashboard\.claude\worktrees\airspace-dashboard-review-d8c166`, branch `claude/airspace-dashboard-review-d8c166`. The main folder is live (global hooks run from it): never touch it.
- Never edit `~/.claude/settings.json`. Hook wiring is documented in `README.md` only.
- Every new test is run and seen to FAIL for the intended reason before the code it guards is written.
- Run tests with `python tests/test_dashboard.py` from the worktree (prints `ok  <name>` per test, then `N checks passed`; any failure raises). Baseline today: `65 checks passed`.
- Docstrings and comments explain *why*, in the voice of the surrounding code (see `read_activity`'s docstring, `dashboard.py:725-743`).
- `PULSE_STATES` order, once all items land: `('asking', 'approval', 'error', 'done')`. Without item 1: `('asking', 'error', 'done')`.
- No Codex support for items 1 and 2 (spec, Non-goals).

## Existing patterns (cite these, don't invent new ones)

- Naming: plain snake_case functions, module-level CAPS constants, private caches as `_NAME` (`dashboard.py:576-583`).
- Error handling: swallow and degrade, never raise into the page — `git()` returns `None` on any failure (`dashboard.py:1055-1064`); hooks fail open with exit 0 (`hooks/block-shared-worktree.py` docstring).
- Logging: none. The server silences `log_message` (`dashboard.py:1723`). Don't add any.
- Data access: `git(cwd, *args)` for git, `tail_entries(path)` for transcripts, `read_activity(entries)` for state.
- Tests: hand-built transcript entries (`tests/test_dashboard.py:64-80`), hand-built render rows (`tests/test_dashboard.py:199-227`), real throwaway git repos via `tempfile.TemporaryDirectory()` + `subprocess.run(['git', ...])` (`tests/test_dashboard.py:1150-1154`), monkeypatching module attributes with `try/finally` restore (`tests/test_dashboard.py` end of file).

## Review Focus

1. **A key cut in half by truncation.** Cards trim command text to 22-70 characters; a key trimmed below 32 characters would slip past the long-run rule. Redact before trimming. (Test in Task 2.)
2. **Things that look like keys but aren't.** Branch names, long Windows paths, 40-char hex commit ids and words like `sk-learn` must survive redaction untouched. (Test in Task 2.)
3. **A session that errored and was then prompted again.** It must leave `error` as soon as it writes a new turn, not stay red. (Test in Task 3.)
4. **The new state reaching the page.** A row in state `error` must render on a card and in the strip without a `KeyError` from `STATE_WORDS`. (Test in Task 3.)
5. **Opening the board as `localhost` instead of `127.0.0.1`.** Must still load; a refused request must not count as the window being open. (Test in Task 4.)

---

### Task 1: Hands-on check — can item 1 be built? (orchestrator + David, NOT dispatched)

This is a manual check with David watching, run by the orchestrating session. It decides whether Task 6 exists. No product code.

**Files (all throwaway, in the session scratchpad, never in the repo):**
- Create: `<scratchpad>/permcheck/.claude/settings.json` — registers `PermissionRequest` and `Notification` hooks for this folder only, each running `log-hook.py`.
- Create: `<scratchpad>/permcheck/log-hook.py` — appends `{"t": <ISO UTC now>, **<stdin JSON>}` as one line to `<scratchpad>/permcheck/hook-log.jsonl`; any error → exit 0.
- Create: `<scratchpad>/permcheck/watch.py <transcript> <desktop-record>` — every 0.5 s, logs to `watch-log.txt` the time and what changed: new transcript lines (their `type`, and `timestamp` if any) and, for the desktop record JSON, each top-level key whose value changed (old → new, trimmed).

- [ ] **Step 1: Ask David to approve the scratch folder's hook settings.** It is persistent configuration, but only inside a throwaway folder; `~/.claude/settings.json` is not touched.
- [ ] **Step 2: Case A (ask mode).** David opens a desktop Code session in `<scratchpad>/permcheck`, permission mode that asks, and asks it to run `powershell -c "Start-Sleep 20; 'done'"`. Start `watch.py` on that session's transcript and its desktop record (find it with `desktop_sessions()` in `dashboard.py:38`, file `local_<id>.json`). David notes the clock time when the prompt appears and when he clicks Allow. The 20-second sleep separates "approved" from "finished".
- [ ] **Step 3: Case B (auto mode).** Same session, switched to auto mode, a command auto mode approves on its own (e.g. `git status`). Note the time.
- [ ] **Step 4: Read the logs and record the answers in the spec** (new section "Task 1 results", dated):
  - (i) Did `PermissionRequest`, or `Notification` with `notification_type: permission_prompt`, fire in case A?
  - (ii) Did anything land in `hook-log.jsonl` in case B? (It must not.)
  - (iii) Did anything on disk change within ~2 s of the Allow click, well before the sleep ended? Name it exactly (file, key, old → new).
  - (iv) Which transcript lines were written between the hook firing and the answer?
- [ ] **Step 5: Decide.** Item 1 goes ahead only if (i) yes, (ii) nothing, and (iii) found a signal. Otherwise mark item 1 dropped in the spec, delete Task 6 from this plan, and tell David in one line. Tasks 2-5 go ahead either way.
- [ ] **Step 6: Delete `<scratchpad>/permcheck`.** Commit the spec update (with David's OK on commits, see Gate 1 question).

---

### Task 2: Hide key-like strings on cards (item 4b)

**Files:**
- Modify: `dashboard.py` — new `_SECRET`, `_LONG_RUN` regexes and `redact()` just above `summarise_tool` (`dashboard.py:450`); call sites in `summarise_tool` (`:458`), `short_tool` (`:482`), `codex_call_label` (`:952` and `:975`), `_fetch_summary` (`:611`).
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `redact(text: str) -> str` — returns `text` with every key-like run replaced by `[redacted]`. Later tasks don't call it.

- [ ] **Step 1: Write the failing test**

```python
def test_key_like_strings_are_hidden_on_cards():
    """A key typed into a command must not reach the screen, but the long
    names cards are full of - branches, paths, commit ids - must."""
    key = 'sk-ant-api03-' + 'Ab1' * 12
    run = 'Ab1' * 12                       # 36 chars, upper+lower+digit
    shown = d.summarise_tool('Bash', {'command': f'curl -H "x-api-key: {key}" https://x'})
    assert key[:20] not in shown and '[redacted]' in shown, shown
    # trimmed to 70 after redaction, so a key past the cut is still caught
    # 45 chars of prefix leave 25 of the run on screen: under 32, so only
    # redacting BEFORE the cut hides it
    shown = d.summarise_tool('Bash', {'command': 'echo ' + 'word ' * 8 + run})
    assert 'Ab1Ab1' not in shown, shown
    assert 'Ab1Ab1' not in d.short_tool('Bash', {'command': f'login {run}'})
    codex = {'type': 'function_call', 'name': 'shell_command',
             'arguments': json.dumps({'command': f'curl -u {key}'})}
    assert 'Ab1Ab1' not in d.codex_call_label(codex, limit=80)
    for word in ('ghp_' + 'a1B2' * 5, 'xoxb-' + '12345678ab', 'AKIA' + 'ABCDEFGH12345678',
                 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc'):
        assert d.redact(f'x {word} y') == 'x [redacted] y', word

    # known look-alikes survive untouched
    for safe in ('claude/airspace-dashboard-review-d8c166',
                 'bc38e0d5414d865bc72935aef9d379816bdc2fc3',
                 r'C:\Users\david\repos\Multi Agent Dashboard\.claude\worktrees'
                 r'\airspace-dashboard-review-d8c166\docs\superpowers\specs'
                 r'\2026-10-19-ideas-from-aos-design.md',
                 # mixed case and digits, but a path segment: must survive
                 r'C:\Users\Dev\MyVeryLongProjectName2026Edition\src',
                 'pip install sk-learn', 'skeleton-directory-name-that-is-long',
                 'test_key_like_strings_are_hidden_on_cards_and_more'):
        assert d.redact(safe) == safe, safe
```

- [ ] **Step 2: Run it, confirm it fails** with `AttributeError: module 'dashboard' has no attribute 'redact'` — then temporarily comment out the `d.redact` lines and confirm the `summarise_tool` assert fails too (proves the call sites are tested, not just the helper). Restore.

Run: `python -c "import sys; sys.path.insert(0, 'tests'); import test_dashboard as t; t.test_key_like_strings_are_hidden_on_cards()"`

- [ ] **Step 3: Implement `redact(text: str) -> str`**

The two patterns, fixed by the spec (narrower than AOS on purpose — read the spec's Item 4b paragraph):

```python
_SECRET = re.compile(
    r'\b(?:(?:sk|pk)[-_][A-Za-z0-9_-]{8,}|gh[po]_[A-Za-z0-9]{8,}'
    r'|xox[abprs]-[A-Za-z0-9-]{8,}|AKIA[0-9A-Z]{16}'
    r'|eyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]+)*)')
# A run touching `/`, `\` or `.` is part of a path, branch or file name.
_LONG_RUN = re.compile(
    r'(?<![A-Za-z0-9+_=/\\.-])[A-Za-z0-9+_=-]{32,}(?![A-Za-z0-9+_=/\\.-])')
```

A `_LONG_RUN` match is replaced only if it contains an upper-case letter, a lower-case letter and a digit (hex commit ids are lower-case, so they survive). Apply `_SECRET` first. If `sk-learn` or the long test name breaks the expected results, adjust the regex, not the test, and report it.

Call sites — redact **before** any trimming:
- `summarise_tool`: `'running: ' + redact(' '.join(...split()))[:70]`
- `short_tool`: `cmd = redact(str(inp.get('command', '')))` at the top of the Bash/PowerShell branch.
- `codex_call_label`: `cmd = redact(' '.join(...split()))` in both shell paths (`:952`, `:975`).
- `_fetch_summary`: `answer = redact(clean_summary(line))` — the summary model's line is shown on the card. The prompt sent to the local model stays unredacted (it never leaves the machine).

- [ ] **Step 4: Run the full suite** — `python tests/test_dashboard.py` → `66 checks passed`.
- [ ] **Step 5: Commit** — `git add dashboard.py tests/test_dashboard.py; git commit -m "Hide key-like strings in command text on cards"`

---

### Task 3: An `error` state for turns that stopped on an API error (item 2)

**Files:**
- Modify: `dashboard.py` — `PULSE_STATES` (`:105`), `STATE_WORDS` (`:113`), new `error_label(entry: dict) -> str` above `read_activity`, `read_activity` (`:725-795`), CSS (`:1404-1473`).
- Modify: `README.md` — one short paragraph under the states description saying an API-error stop reads as `error` with its cause.
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: state string `'error'`; `PULSE_STATES == ('asking', 'error', 'done')`; `STATE_WORDS['error'] == 'stopped on an error'`. Task 6 inserts `'approval'` after `'asking'`.

Real record shape (local transcript, v2.1.275) — `content` is a list holding one text block, so the entry IS in `convo`:
`{"type":"assistant","isApiErrorMessage":true,"error":"rate_limit","apiErrorStatus":429,"quotaLimits":{"resetsAt":1789938000,...},"message":{"model":"<synthetic>","content":[{"type":"text","text":"You've hit your ..."}],...}}`

- [ ] **Step 1: Write the failing tests**

```python
def test_a_turn_that_stopped_on_an_api_error_reads_as_error():
    """Hitting the usage limit is not finishing the turn."""
    def at(sec):
        return f'2026-09-20T20:57:{sec:02d}Z'
    def err(code, resets=None):
        e = {'type': 'assistant', 'timestamp': at(30), 'isApiErrorMessage': True,
             'error': code, 'apiErrorStatus': 429,
             'message': {'model': '<synthetic>',
                         'content': [{'type': 'text', 'text': "You've hit your limit"}]}}
        if resets:
            e['quotaLimits'] = {'resetsAt': resets}
        return e
    before = [
        {'type': 'user', 'timestamp': at(0),
         'message': {'content': [{'type': 'text', 'text': 'go'}]}},
        {'type': 'assistant', 'timestamp': at(10),
         'message': {'model': 'claude-opus-5-5',
                     'content': [{'type': 'text', 'text': 'on it'}]}},
    ]
    got = d.read_activity(before + [err('rate_limit', 1789938000)])
    hhmm = datetime.fromtimestamp(1789938000).strftime('%H:%M')
    assert got['state'] == 'error', got['state']
    assert got['detail'] == f'stopped: usage limit · resets {hhmm}', got['detail']
    # the error record is written by the app, not a model
    assert got['model'] == 'claude-opus-5-5', got['model']

    assert d.read_activity(before + [err('rate_limit')])['detail'] == 'stopped: usage limit'
    assert d.read_activity(before + [err('authentication_failed')])['detail'] == 'stopped: signed out'
    assert d.read_activity(before + [err('overloaded')])['detail'] == 'stopped: error (overloaded)'

    # prompted again: the new turn settles the state, the error is history
    again = before + [err('rate_limit'),
                      {'type': 'user', 'timestamp': at(50),
                       'message': {'content': [{'type': 'text', 'text': 'retry'}]}}]
    assert d.read_activity(again)['state'] == 'thinking'


def test_an_error_stop_shows_in_the_strip_and_on_its_card():
    row = {'agent': 'claude', 'state': 'error', 'title': 'Limit hit',
           'folder': 'X', 'is_main': True, 'branch': 'main', 'dirty': 0,
           'warnings': [], 'pulse': True, 'detail': 'stopped: signed out',
           'says': '', 'summary': '', 'trail': [], 'since': None, 'last_ts': None}
    page = d.render([('X', [row])])
    assert 'class="card error a-claude pulse"' in page, page
    assert '<span class="w error">stopped on an error</span>' in page, 'not in the strip'
    assert 'stopped: signed out' in page
    assert '.state.error {' in page and '.card.error {' in page, 'no colour for error'
    assert d.PULSE_STATES == ('asking', 'error', 'done')
```

- [ ] **Step 2: Run both, confirm they fail for the right reason.** The first must fail on `got['state'] == 'error'` (it reads `done` today) — not on a typo. Then delete just that assert temporarily and confirm the `model` assert fails with `'<synthetic>'` (proves the existing model bug). Restore. The second must fail with `KeyError: 'error'` from `STATE_WORDS`.
- [ ] **Step 3: Implement**
  - `error_label(entry: dict) -> str`: `rate_limit` → `'stopped: usage limit'`, plus `' · resets HH:MM'` from `datetime.fromtimestamp(quotaLimits.resetsAt).strftime('%H:%M')` when present (local time); `authentication_failed` → `'stopped: signed out'`; any other `error` value → `f'stopped: error ({code})'`.
  - `read_activity`: in the `last['type'] == 'assistant'` text branch, if `last.get('isApiErrorMessage')` → `state, detail = 'error', error_label(last)`; else `done` as today. The model picker skips `'<synthetic>'`.
  - `PULSE_STATES = ('asking', 'error', 'done')`; `STATE_WORDS['error'] = 'stopped on an error'`; update the comment above `STATE_WORDS` to name the third stopped state.
  - CSS: `.card.error { border-left-color:#d9822b; }`, `.triage .w.error { color:#f0a35e; }`, `.state.error { color:#f0a35e; }` — orange, distinct from clash red `#e5534b` and asking yellow `#e3b341`.
  - Update the `read_activity` docstring with one paragraph on why `error` is not `done`.
- [ ] **Step 4: Run the full suite** → `68 checks passed`.
- [ ] **Step 5: Commit** — `git commit -m "Show a session that stopped on an API error as stopped, not done"`

---

### Task 4: Refuse requests that don't name this machine (item 4a)

**Files:**
- Modify: `dashboard.py` — `Handler.do_GET` (`:1710-1721`), new `local_request(headers) -> bool` just above `class Handler`.
- Test: `tests/test_dashboard.py` (add `import http.client` and `import threading` to the imports at the top).

**Interfaces:**
- Produces: `local_request(headers) -> bool` — `headers` is the request's `http.client.HTTPMessage` (anything with `.get(name)`).

- [ ] **Step 1: Write the failing test**

```python
def test_the_page_server_refuses_requests_that_name_another_host():
    """A web page elsewhere can point a made-up name at 127.0.0.1 and read the
    board through it. The Host header is what gives that away."""
    real = d.build_page, d._LAST_SEEN
    d.build_page = lambda: '<html>ok</html>'
    srv = d.ThreadingHTTPServer(('127.0.0.1', 0), d.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def get(headers):
        c = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        c.putrequest('GET', '/', skip_host=True)
        for k, v in headers.items():
            c.putheader(k, v)
        c.endheaders()
        status = c.getresponse().status
        c.close()
        return status
    try:
        assert get({'Host': f'127.0.0.1:{port}'}) == 200
        assert get({'Host': f'localhost:{port}'}) == 200
        assert get({'Host': f'127.0.0.1:{port}', 'Origin': f'http://127.0.0.1:{port}'}) == 200
        d._LAST_SEEN = 0.0
        assert get({'Host': f'evil.example:{port}'}) == 403
        assert get({}) == 403
        assert get({'Host': f'127.0.0.1:{port}', 'Origin': 'http://evil.example'}) == 403
        assert get({'Host': f'127.0.0.1:{port}', 'Origin': 'null'}) == 403
        assert d._LAST_SEEN == 0.0, 'a refused request kept the board alive'
    finally:
        srv.shutdown()
        srv.server_close()
        d.build_page, d._LAST_SEEN = real
```

- [ ] **Step 2: Run it, confirm it fails** on the `evil.example` line with `200 != 403` (not on a connection error).
- [ ] **Step 3: Implement**
  - `local_request(headers)`: hostname of `Host` via `urllib.parse.urlsplit('//' + host).hostname` must be `127.0.0.1` or `localhost`; missing `Host` → False; if `Origin` is present its `urlsplit(origin).hostname` must be one of the same two (so `null` fails). Add `import urllib.parse` at the top if `urllib.request` alone doesn't expose it.
  - `do_GET`: first line checks `local_request(self.headers)`; on False `self.send_error(403)` and return — **before** `_LAST_SEEN` is refreshed.
- [ ] **Step 4: Run the full suite** → `69 checks passed`. Then a live check: start `python dashboard.py --no-browser` in the background, `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8765/` → `200`, and with `-H "Host: evil.example"` → `403`. Stop it.
- [ ] **Step 5: Commit** — `git commit -m "Refuse page requests that name another host"`

---

### Task 5: "same file" warning across worktrees (item 5) — PROVISIONAL

Detail to be filled in just before dispatch, against the tree as Tasks 2-4 leave it.

- **Purpose:** two live sessions in different worktrees of one repo that both changed the same file versus trunk get the existing red warning, head `same file`, text naming the other agent and the first shared file plus "and N more" (spec Goal 5; David: "same alarm formatting just with the appropriate language").
- **Protects against:** two worktrees silently editing the same file until merge time.
- **Rough shape:** a new function called from `collect()` after `flag_clashes(rows)`, never from `flag_clashes` itself (both hooks share that and must stay fast). Per qualifying folder: merge-base of `HEAD` and trunk, `git diff --name-only <base>`, plus untracked files from `git status --porcelain`. Trunk found as `hooks/git-workspace-brief.py:62-63` does it. Repos with only one live folder cost nothing.
- **Decide at detail time:** whether "same file" also shows when "same branch" already does (builder's call per spec); whether the page build time grows enough to need a short per-folder cache (measure before/after, spec Risk 2).
- **Test approach:** a throwaway repo with one real worktree (`git worktree add`), as in `tests/test_dashboard.py:1150-1154`; companion assertion that disjoint changes produce no warning.
- **Files:** `dashboard.py`, `tests/test_dashboard.py`, `README.md` (one line beside the other two collision warnings).

---

### Task 6: "needs your approval" (item 1) — PROVISIONAL, CONDITIONAL on Task 1

Exists only if Task 1 Step 5 says go. Detail written from Task 1's recorded signal.

- **Purpose:** a Claude session waiting on a permission prompt shows `approval` / "needs your approval", in the strip and glowing like `asking`.
- **Rough shape:** new fail-open hook `hooks/note-permission-prompt.py` (style model: `hooks/block-shared-worktree.py`; no `dashboard.py` import, no git) writing `{"ts": ...}` to `~/.claude/airspace/approval/<session_id>.json`. The board shows `approval` when the row is `working`, the note exists, the note is newer than the last user/assistant entry's timestamp (not `last_ts`), **and** Task 1's approval signal has not fired since the note. `PULSE_STATES` becomes `('asking', 'approval', 'error', 'done')`.
- **Must not:** show a stale "needs your approval" while an approved command runs (spec anti-goal).
- **Files:** `hooks/note-permission-prompt.py`, `dashboard.py`, `tests/test_dashboard.py`, `README.md` (hook wiring section like the two existing ones; rewrite "Two honest limits").

---

## Final review

After the last task: Codex review of the whole branch (`/codex:review`), per global CLAUDE.md §4. Then Gate 2 — nothing merges to `main` without David's OK.
