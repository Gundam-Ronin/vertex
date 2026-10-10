"""A stand-in for requests.Session that replays canned responses."""

import json


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(payload)

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


class FakeSession:
    def __init__(self, get=(), post=()):
        self._get, self._post = list(get), list(post)
        self.get_calls, self.post_calls = [], []

    def get(self, url, params=None, headers=None, timeout=None):
        self.get_calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        return self._get.pop(0)

    def post(self, url, headers=None, data=None, timeout=None):
        self.post_calls.append({"url": url, "headers": dict(headers or {}), "data": dict(data or {})})
        return self._post.pop(0)
