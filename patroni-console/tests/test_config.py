import pytest

from conftest import raw_config
from patroni_console.config import ConfigError, parse


def test_plaintext_user_password_rejected():
    raw = raw_config()
    raw['users'][0] = {'name': 'admin', 'role': 'admin', 'password': 'secret'}
    with pytest.raises(ConfigError, match='password_hash'):
        parse(raw)


def test_secret_must_be_env_reference():
    raw = raw_config()
    raw['clusters'][0]['rest']['password'] = 'plain'
    with pytest.raises(ConfigError, match='переменную окружения'):
        parse(raw)


def test_missing_env_variable(monkeypatch):
    raw = raw_config()
    raw['clusters'][0]['rest']['password'] = '${NO_SUCH_VAR_X}'
    monkeypatch.delenv('NO_SUCH_VAR_X', raising=False)
    with pytest.raises(ConfigError, match='NO_SUCH_VAR_X'):
        parse(raw)


def test_same_nodes_in_two_clusters_rejected():
    raw = raw_config()
    dup = dict(raw['clusters'][0]); dup['name'] = 'prod'
    raw['clusters'].append(dup)
    with pytest.raises(ConfigError, match='указана и в'):
        parse(raw)


def test_short_secret_rejected(monkeypatch):
    monkeypatch.setenv('PC_TEST_SECRET', 'short')
    with pytest.raises(ConfigError, match='secret_key'):
        parse(raw_config())


def test_unknown_role_rejected():
    raw = raw_config()
    raw['users'][0]['role'] = 'dba'
    with pytest.raises(ConfigError, match='роль'):
        parse(raw)
