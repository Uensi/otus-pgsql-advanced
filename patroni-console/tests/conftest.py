import os
import sys

import pytest
from werkzeug.security import generate_password_hash

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from patroni_console import config as cfgmod  # noqa: E402

NODES = [('n1', 'http://10.0.0.1:8008'), ('n2', 'http://10.0.0.2:8008'), ('n3', 'http://10.0.0.3:8008')]
PASSWORDS = {'admin': 'admin-password-1', 'oper': 'oper-password-1', 'viewer': 'viewer-password-1'}
_HASHES = {u: generate_password_hash(p, method='pbkdf2:sha256:1000') for u, p in PASSWORDS.items()}


def raw_config(**over):
    raw = {
        'web': {'secret_key': '${PC_TEST_SECRET}', 'cookie_secure': False, 'timeout': 1},
        'clusters': [{
            'name': 'test',
            'rest': {'scheme': 'http'},
            'max_lag_bytes': 1048576,
            'nodes': [{'name': n, 'url': u} for n, u in NODES],
            'etcd': {'endpoints': ['http://10.0.0.1:2379', 'http://10.0.0.2:2379', 'http://10.0.0.3:2379']},
        }],
        'users': [
            {'name': 'admin', 'role': 'admin', 'password_hash': _HASHES['admin']},
            {'name': 'oper', 'role': 'operator', 'password_hash': _HASHES['oper']},
            {'name': 'viewer', 'role': 'viewer', 'password_hash': _HASHES['viewer']},
        ],
    }
    raw.update(over)
    return raw


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv('PC_TEST_SECRET', 'x' * 40)


@pytest.fixture
def config(tmp_path):
    c = cfgmod.parse(raw_config())
    c.db_path = str(tmp_path / 'test.db')
    return c


def healthy_view():
    """Ответ GET /cluster Patroni 4 для здорового кластера из трёх нод."""
    return {
        'scope': 'test',
        'members': [
            {'name': 'n1', 'role': 'leader', 'state': 'running', 'host': '10.0.0.1', 'port': 5432,
             'api_url': 'http://10.0.0.1:8008/patroni', 'timeline': 5},
            {'name': 'n2', 'role': 'sync_standby', 'state': 'streaming', 'host': '10.0.0.2', 'port': 5432,
             'timeline': 5, 'lag': 0, 'receive_lag': 0, 'replay_lag': 0, 'lsn': '0/5000060'},
            {'name': 'n3', 'role': 'replica', 'state': 'streaming', 'host': '10.0.0.3', 'port': 5432,
             'timeline': 5, 'lag': 4096, 'lsn': '0/4FFF060'},
        ],
    }


def node_views():
    return {
        'n1': {'state': 'running', 'role': 'primary', 'timeline': 5, 'server_version': 170004,
               'patroni': {'version': '4.0.4', 'scope': 'test', 'name': 'n1'}},
        'n2': {'state': 'streaming', 'role': 'replica', 'timeline': 5,
               'patroni': {'version': '4.0.4', 'scope': 'test', 'name': 'n2'}},
        'n3': {'state': 'streaming', 'role': 'replica', 'timeline': 5,
               'patroni': {'version': '4.0.4', 'scope': 'test', 'name': 'n3'}},
    }
