"""Zenmoney API v8: a full snapshot and writes of complete objects.

Docs: https://github.com/zenmoney/ZenPlugins/wiki/ZenMoney-API
"""
import json
import time
import urllib.error
import urllib.request

API = 'https://api.zenmoney.ru/v8/'
RETRIES = 3


class ZenmoneyError(RuntimeError):
    pass


class Zenmoney:
    def __init__(self, token: str, timeout: int = 120):
        self._token = token
        self._timeout = timeout

    def diff(self, server_timestamp: int = 0) -> dict:
        """Objects changed since `server_timestamp` (0: everything) and the new timestamp to pass next time."""
        return self._call('diff/', {'currentClientTimestamp': int(time.time()), 'serverTimestamp': server_timestamp})

    def write(self, server_timestamp: int, **objects: list[dict]) -> dict:
        """Send complete objects. The server keeps an object only if its `changed` is newer, so it is set here.

        Writing the same objects twice gives the same state, so a retry after a network error is safe.
        The answer is a diff like `diff()`: everything changed since `server_timestamp`, our objects included.
        """
        now = int(time.time())
        objects = {kind: [dict(o, changed=now) for o in items] for kind, items in objects.items() if items}
        return self._call('diff/', {'currentClientTimestamp': now, 'serverTimestamp': server_timestamp, **objects})

    def _call(self, method: str, body: dict) -> dict:
        request = urllib.request.Request(API + method, data=json.dumps(body).encode(), headers={
            'Authorization': 'Bearer ' + self._token, 'Content-Type': 'application/json'})
        for attempt in range(1, RETRIES + 1):
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if error.code < 500 or attempt == RETRIES:
                    raise ZenmoneyError(f'Zenmoney HTTP {error.code}: {error.read().decode()[:300]}') from None
            except (urllib.error.URLError, TimeoutError) as error:
                if attempt == RETRIES:
                    raise ZenmoneyError(f'Zenmoney is unreachable: {error}') from None
            time.sleep(5 * attempt)
        raise AssertionError('unreachable')
