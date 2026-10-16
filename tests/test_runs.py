"""Runs against a fake Zenmoney server: writes, the check after them, an interrupted run, undo."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from autopilot.__main__ import App, RunError

TZ = ZoneInfo('Europe/Moscow')
RULES = '''
[[transfer]]
name = "roundups"
to_account = "Savings"
[transfer.match]
account = "Card"
kind = "expense"
no_payee = true
below = 50
'''


class FakeZenmoney:
    """Keeps objects like the server: recomputes balances and never undeletes a transaction."""

    def __init__(self, transactions):
        self.stamp = 1
        self.fail_writes = False
        self.objects = {
            'instrument': {'2': {'id': 2, 'rate': 1}},
            'tag': {},
            'account': {i: {'id': i, 'title': t, 'instrument': 2, 'startBalance': s, 'balance': 0}
                        for i, t, s in (('card', 'Card', 1000), ('savings', 'Savings', 500))},
            'transaction': {t['id']: t for t in transactions},
        }
        self.recompute()

    def recompute(self):
        for a in self.objects['account'].values():
            a['balance'] = a['startBalance'] + sum(
                (t['income'] if t['incomeAccount'] == a['id'] else 0) - (t['outcome'] if t['outcomeAccount'] == a['id'] else 0)
                for t in self.objects['transaction'].values() if not t['deleted'])

    def diff(self, server_timestamp=0):
        self.stamp += 1
        return {'serverTimestamp': self.stamp, **{k: [dict(o) for o in v.values()] for k, v in self.objects.items()}}

    def write(self, server_timestamp, **objects):
        if self.fail_writes:
            raise ConnectionError('network is down')
        for kind, items in objects.items():
            for o in items:
                old = self.objects[kind].get(str(o['id']))
                self.objects[kind][str(o['id'])] = dict(o, deleted=True) if old and old.get('deleted') else dict(o)
        self.recompute()
        return self.diff()


def tx(id_, day, outcome, payee=None):
    return {'id': id_, 'date': day, 'income': 0, 'outcome': outcome, 'incomeAccount': 'card', 'outcomeAccount': 'card',
            'incomeInstrument': 2, 'outcomeInstrument': 2, 'payee': payee, 'originalPayee': payee, 'comment': None,
            'tag': None, 'deleted': False, 'hold': False, 'source': 'plugin', 'opOutcome': 0, 'changed': 1}


@pytest.fixture
def server():
    day = datetime.now(TZ).date() - timedelta(days=5)
    return FakeZenmoney([tx('buy', day.isoformat(), 463, 'Cafe'), tx('roundup', day.isoformat(), 37)])


@pytest.fixture
def app(tmp_path, server):
    (tmp_path / 'rules.toml').write_text(RULES)
    return App(server, tmp_path / 'rules.toml', tmp_path / 'data', TZ)


def balances(server):
    return {i: a['balance'] for i, a in server.objects['account'].items()}


def test_run_adds_the_roundup_to_the_savings_and_keeps_the_card(app, server):
    before = balances(server)
    app.cmd_run()
    assert server.objects['transaction']['roundup']['incomeAccount'] == 'savings'
    assert balances(server) == {'card': before['card'], 'savings': before['savings'] + 37}


def test_failed_write_is_retried_by_the_next_run(app, server):
    server.fail_writes = True
    with pytest.raises(ConnectionError):
        app.cmd_run()
    server.fail_writes = False
    app.cmd_run()
    assert server.objects['transaction']['roundup']['incomeAccount'] == 'savings'


def test_undo_puts_the_run_back_once(app, server):
    before = {k: dict(v) for k, v in server.objects['transaction'].items()}
    app.cmd_run()
    [journal] = (app.data / 'journal').glob('*.json')
    app.cmd_undo(journal)
    assert server.objects['transaction']['roundup']['incomeAccount'] == before['roundup']['incomeAccount']
    assert server.objects['account']['savings']['balance'] == 500
    with pytest.raises(RunError):
        app.cmd_undo(journal)


def test_undone_transaction_is_not_converted_again(app, server):
    app.cmd_run()
    [journal] = (app.data / 'journal').glob('*.json')
    app.cmd_undo(journal)
    app.cmd_run()
    assert server.objects['transaction']['roundup']['incomeAccount'] == 'card'


def test_lost_state_stops_writing(app, server):
    app.cmd_run()
    (app.data / 'state.json').unlink()
    with pytest.raises(RunError, match='state.json'):
        app.cmd_run()
