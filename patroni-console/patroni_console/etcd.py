"""Read-only диагностика etcd v3 (3.4+) через HTTP/JSON gateway.

Консоль НИЧЕГО не пишет в etcd: глобальная конфигурация Patroni меняется
только через PATCH /config самого Patroni.
"""
import requests


class EtcdClient:
    def __init__(self, etcd_cfg, timeout=5.0, session=None):
        self.cfg = etcd_cfg
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.verify = etcd_cfg.tls.verify
        if etcd_cfg.tls.cert:
            self.session.cert = etcd_cfg.tls.cert
        self._tokens = {}

    def _post(self, endpoint, path, payload, auth=True):
        headers = {}
        if auth and self.cfg.username:
            headers['Authorization'] = self._token(endpoint)
        r = self.session.post(endpoint + path, json=payload, headers=headers, timeout=self.timeout)
        if r.status_code == 401 and auth and self.cfg.username:
            # токен истёк — получаем новый один раз
            self._tokens.pop(endpoint, None)
            headers['Authorization'] = self._token(endpoint)
            r = self.session.post(endpoint + path, json=payload, headers=headers, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _token(self, endpoint):
        if endpoint not in self._tokens:
            j = self._post(endpoint, '/v3/auth/authenticate',
                           {'name': self.cfg.username, 'password': self.cfg.password or ''}, auth=False)
            self._tokens[endpoint] = j['token']
        return self._tokens[endpoint]

    def endpoint_status(self, endpoint):
        """Состояние одного endpoint: здоровье, версия, лидер raft, размер БД, alarms."""
        out = {'endpoint': endpoint, 'ok': False, 'error': None}
        try:
            st = self._post(endpoint, '/v3/maintenance/status', {})
            hdr = st.get('header') or {}
            out.update({
                'version': st.get('version'),
                'member_id': str(hdr.get('member_id', '')),
                'leader_id': str(st.get('leader', '')),
                'raft_term': int(st.get('raftTerm') or hdr.get('raft_term') or 0),
                'raft_index': int(st.get('raftIndex') or 0),
                'db_size': int(st.get('dbSize') or 0),
                'errors': st.get('errors') or [],
            })
            out['is_leader'] = bool(out['member_id']) and out['member_id'] == out['leader_id']
            al = self._post(endpoint, '/v3/maintenance/alarm', {'action': 'GET'})
            out['alarms'] = [a.get('alarm', '?') for a in (al.get('alarms') or [])]
            out['ok'] = not out['errors'] and not out['alarms']
        except requests.HTTPError as e:
            out['error'] = f'ответ etcd: {e}'[:200]
        except requests.Timeout:
            out['error'] = 'таймаут'
        except requests.RequestException as e:
            out['error'] = f'нет связи ({e.__class__.__name__})'
        except (ValueError, KeyError) as e:
            out['error'] = f'неожиданный ответ etcd: {e}'[:200]
        return out

    def members(self):
        """Список членов кластера etcd (с первого ответившего endpoint)."""
        last = None
        for ep in self.cfg.endpoints:
            try:
                j = self._post(ep, '/v3/cluster/member/list', {})
                return [{'id': str(m.get('ID', '')), 'name': m.get('name', ''),
                         'peer_urls': m.get('peerURLs', []), 'client_urls': m.get('clientURLs', []),
                         'is_learner': bool(m.get('isLearner'))}
                        for m in j.get('members') or []], None
            except (requests.RequestException, ValueError) as e:
                last = f'{ep}: {e.__class__.__name__}'
        return [], last

    def health(self):
        """Сводка: статус каждого endpoint, состав, наличие кворума и единого лидера."""
        if not self.cfg.endpoints:
            return {'configured': False}
        eps = [self.endpoint_status(ep) for ep in self.cfg.endpoints]
        members, merr = self.members()
        leaders = {e['leader_id'] for e in eps if e.get('leader_id') and e['leader_id'] != '0'}
        reachable = sum(1 for e in eps if e['error'] is None)
        total = len(members) or len(eps)
        return {
            'configured': True,
            'endpoints': eps,
            'members': members,
            'members_error': merr,
            'leader_id': next(iter(leaders)) if len(leaders) == 1 else None,
            'split_leader_view': len(leaders) > 1,
            'quorum': reachable > total // 2,
        }
