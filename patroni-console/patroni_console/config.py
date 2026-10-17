"""Загрузка и проверка конфигурации.

Секреты в YAML не хранятся: вместо значения пишется ссылка на переменную
окружения `${ИМЯ}`. Пароли пользователей консоли хранятся только хешами
(werkzeug: `pbkdf2:...` или `scrypt:...`), см. `python -m patroni_console.hashpw`.
"""
import os
import re
from dataclasses import dataclass, field

import yaml

ROLES = ('viewer', 'operator', 'admin')
LEVELS = ('info', 'warning', 'critical')
CHANNEL_TYPES = ('webhook', 'telegram')
_ENV_RE = re.compile(r'^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$')
_NAME_RE = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')


class ConfigError(ValueError):
    pass


def _secret(value, where):
    """`${VAR}` -> значение переменной окружения; пустая строка/None -> None."""
    if value in (None, ''):
        return None
    if not isinstance(value, str):
        raise ConfigError(f'{where}: ожидается строка')
    m = _ENV_RE.match(value.strip())
    if not m:
        raise ConfigError(f'{where}: секрет задаётся только ссылкой на переменную окружения ${{VAR}}')
    v = os.environ.get(m.group(1))
    if v is None:
        raise ConfigError(f'{where}: переменная окружения {m.group(1)} не задана')
    return v


@dataclass
class TLS:
    verify: object = True          # True | False | путь к CA
    cert: object = None            # None | путь | (cert, key)

    @classmethod
    def parse(cls, d, where):
        d = d or {}
        ca = d.get('ca_file')
        verify = d.get('verify', True)
        if ca:
            verify = ca
        cert = None
        if d.get('cert_file'):
            cert = (d['cert_file'], d['key_file']) if d.get('key_file') else d['cert_file']
        return cls(verify=verify, cert=cert)


@dataclass
class Node:
    name: str
    url: str                        # базовый URL REST API Patroni, напр. https://10.0.0.1:8008


@dataclass
class EtcdConfig:
    endpoints: list = field(default_factory=list)
    username: str = None
    password: str = None
    tls: TLS = field(default_factory=TLS)


@dataclass
class Cluster:
    name: str
    nodes: list
    rest_username: str = None
    rest_password: str = None
    tls: TLS = field(default_factory=TLS)
    etcd: EtcdConfig = field(default_factory=EtcdConfig)
    max_lag_bytes: int = 16 * 1024 * 1024   # порог предупреждения о лаге


@dataclass
class User:
    name: str
    password_hash: str
    role: str


@dataclass
class WatchConfig:
    interval: float = 10.0     # секунд между проверками кластера
    confirm: int = 2           # сколько проверок подряд условие должно держаться, чтобы открыться/закрыться
    keep_days: int = 90        # сколько хранить события


@dataclass
class Channel:
    name: str
    type: str                  # webhook | telegram
    min_level: str = 'warning'
    url: str = None            # webhook
    token: str = None          # webhook: Bearer-токен; telegram: токен бота
    chat_id: str = None        # telegram
    timeout: float = 10.0


@dataclass
class Config:
    clusters: dict
    users: dict
    secret_key: str
    db_path: str = 'patroni_console.db'
    timeout: float = 5.0
    cookie_secure: bool = True
    watch: WatchConfig = field(default_factory=WatchConfig)
    channels: list = field(default_factory=list)


def _env_or_plain(value, where):
    """Значение может быть ссылкой ${VAR} (тогда берётся из окружения) или обычной строкой."""
    if isinstance(value, str) and _ENV_RE.match(value.strip()):
        return _secret(value, where)
    return value


def _parse_watch(raw):
    w = raw or {}
    cfg = WatchConfig(interval=float(w.get('interval', 10)), confirm=int(w.get('confirm', 2)),
                      keep_days=int(w.get('keep_days', 90)))
    if cfg.interval < 2:
        raise ConfigError('watch.interval: не меньше 2 секунд')
    if cfg.confirm < 1:
        raise ConfigError('watch.confirm: не меньше 1')
    return cfg


def _parse_channels(raw):
    channels, names = [], set()
    for i, c in enumerate(raw or []):
        where = f'notify[{i}]'
        typ = c.get('type')
        if typ not in CHANNEL_TYPES:
            raise ConfigError(f'{where}: type должен быть одним из {CHANNEL_TYPES}')
        name = c.get('name') or f'{typ}-{i}'
        if not _NAME_RE.match(name) or name in names:
            raise ConfigError(f'{where}: недопустимое или повторяющееся имя канала {name!r}')
        names.add(name)
        level = c.get('min_level', 'warning')
        if level not in LEVELS:
            raise ConfigError(f'{where}: min_level должен быть одним из {LEVELS}')
        ch = Channel(name=name, type=typ, min_level=level, timeout=float(c.get('timeout', 10)))
        if typ == 'webhook':
            ch.url = _env_or_plain(c.get('url'), f'{where}.url')
            if not ch.url or not str(ch.url).startswith(('https://', 'http://')):
                raise ConfigError(f'{where}: нужен url http(s)')
            ch.token = _secret(c.get('token'), f'{where}.token')
        else:
            ch.token = _secret(c.get('bot_token'), f'{where}.bot_token')
            ch.chat_id = str(c.get('chat_id') or '')
            if not ch.token or not ch.chat_id:
                raise ConfigError(f'{where}: для telegram нужны bot_token (через ${{VAR}}) и chat_id')
            ch.url = str(c.get('api_url', 'https://api.telegram.org')).rstrip('/')
        channels.append(ch)
    return channels


def _node_url(n, scheme, where):
    if n.get('url'):
        return str(n['url']).rstrip('/')
    host = n.get('host')
    if not host:
        raise ConfigError(f'{where}: нужен url или host')
    return f"{scheme}://{host}:{int(n.get('port', 8008))}"


def parse(raw):
    if not isinstance(raw, dict):
        raise ConfigError('конфиг должен быть YAML-объектом')

    clusters = {}
    for i, c in enumerate(raw.get('clusters') or []):
        where = f'clusters[{i}]'
        name = c.get('name') or ''
        if not _NAME_RE.match(name):
            raise ConfigError(f'{where}: недопустимое имя кластера {name!r}')
        if name in clusters:
            raise ConfigError(f'{where}: кластер {name} указан дважды')
        rest = c.get('rest') or {}
        scheme = rest.get('scheme', 'https')
        nodes = []
        for j, n in enumerate(c.get('nodes') or []):
            nw = f'{where}.nodes[{j}]'
            nn = n.get('name') or ''
            if not _NAME_RE.match(nn):
                raise ConfigError(f'{nw}: недопустимое имя ноды {nn!r}')
            nodes.append(Node(name=nn, url=_node_url(n, scheme, nw)))
        if not nodes:
            raise ConfigError(f'{where}: нет нод')
        e = c.get('etcd') or {}
        etcd = EtcdConfig(
            endpoints=[str(u).rstrip('/') for u in (e.get('endpoints') or [])],
            username=e.get('username'),
            password=_secret(e.get('password'), f'{where}.etcd.password'),
            tls=TLS.parse(e.get('tls'), f'{where}.etcd.tls'),
        )
        clusters[name] = Cluster(
            name=name, nodes=nodes,
            rest_username=rest.get('username'),
            rest_password=_secret(rest.get('password'), f'{where}.rest.password'),
            tls=TLS.parse(rest.get('tls'), f'{where}.rest.tls'),
            etcd=etcd,
            max_lag_bytes=int(c.get('max_lag_bytes', 16 * 1024 * 1024)),
        )
    if not clusters:
        raise ConfigError('не задано ни одного кластера')

    # один и тот же адрес ноды в двух кластерах почти всегда ошибка в конфиге
    seen = {}
    for cl in clusters.values():
        for n in cl.nodes:
            if n.url in seen and seen[n.url] != cl.name:
                raise ConfigError(f'нода {n.url} указана и в {seen[n.url]}, и в {cl.name}')
            seen[n.url] = cl.name

    users = {}
    for i, u in enumerate(raw.get('users') or []):
        where = f'users[{i}]'
        name = u.get('name') or ''
        h = u.get('password_hash') or ''
        role = u.get('role') or ''
        if not _NAME_RE.match(name):
            raise ConfigError(f'{where}: недопустимое имя')
        if role not in ROLES:
            raise ConfigError(f'{where}: роль должна быть одной из {ROLES}')
        if not h.startswith(('pbkdf2:', 'scrypt:')):
            raise ConfigError(f'{where}: нужен password_hash (pbkdf2:/scrypt:), открытые пароли не принимаются')
        users[name] = User(name=name, password_hash=h, role=role)
    if not users:
        raise ConfigError('не задано ни одного пользователя')

    web = raw.get('web') or {}
    secret = _secret(web.get('secret_key'), 'web.secret_key')
    if not secret or len(secret) < 32:
        raise ConfigError('web.secret_key обязателен (≥32 символов, через ${VAR})')

    return Config(
        clusters=clusters, users=users, secret_key=secret,
        db_path=web.get('db_path', 'patroni_console.db'),
        timeout=float(web.get('timeout', 5)),
        cookie_secure=bool(web.get('cookie_secure', True)),
        watch=_parse_watch(raw.get('watch')),
        channels=_parse_channels(raw.get('notify')),
    )


def load(path):
    with open(path, encoding='utf-8') as f:
        return parse(yaml.safe_load(f))
