#!/usr/bin/env python3
"""Checks for the four bits of logic that could fail silently.

Run: python test_dashboard.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

import dashboard as d


def test_slug_matches_claude_layout():
    """The slug rule must match the directory names Claude actually creates."""
    assert d.slug_for(r'C:\Users\david\repos\Multi Agent Dashboard') == \
        'C--Users-david-repos-Multi-Agent-Dashboard'
    assert d.slug_for(r'C:\Users\david\.claude') == 'C--Users-david--claude'
    # every non-alphanumeric becomes a dash, underscores and dots included
    assert d.slug_for(r'C:\a_b.c-d') == 'C--a-b-c-d'

    # known-positive: the derived slug exists on this machine
    real = Path.home() / '.claude' / 'projects' / d.slug_for(Path.home() / '.claude')
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
    assert got['detail'] == 'All done.', got['detail']
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


def test_clashes_flag_shared_folder_and_shared_branch():
    def row(cwd, repo, branch):
        return {'cwd': cwd, 'repo': repo, 'branch': branch, 'warnings': []}

    same_folder = [row(r'C:\repo', 'R', 'main'), row(r'C:\REPO', 'R', 'main')]
    d.flag_clashes(same_folder)
    assert all(len(r['warnings']) == 1 and 'same folder' in r['warnings'][0]
               for r in same_folder), same_folder

    same_branch = [row(r'C:\repo', 'R', 'feat'), row(r'C:\repo-wt', 'R', 'feat')]
    d.flag_clashes(same_branch)
    assert all(len(r['warnings']) == 1 and 'on this branch' in r['warnings'][0]
               for r in same_branch), same_branch

    # the warning names the other agent, so you know which app to go look in
    mixed = [dict(row(r'C:\repo', 'R', 'main'), agent='claude'),
             dict(row(r'C:\repo', 'R', 'main'), agent='codex')]
    d.flag_clashes(mixed)
    assert 'codex' in mixed[0]['warnings'][0], mixed[0]
    assert 'claude' in mixed[1]['warnings'][0], mixed[1]

    unrelated = [row(r'C:\a', 'R', 'main'), row(r'C:\b', 'R', 'other')]
    d.flag_clashes(unrelated)
    assert all(not r['warnings'] for r in unrelated), unrelated


def test_render_survives_a_broken_collect(monkey=None):
    """The page must say what went wrong rather than showing nothing."""
    page = d.render([], error='BoomError: git vanished')
    assert 'BoomError' in page and '<html' in page


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


def test_a_card_only_glows_after_the_state_actually_changes():
    """A brand new session must not glow - on a board restart every card
    would light up at once, which is noise rather than news."""
    d._PREV_STATE.clear()
    assert not d.note_change('s1', 'working', now=100.0), 'first sight glowed'
    assert not d.note_change('s1', 'working', now=101.0), 'no change glowed'
    assert d.note_change('s1', 'asking', now=102.0), 'the change did not glow'
    assert d.note_change('s1', 'asking', now=110.0), 'glow ended too early'
    assert not d.note_change('s1', 'asking', now=102.0 + d.PULSE_SECONDS + 1), \
        'glow never ended'
    # a second session is tracked on its own
    assert not d.note_change('s2', 'asking', now=200.0)
    d._PREV_STATE.clear()


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
    assert got['detail'] == 'Nothing found.', got['detail']
    assert got['says'] == '', got['says']
    assert got['trail'] == ['run git status', 'search codex session docs'], \
        got['trail']
    assert got['since'] == '2026-09-06T10:00:09Z', got['since']

    assert d.read_codex_activity([])['state'] == 'idle'


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


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for t in tests:
        t()
        print('ok  ', t.__name__)
    print(f'\n{len(tests)} checks passed')
