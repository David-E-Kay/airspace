#!/usr/bin/env python3
"""Checks for the four bits of logic that could fail silently.

Run: python test_dashboard.py
"""
import io
import json
import os
import subprocess
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
    assert d.summarise('read the config file', ['Read a.py']) == ''

    d.SUMMARY_MODEL, d.OLLAMA_URL = 'test-model', 'http://127.0.0.1:9'
    try:
        d._SUMMARIES[d.summary_key('said', ['Read a.py'])] = 'reading the config'
        assert d.summarise('said', ['Read a.py']) == 'reading the config'
        # the turn moved on, so the cached answer must not be reused - and
        # asking must hand back an empty line rather than wait for one
        assert d.summarise('said', ['Read b.py']) == ''

        # a turn with nothing in it is not worth waking the model for
        d._ASKED.clear()
        assert d.summarise('   ', ['Read a.py']) == ''
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
        d._fetch_summary('k1', 'said', ['Read a.py'])
        assert d._SUMMARIES['k1'] == 'Reading x', d._SUMMARIES['k1']

        def dead(*a, **k):
            raise OSError('connection refused')

        d.urllib.request.urlopen = dead
        d._ASKED.add('k2')                       # as summarise() would have
        d._fetch_summary('k2', 'said', [])       # must not raise
        assert d._SUMMARIES['k2'] == '', d._SUMMARIES['k2']
        assert 'k2' not in d._ASKED, 'a failed ask was never released'
    finally:
        d.urllib.request.urlopen = real
        d.SUMMARY_MODEL = ''
        d._SUMMARIES.clear()
        d._ASKED.clear()


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
        d._fetch_summary('ka', 'said', [])
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

    def neighbour(cwd, sid='n1', branch='feature'):
        return {'sid': sid, 'agent': 'codex', 'cwd': cwd, 'warnings': [],
                'repo': 'r', 'label': 'app', 'branch': branch, 'dirty': 0,
                'is_main': True}

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
    finally:
        d.session_rows, d.git_info = real_rows, real_git


def test_the_session_start_hook_runs_and_reports_the_workspace():
    """The hook is a separate process reaching back into dashboard.py, so an
    import that only works from the repo root would break it silently."""
    hook = Path(__file__).resolve().parent / 'hooks' / 'git-workspace-brief.py'
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
    assert 'CLAUDE.md section 8' in ctx, ctx
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
        Path(__file__).resolve().parent / 'hooks' / 'git-workspace-brief.py')
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)

    real_clashes, real_stdin, real_out = d.clashes_for, sys.stdin, sys.stdout
    try:
        d.clashes_for = lambda cwd, sid='': [('same branch', 'a codex '
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
    def broken(cwd, sid=''):
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


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for t in tests:
        t()
        print('ok  ', t.__name__)
    print(f'\n{len(tests)} checks passed')
