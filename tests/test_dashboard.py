#!/usr/bin/env python3
"""Checks for the four bits of logic that could fail silently.

Run: python tests/test_dashboard.py
"""
import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# dashboard.py and hooks/ live in the repo root, one level up. Put the root on
# the path so this file runs the same whether it is invoked from here or from
# the root, and anchor on ROOT rather than on this file's own directory.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import dashboard as d


def test_slug_matches_claude_layout():
    """The slug rule must match the directory names Claude actually creates."""
    assert d.slug_for(r'C:\Users\dev\repos\Multi Agent Dashboard') == \
        'C--Users-dev-repos-Multi-Agent-Dashboard'
    assert d.slug_for(r'C:\Users\dev\.claude') == 'C--Users-dev--claude'
    # every non-alphanumeric becomes a dash, underscores and dots included
    assert d.slug_for(r'C:\a_b.c-d') == 'C--a-b-c-d'

    # Known-positive, on a machine that actually runs Claude Code: the derived
    # slug must name a directory that exists. The assertions above are pure
    # arithmetic and would keep passing if Claude changed its layout. Skipped
    # where there is no install to check against, such as a CI runner.
    projects = Path.home() / '.claude' / 'projects'
    if projects.is_dir():
        real = projects / d.slug_for(Path.home() / '.claude')
        assert real.is_dir(), f'slug rule drifted: {real} not found'


def test_tail_reads_last_action_and_what_it_said():
    entries = [
        {'type': 'assistant', 'timestamp': '2026-09-05T10:00:00.000Z',
         'message': {'content': [{'type': 'text', 'text': 'looking now'}]}},
        {'type': 'assistant', 'timestamp': '2026-09-05T10:01:00.000Z',
         'message': {'content': [
             {'type': 'tool_use', 'name': 'Edit',
              'input': {'file_path': r'C:\repo\src\timer.py'}}]}},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / 's.jsonl'
        p.write_text('\n'.join(json.dumps(e) for e in entries), encoding='utf-8')
        got = d.read_activity(d.tail_entries(p))

    assert got['state'] == 'working', got['state']
    assert got['detail'] == 'editing timer.py', got['detail']
    assert got['trail'] == ['Edit timer.py'], got['trail']
    assert got['says'] == 'looking now', got['says']
    assert got['last_ts'] == '2026-09-05T10:01:00.000Z', got['last_ts']


def test_state_is_done_when_the_turn_ended_on_text():
    entries = [
        {'type': 'user', 'timestamp': '2026-09-05T10:00:00Z',
         'message': {'content': [{'type': 'tool_result', 'content': 'ok'}]}},
        {'type': 'assistant', 'timestamp': '2026-09-05T10:00:09Z',
         'message': {'content': [
             {'type': 'tool_use', 'name': 'Grep', 'input': {'pattern': 'x'}},
         ]}},
        {'type': 'assistant', 'timestamp': '2026-09-05T10:00:20Z',
         'message': {'content': [{'type': 'text', 'text': 'All   done.'}]}},
        {'type': 'attachment', 'timestamp': '2026-09-05T10:00:21Z'},
    ]
    got = d.read_activity(entries)
    assert got['state'] == 'done', got['state']
    assert got['detail'] == '', 'a finished turn has no action to name'
    assert got['says'] == 'All done.', got['says']
    assert got['since'] == '2026-09-05T10:00:20Z', got['since']


def test_state_is_thinking_after_a_tool_result():
    entries = [
        {'type': 'assistant', 'timestamp': '2026-09-05T10:00:00Z',
         'message': {'content': [
             {'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'pytest -q'}}]}},
        {'type': 'user', 'timestamp': '2026-09-05T10:00:30Z',
         'message': {'content': [{'type': 'tool_result', 'content': 'passed'}]}},
    ]
    got = d.read_activity(entries)
    assert got['state'] == 'thinking', got['state']
    assert got['trail'] == ['sh pytest'], got['trail']


def test_trail_ignores_the_leading_directory_change():
    """Almost every command starts by changing folder; a trail of 'cd' is
    useless, so the real command has to survive."""
    assert d.short_tool('Bash', {'command': 'cd "C:/a b/c" && pytest -q'}) == 'sh pytest'
    assert d.short_tool('Bash', {'command': 'cd /c/repo && git status'})         == 'sh git status'
    assert d.short_tool('Bash', {'command': 'grep -r foo .'}) == 'sh grep'
    # a command often opens with a folder change AND a variable, and both
    # have to go or the trail reads as the variable name
    assert d.short_tool('Bash', {
        'command': 'cd "C:/x" && SP="C:/tmp/scratch" && cp a.py "$SP/a.py"',
    }) == 'sh cp'
    assert d.short_tool('Bash', {'command': ''}) == 'sh '


def test_tail_skips_partial_first_line():
    """Reading only the tail must not choke on a half line at the cut."""
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / 's.jsonl'
        filler = json.dumps({'type': 'system', 'pad': 'x' * 500})
        good = json.dumps({'type': 'assistant', 'timestamp': '2026-09-05T10:00:00Z',
                           'message': {'content': [
                               {'type': 'tool_use', 'name': 'Bash',
                                'input': {'command': 'pytest  -q'}}]}})
        p.write_text('\n'.join([filler] * 200 + [good]), encoding='utf-8')
        entries = d.tail_entries(p, nbytes=2048)

    assert entries, 'tail returned nothing'
    assert d.read_activity(entries)['detail'] == 'running: pytest -q'


def test_live_sessions_drops_dead_processes():
    with tempfile.TemporaryDirectory() as tmp:
        reg = Path(tmp)
        (reg / 'a.json').write_text(json.dumps(
            {'pid': 111, 'sessionId': 'aaa', 'cwd': r'C:\repo'}), encoding='utf-8')
        (reg / 'b.json').write_text(json.dumps(
            {'pid': 222, 'sessionId': 'bbb', 'cwd': r'C:\other'}), encoding='utf-8')
        (reg / 'c.json').write_text('not json at all', encoding='utf-8')

        alive = d.live_sessions(registry=reg,
                                alive=lambda e: e.get('pid') == 111)

    assert [s['sessionId'] for s in alive] == ['aaa'], alive


def test_live_sessions_hides_the_apps_helper_copies():
    """App 2.42304 starts a read-only fork beside a session, under the same
    hostSessionId, that saves nothing and cannot be opened. The app's own
    record says which entry is the real session; with no record naming
    either, both stay - a session is only hidden on proof."""
    real_ids = d.local_ids_by_cli_session
    with tempfile.TemporaryDirectory() as tmp:
        reg = Path(tmp)
        for pid, sid, host in ((1, 'real', 'local_h'), (2, 'helper', 'local_h'),
                               (3, 'x1', 'local_s'), (4, 'x2', 'local_s'),
                               (5, 'solo', None)):
            (reg / f'{pid}.json').write_text(json.dumps(
                {'pid': pid, 'sessionId': sid, 'cwd': r'C:\repo',
                 'hostSessionId': host}), encoding='utf-8')
        try:
            d.local_ids_by_cli_session = lambda *a: {'real': 'local_h'}
            alive = d.live_sessions(registry=reg, alive=lambda e: True)
        finally:
            d.local_ids_by_cli_session = real_ids

    assert sorted(s['sessionId'] for s in alive) == \
        ['real', 'solo', 'x1', 'x2'], alive


def test_clashes_flag_shared_folder_and_shared_branch():
    def row(cwd, repo, branch):
        return {'cwd': cwd, 'repo': repo, 'branch': branch, 'warnings': []}

    same_folder = [row(r'C:\repo', 'R', 'main'), row(r'C:\REPO', 'R', 'main')]
    d.flag_clashes(same_folder)
    assert all(r['warnings'][0][0] == 'same worktree'
               and 'same files' in r['warnings'][0][1]
               for r in same_folder), same_folder

    same_branch = [row(r'C:\repo', 'R', 'feat'), row(r'C:\repo-wt', 'R', 'feat')]
    d.flag_clashes(same_branch)
    assert all(r['warnings'][0][0] == 'same branch'
               and 'changes will mix' in r['warnings'][0][1]
               for r in same_branch), same_branch

    # the warning names the other agent, so you know which app to go look in
    mixed = [dict(row(r'C:\repo', 'R', 'main'), agent='claude'),
             dict(row(r'C:\repo', 'R', 'main'), agent='codex')]
    d.flag_clashes(mixed)
    assert 'codex' in mixed[0]['warnings'][0][1], mixed[0]
    assert 'claude' in mixed[1]['warnings'][0][1], mixed[1]

    unrelated = [row(r'C:\a', 'R', 'main'), row(r'C:\b', 'R', 'other')]
    d.flag_clashes(unrelated)
    assert all(not r['warnings'] for r in unrelated), unrelated


def test_two_worktrees_that_changed_the_same_file_are_both_warned():
    """Two worktrees of one repo editing the same file clash only at merge
    time, long after anyone could have stopped it. git sees it now."""
    with tempfile.TemporaryDirectory() as tmp:
        main, wt = Path(tmp) / 'main', Path(tmp) / 'wt'
        main.mkdir()
        # no hooks: a machine-wide pre-commit hook refuses the throwaway
        # identity below, and an unmade commit would fail this test confusingly
        run = lambda cwd, *a: subprocess.run(
            ['git', '-c', f'core.hooksPath={os.devnull}', *a], cwd=cwd,
            capture_output=True)
        run(main, 'init', '-q', '-b', 'main')
        run(main, 'config', 'user.email', 'a@b.c')
        run(main, 'config', 'user.name', 'T')
        for name in ('a.txt', 'b.txt', 'c.txt', 'd.txt'):
            (main / name).write_text('x\n')
        run(main, 'add', '.')
        run(main, 'commit', '-q', '-m', 'x')
        run(main, 'worktree', 'add', '-q', str(wt), '-b', 'feat')

        def rows():
            return [{'cwd': str(main), 'agent': 'claude', 'warnings': [],
                     **d.git_info(main)},
                    {'cwd': str(wt), 'agent': 'codex', 'warnings': [],
                     **d.git_info(wt)}]

        # disjoint: main edits b.txt, the worktree edits a.txt -> no warning
        (main / 'b.txt').write_text('main\n')
        (wt / 'a.txt').write_text('feat\n')
        got = d.flag_shared_files(rows())
        assert [r['warnings'] for r in got] == [[], []], got

        # committed on the branch, uncommitted on trunk, and an untracked
        # file both created: all three count
        run(wt, 'commit', '-q', '-am', 'feat')
        (main / 'a.txt').write_text('main\n')
        (main / 'c.txt').write_text('main\n')
        (wt / 'c.txt').write_text('feat\n')
        (main / 'new.txt').write_text('1\n')
        (wt / 'new.txt').write_text('2\n')
        # a name outside ASCII must not break the board or compare unequal
        (main / 'Ð.txt').write_text('1\n')
        (wt / 'Ð.txt').write_text('2\n')
        mine, theirs = d.flag_shared_files(rows())
        assert [h for h, _ in mine['warnings']] == ['same file'], mine
        assert [h for h, _ in theirs['warnings']] == ['same file'], theirs
        text = mine['warnings'][0][1]
        # git quotes the non-ASCII name, and the quote sorts before 'a'
        assert 'codex' in text and 'and 3 more' in text, text
        assert 'claude' in theirs['warnings'][0][1]

        # a session started in a subfolder sees the same repo-wide,
        # repo-relative set, not just the untracked files beneath it
        (wt / 'sub').mkdir()
        (wt / 'sub' / 'deep.txt').write_text('3\n')
        assert d.changed_files(wt / 'sub') == d.changed_files(wt)
        assert 'sub/deep.txt' in d.changed_files(wt / 'sub')

        # one live folder in the repo costs nothing and warns nothing
        solo = rows()[:1]
        assert d.flag_shared_files(solo)[0]['warnings'] == []


def test_changed_files_survives_renames_diff_config_and_a_remote_only_trunk():
    """Three ways the set used to come back too small without any error."""
    def repo(tmp):
        main = Path(tmp) / 'main'
        main.mkdir()
        # no hooks: see the test above
        run = lambda cwd, *a: subprocess.run(
            ['git', '-c', f'core.hooksPath={os.devnull}', *a], cwd=cwd,
            capture_output=True)
        run(main, 'init', '-q', '-b', 'main')
        run(main, 'config', 'user.email', 'a@b.c')
        run(main, 'config', 'user.name', 'T')
        (main / 'sub').mkdir()
        for name in ('a.txt', 'sub/s.txt'):
            (main / name).write_text('x\n')
        run(main, 'add', '.')
        run(main, 'commit', '-q', '-m', 'x')
        return main, run

    # both worktrees move one file, to different names: only the shared
    # source path shows the clash, so a rename has to report its source
    with tempfile.TemporaryDirectory() as tmp:
        main, run = repo(tmp)
        wt = Path(tmp) / 'wt'
        run(main, 'worktree', 'add', '-q', str(wt), '-b', 'feat')
        run(main, 'mv', 'a.txt', 'one.txt')
        run(wt, 'mv', 'a.txt', 'two.txt')
        rows = [{'cwd': str(c), 'agent': 'claude', 'warnings': [],
                 **d.git_info(c)} for c in (main, wt)]
        got = d.flag_shared_files(rows)
        assert [[h for h, _ in r['warnings']] for r in got] == \
            [['same file'], ['same file']], got

        # diff.relative would make a subfolder session report subfolder-relative
        # paths, and drop changes outside it
        (wt / 'sub' / 's.txt').write_text('y\n')
        run(wt, 'config', 'diff.relative', 'true')
        assert d.changed_files(wt / 'sub') == d.changed_files(wt)
        assert 'a.txt' in d.changed_files(wt / 'sub')

    # a clone made on a feature branch has origin/main and no local main
    with tempfile.TemporaryDirectory() as tmp:
        main, run = repo(tmp)
        run(main, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
        run(main, 'symbolic-ref', 'refs/remotes/origin/HEAD',
            'refs/remotes/origin/main')
        run(main, 'checkout', '-q', '-b', 'feat')
        run(main, 'branch', '-q', '-D', 'main')
        (main / 'a.txt').write_text('changed\n')
        assert d.changed_files(main) == {'a.txt'}, d.changed_files(main)


def test_render_survives_a_broken_collect(monkey=None):
    """The page must say what went wrong rather than showing nothing."""
    page = d.render([], error='BoomError: git vanished')
    assert 'BoomError' in page and '<html' in page


def test_a_card_labels_its_app_and_its_tool_row_once():
    """The tool row means nothing without a name on it, the app has to be
    readable at a glance, and the clock used to be printed twice."""
    row = {'agent': 'codex', 'state': 'done', 'title': 'Some thread',
           'folder': 'AlgoTrading', 'is_main': True, 'branch': 'main',
           'dirty': 0, 'warnings': [], 'pulse': True,
           'detail': '', 'says': 'found nothing',
           'summary': 'looked for the config and came up empty',
           'trail': ['run git status'], 'since': None, 'last_ts': None}
    page = d.render([('AlgoTrading', [row])])

    assert 'class="card done a-codex pulse"' in page, page
    assert '.card.a-codex { border-right-color:#' in page, 'no app colour'
    assert '<span class="lbl">last tools</span>' in page, 'tool row unlabelled'
    # two rows of prose about the same turn - each has to say which it is
    assert '<span class="lbl">turn summary</span>looked for the config' in page,         'the summary row is unlabelled'
    assert '<span class="lbl">last message</span>found nothing' in page,         'the message row is unlabelled'
    assert page.count('&middot; &mdash;') + page.count('&middot; —') <= 1, \
        'the same clock is printed twice'
    assert 'class="when"' not in page, 'the duplicate clock came back'

    # a collision needs a headline, or the sentence reads as commentary
    warned = dict(row, warnings=[('same worktree', 'a claude session is here')])
    page = d.render([('AlgoTrading', [warned])])
    assert '<span class="warn-head">warning: same worktree</span>' in page, page
    assert 'a claude session is here' in page, page

    # the footer says whether a summary model actually reached the board
    assert 'summaries: off' in page, page


def test_card_jumps_to_the_session_only_when_a_deep_link_is_known():
    """Click-through fires the desktop app's own deep link; a session with no
    link (opened in a bare terminal) must render exactly as before."""
    base = {'agent': 'claude', 'state': 'done', 'title': 'Some session',
            'folder': 'AlgoTrading', 'is_main': True, 'branch': 'main',
            'dirty': 0, 'warnings': [], 'pulse': False,
            'detail': '', 'says': '', 'summary': '', 'trail': [],
            'since': None, 'last_ts': None}

    linked = dict(base, deep_link='claude://code/continue?session=local_abc')
    page = d.render([('AlgoTrading', [linked])])
    assert 'class="card done a-claude clickable"' in page, page
    assert ("onclick=\"location.href='claude://code/continue"
            "?session=local_abc'\"") in page, page

    page = d.render([('AlgoTrading', [base])])
    assert 'class="card done a-claude">' in page, page
    assert 'code/continue' not in page, page

    # a clickable item highlights on hover so the cursor's "you can click
    # this" is backed up by the item itself changing
    assert '.card.clickable:hover' in d.CSS
    assert '.triage li.jump:hover' in d.CSS
    # the refresh swaps every card out from under a still mouse; the page
    # re-marks the one under it so the highlight does not blink
    assert '.card.clickable.hover' in d.CSS
    assert '.triage li.jump.hover' in d.CSS
    assert 'elementFromPoint' in page, page
    # finding that card settles its plain look first, so the hover fade
    # would replay from plain on every refresh - the page's own mark skips it
    assert '.card.hover { transition:none; }' in d.CSS


def test_deep_link_names_the_thread_for_codex_and_the_app_id_for_claude():
    """Codex takes the thread id straight from the row - the app's route is
    codex://threads/<uuid>. Claude takes the desktop app's own local id, and
    a Claude session the app never opened gets no link at all."""
    ids = {'cli-1': 'local_abc'}

    codex = {'agent': 'codex', 'sid': '01a0ac98-117d-70d1-81fc-1e37455a4689'}
    assert d.deep_link(codex, ids) == (
        'codex://threads/01a0ac98-117d-70d1-81fc-1e37455a4689'), d.deep_link(codex, ids)

    claude = {'agent': 'claude', 'sid': 'cli-1'}
    assert d.deep_link(claude, ids) == (
        'claude://code/continue?session=local_abc'), d.deep_link(claude, ids)

    assert d.deep_link({'agent': 'claude', 'sid': 'cli-2'}, ids) is None


def test_the_program_behind_a_link_is_read_out_of_the_package_windows_names():
    """Handing a codex:// link to Windows fails silently when the link type
    is registered with no program behind it, so the board finds the program
    itself. Windows still names the package and the app inside it, and the
    package's own manifest names the file - none of it guessed, and the
    folder is opened by name because WindowsApps refuses to be listed."""
    if sys.platform != 'win32':
        return
    with tempfile.TemporaryDirectory() as tmp:
        package = 'Vendor.Thing_9.9.9.9_x64__abcdefghijklm'
        folder = Path(tmp) / 'WindowsApps' / package
        (folder / 'app').mkdir(parents=True)
        (folder / 'app' / 'Thing.exe').write_bytes(b'')
        (folder / 'app' / 'Other.exe').write_bytes(b'')
        (folder / 'AppxManifest.xml').write_text(
            '<Package xmlns="http://schemas.microsoft.com/appx/manifest/'
            'foundation/windows10"><Applications>'
            '<Application Id="Other" Executable="app/Other.exe" />'
            '<Application Id="Main" Executable="app/Thing.exe" />'
            '</Applications></Package>', encoding='utf-8')

        answers = {2: '',  # no plain program: that is the broken case
                   15: '@{%s?ms-resource://Vendor.Thing/Files/x.png}' % package,
                   21: 'Vendor.Thing_abcdefghijklm!Main'}
        real_assoc, real_pf = d._assoc, os.environ.get('ProgramFiles')
        d._assoc = lambda scheme, what: (
            answers.get(what, '') if scheme == 'thing' else '')
        os.environ['ProgramFiles'] = tmp
        try:
            got = d.app_exe('thing')
            assert got == folder / 'app' / 'Thing.exe', got

            # the id Windows gave picks the app, not the manifest's order
            answers[21] = 'Vendor.Thing_abcdefghijklm!Other'
            assert d.app_exe('thing') == folder / 'app' / 'Other.exe'

            # an ordinary, unpackaged install answers outright
            answers[2] = str(folder / 'app' / 'Thing.exe')
            assert d.app_exe('thing') == folder / 'app' / 'Thing.exe'

            # a link type nothing owns must not guess at a program
            assert d.app_exe('nothing') is None, 'invented a program'
        finally:
            d._assoc = real_assoc
            if real_pf is not None:
                os.environ['ProgramFiles'] = real_pf

        answers.clear()
        assert d.app_exe('thing') is None


def test_the_link_type_is_mended_only_when_it_points_at_nothing():
    """A link type registered with no program behind it swallows every click
    in silence, which is how Codex arrived here. The board fills that in -
    but it must never overrule an entry that works, whatever it names."""
    if sys.platform != 'win32':
        return
    import winreg

    scheme = 'airspaceselftest'
    key = rf'Software\Classes\{scheme}\shell\open\command'

    def written():
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                return winreg.QueryValueEx(k, '')[0]
        except OSError:
            return None

    def put(value):
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
            winreg.SetValueEx(k, '', 0, winreg.REG_SZ, value)

    def scrub():
        for sub in (key, rf'Software\Classes\{scheme}\shell\open',
                    rf'Software\Classes\{scheme}\shell',
                    rf'Software\Classes\{scheme}'):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
            except OSError:
                pass

    with tempfile.TemporaryDirectory() as tmp:
        here, gone = Path(tmp) / 'Thing.exe', Path(tmp) / 'Old.exe'
        here.write_bytes(b'')
        real = d.app_exe
        d.app_exe = lambda s: here if s == scheme else None
        scrub()
        try:
            # nothing registered at all: the board fills it in
            assert d.mend_link_type(scheme) == here
            assert written() == f'"{here}" "%1"', written()

            # already works: left exactly as it was, even in another program
            put('"C:\\Windows\\System32\\notepad.exe" "%1"')
            assert d.mend_link_type(scheme) is None, 'overruled a working entry'
            assert written() == '"C:\\Windows\\System32\\notepad.exe" "%1"'

            # names a program that is gone - what a Codex update leaves
            put(f'"{gone}" "%1"')
            assert d.mend_link_type(scheme) == here, 'left a stale entry alone'
            assert written() == f'"{here}" "%1"', written()

            # unquoted entries are read too, or every one of them reads stale
            put(f'{here} "%1"')
            assert d.mend_link_type(scheme) is None, 'misread an unquoted entry'

            # no program to name: write nothing rather than guess
            scrub()
            d.app_exe = lambda s: None
            assert d.mend_link_type(scheme) is None
            assert written() is None, 'invented an entry'
        finally:
            d.app_exe = real
            scrub()


def test_desktop_sessions_finds_the_folder_a_packaged_install_hides():
    """A packaged (Store) install virtualises the app's own %APPDATA%: only
    processes the app started see the plain folder, and the board is usually
    started some other way, which left every card unmatched."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        # deliberately not a name starting with Claude: the package folder
        # is named after the publisher of whichever build is installed, so
        # the search must not depend on guessing it
        packaged = (home / 'AppData' / 'Local' / 'Packages' /
                    'AnthropicPBC.ClaudeDesktop_8wekyb3d8bbwe' / 'LocalCache' /
                    'Roaming' / 'Claude' / 'claude-code-sessions')
        packaged.mkdir(parents=True)
        assert d.desktop_sessions(home) == packaged, d.desktop_sessions(home)

        plain = home / 'AppData' / 'Roaming' / 'Claude' / 'claude-code-sessions'
        plain.mkdir(parents=True)
        assert d.desktop_sessions(home) == plain,             'the folder the app writes to must win over the packaged copy'


def test_desktop_sessions_follows_the_app_out_to_claude_data():
    """App 2.42304 moved its records to %LOCALAPPDATA%/Claude-Data/<package>/
    and left only a marker file in both old folders, so every card went
    unclickable."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        # what the move leaves behind: the old folders exist, records don't
        (home / 'AppData' / 'Roaming' / 'Claude').mkdir(parents=True)
        (home / 'AppData' / 'Local' / 'Packages' / 'Claude_pzs8sxrjxfjjc' /
         'LocalCache' / 'Roaming' / 'Claude').mkdir(parents=True)
        moved = (home / 'AppData' / 'Local' / 'Claude-Data' /
                 'Claude_pzs8sxrjxfjjc' / 'claude-code-sessions')
        moved.mkdir(parents=True)
        assert d.desktop_sessions(home) == moved, d.desktop_sessions(home)


def test_local_ids_reverse_map_reads_the_desktop_apps_own_records():
    """The desktop app's session files are the only source for the id its
    own deep link needs - see docs/internals.md."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        nested = root / 'a' / 'b'
        nested.mkdir(parents=True)
        (nested / 'local_abc-123.json').write_text(json.dumps(
            {'sessionId': 'local_abc-123', 'cliSessionId': 'cli-uuid-1'}),
            encoding='utf-8')
        # a record missing cliSessionId (or malformed) must not crash the scan
        (nested / 'local_broken.json').write_text('not json', encoding='utf-8')

        got = d.local_ids_by_cli_session(root)

    assert got == {'cli-uuid-1': 'local_abc-123'}, got


def test_title_prefers_a_rename_and_falls_back_to_a_full_scan():
    """The title is what tells two sessions in one project apart, so it must
    survive being written far outside the tail window."""
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / 's.jsonl'
        early = json.dumps({'type': 'custom-title', 'customTitle': 'First name'})
        filler = json.dumps({'type': 'system', 'pad': 'x' * 500})
        p.write_text('\n'.join([early] + [filler] * 200), encoding='utf-8')

        entries = d.tail_entries(p, nbytes=2048)
        assert not any(e.get('type') == 'custom-title' for e in entries), \
            'fixture is wrong: the title must be outside the tail'
        assert d.read_title(p, entries) == 'First name'

        later = json.dumps({'type': 'custom-title', 'customTitle': 'Renamed'})
        p.write_text(p.read_text(encoding='utf-8') + '\n' + later, encoding='utf-8')
        assert d.read_title(p, d.tail_entries(p, nbytes=2048)) == 'Renamed'

    assert d.read_title(Path(tmp) / 'gone.jsonl', []) == ''


def test_title_scan_result_is_kept():
    """Scanning whole transcripts is slow, so the answer must be reused."""
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / 's.jsonl'
        p.write_text(json.dumps(
            {'type': 'custom-title', 'customTitle': 'Kept'}), encoding='utf-8')
        assert d.read_title(p, []) == 'Kept'
        os.remove(p)
        assert d.read_title(p, []) == 'Kept', 'the scan was repeated'




def test_liveness_rejects_a_recycled_pid():
    """Windows reuses a dead process's PID, so a stale registry entry can point
    at a live stranger and resurrect a finished session."""
    if sys.platform != 'win32':
        return
    me = os.getpid()
    real = d.proc_start(me)
    assert real is not None, 'could not read this process own start time'

    assert d.is_alive({'pid': me, 'procStart': str(real)}), 'live session hidden'
    assert not d.is_alive({'pid': me, 'procStart': '1'}), 'ghost session shown'
    assert d.is_alive({'pid': me}), 'pre-2.1 entry hidden'
    assert not d.is_alive({'pid': 999999, 'procStart': str(real)})
    assert not d.is_alive({'pid': None})


def test_asking_is_told_apart_from_merely_being_finished():
    """Both used to read "waiting for you", which made a session that had
    simply stopped talking look as urgent as one holding a question."""
    def turn(name, inp):
        return [{'type': 'assistant', 'timestamp': '2026-09-05T10:00:00Z',
                 'message': {'content': [
                     {'type': 'text', 'text': 'Classifying this as   architectural.'},
                     {'type': 'tool_use', 'name': name, 'input': inp}]}}]

    got = d.read_activity(turn('AskUserQuestion',
                               {'questions': [{'question': 'Which scope?'}]}))
    assert got['state'] == 'asking', got['state']
    assert got['detail'] == 'Which scope?', got['detail']
    assert got['says'] == 'Classifying this as architectural.', got['says']

    got = d.read_activity(turn('ExitPlanMode', {'plan': 'do the thing'}))
    assert got['state'] == 'asking', got['state']

    # a turn that just stopped talking is finished, not blocked on you
    got = d.read_activity([{'type': 'assistant', 'timestamp': '2026-09-05T10:00:00Z',
                            'message': {'content': [{'type': 'text',
                                                     'text': 'That is all.'}]}}])
    assert got['state'] == 'done', got['state']

    # known-positive: an ordinary tool in the same position is still work
    got = d.read_activity(turn('Read', {'file_path': 'a.py'}))
    assert got['state'] == 'working', got['state']


def test_the_desktop_question_box_reads_as_asking():
    """The app's next-step box is a wrap_up call, and a turn that asks there
    ends on its tool result with nothing said after - which read as a reply
    still being composed."""
    def ended(inp):
        return [{'type': 'assistant', 'timestamp': '2026-10-16T10:00:00Z',
                 'message': {'content': [
                     {'type': 'text', 'text': 'Here is the plan.'},
                     {'type': 'tool_use', 'id': 't1',
                      'name': 'mcp__ccd_turn__wrap_up', 'input': inp}]}},
                {'type': 'user', 'timestamp': '2026-10-16T10:00:01Z',
                 'message': {'content': [{'type': 'tool_result',
                                          'tool_use_id': 't1',
                                          'content': 'Shown.'}]}}]

    got = d.read_activity(ended({'recap': 'Found it.', 'ask': 'Go ahead?'}))
    assert got['state'] == 'asking', got['state']
    assert got['detail'] == 'Go ahead?', got['detail']

    # known-positive: a recap with no question is followed by a closing
    # message, so until that arrives the reply really is being composed
    got = d.read_activity(ended({'recap': 'Found it.'}))
    assert got['state'] == 'thinking', got['state']


def test_powershell_trail_shows_the_command_not_the_variable():
    """PowerShell parks output in a variable first, so taking the first word
    made every command render identically."""
    assert d.short_tool('PowerShell', {'command': '$s = Get-ChildItem C:/x'})         == 'ps Get-ChildItem'
    assert d.short_tool('PowerShell', {'command': 'Get-Process claude'})         == 'ps Get-Process claude'
    # known-positive: bash is untouched, and still loses its leading cd
    assert d.short_tool('Bash', {'command': 'cd /c/r && git status'}) == 'sh git status'


def test_trail_keeps_the_subcommand_but_not_the_arguments():
    """Four `sh git` in a row told the viewer nothing; fetch, commit and
    branch are different acts and have to be distinguishable."""
    def sh(cmd):
        return d.short_tool('Bash', {'command': 'cd /c/repo && ' + cmd})

    assert sh('git fetch origin') == 'sh git fetch'
    assert sh('git commit -q -F -') == 'sh git commit'
    assert sh('npm run test:unit') == 'sh npm run'
    # a flag, a path or a filename is noise, not a subcommand
    assert sh('pytest -q') == 'sh pytest'
    assert sh('cat > /c/tmp/f') == 'sh cat'
    assert sh('python test_dashboard.py') == 'sh python'
    assert d.short_tool('PowerShell', {'command': '$s = Get-ChildItem C:/x'})         == 'ps Get-ChildItem'


def test_git_never_opens_a_console_window():
    """Started without a console, the board gets a brand new window for every
    console program it runs - and it runs git every few seconds."""
    if sys.platform != 'win32':
        return
    seen = {}
    real_run = d.subprocess.run

    def spy(cmd, **kw):
        seen.update(kw)
        return real_run(cmd, **kw)

    d.subprocess.run = spy
    try:
        branch = d.git(Path(__file__).parent, 'branch', '--show-current')
    finally:
        d.subprocess.run = real_run

    assert seen.get('creationflags') == 0x08000000, seen
    assert branch is not None, 'the spy broke the call it was watching'


def test_folders_outside_a_repo_group_by_themselves():
    with tempfile.TemporaryDirectory() as tmp:
        loose = Path(tmp) / 'Scratch Notes'
        loose.mkdir()
        info = d.git_info(loose)

    assert info['label'] == 'Scratch Notes', info
    assert info['branch'] == '' and info['dirty'] == 0, info
    assert info['repo'] == os.path.normcase(str(loose)), info

    # known-positive: a real repo still reports its branch and groups by
    # the shared git directory, not by cwd
    here = d.git_info(Path(__file__).parent)
    assert here['branch'], here
    assert here['repo'].endswith(os.path.normcase(os.path.join('.git', ''))[:-1])         or '.git' in here['repo'], here


def test_headline_uses_the_sessions_own_lead_and_never_splits_a_word():
    """The card used to show the first N characters, which cut mid-word and
    often caught nothing but throat-clearing."""
    assert d.headline('**Codex support lands.**\n\nDetail follows.') == \
        'Codex support lands.'
    assert d.headline('## What changed\n\nlots') == 'What changed'
    assert d.headline('Read [the plan](docs/plan.md) first') == \
        'Read the plan first'
    assert d.headline('') == '' and d.headline('\n\n  \n') == ''

    long = 'First sentence here. Second one runs on and on and on for ages.'
    assert d.headline(long, limit=40) == 'First sentence here.'

    # no sentence break in reach: cut on a space and mark the cut, never
    # mid-word
    run_on = 'alpha bravo charlie delta echo foxtrot golf hotel india juliet'
    got = d.headline(run_on, limit=30)
    assert got.endswith('…') and ' ' == run_on[len(got) - 1], got
    assert len(got) <= 31, got


def test_a_card_glows_only_when_a_session_starts_wanting_you():
    """A brand new session must not glow - on a board restart every card
    would light up at once, which is noise rather than news. Answering a
    question must end the glow rather than start a fresh one."""
    d._PREV_STATE.clear()
    assert not d.note_change('s1', 'working', now=100.0), 'first sight glowed'
    assert not d.note_change('s1', 'working', now=101.0), 'no change glowed'
    assert d.note_change('s1', 'asking', now=102.0), 'the change did not glow'
    assert d.note_change('s1', 'asking', now=110.0), 'glow ended too early'
    assert not d.note_change('s1', 'asking', now=102.0 + d.PULSE_SECONDS + 1), \
        'glow never ended'

    # you answered: back to work, and the glow goes out at once
    assert not d.note_change('s1', 'working', now=130.0), 'answering re-glowed'
    # finishing a turn wants you again, so that does glow
    assert d.note_change('s1', 'done', now=131.0), 'finishing did not glow'

    # a second session is tracked on its own
    assert not d.note_change('s2', 'asking', now=200.0)
    d._PREV_STATE.clear()


def test_local_summaries_are_optional_and_never_block_the_page():
    """The board has to work on a machine with no graphics card, so the model
    is off unless configured - and a card must never wait on one."""
    assert d.SUMMARY_MODEL == '', 'summaries must ship switched off'
    assert d.summarise('fix it', 'read the config file', ['Read a.py']) == ''

    d.SUMMARY_MODEL, d.OLLAMA_URL = 'test-model', 'http://127.0.0.1:9'
    try:
        d._SUMMARIES[d.summary_key('ask', 'said', ['Read a.py'])] = \
            'reading the config'
        assert d.summarise('ask', 'said', ['Read a.py']) == 'reading the config'
        # the turn moved on, so the cached answer must not be reused - and
        # asking must hand back the waiting line at once rather than wait for
        # the model
        assert d.summarise('ask', 'said', ['Read b.py']) == d.SUMMARISING

        # a different request is a different turn, however alike the replies
        d._ASKED.clear()
        assert d.summarise('other', 'said', ['Read a.py']) == d.SUMMARISING

        # a turn with nothing in it is not worth waking the model for
        d._ASKED.clear()
        assert d.summarise('ask', '   ', ['Read a.py']) == ''
        assert not d._ASKED, 'an empty turn was sent to the model'
    finally:
        d.SUMMARY_MODEL, d.OLLAMA_URL = '', 'http://127.0.0.1:11434'
        d._SUMMARIES.clear()
        d._ASKED.clear()


def test_a_small_model_gets_tidied_up_after_itself():
    """Asking a 1.5B model not to restate the question made it shout and echo
    the prompt instead, so the tidying is a rule rather than a request."""
    assert d.clean_summary('This coding session is reading the config') == \
        'reading the config'
    assert d.clean_summary('The session aims to fix the parser.') == \
        'fix the parser'
    assert d.clean_summary('Coding session focusing on ten questions') == \
        'ten questions'
    assert d.clean_summary('It said: Written to') == 'Written to'
    assert d.clean_summary('SEARCHING FOR A PUBLIC DOC') == \
        'Searching for a public doc'
    assert d.clean_summary('  "editing  the board"  ') == 'editing the board'
    # a line that was already fine must come back untouched
    assert d.clean_summary('Blocked - safety system prevents force push') == \
        'Blocked - safety system prevents force push'
    assert d.clean_summary('') == ''


def test_the_models_answer_is_tidied_and_a_dead_model_costs_nothing():
    """The tidying only helps if the answer actually goes through it, and a
    model that is missing or down must not break the page."""
    class Fake:
        def __init__(self, text):
            self.text = text

        def read(self):
            return json.dumps({'response': self.text}).encode('utf-8')

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    real = d.urllib.request.urlopen
    d.SUMMARY_MODEL = 'test-model'
    try:
        d.urllib.request.urlopen = \
            lambda *a, **k: Fake('THIS CODING SESSION IS READING X')
        d._fetch_summary('k1', 'ask', 'said', ['Read a.py'])
        assert d._SUMMARIES['k1'] == 'Reading x', d._SUMMARIES['k1']

        def dead(*a, **k):
            raise OSError('connection refused')

        d.urllib.request.urlopen = dead
        d._ASKED.add('k2')                       # as summarise() would have
        d._fetch_summary('k2', 'ask', 'said', [])  # must not raise
        assert 'k2' not in d._SUMMARIES, 'a failure was cached as the answer'
        assert 'k2' not in d._ASKED, 'a failed ask was never released'
        assert d._DOWN_UNTIL > time.time(), 'a failure set no quiet spell'
    finally:
        d.urllib.request.urlopen = real
        d.SUMMARY_MODEL = ''
        d._SUMMARIES.clear()
        d._ASKED.clear()
        d._DOWN_UNTIL = 0.0


def test_the_board_asks_again_once_ollama_comes_back():
    """Ollama not running is the ordinary case - it is a separate program,
    and the board does not start it. The old code asked once, cached the
    nothing it got back as that turn's summary, and never asked again, so a
    board opened before Ollama stayed blank until it was restarted. It must
    go quiet for a spell and then try again by itself."""
    class Answer:
        def read(self):
            return json.dumps({'response': 'reading the config'}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    asks = []

    def dead(*a, **k):
        asks.append(1)
        raise OSError('connection refused')

    real, started = d.urllib.request.urlopen, d.threading.Thread
    d.SUMMARY_MODEL = 'test-model'
    try:
        # run the fetch on this thread, so the test never races one
        d.threading.Thread = lambda target, args, daemon: type(
            'Now', (), {'start': lambda _s: target(*args)})()
        d.urllib.request.urlopen = dead

        # it cannot know Ollama is down until it has tried, so the first
        # card says it is waiting and the next refresh drops the line
        assert d.summarise('ask', 'said', []) == d.LOADING_MODEL
        assert asks == [1], 'the first turn never reached Ollama'
        # still down, so the whole feature holds off rather than retrying
        assert d.summarise('other', 'said', []) == ''
        assert asks == [1], 'it kept hammering a dead Ollama'

        # the quiet spell runs out and Ollama is back
        d._DOWN_UNTIL = 0.0
        d.urllib.request.urlopen = lambda *a, **k: Answer()
        assert d.summarise('later', 'said', []) == d.LOADING_MODEL
        assert d.summarise('later', 'said', []) == 'reading the config'
        assert d._DOWN_UNTIL == 0.0, 'a good answer left the board holding off'
    finally:
        d.urllib.request.urlopen, d.threading.Thread = real, started
        d.SUMMARY_MODEL = ''
        d._SUMMARIES.clear()
        d._ASKED.clear()
        d._DOWN_UNTIL = 0.0


def test_background_codex_job_is_noted_on_the_claude_card():
    """A review run in the background ends Claude's turn, so the transcript
    reads `done` while Codex is still at it. The Codex plugin's own job log
    says which session started it and which process is doing the work."""
    now = datetime.now(timezone.utc)
    iso = lambda t: t.isoformat().replace('+00:00', 'Z')
    dead_pid = 4_000_000  # far above any PID Windows hands out
    jobs = [
        {'id': 'live', 'status': 'running', 'jobClass': 'review', 'kindLabel': 'review',
         'sessionId': 'S1', 'pid': os.getpid(),
         'startedAt': iso(now), 'updatedAt': iso(now)},
        {'id': 'queued', 'status': 'queued', 'jobClass': 'task', 'kindLabel': 'rescue',
         'sessionId': 'S2', 'pid': os.getpid(), 'updatedAt': iso(now)},
        # The machine crashed mid-review: the log still says running.
        {'id': 'crashed', 'status': 'running', 'jobClass': 'review', 'kindLabel': 'review',
         'sessionId': 'S3', 'pid': dead_pid, 'updatedAt': iso(now)},
        # The PID was handed to a newer process after the job's one died.
        {'id': 'recycled', 'status': 'running', 'jobClass': 'review', 'kindLabel': 'review',
         'sessionId': 'S4', 'pid': os.getpid(),
         'updatedAt': iso(now - timedelta(days=30))},
        {'id': 'old', 'status': 'completed', 'jobClass': 'review', 'kindLabel': 'review',
         # A live PID, so only the status can rule it out.
         'sessionId': 'S5', 'pid': os.getpid(), 'updatedAt': iso(now)},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / 'codex-openai-codex' / 'state' / 'repo-abc'
        state.mkdir(parents=True)
        (state / 'state.json').write_text(json.dumps({'jobs': jobs}), encoding='utf-8')
        (Path(tmp) / 'other' / 'state' / 'junk').mkdir(parents=True)
        (Path(tmp) / 'other' / 'state' / 'junk' / 'state.json').write_text('{oops', encoding='utf-8')

        got = d.codex_jobs(roots=[tmp])
        assert set(got) == {'S1', 'S2'}, got
        assert got == {'S1': 'Codex review', 'S2': 'Codex rescue'}, got

    # The turn can be over while Codex works: the state stays what it is.
    done = {'state': 'done', 'detail': '', 'since': 'x', 'last_ts': 'x'}
    over = d.with_background(dict(done), [], got['S1'])
    assert over['state'] == 'done' and over['background'] == ['Codex review'], over
    # The plugin runs that review as a background command of its own, so the
    # transcript holds it too. It is one task, told by its Codex name.
    launch = [{'label': 'Codex review', 'command': 'node codex-companion.mjs review'}]
    assert d.with_background(dict(done), launch, got['S1'])['background'] == ['Codex review']
    assert d.with_background(dict(done), [], None)['background'] == []


def bg_launch(use_id, task_id, tool, inp, agent=False):
    """A background task starting, as the transcript records it: the call,
    then a result that hands back at once carrying the task's id."""
    result = ({'isAsync': True, 'status': 'async_launched', 'agentId': task_id}
              if agent else {'stdout': '', 'backgroundTaskId': task_id})
    return [
        {'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'id': use_id, 'name': tool, 'input': inp}]}},
        {'type': 'user', 'toolUseResult': result, 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': use_id, 'content': 'started'}]}},
    ]


def bg_notice(task_id, use_id='', status='completed'):
    """The message the app adds when a background task reports back."""
    use = f'<tool-use-id>{use_id}</tool-use-id>\n' if use_id else ''
    stat = f'<status>{status}</status>\n' if status else ''
    return {'type': 'user', 'origin': {'kind': 'task-notification'},
            'message': {'content': f'<task-notification>\n<task-id>{task_id}</task-id>\n'
                                   f'{use}{stat}<summary>x</summary>\n</task-notification>'}}


def test_a_running_background_task_is_noted_not_mistaken_for_work():
    """Starting a background task hands back at once, so the turn can end
    while it runs. The turn really is over - it may be your turn, or a
    question may be waiting - so the state is left alone and the card notes
    the task. A task is running until the app reports it back."""
    reply = {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Started it.'}]}}
    entries = [
        *bg_launch('tu1', 'agentA', 'Agent', {'description': 'Review Task 4'}, agent=True),
        *bg_launch('tu2', 'bashB', 'Bash', {'command': 'pytest -q', 'description': 'Run the tests'}),
        *bg_launch('tu3', 'bashC', 'Bash', {'command': 'sleep 1'}),
        *bg_launch('tu4', 'bashD', 'Bash', {'command': 'npm run build'}),
        # A Monitor event names a task but carries no status: not an ending.
        bg_notice('bashB', status=''),
        bg_notice('bashC', 'tu3'),
        # Still queued behind the turn, not yet delivered, but finished all the same.
        {'type': 'queue-operation', 'operation': 'enqueue',
         'content': bg_notice('bashD', 'tu4', 'failed')['message']['content']},
        reply,
    ]
    tasks = d.background_tasks(entries)
    assert [t['label'] for t in tasks] == ['Review Task 4', 'Run the tests'], tasks

    act = d.with_background(d.read_activity(entries), tasks, None)
    assert act['state'] == 'done', act
    assert act['background'] == ['Review Task 4', 'Run the tests'], act

    # Both report back: nothing is running.
    entries += [bg_notice('agentA', 'tu1'), bg_notice('bashB', 'tu2', 'killed'), reply]
    assert d.background_tasks(entries) == []
    # Stopping a task by hand ends it, and the app sends no note for that.
    stopped = [*bg_launch('tu5', 'bashE', 'Bash', {'command': 'grep -r x'}),
               {'type': 'assistant', 'message': {'content': [
                   {'type': 'tool_use', 'id': 'tu6', 'name': 'TaskStop',
                    'input': {'task_id': 'bashE'}}]}}]
    assert d.background_tasks(stopped) == []
    # Reading a task's output names it too, and leaves it running.
    peeked = [*stopped[:2], {'type': 'assistant', 'message': {'content': [
        {'type': 'tool_use', 'id': 'tu7', 'name': 'TaskOutput', 'input': {'task_id': 'bashE'}}]}}]
    assert [t['label'] for t in d.background_tasks(peeked)] == ['grep -r x']
    # A command run in the foreground is not a background task.
    plain = [{'type': 'user', 'toolUseResult': {'stdout': 'ok'},
              'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'x'}]}}]
    assert d.background_tasks(plain) == []


def test_codex_liveness_is_a_held_lock_not_a_guess():
    """Codex writes no process id, but each open thread keeps its lock file
    open - and Windows releases the handle when the process dies."""
    if sys.platform != 'win32':
        return
    with tempfile.TemporaryDirectory() as tmp:
        locks = Path(tmp) / 'thread-writer-locks'
        locks.mkdir()
        held = locks / 'aaa.lock'
        free = locks / 'bbb.lock'
        held.write_text('', encoding='utf-8')
        free.write_text('', encoding='utf-8')
        (locks / '.coordination.lock').write_text('', encoding='utf-8')

        with open(held, 'r+', encoding='utf-8'):       # this process holds it
            assert d.codex_lock_held(held), 'a live thread was reported dead'
            assert not d.codex_lock_held(free), 'an abandoned lock read as live'
            assert d.codex_live_ids(root=tmp) == ['aaa'], d.codex_live_ids(root=tmp)

        assert not d.codex_lock_held(held), 'the lock stayed live after release'
        assert not d.codex_lock_held(locks / 'gone.lock')


def test_codex_reads_the_turn_markers_and_the_command():
    """Codex brackets every turn with task_started and task_complete, so its
    state is read off the log rather than inferred."""
    def call(cmd):
        return {'type': 'response_item', 'timestamp': '2026-09-06T10:00:05Z',
                'payload': {'type': 'function_call', 'name': 'exec_command',
                            'arguments': json.dumps({'cmd': cmd})}}

    open_turn = [
        {'type': 'event_msg', 'timestamp': '2026-09-06T10:00:00Z',
         'payload': {'type': 'task_started'}},
        call('git status'),
    ]
    got = d.read_codex_activity(open_turn)
    assert got['state'] == 'working', got['state']
    assert got['detail'] == 'run git status', got['detail']
    assert got['trail'] == ['run git status'], got['trail']

    closed = open_turn + [
        {'type': 'response_item', 'timestamp': '2026-09-06T10:00:07Z',
         'payload': {'type': 'web_search_call',
                     'action': {'query': 'codex session docs'}}},
        {'type': 'event_msg', 'timestamp': '2026-09-06T10:00:09Z',
         'payload': {'type': 'task_complete',
                     'last_agent_message': '**Nothing found.**\n\nDetail.'}},
    ]
    got = d.read_codex_activity(closed)
    assert got['state'] == 'done', got['state']
    assert got['detail'] == '', 'a finished turn has no action to name'
    assert got['says'] == 'Nothing found.', got['says']
    assert got['trail'] == ['run git status', 'search codex session docs'], \
        got['trail']
    assert got['since'] == '2026-09-06T10:00:09Z', got['since']

    assert d.read_codex_activity([])['state'] == 'idle'


def test_codex_exec_wrapper_extracts_the_real_command():
    """The computer-use tool names every command 'exec' and buries the real
    command inside a JS snippet instead of a plain JSON payload."""
    payload = {'type': 'custom_tool_call', 'name': 'exec',
               'input': 'const r = await tools.exec_command('
                        '{"cmd":"git status"});\ntext(r.output);'}
    assert d.codex_call_label(payload) == 'run git status', \
        d.codex_call_label(payload)
    assert d.codex_call_label({'type': 'custom_tool_call', 'name': 'exec'}) \
        == 'run'


def test_codex_labels_every_shape_the_real_logs_use():
    """Each input below is a shape copied from a real rollout. The exec
    wrapper is JavaScript, so its command key may be unquoted, may be named
    `command`, and the call may not be a shell at all. A label of plain
    `run` for all of them is the bug this guards."""
    def wrapped(js):
        return {'type': 'custom_tool_call', 'name': 'exec', 'input': js}

    cases = [
        # the most common Codex tool by far, and it names the text `command`
        ({'type': 'function_call', 'name': 'shell_command',
          'arguments': json.dumps({'command': 'git status',
                                   'workdir': 'C:\\x', 'timeout_ms': 10000})},
         'run git status'),
        # JavaScript object key with no quotes around it
        (wrapped('text(await tools.exec_command({cmd:"git status",'
                 '"max_output_tokens":7000}));\n'), 'run git status'),
        # the wrapper calling the shell tool whose key is `command`
        (wrapped('const a = await tools.shell_command('
                 '{"command":"git status"});'), 'run git status'),
        # JavaScript string escapes: `\\` in the source is one backslash
        (wrapped(r'tools.exec_command({cmd:"Get-Content C:\\x.md"})'),
         r'run Get-Content C:\x.md'),
        # a tool that is not a shell: name it, do not call it `run`
        (wrapped('const r = await tools.web__run({search_query:[{q:"x"}]});'),
         'web__run'),
        (wrapped('const r = await tools.mcp__codex_apps__sites_get_site('
                 '{project_id:"p"});'), 'sites_get_site'),
        # a command held in a variable cannot be read from the source text
        (wrapped('tools.exec_command({cmd,workdir:"C:\\\\x"});'), 'run'),
    ]
    bad = [(want, d.codex_call_label(p)) for p, want in cases
           if d.codex_call_label(p) != want]
    assert not bad, f'(wanted, got): {bad}'


def test_codex_title_and_working_folder_come_off_disk():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / 'session_index.jsonl').write_text('\n'.join([
            json.dumps({'id': 'aaa', 'thread_name': 'First thread'}),
            'not json at all',
            json.dumps({'id': 'bbb'}),
        ]), encoding='utf-8')
        assert d.codex_titles(root=root) == {'aaa': 'First thread'}

        day = root / 'sessions' / '2026' / '09' / '06'
        day.mkdir(parents=True)
        roll = day / 'rollout-2026-09-06T10-00-00-aaa.jsonl'
        roll.write_text(json.dumps({
            'type': 'session_meta',
            'payload': {'cwd': r'C:\repos\Thing', 'git': {'branch': 'main'}},
        }), encoding='utf-8')

        assert d.codex_rollout('aaa', root=root) == roll
        assert d.codex_meta(roll)['cwd'] == r'C:\repos\Thing'
        # a thread with a lock but no log yet is a blank tab, not a session
        assert d.codex_rollout('zzz', root=root) is None

    assert d.codex_titles(root=Path(tmp) / 'gone') == {}


def test_codex_hides_its_own_guardian_review_threads():
    """A guardian_review thread is Codex checking its own next action for
    safety, not a task anyone asked for - it has no business on the board."""
    if sys.platform != 'win32':
        return
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        locks = root / 'thread-writer-locks'
        locks.mkdir()
        real, guard = locks / 'real.lock', locks / 'guard.lock'
        real.write_text('', encoding='utf-8')
        guard.write_text('', encoding='utf-8')

        day = root / 'sessions' / '2026' / '09' / '14'
        day.mkdir(parents=True)
        (day / 'rollout-2026-09-14T10-00-00-real.jsonl').write_text(json.dumps({
            'type': 'session_meta', 'payload': {'cwd': r'C:\repos\Thing'},
        }), encoding='utf-8')
        (day / 'rollout-2026-09-14T10-05-00-guard.jsonl').write_text(json.dumps({
            'type': 'session_meta',
            'payload': {'cwd': r'C:\repos\Thing',
                        'thread_source': 'guardian_review'},
        }), encoding='utf-8')

        with open(real, 'r+', encoding='utf-8'), open(guard, 'r+', encoding='utf-8'):
            rows = d.codex_rows(root=root)

    assert [r['sid'] for r in rows] == ['real'], rows


def test_a_second_launch_opens_a_window_instead_of_a_second_server():
    """Windows lets two ThreadingHTTPServers bind the same port at once
    (SO_REUSEADDR), so the guard has to catch this before binding, not after."""
    real_connect = d.socket.create_connection
    real_server_cls = d.ThreadingHTTPServer
    real_argv = sys.argv[:]

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    try:
        sys.argv = ['dashboard.py', '--no-browser']

        # someone already answers on the port: must not construct a server
        d.socket.create_connection = lambda *a, **k: FakeSocket()
        d.ThreadingHTTPServer = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError('a second server must never be constructed'))
        d.main()  # returns quietly instead of raising

        # nothing answers: the real path still has to construct and serve
        built = []

        class FakeServer:
            def __init__(self, *a, **k):
                built.append(a)

            def serve_forever(self):
                pass

        def dead(*a, **k):
            raise OSError('connection refused')

        d.socket.create_connection = dead
        d.ThreadingHTTPServer = FakeServer
        d.main()
        assert built, 'a real server must be constructed when nothing answers'
    finally:
        d.socket.create_connection = real_connect
        d.ThreadingHTTPServer = real_server_cls
        sys.argv = real_argv


def test_the_gpu_is_given_back_when_the_window_shuts():
    """A board nobody is looking at must not sit on VRAM. Two halves: the
    page going quiet is what counts as shut, and the unload has to actually
    reach Ollama with a keep_alive of zero."""
    now = 1000.0
    real_seen = d._LAST_SEEN
    try:
        d._LAST_SEEN = now
        assert not d.window_gone(now + d.IDLE_EXIT_SECONDS - 1), \
            'a page that just refreshed is not a shut window'
        assert d.window_gone(now + d.IDLE_EXIT_SECONDS + 1), \
            'the window has been silent for longer than a refresh can explain'
    finally:
        d._LAST_SEEN = real_seen

    sent = []

    class Fake:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    real = d.urllib.request.urlopen
    try:
        d.urllib.request.urlopen = \
            lambda req, **k: (sent.append(json.loads(req.data)), Fake())[1]

        d.SUMMARY_MODEL = ''
        d.unload_model()
        assert sent == [], 'nothing to unload when summaries are off'

        d.SUMMARY_MODEL = 'test-model'
        d.unload_model()
        assert len(sent) == 1 and sent[0]['keep_alive'] == 0, sent
        assert sent[0]['model'] == 'test-model', sent

        def dead(*a, **k):
            raise OSError('connection refused')

        d.urllib.request.urlopen = dead
        d.unload_model()          # Ollama already gone must not raise
    finally:
        d.urllib.request.urlopen = real
        d.SUMMARY_MODEL = ''


def test_a_summary_does_not_park_the_model_in_the_gpu_for_half_an_hour():
    """The hold after a summary has to be short enough that a board left
    open, but no longer summarising anything, gives the GPU back on its own."""
    assert d.KEEP_ALIVE.endswith('m') and int(d.KEEP_ALIVE[:-1]) <= 5, \
        d.KEEP_ALIVE

    sent = []

    class Fake:
        def read(self):
            return json.dumps({'response': 'x'}).encode('utf-8')

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    real = d.urllib.request.urlopen
    d.SUMMARY_MODEL = 'test-model'
    try:
        d.urllib.request.urlopen = \
            lambda req, **k: (sent.append(json.loads(req.data)), Fake())[1]
        d._fetch_summary('ka', 'ask', 'said', [])
        assert sent[0]['keep_alive'] == d.KEEP_ALIVE, sent
    finally:
        d.urllib.request.urlopen = real
        d.SUMMARY_MODEL = ''
        d._SUMMARIES.clear()


def test_a_starting_session_sees_the_collision_it_is_walking_into():
    """The hook asks this before its own transcript exists, so the session
    doing the asking has to be counted in even though nothing lists it yet."""
    here = os.path.normcase(os.path.abspath('C:/repos/app'))
    other = os.path.normcase(os.path.abspath('C:/repos/app-wt'))

    def neighbour(cwd, sid='n1', branch='feature', pid=None):
        return {'sid': sid, 'agent': 'codex', 'cwd': cwd, 'warnings': [],
                'pid': pid, 'repo': 'r', 'label': 'app', 'branch': branch,
                'dirty': 0, 'is_main': True}

    real_rows, real_git = d.session_rows, d.git_info
    try:
        d.git_info = lambda cwd: {'repo': 'r', 'label': 'app',
                                  'branch': 'feature', 'dirty': 0,
                                  'is_main': True}

        d.session_rows = lambda: []
        assert d.clashes_for(here) == [], 'an empty machine has no collisions'

        d.session_rows = lambda: [neighbour(here)]
        heads = [h for h, _ in d.clashes_for(here)]
        assert heads == ['same worktree'], heads

        # the same folder, but it is us - a session must not warn about itself
        d.session_rows = lambda: [neighbour(here, sid='abc123')]
        assert d.clashes_for(here, 'abc123') == [], 'warned about itself'

        # a different folder on the same branch is the case the old hook,
        # which only counted transcripts in this project, could never see
        d.session_rows = lambda: [neighbour(other)]
        heads = [h for h, _ in d.clashes_for(here)]
        assert heads == ['same branch'], heads

        d.session_rows = lambda: [neighbour(other, branch='other-branch')]
        assert d.clashes_for(here) == [], 'a different branch is not a clash'

        # Clearing a session reuses the process, and the registry - which is
        # filed under the pid - only catches up seconds after SessionStart.
        # Until it does, our own entry still names the session we replaced.
        stale = [neighbour(here, sid='just-cleared', pid=37932)]
        d.session_rows = lambda: stale
        assert d.clashes_for(here, 'fresh-sid', pid=37932) == [], \
            'warned about the session it replaced in its own process'
        # and the same row in a different process is still a real neighbour
        heads = [h for h, _ in d.clashes_for(here, 'fresh-sid', pid=99999)]
        assert heads == ['same worktree'], heads
    finally:
        d.session_rows, d.git_info = real_rows, real_git


def test_the_session_start_hook_runs_and_reports_the_workspace():
    """The hook is a separate process reaching back into dashboard.py, so an
    import that only works from the repo root would break it silently."""
    hook = ROOT / 'hooks' / 'git-workspace-brief.py'
    assert hook.exists(), hook

    with tempfile.TemporaryDirectory() as tmp:
        for args in (['init', '-q'], ['config', 'user.email', 'a@b.c'],
                     ['config', 'user.name', 'T'],
                     ['commit', '-q', '--allow-empty', '-m', 'x']):
            subprocess.run(['git', *args], cwd=tmp, capture_output=True)
        out = subprocess.run(
            [sys.executable, str(hook)],
            input=json.dumps({'cwd': tmp, 'session_id': 'nobody'}),
            capture_output=True, text=True, timeout=60, cwd=tempfile.gettempdir())
        assert out.returncode == 0, out.stderr
        ctx = json.loads(out.stdout)['hookSpecificOutput']['additionalContext']

    assert 'GIT WORKSPACE' in ctx, ctx
    assert 'trunk: main' in ctx, ctx
    assert 'worktrees' in ctx, ctx
    assert 'trunk / new branch / new worktree' in ctx, ctx
    # a fresh empty repo has nobody else in it
    assert 'WARNING' not in ctx, ctx
    # ...and "nobody else" must mean nobody, not a failed import
    assert 'collision check unavailable' not in ctx, ctx


def test_the_hook_actually_prints_the_collisions_it_is_given():
    """Companion to the check above. That one only ever proves the hook can
    stay quiet; a hook wired to nothing would pass it forever."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'workspace_brief',
        ROOT / 'hooks' / 'git-workspace-brief.py')
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)

    real_clashes, real_stdin, real_out = d.clashes_for, sys.stdin, sys.stdout
    try:
        d.clashes_for = lambda cwd, sid='', pid=None: [('same branch', 'a codex '
                                              'session saves here too')]
        sys.stdin = io.StringIO(json.dumps({'cwd': os.getcwd(),
                                            'session_id': 'nobody'}))
        sys.stdout = io.StringIO()
        hook.main()
        ctx = json.loads(sys.stdout.getvalue())['hookSpecificOutput'][
            'additionalContext']
    finally:
        d.clashes_for, sys.stdin, sys.stdout = real_clashes, real_stdin, real_out

    assert 'WARNING: SAME BRANCH' in ctx, ctx
    assert 'a codex session saves here too' in ctx, ctx

    # and when the check cannot run at all, silence would read as "nobody
    # else is here" - the one wrong answer that gets work clobbered
    def broken(cwd, sid='', pid=None):
        raise RuntimeError('no dashboard')

    try:
        d.clashes_for = broken
        sys.stdin = io.StringIO(json.dumps({'cwd': os.getcwd(),
                                            'session_id': 'nobody'}))
        sys.stdout = io.StringIO()
        hook.main()
        ctx = json.loads(sys.stdout.getvalue())['hookSpecificOutput'][
            'additionalContext']
    finally:
        d.clashes_for, sys.stdin, sys.stdout = real_clashes, real_stdin, real_out

    assert 'collision check unavailable' in ctx, ctx
    assert 'RuntimeError' in ctx, ctx




def test_model_and_app_names_are_trimmed_to_what_is_readable():
    """Both go on the card, so an id nobody can read is a bug you see."""
    assert d.pretty_model('claude-opus-5') == 'Opus 5'
    assert d.pretty_model('claude-haiku-4-5-20251001') == 'Haiku 4.5'
    assert d.pretty_model('claude-sonnet-5') == 'Sonnet 5'
    assert d.pretty_model('gpt-5.4-mini') == 'GPT-5.4-mini'
    assert d.pretty_model('') == ''
    # an id matching no rule is shown as it is rather than mangled
    assert d.pretty_model('something-else-1') == 'Something-else-1'

    assert d.pretty_app('claude-desktop') == 'desktop'
    assert d.pretty_app('cli') == 'terminal'
    assert d.pretty_app('Codex Desktop') == 'desktop'
    assert d.pretty_app('vscode') == 'vscode'
    assert d.pretty_app(None) == ''


def test_the_model_in_use_is_read_from_both_transcript_formats():
    claude = [
        {'type': 'assistant', 'timestamp': '2026-09-07T10:00:00Z',
         'message': {'model': 'claude-opus-5',
                     'content': [{'type': 'text', 'text': 'hello'}]}},
    ]
    assert d.read_activity(claude)['model'] == 'claude-opus-5'
    # ...and a transcript that never names one must not invent it
    nameless = [dict(claude[0], message={'content': claude[0]['message']['content']})]
    assert d.read_activity(nameless)['model'] == ''

    codex = [{'type': 'turn_context', 'timestamp': '2026-09-07T10:00:00Z',
              'payload': {'model': 'gpt-5.4-mini', 'effort': 'medium'}}]
    assert d.read_codex_activity(codex)['model'] == 'gpt-5.4-mini'


def test_a_busy_session_that_has_gone_quiet_says_so():
    """The one thing the log can honestly report. It cannot tell a session
    parked on a permission prompt from one running a slow command, so the
    card states the silence and leaves the reading to you."""
    def card(state, minutes):
        when = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        return {'agent': 'claude', 'state': state, 'title': 't',
                'folder': 'f', 'is_main': True, 'branch': 'main', 'dirty': 0,
                'warnings': [], 'pulse': False, 'detail': '', 'says': '',
                'trail': [], 'since': when.isoformat(), 'app': '', 'model': '',
                'started': when.isoformat(), 'committed': None,
                'last_ts': when.isoformat()}

    loud = d.render([('app', [card('working', d.QUIET_MINUTES - 1)])])
    assert 'silent' not in loud, 'called a working session quiet too early'

    hushed = d.render([('app', [card('working', d.QUIET_MINUTES + 5)])])
    assert 'silent' in hushed, hushed

    # a finished session is not "quiet", it is done - the label would be noise
    ended = d.render([('app', [card('done', d.QUIET_MINUTES + 5)])])
    assert 'silent' not in ended, ended


def test_the_card_notes_background_tasks_beside_the_state():
    """The state keeps saying whose turn it is. The note says something is
    still running, and hovering it names what."""
    def card(state, background):
        now = datetime.now(timezone.utc).isoformat()
        return {'agent': 'claude', 'state': state, 'title': 't',
                'folder': 'f', 'is_main': True, 'branch': 'main', 'dirty': 0,
                'warnings': [], 'pulse': False, 'detail': '', 'says': '',
                'trail': [], 'since': now, 'app': '', 'model': '',
                'started': now, 'committed': None, 'last_ts': now,
                'background': background}

    one = d.render([('app', [card('done', ['Codex review'])])])
    assert 'done — your turn' in one and 'background task running' in one, one
    assert 'title="Codex review"' in one, one
    two = d.render([('app', [card('asking', ['Review Task 4', 'Run <tests>'])])])
    assert '2 background tasks running' in two, two
    assert 'Review Task 4, Run &lt;tests&gt;' in two, 'the names are escaped'
    # Busy already says something is running.
    assert 'background task' not in d.render([('app', [card('working', ['x'])])])
    assert 'background task' not in d.render([('app', [card('done', [])])])


def test_waiting_sessions_come_first_and_are_counted_in_the_title():
    """The board is read at a glance, and the glance is always asking the
    same question: is anything waiting on me?"""
    def row(state, label, title):
        return {'agent': 'claude', 'sid': title, 'cwd': '/repos/' + label,
                'state': state, 'title': title, 'repo': label, 'label': label,
                'branch': 'main', 'dirty': 0, 'is_main': True, 'folder': label,
                'warnings': [], 'says': '', 'detail': '', 'trail': [],
                'since': None, 'last_ts': None, 'app': '', 'model': '',
                'started': None, 'committed': None, 'pulse': False}

    busy = row('working', 'aaa', 'busy one')
    idle_ = row('working', 'zzz', 'other busy one')
    asking = row('asking', 'zzz', 'needs an answer')

    real = d.session_rows
    try:
        d.session_rows = lambda: [busy, idle_, asking]
        groups = d.collect()
    finally:
        d.session_rows = real

    # 'zzz' holds the waiting session, so it outranks 'aaa' despite the name
    assert [g[0] for g in groups] == ['zzz', 'aaa'], groups
    assert groups[0][1][0]['title'] == 'needs an answer', groups[0][1]

    page = d.render(groups)
    assert '<title>1 waiting' in page, page[:400]
    assert 'stopped for you' in page and 'needs an answer' in page

    quiet_page = d.render([('aaa', [busy])])
    assert '<title>Airspace</title>' in quiet_page, quiet_page[:400]
    assert 'stopped for you' not in quiet_page


def test_the_strip_says_which_kind_of_stopped_each_session_is():
    """A session that asked a question and one that simply finished its turn
    both stop the work, but they are not the same thing and the strip used to
    call both of them "waiting on you" - which read as a question nobody had
    asked. Each line now says which, and the question sorts above the rest."""
    def row(state, title):
        return {'agent': 'claude', 'state': state, 'title': title,
                'folder': 'f', 'is_main': True, 'branch': 'main', 'dirty': 0,
                'warnings': [], 'pulse': False, 'detail': '', 'says': '',
                'trail': [], 'since': None, 'last_ts': None, 'app': '',
                'model': '', 'started': None, 'committed': None}

    page = d.render([('app', [row('done', 'finished a turn'),
                              row('asking', 'asked a question'),
                              row('working', 'still going')])])

    assert '2 sessions stopped for you' in page, page[:600]
    # the working session is not in the strip at all
    strip = page.split('<div class="triage">')[1].split('</div>')[0]
    assert 'still going' not in strip, strip
    # each line carries its own label, and the question comes first
    assert strip.index('needs your answer') < strip.index('done'), strip
    assert strip.index('asked a question') < strip.index('finished a turn'), strip
    # one line each, not a comma-separated run
    assert strip.count('<li>') == 2, strip

    # a lone finished session is never described as having asked anything
    alone = d.render([('app', [row('done', 'finished a turn')])])
    solo = alone.split('<div class="triage">')[1].split('</ul>')[0]
    assert '1 session stopped for you' in solo, solo
    assert 'needs your answer' not in solo, solo


def test_the_strip_names_the_project_before_each_title():
    """The strip only showed the session title, so two sessions with similar
    titles in different projects looked alike. The project name now leads each
    line in bold, so it is the first thing read."""
    r = {'agent': 'claude', 'state': 'done', 'title': 'Handoff fix',
         'folder': 'f', 'is_main': True, 'branch': 'main', 'dirty': 0,
         'warnings': [], 'pulse': False, 'detail': '', 'says': '',
         'trail': [], 'since': None, 'last_ts': None, 'app': '',
         'model': '', 'started': None, 'committed': None}

    page = d.render([('ContextOps', [r])])
    strip = page.split('<div class="triage">')[1].split('</ul>')[0]
    assert '<b>ContextOps</b>: Handoff fix' in strip, strip


def test_the_strip_shows_how_long_each_claude_cache_has_left():
    """A stopped Claude session is cheap to pick back up for an hour after its
    last reply, while its prompt cache is warm. The strip shows the time left
    with a traffic light, so the session about to go cold is the one you see."""
    def row(agent, title, mins_ago):
        since = (datetime.now(timezone.utc)
                 - timedelta(minutes=mins_ago, seconds=10)).isoformat()
        return {'agent': agent, 'state': 'done', 'title': title,
                'folder': 'f', 'is_main': True, 'branch': 'main', 'dirty': 0,
                'warnings': [], 'pulse': False, 'detail': '', 'says': '',
                'trail': [], 'since': since, 'last_ts': since, 'app': '',
                'model': '', 'started': None, 'committed': None}

    page = d.render([('app', [row('claude', 'green one', 26),
                              row('claude', 'yellow one', 45),
                              row('claude', 'red one', 55),
                              row('claude', 'cold one', 70),
                              row('codex', 'codex one', 5)])])
    strip = page.split('<div class="triage">')[1].split('</ul>')[0]
    lines = {l.split('</b>: ')[1].split('<')[0]: l
             for l in strip.split('<li>')[1:]}
    assert 'cache-light green' in lines['green one'], lines['green one']
    assert 'Cache state' in lines['green one'], lines['green one']
    assert '-- 34 mins remaining' in lines['green one'], lines['green one']
    assert 'cache-light yellow' in lines['yellow one'], lines['yellow one']
    assert '-- 15 mins remaining' in lines['yellow one'], lines['yellow one']
    assert 'cache-light red' in lines['red one'], lines['red one']
    assert '-- 5 mins remaining' in lines['red one'], lines['red one']
    assert '-- expired' in lines['cold one'], lines['cold one']
    # Codex caches on OpenAI's terms, not this hour, so it gets no light
    assert 'Cache state' not in lines['codex one'], lines['codex one']
    # it follows the project and title, before the app
    g = lines['green one']
    assert g.index('green one') < g.index('Cache state') < g.index('class="where"'), g


def test_the_cache_window_comes_from_the_last_reply():
    """A subscription caches for an hour, but not always: past the usage limit
    the same session drops to five minutes, then goes back. Each reply records
    which window it wrote, so the last reply that wrote one is the answer."""
    def reply(five, hour, side=False):
        return {'type': 'assistant', 'timestamp': '2026-10-17T10:00:00Z',
                'isSidechain': side,
                'message': {'content': [{'type': 'text', 'text': 'ok'}],
                            'usage': {'cache_creation': {
                                'ephemeral_5m_input_tokens': five,
                                'ephemeral_1h_input_tokens': hour}}}}
    read = lambda *es: d.read_activity(list(es))['cache_ttl']
    assert read(reply(0, 500)) == 60
    assert read(reply(0, 500), reply(400, 0)) == 5
    assert read(reply(400, 0), reply(0, 500)) == 60
    # a reply that only read the cache says nothing; the one before decides
    assert read(reply(400, 0), reply(0, 0)) == 5
    # a subagent's reply is not the session's cache
    assert read(reply(0, 500), reply(400, 0, side=True)) == 60
    # nothing recorded at all: assume the hour, as before
    assert read(reply(0, 0)) == 60

    since = (datetime.now(timezone.utc)
             - timedelta(minutes=2, seconds=10)).isoformat()
    r = {'agent': 'claude', 'since': since, 'cache_ttl': 5}
    assert '-- 3 mins remaining' in d.cache_light(r), d.cache_light(r)
    assert 'cache-light red' in d.cache_light(r), d.cache_light(r)


def test_old_uncommitted_work_is_called_out_but_fresh_work_is_not():
    def card(dirty, commit_minutes):
        when = datetime.now(timezone.utc) - timedelta(minutes=commit_minutes)
        return {'agent': 'claude', 'state': 'working', 'title': 't',
                'folder': 'f', 'is_main': True, 'branch': 'main',
                'dirty': dirty, 'warnings': [], 'pulse': False, 'detail': '',
                'says': '', 'trail': [], 'since': None, 'app': '', 'model': '',
                'started': None, 'committed': when.isoformat(), 'last_ts': None}

    old = d.render([('app', [card(3, d.STALE_COMMIT_MINUTES + 30)])])
    assert 'nothing committed for' in old, old
    fresh = d.render([('app', [card(3, 5)])])
    assert 'nothing committed for' not in fresh, fresh
    # nothing uncommitted, nothing to warn about however old the last commit
    clean = d.render([('app', [card(0, d.STALE_COMMIT_MINUTES * 10)])])
    assert 'nothing committed for' not in clean, clean


def test_a_long_span_is_reported_in_days():
    """'245h 17m' is a number nobody reads."""
    now = datetime.now(timezone.utc)
    assert d.span((now - timedelta(minutes=90)).isoformat()) == '1h 30m'
    assert d.span((now - timedelta(days=10)).isoformat()) == '10 days'
    assert d.span(None) == '—'


def test_the_strip_names_the_app_each_stopped_session_is_in():
    """The strip is read instead of the cards, so a line that says a session
    wants you but not where it lives sends you hunting through windows."""
    def row(agent, app, title):
        return {'agent': agent, 'state': 'asking', 'title': title,
                'folder': 'f', 'is_main': True, 'branch': 'main', 'dirty': 0,
                'warnings': [], 'pulse': False, 'detail': '', 'says': '',
                'trail': [], 'since': None, 'last_ts': None, 'app': app,
                'model': '', 'started': None, 'committed': None}

    page = d.render([('app', [row('claude', 'desktop', 'in the app'),
                              row('codex', 'terminal', 'in a shell'),
                              row('claude', '', 'app unknown')])])
    strip = page.split('<div class="triage">')[1].split('</ul>')[0]
    lines = strip.split('<li>')[1:]
    assert 'claude &middot; desktop' in lines[0], lines[0]
    assert 'codex &middot; terminal' in lines[1], lines[1]
    # no app on file: the agent alone, never a dangling separator
    assert 'claude' in lines[2] and '&middot;' not in lines[2], lines[2]


def test_the_summary_model_is_given_the_request_and_the_whole_reply():
    """What the card shows and what the model reads are different things.

    The card gets one trimmed line. The model used to get that same line, so
    the only move left to it was to reword a sentence. It now gets what was
    asked and the reply entire.
    """
    reply = ('## Cutting the retry loop\n\n'
             'The [handler](a.py) swallowed the timeout, so a failed upload '
             'looked like a clean one. ' + 'Detail. ' * 40)
    entries = [
        {'type': 'user', 'timestamp': '2026-09-05T10:00:00Z',
         'message': {'content': 'why do uploads silently fail?'}},
        {'type': 'assistant', 'timestamp': '2026-09-05T10:00:20Z',
         'message': {'content': [{'type': 'text', 'text': reply}]}},
        # the harness writes these; neither is something you typed
        {'type': 'user', 'timestamp': '2026-09-05T10:00:25Z', 'isMeta': True,
         'message': {'content': 'Caveat: the messages below were generated'}},
        {'type': 'user', 'timestamp': '2026-09-05T10:00:30Z',
         'message': {'content': [{'type': 'tool_result', 'content': 'ok'}]}},
    ]
    got = d.read_activity(entries)
    assert got['asked'] == 'why do uploads silently fail?', got['asked']
    assert 'swallowed the timeout' in got['says_full'], got['says_full']
    assert len(got['says_full']) > 400, \
        'the model is still being handed a trimmed line'
    # the card itself stays short, and stays the first line
    assert got['says'] == 'Cutting the retry loop', got['says']


def test_the_request_and_the_reply_both_reach_the_model():
    """The plumbing above is worth nothing if the prompt drops it."""
    sent = {}

    class Fake:
        def read(self):
            return json.dumps({'response': 'fixing the silent upload retry'}) \
                .encode('utf-8')

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    real = d.urllib.request.urlopen
    d.SUMMARY_MODEL = 'test-model'
    try:
        def spy(req, *a, **k):
            sent.update(json.loads(req.data.decode('utf-8')))
            return Fake()

        d.urllib.request.urlopen = spy
        d._fetch_summary('k', 'why do uploads silently fail?',
                         'the handler swallowed the timeout',
                         ['Read a.py', 'sh pytest'])
        prompt = sent['prompt']
        assert 'why do uploads silently fail?' in prompt, prompt
        assert 'swallowed the timeout' in prompt, prompt
        assert 'sh pytest' in prompt, prompt
        assert d._SUMMARIES['k'] == 'fixing the silent upload retry'
    finally:
        d.urllib.request.urlopen = real
        d.SUMMARY_MODEL = ''
        d._SUMMARIES.clear()
        d._ASKED.clear()


def test_the_page_refreshes_without_reloading_itself():
    """A whole-page reload repaints the tab icon, and Windows flashes the
    taskbar button with it - every ten seconds, all day."""
    page = d.render([])
    assert 'http-equiv="refresh"' not in page, \
        'a meta refresh reloads the page, which flashes the taskbar icon'
    assert 'document.body.innerHTML' in page, 'nothing refreshes the board'
    assert f'{d.REFRESH_SECONDS * 1000}' in page, 'the refresh has no interval'
    # the board shuts down when the page stops asking, so the timer a hidden
    # window is throttled down to must still be comfortably inside the window
    assert d.IDLE_EXIT_SECONDS > 60, \
        'a minimised window would be mistaken for a closed one'


def test_a_session_opened_only_to_read_is_left_off_the_board():
    """Clicking into an old chat starts a real process for it, so it reads as
    live. What gives it away is that its last log entry predates the process
    now holding it open."""
    opened = '2026-09-13T23:39:09.453000+00:00'
    assert not d.worked_since_opening(opened, '2026-09-04T22:39:30.694Z')
    # known-positive: the same rule must keep a session that did work, or the
    # check above would pass just as well with the board hiding everything
    assert d.worked_since_opening(opened, '2026-09-13T23:45:53.773Z')
    # seconds count - a new session writing its first line must not be hidden
    assert d.worked_since_opening(opened, '2026-09-13T23:39:29.000Z')
    # a timestamp that will not parse is never proof of anything
    assert d.worked_since_opening(opened, None)
    assert d.worked_since_opening(None, '2026-09-04T22:39:30.694Z')
    assert d.worked_since_opening(opened, 'not a date')

    # and the rule has to actually reach the rows the board renders
    started = datetime.now(timezone.utc) - timedelta(hours=1)
    def said(when):
        return json.dumps({'type': 'assistant', 'timestamp': when.isoformat(),
                           'message': {'content': [{'type': 'text',
                                                    'text': 'all done'}]}})

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / 'dormant.jsonl').write_text(
            said(started - timedelta(days=9)), encoding='utf-8')
        (Path(tmp) / 'busy.jsonl').write_text(
            said(started + timedelta(minutes=5)), encoding='utf-8')

        real_live, real_path = d.live_sessions, d.transcript_path
        try:
            d.live_sessions = lambda: [
                {'sessionId': sid, 'cwd': str(tmp), 'pid': 1,
                 'startedAt': started.timestamp() * 1000}
                for sid in ('dormant', 'busy')]
            d.transcript_path = lambda cwd, sid: Path(tmp) / f'{sid}.jsonl'
            rows = d.claude_rows()
        finally:
            d.live_sessions, d.transcript_path = real_live, real_path

    assert [r['sid'] for r in rows] == ['busy'], rows


def test_the_summary_line_says_it_is_waiting_instead_of_going_blank():
    """A blank line while the model loads reads as a broken feature."""
    real_model, real_down = d.SUMMARY_MODEL, d._DOWN_UNTIL
    d.SUMMARY_MODEL, d._DOWN_UNTIL = 'test-model', 0.0
    key = d.summary_key('q', 'said something', ['Read x'])
    try:
        # in flight, nothing answered yet: the wait worth naming is the model
        d._ASKED.add(key)
        assert d.summarise('q', 'said something', ['Read x']) == d.LOADING_MODEL

        # once one answer is in, the model is loaded and waits are short
        d._SUMMARIES['someone else'] = 'fixing the upload retry'
        assert d.summarise('q', 'said something', ['Read x']) == d.SUMMARISING

        # known-positive: a real answer must still come back as itself, or the
        # assertions above would pass with every card stuck on a placeholder
        d._SUMMARIES[key] = 'fixing the upload retry'
        assert d.summarise('q', 'said something',
                           ['Read x']) == 'fixing the upload retry'

        # Ollama down is not "loading" - the footer explains that one, and a
        # card promising a line that is never coming would be a lie
        d._SUMMARIES.pop(key)
        d._DOWN_UNTIL = time.time() + 60
        assert d.summarise('q', 'said something', ['Read x']) == ''
    finally:
        d.SUMMARY_MODEL, d._DOWN_UNTIL = real_model, real_down
        d._ASKED.clear()
        d._SUMMARIES.clear()

    # and the page has to tell the two apart, or a placeholder reads as prose
    row = {'state': 'done', 'since': None, 'detail': '', 'says': '',
           'trail': [], 'warnings': [], 'summary': d.LOADING_MODEL,
           'folder': 'repo', 'branch': 'main', 'dirty': 0, 'committed': None,
           'agent': 'claude', 'app': 'desktop', 'model': '', 'sid': 'a',
           'cwd': r'C:/repo', 'title': 't', 'started': None, 'is_main': True,
           'pulse': False}
    page = d.render([('repo', [row])])
    assert 'says waiting' in page, 'the placeholder is styled as a summary'
    assert '.says.waiting' in page, 'nothing makes the placeholder look apart'
    assert 'says waiting' not in d.render(
        [('repo', [dict(row, summary='fixing the upload retry')])])


def _load_guard():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'shared_guard', ROOT / 'hooks' / 'block-shared-worktree.py')
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    return guard


def test_the_edit_guard_blocks_only_whoever_arrived_second():
    guard = _load_guard()
    live = r'c:\repo'
    wt = r'c:\repo\.claude\worktrees\foo'
    top_of = str.lower  # each session's cwd is its own folder here
    t9, t10, t11 = ('2026-09-24T09:00:00+00:00', '2026-09-24T10:00:00+00:00',
                    '2026-09-24T11:00:00Z')

    def n(top, mine, others):
        return len(guard.blockers(top, mine, others, top_of=top_of))

    assert n(live, t10, [{'cwd': live, 'started': t9}]) == 1, 'they were first'
    assert n(live, t10, [{'cwd': live, 'started': t11}]) == 0, 'I was first'
    # a worktree lives inside the checkout's path but is a different folder
    assert n(live, t10, [{'cwd': wt, 'started': t9}]) == 0
    assert n(wt, t10, [{'cwd': live, 'started': t9}]) == 0
    # unknown on either side: the newcomer yields
    assert n(live, t10, [{'cwd': live, 'started': None}]) == 1
    assert n(live, None, [{'cwd': live, 'started': t11}]) == 1
    # Claude writes +00:00 and Codex writes Z; one second apart must still sort
    assert n(live, '2026-09-24T10:00:00Z',
             [{'cwd': live, 'started': '2026-09-24T09:59:59+00:00'}]) == 1
    assert n(live, t10, []) == 0


def test_the_edit_guard_runs_and_stays_out_of_the_way():
    """Separate process, like the brief: an import that only works from the
    repo root would otherwise fail open silently and guard nothing."""
    hook = ROOT / 'hooks' / 'block-shared-worktree.py'
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(['git', 'init', '-q'], cwd=tmp, capture_output=True)
        for stdin in (json.dumps({'session_id': 'nobody', 'tool_input':
                                  {'file_path': os.path.join(tmp, 'x.py')}}),
                      '', 'not json {{{'):
            out = subprocess.run([sys.executable, str(hook)], input=stdin,
                                 capture_output=True, text=True, timeout=60,
                                 cwd=tempfile.gettempdir())
            assert out.returncode == 0, (stdin, out.stderr)


def test_the_edit_guard_actually_blocks_the_session_it_is_given():
    """Companion to the check above, which only proves the guard can stay
    quiet; a guard wired to nothing would pass it forever."""
    guard = _load_guard()
    real = d.session_rows, d.live_sessions, sys.stdin, sys.stderr
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(['git', 'init', '-q'], cwd=tmp, capture_output=True)
        try:
            d.live_sessions = lambda: [{'sessionId': 'me', 'pid': 1,
                                        'startedAt': 1790000000000}]
            d.session_rows = lambda: [
                {'sid': 'me', 'pid': 1, 'cwd': tmp, 'started': None},
                {'sid': 'other', 'pid': 2, 'cwd': tmp, 'agent': 'codex',
                 'title': 'first one here', 'started': '2026-01-01T00:00:00Z'}]
            sys.stdin = io.StringIO(json.dumps({'session_id': 'me', 'tool_input':
                                    {'file_path': os.path.join(tmp, 'x.py')}}))
            sys.stderr = io.StringIO()
            code, msg = guard.main(), sys.stderr.getvalue()
        finally:
            d.session_rows, d.live_sessions, sys.stdin, sys.stderr = real
    assert code == 2, msg
    assert 'codex session "first one here"' in msg, msg


def test_the_edit_guard_ignores_the_apps_helper_copy():
    """A helper fork shares its parent's hostSessionId and edits nothing, so
    it must not hold the folder against anyone - here, against the very
    session it was copied from, restarted after it. A genuine neighbour that
    got there first still blocks."""
    guard = _load_guard()
    real = (d.live_sessions, d.local_ids_by_cli_session, sys.stdin, sys.stderr)
    real_live = d.live_sessions
    with tempfile.TemporaryDirectory() as tmp, \
            tempfile.TemporaryDirectory() as regdir:
        subprocess.run(['git', 'init', '-q'], cwd=tmp, capture_output=True)
        reg = Path(regdir)

        def entry(pid, sid, host, started):
            (reg / f'{pid}.json').write_text(json.dumps(
                {'pid': pid, 'sessionId': sid, 'cwd': tmp,
                 'hostSessionId': host, 'startedAt': started}), encoding='utf-8')

        def run():
            sys.stdin = io.StringIO(json.dumps({'session_id': 'me', 'tool_input':
                                    {'file_path': os.path.join(tmp, 'x.py')}}))
            sys.stderr = io.StringIO()
            return guard.main(), sys.stderr.getvalue()

        try:
            d.live_sessions = lambda: real_live(registry=reg, alive=lambda e: True)
            d.local_ids_by_cli_session = lambda *a: {'me': 'local_me',
                                                     'other': 'local_other'}
            entry(1, 'me', 'local_me', 1790000005000)
            entry(2, 'helper', 'local_me', 1790000001000)
            helper_only = run()
            entry(3, 'other', 'local_other', 1790000000000)
            with_neighbour = run()
        finally:
            d.live_sessions, d.local_ids_by_cli_session, sys.stdin, sys.stderr = real
    assert helper_only[0] == 0, helper_only[1]
    assert with_neighbour[0] == 2, 'a real neighbour must still block'


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
    # the computer-use tool's JS wrapper is a second shell path
    wrapped = {'type': 'custom_tool_call', 'name': 'exec',
               'input': 'tools.exec_command({cmd: "curl -u ' + key + '"})'}
    assert 'Ab1Ab1' not in d.codex_call_label(wrapped, limit=80)
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


def test_summary_line_is_redacted_before_it_is_trimmed():
    """The summary is cut to 110 characters. A key that straddles the cut is
    left as a fragment too short to recognise, so redact must run first."""
    class Fake:
        def __init__(self, text):
            self.text = text

        def read(self):
            return json.dumps({'response': self.text}).encode('utf-8')

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    # 85 characters of prose, then a 36-character run: 25 of them fit under
    # the 110 cut, which is under the 32 a bare run needs to be recognised
    line = 'word ' * 17 + 'Ab1' * 12
    real, real_model = d.urllib.request.urlopen, d.SUMMARY_MODEL
    d.SUMMARY_MODEL = 'test-model'
    try:
        d.urllib.request.urlopen = lambda *a, **k: Fake(line)
        d._fetch_summary('k-straddle', 'ask', 'said', [])
        shown = d._SUMMARIES['k-straddle']
        assert 'Ab1Ab1' not in shown and '[redacted]' in shown, shown
    finally:
        d.urllib.request.urlopen = real
        d.SUMMARY_MODEL = real_model
        d._SUMMARIES.clear()
        d._ASKED.clear()
        d._DOWN_UNTIL = 0.0


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


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for t in tests:
        t()
        print('ok  ', t.__name__)
    print(f'\n{len(tests)} checks passed')
