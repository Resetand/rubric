"""Behavior of the planner on small synthetic books. What is checked is what would be written to Zenmoney."""
import itertools
from datetime import date
from pathlib import Path

import pytest

from autopilot import config, engine

TODAY = date(2026, 10, 12)
RULES = '''
lookback_days = 30
settle_days = 1
ignore_accounts = ["Service"]

[[transfer]]
name = "roundups"
to_account = "Savings"
[transfer.match]
account = "Card"
kind = "expense"
no_payee = true
no_comment = true
below = 50
foreign = false
same_day_purchase = true

[[fx_transfer]]
name = "fx"
to_accounts = ["Abroad GEL"]
income_regex = "private transfers"
[fx_transfer.match]
account = "Card"
kind = "expense"
payee = ["ME"]

[refund]
name = "refunds"
max_days = 60
skip_payee_regex = '^[A-Z][a-z]+ [A-Z]\\.$'

[[category]]
name = "self payments"
settle_days = 3
bands = [{ below = 700, category = "Transport / Taxi" }, { category = "Food / Delivery" }]
[category.match]
account = "Card"
kind = "expense"
payee = ["To myself"]
below = 20000

[[notice]]
name = "big"
text = "check"
[notice.match]
payee = ["To myself"]
at_least = 20000
'''
ids = itertools.count()


@pytest.fixture
def rules(tmp_path: Path) -> config.Rules:
    path = tmp_path / 'rules.toml'
    path.write_text(RULES)
    return config.load(path)


def op(day: str, account='card', out=0.0, inc=0.0, payee=None, comment=None, **extra) -> dict:
    return {'id': f'op{next(ids)}', 'date': day, 'income': inc, 'outcome': out, 'incomeAccount': account,
            'outcomeAccount': account, 'incomeInstrument': extra.pop('instrument', 2), 'outcomeInstrument': 2,
            'payee': payee, 'originalPayee': payee, 'comment': comment, 'tag': None, 'deleted': False,
            'hold': False, 'source': 'plugin', 'opIncome': 0, 'opOutcome': 0, 'incomeBankID': None, **extra}


def book(*transactions) -> engine.Book:
    accounts = [('card', 'Card', 2), ('savings', 'Savings', 2), ('wallet', 'Wallet', 2), ('gel', 'Abroad GEL', 3),
                ('service', 'Service', 2)]
    return engine.Book({
        'serverTimestamp': 1,
        'account': [{'id': i, 'title': t, 'instrument': c, 'balance': 0, 'startBalance': 0} for i, t, c in accounts],
        'instrument': [{'id': 2, 'rate': 1}, {'id': 3, 'rate': 35}],
        'tag': [{'id': 'transport', 'title': 'Transport'}, {'id': 'taxi', 'title': 'Taxi', 'parent': 'transport'},
                {'id': 'food', 'title': 'Food'}, {'id': 'delivery', 'title': 'Delivery', 'parent': 'food'}],
        'transaction': list(transactions),
    })


def plan(rules, *transactions, handled=()) -> engine.Plan:
    return engine.plan(book(*transactions), rules, TODAY, set(handled))


def written(p: engine.Plan) -> dict[str, dict]:
    return {o['id']: o for a in p.actions for o in a.after}


def deleted(p: engine.Plan) -> set[str]:
    return {i for i, o in written(p).items() if o.get('deleted')}


# --- refunds ---------------------------------------------------------------------------------------------------------

def test_refund_deletes_the_payment_and_the_refund(rules):
    buy, back = op('2026-10-01', out=900, payee='Shop'), op('2026-10-03', inc=900, payee='Shop')
    assert deleted(plan(rules, buy, back)) == {buy['id'], back['id']}


def test_refund_takes_the_nearest_earlier_of_identical_payments(rules):
    old = op('2026-09-20', out=430, payee='Games')
    near = op('2026-10-05', out=430, payee='Games')
    back = op('2026-10-07', inc=430, payee='Games')
    later = op('2026-10-08', out=430, payee='Games')
    assert deleted(plan(rules, old, near, back, later)) == {near['id'], back['id']}


@pytest.mark.parametrize('buy, back', [
    (op('2026-10-05', out=900, payee='Shop'), op('2026-10-04', inc=900, payee='Shop')),          # refund before payment
    (op('2026-07-01', out=900, payee='Shop'), op('2026-10-05', inc=900, payee='Shop')),          # older than max_days
    (op('2026-10-01', out=900, payee='Shop'), op('2026-10-03', inc=400, payee='Shop')),          # partial
    (op('2026-10-01', out=900, payee='Shop'), op('2026-10-03', inc=900, payee='Other')),         # another merchant
    (op('2026-10-01', out=900, payee='Shop'), op('2026-10-03', 'wallet', inc=900, payee='Shop')),  # another account
    (op('2026-10-01', out=900, payee='Anna K.'), op('2026-10-03', inc=900, payee='Anna K.')),    # a person paid back
    (op('2026-10-01', out=900, payee='Shop', source='user'), op('2026-10-03', inc=900, payee='Shop')),  # manual payment
])
def test_refund_without_a_true_pair_is_left_alone(rules, buy, back):
    assert not plan(rules, buy, back).actions


def test_refund_finds_the_merchant_in_the_comment_by_prefix_shared_word_and_transliteration(rules):
    pairs = [(op('2026-10-01', out=150, payee='Scooter'), op('2026-10-01', inc=150, comment='SCOOTER_SBP')),
             (op('2026-10-02', out=99, payee='Shop Services'), op('2026-10-02', inc=99, comment='Services of Shop')),
             (op('2026-10-03', out=19, payee='Иви'), op('2026-10-03', inc=19, payee='ivi.ru'))]
    p = plan(rules, *[t for pair in pairs for t in pair])
    assert deleted(p) == {t['id'] for pair in pairs for t in pair}


def test_refund_waits_while_its_purchase_is_a_fresh_hold(rules):
    buy, back = op('2026-10-10', out=430, payee='Games', hold=True), op('2026-10-10', inc=430, payee='Games')
    p = plan(rules, buy, back)
    assert not p.actions and not p.notices


def test_refund_of_a_purchase_held_for_good_is_deleted(rules):
    buy, back = op('2026-10-01', out=430, payee='Games', hold=True), op('2026-10-02', inc=430, payee='Games')
    assert deleted(plan(rules, buy, back)) == {buy['id'], back['id']}


def test_refund_is_not_paired_by_a_word_many_payees_share(rules):
    others = [op('2026-09-20', out=100 + i, payee=f'Shop{i} Samara') for i in range(3)]
    buy, back = op('2026-10-01', out=900, payee='Pizza Samara'), op('2026-10-03', inc=900, payee='Sushi Samara')
    assert not plan(rules, *others, buy, back).actions


def test_cashback_of_the_same_amount_is_not_a_refund(rules):
    assert not plan(rules, op('2026-10-01', out=150, payee='Shop'), op('2026-10-03', inc=150, payee='Cashback')).actions


# --- roundups ----------------------------------------------------------------------------------------------------------

def test_roundup_becomes_a_transfer_that_adds_it_to_the_savings_and_keeps_the_card(rules):
    roundup = op('2026-10-10', out=37)
    p = plan(rules, op('2026-10-10', out=463, payee='Cafe'), roundup)
    [action] = p.actions
    assert written(p)[roundup['id']]['incomeAccount'] == 'savings'
    assert engine.balance_deltas(action) == pytest.approx({'card': 0, 'savings': 37})


@pytest.mark.parametrize('roundup', [
    op('2026-10-10', out=37, opOutcome=1),        # paid in another currency
    op('2026-10-10', out=50),                     # not below the limit
    op('2026-10-10', out=37, comment='fee'),
    op('2026-10-11', out=37),                     # no payment that day
])
def test_expense_that_is_not_a_roundup_is_left_alone(rules, roundup):
    assert not plan(rules, op('2026-10-10', out=463, payee='Cafe'), roundup).actions


# --- categories --------------------------------------------------------------------------------------------------------

def test_category_is_chosen_by_amount_band(rules):
    cheap, dear = op('2026-10-05', out=699, payee='To myself'), op('2026-10-05', out=700, payee='To myself')
    w = written(plan(rules, cheap, dear))
    assert (w[cheap['id']]['tag'], w[dear['id']]['tag']) == (['taxi'], ['delivery'])


def test_expense_with_a_matching_income_on_another_own_account_is_an_unmerged_transfer(rules):
    out = op('2026-10-05', out=500, payee='To myself')
    assert not plan(rules, out, op('2026-10-06', 'wallet', inc=500, payee='To myself')).actions


def test_right_category_is_remembered_without_a_write(rules):
    t = op('2026-10-05', out=300, payee='To myself', tag=['taxi'])
    p = plan(rules, t)
    assert not p.actions and t['id'] in p.handled()


# --- fx transfers ------------------------------------------------------------------------------------------------------

def test_fx_transfer_merges_expense_with_the_foreign_income(rules):
    out, inc = op('2026-10-05', out=3500, payee='ME'), op('2026-10-06', 'gel', inc=101, payee='Private transfers', instrument=3)
    p = plan(rules, out, inc)
    assert written(p)[out['id']]['incomeAccount'] == 'gel' and deleted(p) == {inc['id']}


def test_fx_transfer_with_two_candidates_is_reported_not_merged(rules):
    a, b = op('2026-10-05', out=3500, payee='ME'), op('2026-10-05', out=3500, payee='ME')
    p = plan(rules, a, b, op('2026-10-06', 'gel', inc=100, payee='Private transfers', instrument=3))
    assert not p.actions and {i for n in p.notices for i in n.ids} == {a['id'], b['id']}


# --- what no rule may touch --------------------------------------------------------------------------------------------

@pytest.mark.parametrize('extra, day', [
    ({}, '2026-10-12'),                        # too fresh: settle_days
    ({}, '2026-09-01'),                        # older than lookback_days
    ({'hold': True}, '2026-10-10'),            # a hold younger than hold_days
    ({'source': 'user'}, '2026-10-05'),
])
def test_operation_outside_the_window_or_not_from_a_bank_is_not_touched(rules, extra, day):
    p = plan(rules, op(day, out=463, payee='Cafe'), op(day, out=37, **extra))
    assert not p.actions


def test_hold_older_than_hold_days_is_treated_as_posted(rules):
    roundup = op('2026-10-05', out=37, hold=True)
    p = plan(rules, op('2026-10-05', out=463, payee='Cafe', hold=True), roundup)
    assert written(p)[roundup['id']]['incomeAccount'] == 'savings'


def test_operation_handled_by_an_earlier_run_is_not_touched_again(rules):
    roundup = op('2026-10-10', out=37)
    assert not plan(rules, op('2026-10-10', out=463, payee='Cafe'), roundup, handled=[roundup['id']]).actions


def test_every_action_moves_balances_only_as_it_declares(rules):
    p = plan(rules,
             op('2026-10-10', out=463, payee='Cafe'), op('2026-10-10', out=37),
             op('2026-10-01', out=900, payee='Shop'), op('2026-10-03', inc=900, payee='Shop'),
             op('2026-10-05', out=3500, payee='ME'), op('2026-10-06', 'gel', inc=101, payee='Private transfers', instrument=3),
             op('2026-10-05', out=300, payee='To myself'))
    assert len(p.actions) == 4
    for action in p.actions:
        deltas = engine.balance_deltas(action)
        assert all(abs(deltas[a] - action.moves.get(a, 0)) < 0.005 for a in deltas), action.rule
    assert sum(sum(a.moves.values()) for a in p.actions) == pytest.approx(37)   # only the roundup reaches the savings


def test_large_self_transfer_is_reported_once(rules):
    t = op('2026-10-05', out=50000, payee='To myself')
    first = plan(rules, t)
    assert first.notices and not plan(rules, t, handled=first.handled()).notices


# --- configuration -----------------------------------------------------------------------------------------------------

def test_renamed_account_stops_the_run(rules, tmp_path):
    path = tmp_path / 'renamed.toml'
    path.write_text(RULES.replace('to_account = "Savings"', 'to_account = "Old savings"'))
    with pytest.raises(config.ConfigError, match='Old savings'):
        engine.plan(book(), config.load(path), TODAY, set())


def test_unknown_key_in_rules_is_an_error(tmp_path):
    path = tmp_path / 'typo.toml'
    path.write_text(RULES.replace('below = 50', 'bellow = 50'))
    with pytest.raises(config.ConfigError, match='bellow'):
        config.load(path)
