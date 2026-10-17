"""Подставной requests.Session: отвечает заготовками по (метод, URL). В сеть не ходит."""
import json

import requests


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.content = b'' if body is None else json.dumps(body).encode()
        self.text = self.content.decode()
        self.ok = 200 <= status < 300

    def json(self):
        if self._body is None:
            raise ValueError('empty')
        return self._body

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f'HTTP {self.status_code}')


class FakeSession:
    """routes: {(METHOD, url): (status, body) | Exception}. Неизвестный URL — ConnectionError."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.auth = None
        self.verify = True
        self.cert = None

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        r = self.routes.get((method, url))
        if r is None:
            raise requests.ConnectionError(f'нет маршрута {method} {url}')
        if isinstance(r, Exception):
            raise r
        return FakeResponse(*r)

    def post(self, url, **kw):
        return self.request('POST', url, **kw)


def patroni_routes(nodes, cluster_view, node_views=None, down=()):
    """nodes: [(name, url)]; node_views: {name: /patroni body}."""
    routes = {}
    for name, url in nodes:
        if name in down:
            continue
        routes[('GET', url + '/patroni')] = (200, (node_views or {}).get(name, {'state': 'running', 'role': 'replica'}))
        routes[('GET', url + '/cluster')] = (200, cluster_view)
    return routes
