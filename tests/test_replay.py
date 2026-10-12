"""The rules replayed on the real history before the manual cleanup of 2026-10-12 must only propose what the user
approved then (data/replay/after.json is the state after it). Local data only: skipped where it is absent."""
import dataclasses
import json
from datetime import date
from pathlib import Path

import pytest

from autopilot import config, engine

ROOT = Path(__file__).resolve().parents[1]
REPLAY = ROOT / 'data/replay'
pytestmark = pytest.mark.skipif(not (REPLAY / 'before.json').exists() or not (ROOT / 'rules.toml').exists(),
                                reason='personal replay data and rules.toml are local only')


def load(name: str) -> dict:
    return json.loads((REPLAY / name).read_text())


def test_every_proposed_write_matches_what_the_user_approved():
    before, after = load('before.json'), load('after.json')
    before['tag'], before['account'] = after['tag'], after['account']   # the category tree was built during the cleanup
    for t in before['transaction']:
        t['tag'] = None   # old categories look chosen by hand to the category rule; a fresh import has none
    rules = dataclasses.replace(config.load(ROOT / 'rules.toml'), lookback_days=400)
    p = engine.plan(engine.Book(before), rules, date(2026, 10, 12), set())
    final = {t['id']: t for t in after['transaction']}
    fields = ('deleted', 'tag', 'incomeAccount', 'outcomeAccount', 'income', 'outcome')
    wrong = [(a.rule, a.text) for a in p.actions for o in a.after
             if any((o.get(f) or None) != (final[o['id']].get(f) or None) for f in (('deleted',) if o.get('deleted') else fields))]
    assert len(p.actions) > 800 and not wrong


def test_clean_history_needs_no_writes():
    p = engine.plan(engine.Book(load('after.json')), config.load(ROOT / 'rules.toml'), date(2026, 10, 12), set())
    assert not p.actions
