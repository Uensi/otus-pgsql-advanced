"""Имитатор кластера Patroni 4 для тестов операций. В сеть не ходит.

Поведение упрощено, но повторяет то, на что опирается консоль:
switchover/failover меняют лидера и таймлайн, рестарт сбрасывает pending_restart,
пауза ставится через PATCH /config, на ошибки отвечает кодами как Patroni.
"""
import copy

import requests

from fakes import FakeResponse


class SimCluster:
    def __init__(self, names=('n1', 'n2', 'n3'), base='http://10.0.0.{i}:8008'):
        self.urls = {n: base.format(i=i + 1) for i, n in enumerate(names)}
        self.by_url = {u: n for n, u in self.urls.items()}
        self.members = {}
        for i, n in enumerate(names):
            self.members[n] = {'name': n, 'role': 'leader' if i == 0 else 'replica',
                               'state': 'running' if i == 0 else 'streaming', 'timeline': 5,
                               'lag': None if i == 0 else 0, 'tags': {}, 'pending_restart': False}
        self.config = {'ttl': 30, 'loop_wait': 10, 'maximum_lag_on_failover': 1048576}
        self.paused = False
        self.scheduled = None
        self.history = []
        self.down = set()
        self.calls = []
        # сценарные переключатели
        self.refuse = {}             # path -> (status, body)
        self.read_timeout_on = set() # path: запрос «уходит», но ответа нет
        self.old_leader_rejoins = True
        self.leader_change_works = True
        self.restart_heals = True
        self.config_patch_ignored = False

    @property
    def leader(self):
        return next((n for n, m in self.members.items() if m['role'] == 'leader'), None)

    def session(self):
        return _SimSession(self)

    # ---- представления ----
    def view(self):
        ms = []
        for m in self.members.values():
            d = {k: v for k, v in m.items() if v is not None and k != 'pending_restart'}
            if m['pending_restart']:
                d['pending_restart'] = True
            if m['role'] != 'leader' and m['lag'] is not None:
                d['lag'] = m['lag']
            ms.append(d)
        v = {'scope': 'test', 'members': ms}
        if self.paused:
            v['pause'] = True
        if self.scheduled:
            v['scheduled_switchover'] = self.scheduled
        return v

    def node_view(self, name):
        m = self.members[name]
        return {'state': m['state'], 'role': 'primary' if m['role'] == 'leader' else 'replica',
                'timeline': m['timeline'], 'pending_restart': m['pending_restart'],
                'patroni': {'version': '4.0.4', 'scope': 'test', 'name': name}}

    # ---- действия ----
    def _promote(self, new):
        old = self.leader
        tl = max(m['timeline'] for m in self.members.values()) + 1
        self.history.append([tl - 1, 1000 * tl, 'no recovery target specified', '2026-10-16T10:00:00+03:00', new])
        for m in self.members.values():
            m['timeline'] = tl
        self.members[new].update(role='leader', state='running', lag=None)
        if old:
            if self.old_leader_rejoins:
                self.members[old].update(role='replica', state='streaming', lag=0)
            else:
                self.members[old].update(role='replica', state='stopped', lag=None)

    def handle(self, method, url, body):
        base = next(u for u in self.by_url if url.startswith(u))
        node = self.by_url[base]
        path = url[len(base):]
        self.calls.append((method, node, path, body))
        if node in self.down:
            raise requests.ConnectionError('down')
        if (method, path) in self.refuse:
            return FakeResponse(*self.refuse[(method, path)])
        if method == 'GET':
            data = {'/patroni': lambda: self.node_view(node), '/cluster': self.view,
                    '/config': lambda: dict(self.config, **({'pause': True} if self.paused else {})),
                    '/history': lambda: self.history}.get(path)
            return FakeResponse(200, copy.deepcopy(data())) if data else FakeResponse(404, None)
        timeout = path in self.read_timeout_on
        resp = self._mutate(method, node, path, body or {})
        if timeout:
            raise requests.ReadTimeout('read timeout')
        return resp

    def _mutate(self, method, node, path, body):
        if path == '/switchover' and method == 'POST':
            if body.get('leader') != self.leader:
                return FakeResponse(412, 'leader name does not match')
            if body.get('scheduled_at'):
                self.scheduled = {'at': body['scheduled_at'], 'from': body['leader'], 'to': body.get('candidate')}
                return FakeResponse(202, 'Switchover scheduled')
            cand = body.get('candidate') or next(n for n, m in self.members.items() if m['role'] != 'leader')
            if self.leader_change_works:
                self._promote(cand)
                return FakeResponse(200, f'Successfully switched over to "{cand}"')
            return FakeResponse(503, 'Switchover failed')
        if path == '/switchover' and method == 'DELETE':
            if not self.scheduled:
                return FakeResponse(404, 'no scheduled switchover')
            self.scheduled = None
            return FakeResponse(200, 'scheduled switchover deleted')
        if path == '/failover':
            if self.leader_change_works:
                self._promote(body['candidate'])
                return FakeResponse(200, f'Successfully failed over to "{body["candidate"]}"')
            return FakeResponse(503, 'Failover failed')
        if path == '/config' and method == 'PATCH':
            if 'pause' in body:
                self.paused = bool(body['pause'])
            from patroni_console.dynconf import merge
            if not self.config_patch_ignored:
                self.config = merge(self.config, {k: v for k, v in body.items() if k != 'pause'})
            return FakeResponse(200, self.config)
        if path == '/restart':
            if self.restart_heals:
                self.members[node]['pending_restart'] = False
                return FakeResponse(200, 'restarted successfully')
            self.members[node]['state'] = 'crashed'
            return FakeResponse(503, 'restart failed')
        if path == '/reinitialize':
            self.members[node].update(state='streaming', lag=0, timeline=self.members[self.leader]['timeline'])
            return FakeResponse(200, 'reinitialize started')
        return FakeResponse(404, None)


class _SimSession:
    def __init__(self, sim):
        self.sim = sim
        self.auth = None
        self.verify = True
        self.cert = None

    def request(self, method, url, json=None, **kw):
        return self.sim.handle(method, url, json)

    def post(self, url, **kw):
        return self.request('POST', url, **kw)
