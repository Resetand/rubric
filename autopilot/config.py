"""rules.toml -> typed rules. Everything specific to the user lives in that file; this module only checks its shape.

Names of accounts and categories are resolved against the live Zenmoney data in `engine.Book`, so a renamed
account fails the run loudly instead of silently matching nothing.
"""
import re
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Match:
    """Conditions on one transaction. All given conditions must hold."""
    account: str | None = None            # account title
    kind: str | None = None               # 'expense' | 'income'; transfers never match
    payee: tuple[str, ...] = ()           # exact, case-insensitive
    payee_regex: str | None = None
    no_payee: bool = False
    no_comment: bool = False
    at_least: float | None = None         # amount in the account currency
    below: float | None = None
    foreign: bool | None = None           # paid in another currency (the bank shows the original amount)
    same_day_purchase: bool = False        # the same account has a purchase (an expense with a payee) that day


@dataclass(frozen=True)
class TransferRule:
    """Turn a matched expense into a transfer to `to_account`."""
    name: str
    match: Match
    to_account: str
    keep_target_balance: bool = False     # the bank already counts the money in the target balance
    settle_days: int | None = None


@dataclass(frozen=True)
class Band:
    category: str                         # 'Parent / Child' or 'Title'
    below: float | None = None            # None: any amount (the last band)


@dataclass(frozen=True)
class CategoryRule:
    """Set the category of a matched transaction by its amount: the first band whose `below` is above it wins."""
    name: str
    match: Match
    bands: tuple[Band, ...]
    settle_days: int | None = None


@dataclass(frozen=True)
class FxTransferRule:
    """Merge a matched expense with the income it became on another-currency account into one transfer."""
    name: str
    match: Match
    to_accounts: tuple[str, ...]
    income_regex: str                          # regex over the income's payee and comment
    max_days: int = 2                     # income date - expense date
    rate_tolerance: float = 0.15          # |expense / (income * current rate) - 1|
    settle_days: int | None = None


@dataclass(frozen=True)
class RefundRule:
    """Delete a purchase together with its full refund: same account, same amount, same merchant."""
    name: str
    max_days: int = 60
    merchant_aliases: tuple[tuple[str, ...], ...] = ()   # payee names of one merchant, one group per merchant
    skip_payee_regex: str | None = None   # payees that are people, not shops: money back is not a refund
    settle_days: int | None = None


@dataclass(frozen=True)
class NoticeRule:
    """Tell the user about a matched transaction once. Nothing is written."""
    name: str
    match: Match
    text: str
    settle_days: int | None = None


@dataclass(frozen=True)
class Rules:
    lookback_days: int
    settle_days: int
    max_writes_per_run: int
    unmerged_transfer_days: int
    ignore_accounts: tuple[str, ...]
    transfers: tuple[TransferRule, ...]
    categories: tuple[CategoryRule, ...]
    fx_transfers: tuple[FxTransferRule, ...]
    refund: RefundRule | None
    notices: tuple[NoticeRule, ...]


def load(path: Path) -> Rules:
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f'{path}: {error}') from None
    top = _take(raw, 'rules.toml', {'lookback_days', 'settle_days', 'max_writes_per_run', 'unmerged_transfer_days', 'ignore_accounts',
                                    'transfer', 'category', 'fx_transfer', 'refund', 'notice'})
    return Rules(
        lookback_days=top.get('lookback_days', 30),
        settle_days=top.get('settle_days', 1),
        max_writes_per_run=top.get('max_writes_per_run', 40),
        unmerged_transfer_days=top.get('unmerged_transfer_days', 3),
        ignore_accounts=tuple(top.get('ignore_accounts', ())),
        transfers=tuple(_rule(TransferRule, r, 'transfer') for r in top.get('transfer', [])),
        categories=tuple(_rule(CategoryRule, r, 'category') for r in top.get('category', [])),
        fx_transfers=tuple(_rule(FxTransferRule, r, 'fx_transfer') for r in top.get('fx_transfer', [])),
        refund=_rule(RefundRule, top['refund'], 'refund') if 'refund' in top else None,
        notices=tuple(_rule(NoticeRule, r, 'notice') for r in top.get('notice', [])),
    )


def _rule(cls, raw: dict, section: str):
    where = f"[{section}] {raw.get('name', '')}".strip()
    values = _take(raw, where, {f.name for f in fields(cls)})
    if 'match' in values:
        values['match'] = _match(values['match'], where)
    if 'bands' in values:
        values['bands'] = tuple(Band(**_take(b, where + ' bands', {'category', 'below'})) for b in values['bands'])
        if not values['bands'] or values['bands'][-1].below is not None:
            raise ConfigError(f'{where}: the last band must have no `below`, so every amount gets a category')
    for key in ('to_accounts', 'merchant_aliases'):
        if key in values:
            values[key] = tuple(tuple(v) if isinstance(v, list) else v for v in values[key])
    for key in ('income_regex', 'skip_payee_regex'):
        if values.get(key):
            _regex(values[key], where)
    try:
        return cls(**values)
    except TypeError as error:
        raise ConfigError(f'{where}: {error}') from None


def _match(raw: dict, where: str) -> Match:
    values = _take(raw, where + ' match', {f.name for f in fields(Match)})
    if values.get('kind') not in (None, 'expense', 'income'):
        raise ConfigError(f"{where}: kind must be 'expense' or 'income'")
    if 'payee' in values:
        values['payee'] = tuple(values['payee'])
    if values.get('payee_regex'):
        _regex(values['payee_regex'], where)
    return Match(**values)


def _take(raw: dict, where: str, allowed: set[str]) -> dict:
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f'{where}: unknown keys {sorted(unknown)}; allowed: {sorted(allowed)}')
    return dict(raw)


def _regex(pattern: str, where: str):
    try:
        re.compile(pattern)
    except re.error as error:
        raise ConfigError(f'{where}: bad regex {pattern!r}: {error}') from None
