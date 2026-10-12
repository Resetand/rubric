"""Local copy of the Zenmoney data, kept current with incremental diffs.

The first run (or a broken or week-old file) downloads everything; later runs fetch only what changed since the
last `serverTimestamp`. The copy holds the whole history, so a refund can still find a purchase made weeks ago.
"""
import json
import os
import time
from pathlib import Path

FULL_REFRESH_SECONDS = 7 * 24 * 3600   # a full download once a week heals any drift of the copy
KINDS = ('instrument', 'account', 'tag', 'transaction')   # what the rules read; budgets and the rest are not kept


class Mirror:
    def __init__(self, path: Path):
        self.path = path
        self.server_timestamp = 0
        self.full_at = 0
        self.objects: dict[str, dict[str, dict]] = {}

    @classmethod
    def load(cls, path: Path) -> 'Mirror':
        mirror = cls(path)
        try:
            raw = json.loads(path.read_text())
            mirror.server_timestamp, mirror.full_at = raw['serverTimestamp'], raw['fullAt']
            mirror.objects = {kind: {str(o['id']): o for o in items} for kind, items in raw['objects'].items()}
        except (OSError, ValueError, KeyError):
            mirror.server_timestamp = 0   # missing or broken: start from a full download
        return mirror

    def needs_full(self, now: float) -> bool:
        return self.server_timestamp == 0 or now - self.full_at > FULL_REFRESH_SECONDS

    def apply(self, diff: dict, full: bool = False):
        if full:
            self.objects, self.full_at = {}, int(time.time())
        for kind in KINDS:
            bucket = self.objects.setdefault(kind, {})
            for o in diff.get(kind) or []:
                bucket[str(o['id'])] = o
        for gone in diff.get('deletion') or []:
            self.objects.get(gone['object'], {}).pop(str(gone['id']), None)
        self.server_timestamp = diff['serverTimestamp']

    def snapshot(self) -> dict:
        """The same shape as a full diff from the API."""
        return {'serverTimestamp': self.server_timestamp, **{kind: list(self.objects.get(kind, {}).values()) for kind in KINDS}}

    def save(self):
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps({'serverTimestamp': self.server_timestamp, 'fullAt': self.full_at,
                                   'objects': {k: list(v.values()) for k, v in self.objects.items()}}, ensure_ascii=False))
        os.replace(tmp, self.path)
