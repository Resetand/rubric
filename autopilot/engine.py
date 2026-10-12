"""What the rules ask to write on the current Zenmoney data. Pure: no network, no clock, no files.

Safety rules that hold for every rule type:
- only transactions imported by a bank plugin, never manual ones, never holds (the bank still replaces them);
- a transaction is decided once: ids `handled` by previous runs are never looked at again,
  so a later manual edit by the user wins;
- a transaction is looked at only after `settle_days` and within `lookback_days`;
- an expense that has an income of the same amount on another own account nearby is an unmerged transfer:
  Zenmoney merges those itself, no rule touches it;
- every action keeps every account balance as it is (checked here, an action that would move a balance is a bug).
"""
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from .config import ConfigError, FxTransferRule, Match, RefundRule, Rules

UNMERGED_TRANSFER_DAYS = 2
CENT = 0.005


def kind(t: dict) -> str:
    if t['income'] and t['outcome'] and t['incomeAccount'] != t['outcomeAccount']:
        return 'transfer'
    return 'income' if t['income'] else 'expense'


def payee(t: dict) -> str:
    return (t.get('payee') or t.get('originalPayee') or '').strip()


def counterparty(t: dict) -> str:
    """Payee, or the comment when there is none: T-Bank puts the shop of SBP purchases and refunds there."""
    return payee(t) or (t.get('comment') or '').strip()


def amount(t: dict) -> float:
    return t['income'] if kind(t) == 'income' else t['outcome']


def account_of(t: dict) -> str:
    return t['incomeAccount'] if kind(t) == 'income' else t['outcomeAccount']


class Book:
    """Live Zenmoney data with lookups by the names used in rules.toml."""

    def __init__(self, snapshot: dict):
        self.server_timestamp = snapshot['serverTimestamp']
        self.accounts = {a['id']: a for a in snapshot['account']}
        self.rates = {i['id']: i['rate'] for i in snapshot['instrument']}
        tags = {t['id']: t for t in snapshot['tag']}
        self.categories = {}
        for t in tags.values():
            parent = tags.get(t.get('parent'))
            path = f"{parent['title']} / {t['title']}" if parent else t['title']
            if not t.get('archive'):
                self.categories[path] = t['id']
        self.transactions = [t for t in snapshot['transaction'] if not t['deleted']]

    def account_id(self, name: str) -> str:
        if name in self.accounts:
            return name
        found = [a['id'] for a in self.accounts.values() if a['title'] == name and not a.get('archive')]
        if len(found) != 1:
            raise ConfigError(f'account {name!r}: {"not found" if not found else "ambiguous"} in Zenmoney')
        return found[0]

    def category_id(self, path: str) -> str:
        if path not in self.categories:
            raise ConfigError(f'category {path!r} not found in Zenmoney (use "Parent / Child")')
        return self.categories[path]

    def title(self, account_id: str) -> str:
        return self.accounts[account_id]['title']


@dataclass
class Action:
    rule: str
    text: str
    before: list[dict]
    after: list[dict]
    start_balance_deltas: dict[str, float] = field(default_factory=dict)   # account id -> startBalance delta


@dataclass
class Notice:
    rule: str
    text: str
    ids: list[str]


@dataclass
class Plan:
    actions: list[Action] = field(default_factory=list)
    notices: list[Notice] = field(default_factory=list)
    unchanged: dict[str, str] = field(default_factory=dict)   # id -> rule: already right, remember without writing

    def handled(self) -> dict[str, str]:
        """Everything this plan decided about, to be remembered once it is written."""
        out = dict(self.unchanged)
        for a in self.actions:
            out.update({t['id']: a.rule for t in a.before})
        for n in self.notices:
            out.update({i: n.rule for i in n.ids})
        return out


def plan(book: Book, rules: Rules, today: date, handled: set[str]) -> Plan:
    _check_names(book, rules)
    p = _Planner(book, rules, today, handled)
    for rule in rules.fx_transfers:
        p.fx_transfer(rule)
    for rule in rules.transfers:
        p.transfer(rule)
    if rules.refund:
        p.refund(rules.refund)
    for rule in rules.categories:
        p.category(rule)
    for rule in rules.notices:
        p.notice(rule)
    for action in p.out.actions:
        _assert_balances_kept(action)
    return p.out


class _Planner:
    def __init__(self, book: Book, rules: Rules, today: date, handled: set[str]):
        self.book, self.rules, self.today = book, rules, today
        self.ignored = {book.account_id(a) for a in rules.ignore_accounts}
        self.decided = set(handled)
        self.out = Plan()
        self.purchase_days = {(t['outcomeAccount'], t['date']) for t in book.transactions
                          if kind(t) == 'expense' and payee(t) and not t.get('hold')}
        self.incomes = defaultdict(list)   # (instrument, amount in cents) -> incomes
        for t in book.transactions:
            if kind(t) == 'income':
                self.incomes[(t['incomeInstrument'], round(t['income'] * 100))].append(t)

    # --- eligibility -------------------------------------------------------------------------------------------

    def eligible(self, t: dict, settle_days: int | None) -> bool:
        settle = self.rules.settle_days if settle_days is None else settle_days
        day = date.fromisoformat(t['date'])
        return (t['id'] not in self.decided and t.get('source') == 'plugin' and not t.get('hold')
                and kind(t) != 'transfer'
                and not {t['incomeAccount'], t['outcomeAccount']} & self.ignored
                and self.today - timedelta(days=self.rules.lookback_days) <= day <= self.today - timedelta(days=settle)
                and not self.unmerged_transfer(t))

    def unmerged_transfer(self, t: dict) -> bool:
        if kind(t) != 'expense':
            return False
        day = date.fromisoformat(t['date'])
        return any(i['incomeAccount'] != t['outcomeAccount'] and i['incomeAccount'] not in self.ignored
                   and abs((date.fromisoformat(i['date']) - day).days) <= UNMERGED_TRANSFER_DAYS
                   for i in self.incomes[(t['outcomeInstrument'], round(t['outcome'] * 100))])

    def matches(self, t: dict, m: Match) -> bool:
        k, text, value = kind(t), payee(t), amount(t)
        foreign = bool(t.get('opIncome') if k == 'income' else t.get('opOutcome'))
        return not (
            k == 'transfer'
            or (m.kind and k != m.kind)
            or (m.account and account_of(t) != self.book.account_id(m.account))
            or (m.payee and text.casefold() not in {p.casefold() for p in m.payee})
            or (m.payee_regex and not re.search(m.payee_regex, text, re.I))
            or (m.no_payee and text)
            or (m.no_comment and (t.get('comment') or '').strip())
            or (m.at_least is not None and value < m.at_least)
            or (m.below is not None and value >= m.below)
            or (m.foreign is not None and foreign != m.foreign)
            or (m.same_day_purchase and (account_of(t), t['date']) not in self.purchase_days))

    def candidates(self, m: Match, settle_days: int | None) -> list[dict]:
        found = [t for t in self.book.transactions if self.eligible(t, settle_days) and self.matches(t, m)]
        return sorted(found, key=lambda t: (t['date'], t['id']))

    def add_action(self, action: Action):
        ids = [t['id'] for t in action.before]
        assert not set(ids) & self.decided, f'{action.rule}: a transaction is used twice'
        self.decided.update(ids)
        self.out.actions.append(action)

    def add_notice(self, rule: str, text: str, ts: list[dict]):
        self.decided.update(t['id'] for t in ts)
        self.out.notices.append(Notice(rule, text, [t['id'] for t in ts]))

    def describe(self, t: dict) -> str:
        sign = '+' if kind(t) == 'income' else '−'
        return f"{t['date']} «{counterparty(t) or 'без получателя'}» {sign}{amount(t):,.2f} ({self.book.title(account_of(t))})"

    # --- rule types --------------------------------------------------------------------------------------------

    def fx_transfer(self, rule: FxTransferRule):
        targets = {self.book.account_id(a) for a in rule.to_accounts}
        outs = [t for t in self.candidates(rule.match, rule.settle_days) if kind(t) == 'expense']
        ins = [t for t in self.book.transactions
               if kind(t) == 'income' and t['incomeAccount'] in targets and t.get('source') == 'plugin'
               and not t.get('hold') and t['id'] not in self.decided
               and re.search(rule.income_regex, counterparty(t) + ' ' + (t.get('comment') or ''), re.I)]
        pairs = defaultdict(list)
        for o in outs:
            for i in ins:
                days = (date.fromisoformat(i['date']) - date.fromisoformat(o['date'])).days
                if i['incomeInstrument'] == o['outcomeInstrument'] or not 0 <= days <= rule.max_days:
                    continue
                ratio = o['outcome'] * self.book.rates[o['outcomeInstrument']] / (i['income'] * self.book.rates[i['incomeInstrument']])
                if abs(ratio - 1) <= rule.rate_tolerance:
                    pairs[o['id']].append(i)
        income_uses = defaultdict(int)
        for found in pairs.values():
            for i in found:
                income_uses[i['id']] += 1
        for o in outs:
            found = pairs.get(o['id'], [])
            if len(found) == 1 and income_uses[found[0]['id']] == 1:
                i = found[0]
                merged = dict(o, incomeAccount=i['incomeAccount'], income=i['income'], incomeInstrument=i['incomeInstrument'],
                              incomeBankID=i.get('incomeBankID'), tag=None)
                self.add_action(Action(rule.name, f"{self.describe(o)} → {i['income']:,.2f} {self.book.title(i['incomeAccount'])}",
                                [o, i], [merged, dict(i, deleted=True)]))
            elif found:
                self.add_notice(rule.name, f'{self.describe(o)}: несколько подходящих поступлений, склей вручную', [o])

    def transfer(self, rule):
        target = self.book.account_id(rule.to_account)
        for t in self.candidates(rule.match, rule.settle_days):
            if self.book.accounts[target]['instrument'] != (t['outcomeInstrument'] if kind(t) == 'expense' else t['incomeInstrument']):
                raise ConfigError(f'{rule.name}: {rule.to_account!r} is in another currency')
            if kind(t) == 'expense':
                after = dict(t, incomeAccount=target, income=t['outcome'], incomeInstrument=t['outcomeInstrument'], tag=None)
                delta = -t['outcome']
            else:
                after = dict(t, outcomeAccount=target, outcome=t['income'], outcomeInstrument=t['incomeInstrument'], tag=None)
                delta = t['income']
            self.add_action(Action(rule.name, self.describe(t), [t], [after],
                            {target: delta} if rule.keep_target_balance else {}))

    def refund(self, rule: RefundRule):
        groups = {name.casefold().replace('ё', 'е'): n for n, group in enumerate(rule.merchant_aliases) for name in group}

        def merchant(text: str) -> str:
            s = text.casefold().replace('ё', 'е')
            return f'group:{groups[s]}' if s in groups else re.sub(r'[^0-9a-zа-я]', '', s)

        def same(a: str, b: str) -> bool:
            if a == b:
                return True
            if a.startswith('group:') or b.startswith('group:') or min(len(a), len(b)) < 4:
                return False
            return a.startswith(b) or b.startswith(a)   # «Whoosh» / «WHOOSH_SBP», «Boosty» / «Boosty.to»

        refunds = [t for t in self.book.transactions if kind(t) == 'income' and counterparty(t) and self.eligible(t, rule.settle_days)
                   and not (rule.skip_payee_regex and re.search(rule.skip_payee_regex, counterparty(t)))]
        for r in sorted(refunds, key=lambda t: (t['date'], t['id'])):
            key, day = merchant(counterparty(r)), date.fromisoformat(r['date'])
            found = [p for p in self.book.transactions
                     if kind(p) == 'expense' and p['outcomeAccount'] == r['incomeAccount'] and p['id'] not in self.decided
                     and p.get('source') == 'plugin' and abs(p['outcome'] - r['income']) < CENT
                     and 0 <= (day - date.fromisoformat(p['date'])).days <= rule.max_days and counterparty(p) and same(merchant(counterparty(p)), key)]
            posted = [p for p in found if not p.get('hold')]
            if posted:
                p = max(posted, key=lambda t: (t['date'], t['id']))   # the nearest earlier purchase: a refund follows its purchase
                self.add_action(Action(rule.name, f"{self.describe(p)} и возврат {r['date']}", [p, r],
                                [dict(p, deleted=True), dict(r, deleted=True)]))
            elif found:
                self.add_notice(rule.name, f'{self.describe(r)}: возврат, а покупка все еще на холде — удали пару вручную', [r])

    def category(self, rule):
        for t in self.candidates(rule.match, rule.settle_days):
            value = amount(t)
            band = next(b for b in rule.bands if b.below is None or value < b.below)
            tag = self.book.category_id(band.category)
            if t.get('tag') == [tag]:
                self.out.unchanged[t['id']] = rule.name
                self.decided.add(t['id'])
            else:
                self.add_action(Action(rule.name, f'{self.describe(t)} → {band.category}', [t], [dict(t, tag=[tag])]))

    def notice(self, rule):
        for t in self.candidates(rule.match, rule.settle_days):
            self.add_notice(rule.name, f'{self.describe(t)}: {rule.text}', [t])


def _check_names(book: Book, rules: Rules):
    """Resolve every name up front: a renamed account or category stops the run even if nothing matches today."""
    for name in rules.ignore_accounts:
        book.account_id(name)
    for rule in (*rules.transfers, *rules.categories, *rules.fx_transfers, *rules.notices):
        if rule.match.account:
            book.account_id(rule.match.account)
    for rule in rules.transfers:
        book.account_id(rule.to_account)
    for rule in rules.fx_transfers:
        for name in rule.to_accounts:
            book.account_id(name)
    for rule in rules.categories:
        for band in rule.bands:
            book.category_id(band.category)


def balance_deltas(action: Action) -> dict[str, float]:
    deltas = defaultdict(float, action.start_balance_deltas)
    for sign, ts in ((-1, action.before), (1, action.after)):
        for t in ts:
            if not t.get('deleted'):
                deltas[t['incomeAccount']] += sign * t['income']
                deltas[t['outcomeAccount']] -= sign * t['outcome']
    return deltas


def _assert_balances_kept(action: Action):
    moved = {a: d for a, d in balance_deltas(action).items() if abs(d) >= CENT}
    assert not moved, f'{action.rule}: {action.text} would move balances {moved}'
