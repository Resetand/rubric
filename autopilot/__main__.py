"""autopilot: applies the rules from rules.toml to new Zenmoney transactions.

    python -m autopilot plan            what would be written now; writes nothing to Zenmoney
    python -m autopilot run             apply once and report
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
    def __init__(self):
        _load_dotenv(Path('.env'))
        token = os.environ.get('ZENMONEY_TOKEN')
        if not token:
            raise RunError('ZENMONEY_TOKEN is not set')
        self.zen = Zenmoney(token)
        self.rules_path = Path(os.environ.get('AUTOPILOT_RULES', 'rules.toml'))
        self.data = Path(os.environ.get('AUTOPILOT_DATA', 'data'))
        self.tz = ZoneInfo(os.environ.get('AUTOPILOT_TIMEZONE', 'Europe/Moscow'))
        (self.data / 'journal').mkdir(parents=True, exist_ok=True)
        self.state_path = self.data / 'state.json'
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {'handled': {}}

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
        today = datetime.now(self.tz).date()
        p = engine.plan(book, rules, today, set(self.state['handled']))
        writes = sum(len(a.after) for a in p.actions)
        if writes > rules.max_writes_per_run:
            raise RunError(f'{writes} writes planned, the limit is {rules.max_writes_per_run}: nothing written. '
                           'Check `python -m autopilot plan`; if it is right, raise max_writes_per_run for one run.')
        return book, p

    def apply(self, mirror: Mirror, book: engine.Book, p: engine.Plan) -> list[str]:
        """Write the plan, then read it back. Returns problems found by the check (empty: all as planned)."""
        balances = {a: book.accounts[a]['balance'] for a in book.accounts}
        stamp = datetime.now(self.tz).strftime('%Y%m%d-%H%M%S')
        journal = self.data / 'journal' / f'{stamp}.json'
        journal.write_text(json.dumps({'actions': [vars(a) for a in p.actions]}, ensure_ascii=False, indent=1))

        objects = [o for a in p.actions for o in a.after]
        mirror.apply(self.zen.write(mirror.server_timestamp, transaction=objects))
        deltas = defaultdict(float)
        for a in p.actions:
            for account, delta in a.start_balance_deltas.items():
                deltas[account] += delta
        if deltas:
            accounts = mirror.objects['account']
            fixed = [dict(accounts[a], startBalance=round(accounts[a]['startBalance'] + d, 2),
                          balance=round(accounts[a]['balance'] + d, 2)) for a, d in deltas.items()]
            mirror.apply(self.zen.write(mirror.server_timestamp, account=fixed))
        mirror.save()

        problems = []
        live = mirror.objects['transaction']
        for o in objects:
            got = live.get(o['id'], {})
            problems += [f"{o['id']}: {f} is {got.get(f)!r}, expected {o.get(f)!r}" for f in VERIFY_FIELDS if got.get(f) != o.get(f)]
        touched = {t[k] for a in p.actions for t in a.before for k in ('incomeAccount', 'outcomeAccount')}
        for account in touched:
            now = mirror.objects['account'][account]['balance']
            if abs(now - balances[account]) >= engine.CENT:
                problems.append(f'balance of «{book.title(account)}» moved {balances[account]:,.2f} → {now:,.2f}')
        self.remember(p.handled())
        return problems

    def remember(self, handled: dict[str, str]):
        today = datetime.now(self.tz).date().isoformat()
        self.state['handled'].update({i: f'{rule} {today}' for i, rule in handled.items()})
        self.save_state()

    def save_state(self):
        tmp = self.state_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1))
        os.replace(tmp, self.state_path)

    def log(self, text: str):
        print(f"[{datetime.now(self.tz):%Y-%m-%d %H:%M}] {text}", flush=True)

    # --- commands --------------------------------------------------------------------------------------------------

    def cmd_plan(self):
        _, p = self.plan(self.sync())
        print(report(p) or 'nothing to do')

    def cmd_run(self):
        mirror = self.sync()
        book, p = self.plan(mirror)
        if not p.actions:
            self.remember(p.handled())
            if p.notices:
                self.log(report(p))
            return
        problems = self.apply(mirror, book, p)
        text = report(p)
        if problems:
            text += '\n\n⚠️ После записи не сошлось — проверь:\n' + '\n'.join(problems[:10])
        self.log(text)

    def cmd_loop(self, minutes: int):
        while True:
            try:
                self.cmd_run()
            except Exception as error:   # noqa: BLE001 — a long-running loop logs and goes on
                self.log(f'ошибка, после нее ничего не записано: {type(error).__name__}: {error}')
                traceback.print_exc()
            time.sleep(minutes * 60)

    def cmd_undo(self, path: Path):
        """Updated transactions get their old content back. Deleted ones cannot be undeleted in Zenmoney, so they come
        back as copies with new ids. Every id involved stays handled: no rule redoes it."""
        actions = json.loads(path.read_text())['actions']
        mirror = self.sync()
        live = mirror.objects['transaction']
        restore, created = [], []
        for a in actions:
            for old in a['before']:
                if live.get(old['id'], {}).get('deleted'):
                    copy = dict(old, id=str(uuid.uuid4()), created=int(time.time()))
                    created.append(copy)
                    restore.append(copy)
                else:
                    restore.append(old)
        mirror.apply(self.zen.write(mirror.server_timestamp, transaction=restore))
        deltas = defaultdict(float)
        for a in actions:
            for account, delta in a['start_balance_deltas'].items():
                deltas[account] -= delta
        if deltas:
            accounts = mirror.objects['account']
            mirror.apply(self.zen.write(mirror.server_timestamp, account=[
                dict(accounts[a], startBalance=round(accounts[a]['startBalance'] + d, 2), balance=round(accounts[a]['balance'] + d, 2))
                for a, d in deltas.items()]))
        mirror.save()
        self.remember({t['id']: 'undo' for t in created})
        print(f'restored {len(restore) - len(created)} transactions, re-created {len(created)} deleted ones as copies')


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


def _load_dotenv(path: Path):
    if path.exists():
        for line in path.read_text().splitlines():
            key, sep, value = line.partition('=')
            if sep and not line.lstrip().startswith('#'):
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main(argv: list[str]):
    os.umask(0o077)   # the mirror and the journal are the whole financial history: owner only
    command = argv[0] if argv else 'plan'
    app = App()
    lock = open(app.data / '.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit('another autopilot run holds data/.lock')
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
    except (RunError, config.ConfigError) as error:
        sys.exit(f'autopilot: {error}')
