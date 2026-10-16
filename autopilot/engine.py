"""What the rules ask to write on the current Zenmoney data. Pure: no network, no clock, no files.

Safety rules that hold for every rule type:
- only transactions imported by a bank plugin, never manual ones;
- a hold is waited out for `hold_days`, then treated as posted: in Zenmoney T-Bank holds keep the flag for good
  (no hold has ever been replaced or unflagged by the plugin), while the bank posts them within a day;
- a transaction is decided once: ids `handled` by previous runs are never looked at again,
  so a later manual edit by the user wins;
- a transaction is looked at only after `settle_days` and within `lookback_days`;
- an expense or income with a counterpart of the same amount on another own account nearby is one leg of an
  unmerged transfer: Zenmoney merges those itself, no rule touches either leg;
- an action changes balances only as it declares in `moves`: a transfer rule adds to its target account exactly the
  money the bank already moved there and the plugin did not record (a T-Bank roundup comes as a card expense only);
  every other balance stays as it is. Checked here: anything else is a bug.
"""
import re
from collections.abc import Iterable
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from .config import ConfigError, FxTransferRule, Match, RefundRule, Rules

CENT = 0.005
COMMON_WORD_PAYEES = 5   # a word in the names of this many payees (city, «ООО», «restoran») tells nothing about the shop
TRANSLIT = str.maketrans({'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e', 'ж': 'zh', 'з': 'z', 'и': 'i',
                          'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't',
                          'у': 'u', 'ф': 'f', 'х': 'h', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch', 'ъ': '', 'ы': 'y', 'ь': '',
                          'э': 'e', 'ю': 'yu', 'я': 'ya'})


class PlanError(RuntimeError):
    """A bug: the planner produced something that breaks its own guarantees. Nothing is written."""


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
        self.categories, self.ambiguous = {}, set()
        for t in tags.values():
            parent = tags.get(t.get('parent'))
            path = f"{parent['title']} / {t['title']}" if parent else t['title']
            if not t.get('archive'):
                if path in self.categories:
                    self.ambiguous.add(path)
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
        if path not in self.categories or path in self.ambiguous:
            problem = 'is not unique' if path in self.ambiguous else 'not found (use "Parent / Child")'
            raise ConfigError(f'category {path!r} {problem} in Zenmoney')
        return self.categories[path]

    def title(self, account_id: str) -> str:
        return self.accounts[account_id]['title']


@dataclass
class Action:
    rule: str
    text: str
    before: list[dict]
    after: list[dict]
    moves: dict[str, float] = field(default_factory=dict)   # account id -> balance change the action is meant to make


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
    """Each rule stands on its own: one whose account or category is not in Zenmoney is skipped and reported,
    the others still run. Names are resolved up front, so a renamed account disables its rule even on a day
    nothing matches, instead of surfacing weeks later."""
    p = _Planner(book, rules, today, handled)
    for rule in rules.fx_transfers:
        if p.usable(rule, accounts=(rule.match.account, *rule.to_accounts)):
            p.fx_transfer(rule)
    for rule in rules.transfers:
        if p.usable(rule, accounts=(rule.match.account, rule.to_account)):
            p.transfer(rule)
    if rules.refund:
        p.refund(rules.refund)
    for rule in rules.categories:
        if p.usable(rule, accounts=(rule.match.account,), categories=[b.category for b in rule.bands]):
            p.category(rule)
    for rule in rules.notices:
        if p.usable(rule, accounts=(rule.match.account,)):
            p.notice(rule)
    for action in p.out.actions:
        _assert_balances_kept(action)
    return p.out


class _Planner:
    def __init__(self, book: Book, rules: Rules, today: date, handled: set[str]):
        self.book, self.rules, self.today = book, rules, today
        self.decided = set(handled)
        self.out = Plan()
        self.ignored = self._ignored_accounts()
        self.purchase_days = {(t['outcomeAccount'], t['date']) for t in book.transactions if kind(t) == 'expense' and payee(t)}
        self.by_amount = defaultdict(list)   # (kind, instrument, amount in cents) -> transactions
        for t in book.transactions:
            if kind(t) != 'transfer':
                instrument = t['incomeInstrument'] if kind(t) == 'income' else t['outcomeInstrument']
                self.by_amount[(kind(t), instrument, round(amount(t) * 100))].append(t)
        # names used for own money in rules (self transfers): money back from them is never a refund
        self.own_payees = {p.casefold() for r in (*rules.transfers, *rules.categories, *rules.fx_transfers, *rules.notices)
                           for p in r.match.payee}
        payees = {counterparty(t) for t in book.transactions if kind(t) != 'transfer' and counterparty(t)}
        word_payees = defaultdict(int)
        for name in payees:
            for w in _words(name):
                word_payees[w] += 1
        self.common_words = {w for w, n in word_payees.items() if n >= COMMON_WORD_PAYEES}

    # --- eligibility -------------------------------------------------------------------------------------------

    def usable(self, rule, accounts: Iterable[str | None] = (), categories: Iterable[str] = ()) -> bool:
        """False, with a notice, when a name the rule needs is not in Zenmoney (renamed, deleted, duplicated)."""
        try:
            for name in accounts:
                if name:
                    self.book.account_id(name)
            for path in categories:
                self.book.category_id(path)
        except ConfigError as error:
            self.out.notices.append(Notice('rules.toml', f'правило «{rule.name}» отключено, пока не исправишь rules.toml: {error}', []))
            return False
        return True

    def _ignored_accounts(self) -> set[str]:
        """Accounts never touched. One that is gone from Zenmoney has nothing to protect: reported, not fatal."""
        ids = set()
        for name in self.rules.ignore_accounts:
            try:
                ids.add(self.book.account_id(name))
            except ConfigError as error:
                self.out.notices.append(Notice('rules.toml', f'ignore_accounts: {error}; нечего игнорировать, убери его из rules.toml', []))
        return ids

    def eligible(self, t: dict, settle_days: int | None) -> bool:
        settle = self.rules.settle_days if settle_days is None else settle_days
        day = date.fromisoformat(t['date'])
        return (t['id'] not in self.decided and t.get('source') == 'plugin' and self.hold_waited(t)
                and kind(t) != 'transfer'
                and not {t['incomeAccount'], t['outcomeAccount']} & self.ignored
                and self.today - timedelta(days=self.rules.lookback_days) <= day <= self.today - timedelta(days=settle)
                and not self.unmerged_transfer(t))

    def hold_waited(self, t: dict) -> bool:
        return not t.get('hold') or date.fromisoformat(t['date']) <= self.today - timedelta(days=self.rules.hold_days)

    def same_merchant(self, a: str, b: str) -> bool:
        """«Whoosh» / «WHOOSH_SBP», «Иви» / «ivi.ru», «Яндекс Сервисы» / «Сервисы Яндекса»: no lists of shops."""
        ca, cb = _compact(a), _compact(b)
        if ca and cb and (ca == cb or (min(len(ca), len(cb)) >= 3 and (ca.startswith(cb) or cb.startswith(ca)))):
            return True
        wa, wb = _words(a) - self.common_words, _words(b) - self.common_words
        return any(x.startswith(y) or y.startswith(x) for x in wa for y in wb)

    def unmerged_transfer(self, t: dict) -> bool:
        """An expense with an income of the same amount on another own account nearby, or the other way round."""
        k = kind(t)
        if k == 'transfer':
            return False
        other = 'income' if k == 'expense' else 'expense'
        instrument = t['incomeInstrument'] if k == 'income' else t['outcomeInstrument']
        day = date.fromisoformat(t['date'])
        return any(account_of(c) != account_of(t) and account_of(c) not in self.ignored
                   and abs((date.fromisoformat(c['date']) - day).days) <= self.rules.unmerged_transfer_days
                   for c in self.by_amount[(other, instrument, round(amount(t) * 100))])

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
        if set(ids) & self.decided:
            raise PlanError(f'{action.rule}: a transaction is used twice')
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
               and self.hold_waited(t) and t['id'] not in self.decided
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
                move = t['outcome']
            else:
                after = dict(t, outcomeAccount=target, outcome=t['income'], outcomeInstrument=t['incomeInstrument'], tag=None)
                move = -t['income']
            self.add_action(Action(rule.name, self.describe(t), [t], [after], {target: move}))

    def refund(self, rule: RefundRule):
        refunds = [t for t in self.book.transactions if kind(t) == 'income' and counterparty(t) and self.eligible(t, rule.settle_days)
                   and counterparty(t).casefold() not in self.own_payees
                   and not (rule.skip_payee_regex and re.search(rule.skip_payee_regex, counterparty(t)))]
        for r in sorted(refunds, key=lambda t: (t['date'], t['id'])):
            day = date.fromisoformat(r['date'])
            found = [p for p in self.book.transactions
                     if kind(p) == 'expense' and p['outcomeAccount'] == r['incomeAccount'] and p['id'] not in self.decided
                     and p.get('source') == 'plugin' and abs(p['outcome'] - r['income']) < CENT
                     and 0 <= (day - date.fromisoformat(p['date'])).days <= rule.max_days
                     and counterparty(p) and self.same_merchant(counterparty(p), counterparty(r))
                     and not self.unmerged_transfer(p)]
            ready = [p for p in found if self.hold_waited(p)]
            nearest = max(ready, key=lambda t: (t['date'], t['id'])) if ready else None
            # a fresh hold at least as recent as the nearest ready purchase may be the one refunded: wait it out
            fresh_hold = any(not self.hold_waited(p) and (nearest is None or p['date'] >= nearest['date']) for p in found)
            if nearest and not fresh_hold:
                p = nearest   # the nearest earlier purchase: a refund follows its purchase
                self.add_action(Action(rule.name, f"{self.describe(p)} и возврат {r['date']}", [p, r],
                                [dict(p, deleted=True), dict(r, deleted=True)]))

    def category(self, rule):
        for t in self.candidates(rule.match, rule.settle_days):
            value = amount(t)
            band = next(b for b in rule.bands if b.below is None or value < b.below)
            tag = self.book.category_id(band.category)
            ours = {self.book.category_id(b.category) for b in rule.bands}
            if t.get('tag') == [tag] or (t.get('tag') and not set(t['tag']) <= ours):   # right, or chosen by hand
                self.out.unchanged[t['id']] = rule.name
                self.decided.add(t['id'])
            else:
                self.add_action(Action(rule.name, f'{self.describe(t)} → {band.category}', [t], [dict(t, tag=[tag])]))

    def notice(self, rule):
        for t in self.candidates(rule.match, rule.settle_days):
            self.add_notice(rule.name, f'{self.describe(t)}: {rule.text}', [t])


def _compact(name: str) -> str:
    return re.sub(r'[^a-z0-9]', '', name.casefold().translate(TRANSLIT))


def _words(name: str) -> set[str]:
    return {w for w in re.findall(r'[a-z0-9]+', name.casefold().translate(TRANSLIT)) if len(w) >= 4}


def balance_deltas(action: Action) -> dict[str, float]:
    """How writing the action changes the balance of each account."""
    deltas = defaultdict(float)
    for sign, ts in ((-1, action.before), (1, action.after)):
        for t in ts:
            if not t.get('deleted'):
                deltas[t['incomeAccount']] += sign * t['income']
                deltas[t['outcomeAccount']] -= sign * t['outcome']
    return deltas


def _assert_balances_kept(action: Action):
    deltas = balance_deltas(action)
    wrong = {a: round(deltas.get(a, 0) - action.moves.get(a, 0), 2) for a in set(deltas) | set(action.moves)
             if abs(deltas.get(a, 0) - action.moves.get(a, 0)) >= CENT}
    if wrong:
        raise PlanError(f'{action.rule}: {action.text} would move balances beyond what it declares: {wrong}')
