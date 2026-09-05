#!/usr/bin/env python3
"""Checks for the four bits of logic that could fail silently.

Run: python test_dashboard.py
"""
import json
import os
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


def test_tail_reads_last_action_and_prompt():
    entries = [
        {'type': 'last-prompt', 'lastPrompt': 'fix the   timer bug'},
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
    assert got['prompt'] == 'fix the timer bug', got['prompt']
    assert got['last_ts'] == '2026-09-05T10:01:00.000Z', got['last_ts']


def test_state_is_waiting_when_the_turn_ended_on_text():
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
    assert got['state'] == 'waiting', got['state']
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
    assert d.short_tool('Bash', {'command': 'cd /c/repo && git status'}) == 'sh git'
    assert d.short_tool('Bash', {'command': 'grep -r foo .'}) == 'sh grep'
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

        alive = d.live_sessions(registry=reg, pids={111})

    assert [s['sessionId'] for s in alive] == ['aaa'], alive


def test_clashes_flag_shared_folder_and_shared_branch():
    def row(cwd, repo, branch):
        return {'cwd': cwd, 'repo': repo, 'branch': branch, 'warnings': []}

    same_folder = [row(r'C:\repo', 'R', 'main'), row(r'C:\REPO', 'R', 'main')]
    d.flag_clashes(same_folder)
    assert [r['warnings'] for r in same_folder] == \
        [['same folder as another session']] * 2, same_folder

    same_branch = [row(r'C:\repo', 'R', 'feat'), row(r'C:\repo-wt', 'R', 'feat')]
    d.flag_clashes(same_branch)
    assert all(r['warnings'] == ['same branch as another session']
               for r in same_branch), same_branch

    unrelated = [row(r'C:\a', 'R', 'main'), row(r'C:\b', 'R', 'other')]
    d.flag_clashes(unrelated)
    assert all(not r['warnings'] for r in unrelated), unrelated


def test_render_survives_a_broken_collect(monkey=None):
    """The page must say what went wrong rather than showing nothing."""
    page = d.render([], error='BoomError: git vanished')
    assert 'BoomError' in page and '<html' in page


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for t in tests:
        t()
        print('ok  ', t.__name__)
    print(f'\n{len(tests)} checks passed')
