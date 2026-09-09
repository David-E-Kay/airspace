"""Write assets/demo.html: the real board, fed invented sessions.

    python assets/demo_board.py

Open the file it writes and screenshot that for the README, rather than a
real board - a real one is full of whatever you happen to be working on.
Re-run it whenever the page changes so the screenshot stops being a lie.
"""
import io
import os
import sys
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The invented rows below carry summary lines, so the footer has to name a
# model or the screenshot contradicts itself. Read at import time.
os.environ.setdefault('BOARD_SUMMARY_MODEL', 'qwen2.5:1.5b-instruct')
import dashboard as d  # noqa: E402

now = datetime.now(timezone.utc)


def ago(**kw):
    return (now - timedelta(**kw)).isoformat()


def row(**kw):
    base = {'agent': 'claude', 'state': 'working', 'title': '', 'folder': '',
            'is_main': True, 'branch': 'main', 'dirty': 0, 'warnings': [],
            'pulse': False, 'detail': '', 'says': '', 'summary': '',
            'trail': [], 'since': None, 'last_ts': None, 'app': 'desktop',
            'model': 'claude-opus-5', 'started': None, 'committed': None}
    base.update(kw)
    return base


checkout = row(
    state='asking', title='Checkout retry logic', folder='storefront',
    branch='fix-payment-retry', dirty=3, pulse=True,
    says='Retrying on a declined card would double-charge if the first '
         'attempt actually settled. Should I only retry network timeouts?',
    summary='asking whether to retry declined cards or only timeouts',
    trail=['Read payments.py', 'sh pytest'], since=ago(minutes=2),
    last_ts=ago(minutes=2), started=ago(hours=1, minutes=40),
    committed=ago(minutes=12))

migration = row(
    state='done', title='Postgres migration dry run', folder='storefront',
    branch='fix-payment-retry', dirty=3,
    warnings=[('same branch', 'a codex session in storefront-wt saves to this '
                              'branch too; the two sets of changes will mix')],
    says='Dry run finished clean. 14 tables, no data loss, 1.2s.',
    summary='dry run passed on all fourteen tables',
    trail=['sh psql', 'Read schema.sql'], since=ago(minutes=9),
    last_ts=ago(minutes=9), started=ago(hours=3, minutes=5),
    committed=ago(minutes=12))

codex = row(
    agent='codex', app='terminal', model='gpt-5-codex',
    state='working', title='Backfill script', folder='storefront-wt',
    branch='fix-payment-retry', is_main=False, dirty=1,
    warnings=[('same branch', 'a claude session in storefront saves to this '
                              'branch too; the two sets of changes will mix')],
    detail='running: python backfill.py --dry-run',
    says='Walking the orders table in batches of 5000.',
    trail=['sh python', 'sh git status'], since=ago(minutes=1),
    last_ts=ago(minutes=1), started=ago(minutes=48),
    committed=ago(hours=4, minutes=30))

infra = row(
    state='working', title='Nightly build times', folder='infra',
    branch='main', dirty=0, detail='running: ./gradlew build --scan',
    says='Kicking off a clean build to get a baseline.',
    summary='timing a clean build to get a baseline',
    trail=['sh gradlew', 'Read build.gradle'], since=ago(minutes=17),
    last_ts=ago(minutes=17), started=ago(hours=2, minutes=6),
    committed=ago(minutes=40))

docs = row(
    state='thinking', title='API reference rewrite', folder='docs-site',
    branch='rewrite-reference', dirty=7, model='claude-sonnet-5',
    detail='reading auth.md', says='Three pages still describe the old token '
                                   'flow.',
    summary='found three pages still on the old token flow',
    trail=['Grep token', 'Read auth.md'], since=ago(seconds=40),
    last_ts=ago(seconds=40), started=ago(hours=26),
    committed=ago(hours=5, minutes=20))

groups = [('storefront', [checkout, migration, codex]),
          ('infra', [infra]), ('docs-site', [docs])]
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'demo.html')
io.open(out, 'w', encoding='utf-8').write(d.render(groups))
print('wrote', out)
