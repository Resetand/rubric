"""autopilot: applies the rules from rules.toml to new Zenmoney transactions.

    python -m autopilot plan            what would be written now; writes nothing to Zenmoney
    python -m autopilot run             apply once
    python -m autopilot loop [MINUTES]  apply every MINUTES (default 30); for the container
    python -m autopilot undo JOURNAL    put back what one run changed (data/journal/<run>.json)

Settings come from the environment (or .env in the current directory): ZENMONEY_TOKEN, AUTOPILOT_RULES (rules.toml),
AUTOPILOT_DATA (data), AUTOPILOT_TIMEZONE (Europe/Moscow). Reports go to stdout (`make logs`).
"""
import fcntl
import json
import os
import sys
import time
import traceback
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, engine
from .mirror import Mirror
from .zenmoney import Zenmoney

VERIFY_FIELDS = ('deleted', 'tag', 'income', 'outcome', 'incomeAccount', 'outcomeAccount', 'incomeInstrument')


class RunError(RuntimeError):
    pass


class App:
    def __init__(self, zen: Zenmoney, rules_path: Path, data: Path, tz: ZoneInfo):
        self.zen, self.rules_path, self.data, self.tz = zen, rules_path, data, tz
        self.journal_dir = data / 'journal'
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = data / 'state.json'
        self.state: dict = {}

    @classmethod
    def from_env(cls) -> 'App':
        _load_dotenv(Path('.env'))
        token = os.environ.get('ZENMONEY_TOKEN')
        if not token:
            raise RunError('ZENMONEY_TOKEN is not set')
        return cls(Zenmoney(token), Path(os.environ.get('AUTOPILOT_RULES', 'rules.toml')),
                   Path(os.environ.get('AUTOPILOT_DATA', 'data')), ZoneInfo(os.environ.get('AUTOPILOT_TIMEZONE', 'Europe/Moscow')))

    # --- state ---------------------------------------------------------------------------------------------------
    # state.json: {"handled": {id: "rule date"}, "pending": {...} while a start balance compensation is in flight}

    @contextmanager
    def locked(self):
        """One run at a time, and the state read fresh under the lock: another process (undo) may have changed it."""
        with open(self.data / '.lock', 'w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RunError('another autopilot run holds data/.lock') from None
            self.state = self.load_state()
            yield

    def load_state(self) -> dict:
        if not self.state_path.exists():
            if any(self.journal_dir.iterdir()):
                raise RunError('data/state.json is missing but journals exist: without it the rules would redo the last '
                               'lookback_days over manual edits. Restore it from a backup; nothing written.')
            return {'handled': {}}
        try:
            return json.loads(self.state_path.read_text())
        except ValueError as error:
            raise RunError(f'data/state.json is broken ({error}); nothing written') from None

    def save_state(self):
        tmp = self.state_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1))
        os.replace(tmp, self.state_path)

    def remember(self, handled: dict[str, str]):
        today = datetime.now(self.tz).date().isoformat()
        self.state['handled'].update({i: f'{rule} {today}' for i, rule in handled.items()})
        self.save_state()

    # --- steps ---------------------------------------------------------------------------------------------------

    def sync(self) -> Mirror:
        mirror = Mirror.load(self.data / 'mirror.json')
        full = mirror.needs_full(time.time())
        mirror.apply(self.zen.diff(0 if full else mirror.server_timestamp), full=full)
        mirror.save()
        return mirror

    def plan(self, mirror: Mirror) -> tuple[engine.Book, engine.Plan]:
        rules = config.load(self.rules_path)
        book = engine.Book(mirror.snapshot())
        p = engine.plan(book, rules, datetime.now(self.tz).date(), set(self.state['handled']))
        writes = sum(len(a.after) for a in p.actions)
        if writes > rules.max_writes_per_run:
            raise RunError(f'{writes} writes planned, the limit is {rules.max_writes_per_run}: nothing written. '
                           'Check `make plan`; if it is right, raise max_writes_per_run for one run.')
        return book, p

    def finish_pending(self, mirror: Mirror):
        """Complete a start balance compensation interrupted between the two writes of a run.

        Transactions and accounts go in two requests. If the run broke after the first, the converted transactions
        are no longer candidates for any rule, so the compensation must be finished here or it is lost for good.
        """
        pending = self.state.get('pending')
        if not pending:
            return
        live, accounts = mirror.objects['transaction'], mirror.objects['account']
        landed = [live.get(i, {}).get('incomeAccount') == want['incomeAccount']
                  and live.get(i, {}).get('outcomeAccount') == want['outcomeAccount']
                  for i, want in pending['transactions'].items()]
        fixes = []
        for account, change in pending['accounts'].items():
            now = accounts[account]['startBalance']
            if abs(now - change['to']) < engine.CENT:
                continue
            if abs(now - change['from']) >= engine.CENT or (any(landed) and not all(landed)):
                raise RunError(f'start balance of «{accounts[account]["title"]}» is {now}, expected {change["from"]} or '
                               f'{change["to"]} (journal {pending["journal"]}): check it by hand, then remove "pending" '
                               'from data/state.json. Nothing written.')
            if all(landed):
                delta = change['to'] - change['from']
                fixes.append(dict(accounts[account], startBalance=change['to'], balance=round(accounts[account]['balance'] + delta, 2)))
        if fixes:
            mirror.apply(self.zen.write(mirror.server_timestamp, account=fixes))
            mirror.save()
            self.log(f'finished an interrupted balance compensation from {pending["journal"]}')
        del self.state['pending']
        self.save_state()

    def apply(self, mirror: Mirror, book: engine.Book, p: engine.Plan) -> list[str]:
        """Write the plan, then read it back. Returns problems found by the check (empty: all as planned)."""
        balances = {a: book.accounts[a]['balance'] for a in book.accounts}
        journal = self.journal_dir / f'{datetime.now(self.tz):%Y%m%d-%H%M%S}.json'
        record = {'status': 'writing', 'actions': [vars(a) for a in p.actions]}
        _write_json(journal, record)

        deltas = defaultdict(float)
        for a in p.actions:
            for account, delta in a.start_balance_deltas.items():
                deltas[account] += delta
        accounts = mirror.objects['account']
        if deltas:
            self.state['pending'] = {
                'journal': journal.name,
                'accounts': {a: {'from': accounts[a]['startBalance'], 'to': round(accounts[a]['startBalance'] + d, 2)}
                             for a, d in deltas.items()},
                'transactions': {o['id']: {'incomeAccount': o['incomeAccount'], 'outcomeAccount': o['outcomeAccount']}
                                 for a in p.actions if a.start_balance_deltas for o in a.after}}
            self.save_state()

        objects = [o for a in p.actions for o in a.after]
        mirror.apply(self.zen.write(mirror.server_timestamp, transaction=objects))
        if deltas:
            accounts = mirror.objects['account']
            mirror.apply(self.zen.write(mirror.server_timestamp, account=[
                dict(accounts[a], startBalance=change['to'], balance=round(accounts[a]['balance'] + deltas[a], 2))
                for a, change in self.state['pending']['accounts'].items()]))
            del self.state['pending']
        mirror.save()
        self.remember(p.handled())
        _write_json(journal, dict(record, status='applied'))

        problems = []
        live = mirror.objects['transaction']
        for o in objects:
            got = live.get(o['id'], {})
            problems += [f"{o['id']}: {f} is {got.get(f)!r}, expected {o.get(f)!r}" for f in VERIFY_FIELDS if got.get(f) != o.get(f)]
        touched = {o[k] for a in p.actions for o in (*a.before, *a.after) for k in ('incomeAccount', 'outcomeAccount')}
        touched |= set(deltas)
        for account in touched:
            now = mirror.objects['account'][account]['balance']
            if abs(now - balances[account]) >= engine.CENT:
                problems.append(f'balance of «{book.title(account)}» moved {balances[account]:,.2f} → {now:,.2f}')
        return problems

    def log(self, text: str):
        print(f'[{datetime.now(self.tz):%Y-%m-%d %H:%M %z}] {text}', flush=True)

    # --- commands --------------------------------------------------------------------------------------------------

    def cmd_plan(self):
        with self.locked():
            _, p = self.plan(self.sync())
        print(report(p) or 'nothing to do')

    def cmd_run(self):
        with self.locked():
            mirror = self.sync()
            self.finish_pending(mirror)
            book, p = self.plan(mirror)
            if not p.actions:
                self.remember(p.handled())
                self.log(report(p) or 'nothing to do')
                return
            problems = self.apply(mirror, book, p)
        text = report(p)
        if problems:
            text += '\n⚠️ After the write something does not match, check it (make undo can put the run back):\n' + '\n'.join(problems[:10])
        self.log(text)

    def cmd_loop(self, minutes: int):
        self.log(f'started: a run every {minutes} minutes')
        while True:
            try:
                self.cmd_run()
            except Exception as error:   # noqa: BLE001 — a long-running loop logs and goes on
                self.log(f'error: {type(error).__name__}: {error}')
                traceback.print_exc()
            time.sleep(minutes * 60)

    def cmd_undo(self, path: Path):
        """Put back one applied run. Only transactions still exactly as the run left them are restored: a later edit
        by the user or the bank wins. Deleted ones cannot be undeleted in Zenmoney, so they come back as copies with
        new ids. Every id involved stays handled, so no rule redoes it."""
        record = json.loads(path.read_text())
        if record.get('status') != 'applied':
            raise RunError(f"{path.name} has status {record.get('status')!r}: only an applied run can be undone")
        with self.locked():
            mirror = self.sync()
            live = mirror.objects['transaction']
            restore, created, skipped = [], [], []
            for a in record['actions']:
                for old, new in zip(a['before'], a['after']):
                    current = live.get(new['id'], {})
                    if any(current.get(f) != new.get(f) for f in VERIFY_FIELDS):
                        skipped.append(new['id'])
                    elif new.get('deleted'):
                        copy = dict(old, id=str(uuid.uuid4()), created=int(time.time()))
                        created.append(copy)
                        restore.append(copy)
                    else:
                        restore.append(dict(old, changed=current.get('changed', 0)))
            if restore:
                mirror.apply(self.zen.write(mirror.server_timestamp, transaction=restore))
            deltas = defaultdict(float)
            for a in record['actions']:
                if not {o['id'] for o in a['after']} & set(skipped):
                    for account, delta in a['start_balance_deltas'].items():
                        deltas[account] -= delta
            if deltas:
                accounts = mirror.objects['account']
                mirror.apply(self.zen.write(mirror.server_timestamp, account=[
                    dict(accounts[a], startBalance=round(accounts[a]['startBalance'] + d, 2), balance=round(accounts[a]['balance'] + d, 2))
                    for a, d in deltas.items()]))
            mirror.save()
            self.remember({t['id']: 'undo' for t in created})
            _write_json(path, dict(record, status='undone'))
        print(f'restored {len(restore) - len(created)}, re-created {len(created)} deleted as copies, '
              f'skipped {len(skipped)} changed since the run: {skipped[:5]}')


def report(p: engine.Plan) -> str:
    lines = []
    by_rule = defaultdict(list)
    for a in p.actions:
        by_rule[a.rule].append(a)
    for rule, actions in by_rule.items():
        total = sum(engine.amount(a.before[0]) for a in actions)
        lines.append(f'{rule}: {len(actions)} на {total:,.2f}')
        lines += [f'  • {a.text}' for a in actions[:5]] + ([f'  • … еще {len(actions) - 5}'] if len(actions) > 5 else [])
    if p.notices:
        lines.append('Нужно решение:')
        lines += [f'  • {n.rule}: {n.text}' for n in p.notices]
    return '\n'.join(lines)


def _write_json(path: Path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1))
    os.replace(tmp, path)


def _load_dotenv(path: Path):
    if path.exists():
        for line in path.read_text().splitlines():
            key, sep, value = line.partition('=')
            if sep and not line.lstrip().startswith('#'):
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main(argv: list[str]):
    os.umask(0o077)   # the mirror and the journal are the whole financial history: owner only
    command = argv[0] if argv else 'plan'
    app = App.from_env()
    if command == 'plan':
        app.cmd_plan()
    elif command == 'run':
        app.cmd_run()
    elif command == 'loop':
        app.cmd_loop(int(argv[1]) if len(argv) > 1 else 30)
    elif command == 'undo' and len(argv) == 2:
        app.cmd_undo(Path(argv[1]))
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except (RunError, config.ConfigError, engine.PlanError) as error:
        sys.exit(f'autopilot: {error}')
