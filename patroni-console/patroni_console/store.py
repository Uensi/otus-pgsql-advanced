"""SQLite: серверные сессии, аудит, ограничение попыток входа.

Одна база, один процесс. Соединение открывается на каждую операцию:
нагрузка у консоли — единицы запросов в секунду.
"""
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    username    TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('viewer', 'operator', 'admin')),
    csrf        TEXT NOT NULL,
    created_at  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    remote_addr TEXT
);
CREATE INDEX IF NOT EXISTS sessions_username ON sessions(username);

CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    username    TEXT,
    remote_addr TEXT,
    action      TEXT NOT NULL,
    cluster     TEXT,
    params      TEXT NOT NULL DEFAULT '{}',
    outcome     TEXT NOT NULL CHECK (outcome IN ('ok', 'denied', 'error')),
    detail      TEXT,
    request_id  TEXT
);
CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts);

CREATE TABLE IF NOT EXISTS operations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster         TEXT NOT NULL,
    kind            TEXT NOT NULL,
    params          TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL CHECK (status IN
                      ('pending', 'running', 'scheduled', 'succeeded', 'warning', 'failed', 'unknown', 'interrupted', 'cancelled')),
    created_by      TEXT NOT NULL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    idempotency_key TEXT,
    request_hash    TEXT,
    result          TEXT,
    log             TEXT NOT NULL DEFAULT '[]'
);
-- не больше одной выполняющейся операции на кластер
CREATE UNIQUE INDEX IF NOT EXISTS operations_one_active ON operations(cluster) WHERE status IN ('pending', 'running');
CREATE UNIQUE INDEX IF NOT EXISTS operations_idem ON operations(cluster, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS operations_cluster ON operations(cluster, id);

-- журнал событий кластера (наблюдатель)
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster TEXT NOT NULL,
    ts      REAL NOT NULL,
    kind    TEXT NOT NULL CHECK (kind IN ('opened', 'resolved', 'leader_change', 'info')),
    level   TEXT NOT NULL CHECK (level IN ('info', 'warning', 'critical')),
    code    TEXT NOT NULL,
    node    TEXT,
    text    TEXT NOT NULL,
    op_id   INTEGER
);
CREATE INDEX IF NOT EXISTS events_cluster ON events(cluster, id);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);

-- открытые проблемы: переживают перезапуск, чтобы не слать повторные уведомления
CREATE TABLE IF NOT EXISTS conditions (
    cluster   TEXT NOT NULL,
    key       TEXT NOT NULL,
    level     TEXT NOT NULL,
    code      TEXT NOT NULL,
    node      TEXT,
    text      TEXT NOT NULL,
    opened_at REAL NOT NULL,
    event_id  INTEGER NOT NULL,
    PRIMARY KEY (cluster, key)
);

CREATE TABLE IF NOT EXISTS watch_state (
    cluster    TEXT PRIMARY KEY,
    leader     TEXT,
    timeline   INTEGER,
    scheduled  INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);

-- версии глобального конфига Patroni: до и после каждого изменения
CREATE TABLE IF NOT EXISTS config_versions (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster  TEXT NOT NULL,
    ts       REAL NOT NULL,
    source   TEXT NOT NULL CHECK (source IN ('console', 'external')),
    username TEXT,
    op_id    INTEGER,
    before   TEXT NOT NULL,
    after    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS config_versions_cluster ON config_versions(cluster, id);

-- outbox уведомлений: пишется в одной транзакции с событием
CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    channel    TEXT NOT NULL,
    status     TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed')),
    attempts   INTEGER NOT NULL DEFAULT 0,
    next_at    REAL NOT NULL,
    last_error TEXT,
    sent_at    REAL,
    UNIQUE (event_id, channel)
);
CREATE INDEX IF NOT EXISTS notifications_due ON notifications(status, next_at);

CREATE TABLE IF NOT EXISTS login_failures (
    key  TEXT NOT NULL,
    ts   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS login_failures_key ON login_failures(key, ts);
"""

SESSION_IDLE = 2 * 3600        # без активности
SESSION_ABSOLUTE = 12 * 3600   # жёсткий предел
LOGIN_WINDOW = 15 * 60
LOGIN_MAX_PER_KEY = 10


class ClusterBusy(Exception):
    def __init__(self, active):
        super().__init__('на кластере уже выполняется операция')
        self.active = active


class IdempotencyConflict(Exception):
    pass


class Store:
    def __init__(self, path):
        self.path = path
        with self._db() as db:
            db.executescript(SCHEMA)
            cols = {r['name'] for r in db.execute('PRAGMA table_info(watch_state)')}
            if 'config_hash' not in cols:  # база от версии без отслеживания конфига
                db.execute('ALTER TABLE watch_state ADD COLUMN config_hash TEXT')
                db.execute('ALTER TABLE watch_state ADD COLUMN config TEXT')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA journal_mode=WAL')
            yield db
            db.commit()
        finally:
            db.close()

    # ---- сессии ----
    def create_session(self, username, role, remote_addr):
        sid = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        now = time.time()
        with self._db() as db:
            db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?)',
                       (sid, username, role, csrf, now, now, remote_addr))
        return sid, csrf

    def get_session(self, sid, current_role=None):
        """Возвращает живую сессию и продлевает её. current_role — роль из
        текущего конфига: если пользователя удалили или понизили, сессия гаснет."""
        if not sid:
            return None
        now = time.time()
        with self._db() as db:
            row = db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
            if not row:
                return None
            expired = (now - row['last_seen'] > SESSION_IDLE) or (now - row['created_at'] > SESSION_ABSOLUTE)
            role_changed = current_role is not None and current_role(row['username']) != row['role']
            if expired or role_changed:
                db.execute('DELETE FROM sessions WHERE id=?', (sid,))
                return None
            db.execute('UPDATE sessions SET last_seen=? WHERE id=?', (now, sid))
            return dict(row)

    def delete_session(self, sid):
        with self._db() as db:
            db.execute('DELETE FROM sessions WHERE id=?', (sid,))

    # ---- ограничение входа: по аккаунту и по адресу ----
    def login_blocked(self, keys):
        cutoff = time.time() - LOGIN_WINDOW
        with self._db() as db:
            db.execute('DELETE FROM login_failures WHERE ts<?', (cutoff,))
            for k in keys:
                n = db.execute('SELECT count(*) FROM login_failures WHERE key=? AND ts>=?', (k, cutoff)).fetchone()[0]
                if n >= LOGIN_MAX_PER_KEY:
                    return True
        return False

    def login_failed(self, keys):
        now = time.time()
        with self._db() as db:
            db.executemany('INSERT INTO login_failures VALUES (?,?)', [(k, now) for k in keys])

    def login_ok(self, keys):
        with self._db() as db:
            db.executemany('DELETE FROM login_failures WHERE key=?', [(k,) for k in keys])

    # ---- операции ----
    def op_create(self, cluster, kind, params, user, idempotency_key=None, request_hash=None):
        """Создаёт операцию в статусе pending. Возвращает (op, created).
        Повтор с тем же ключом и тем же телом возвращает существующую операцию;
        с другим телом — IdempotencyConflict. Занятый кластер — ClusterBusy."""
        now = time.time()
        with self._db() as db:
            if idempotency_key:
                row = db.execute('SELECT * FROM operations WHERE cluster=? AND idempotency_key=?',
                                 (cluster, idempotency_key)).fetchone()
                if row:
                    if row['request_hash'] != request_hash:
                        raise IdempotencyConflict()
                    return self._op(row), False
            try:
                cur = db.execute(
                    'INSERT INTO operations (cluster, kind, params, status, created_by, created_at, updated_at, '
                    'idempotency_key, request_hash) VALUES (?,?,?,?,?,?,?,?,?)',
                    (cluster, kind, json.dumps(params, ensure_ascii=False), 'pending', user, now, now,
                     idempotency_key, request_hash))
            except sqlite3.IntegrityError:
                active = db.execute("SELECT id, kind FROM operations WHERE cluster=? AND status IN ('pending','running')",
                                    (cluster,)).fetchone()
                raise ClusterBusy(dict(active) if active else None)
            row = db.execute('SELECT * FROM operations WHERE id=?', (cur.lastrowid,)).fetchone()
        return self._op(row), True

    def op_by_key(self, cluster, key):
        with self._db() as db:
            row = db.execute('SELECT * FROM operations WHERE cluster=? AND idempotency_key=?', (cluster, key)).fetchone()
        return (self._op(row), row['request_hash']) if row else (None, None)

    def op_update(self, op_id, status=None, result=None, log_line=None):
        now = time.time()
        with self._db() as db:
            row = db.execute('SELECT log FROM operations WHERE id=?', (op_id,)).fetchone()
            log = json.loads(row['log'])
            if log_line:
                log.append({'ts': now, 'msg': log_line})
            sets, args = ['updated_at=?', 'log=?'], [now, json.dumps(log, ensure_ascii=False)]
            if status:
                sets.append('status=?'); args.append(status)
            if result is not None:
                sets.append('result=?'); args.append(json.dumps(result, ensure_ascii=False))
            db.execute(f'UPDATE operations SET {", ".join(sets)} WHERE id=?', (*args, op_id))

    def op_get(self, op_id):
        with self._db() as db:
            row = db.execute('SELECT * FROM operations WHERE id=?', (op_id,)).fetchone()
        return self._op(row) if row else None

    def op_list(self, cluster, limit=50):
        with self._db() as db:
            rows = db.execute('SELECT * FROM operations WHERE cluster=? ORDER BY id DESC LIMIT ?',
                              (cluster, int(limit))).fetchall()
        return [self._op(r) for r in rows]

    def op_interrupt_stale(self):
        """При старте процесса: операции, оборвавшиеся вместе с прошлым процессом."""
        with self._db() as db:
            db.execute("UPDATE operations SET status='interrupted', updated_at=? WHERE status IN ('pending','running')",
                       (time.time(),))

    @staticmethod
    def _op(row):
        d = dict(row)
        d['params'] = json.loads(d['params'] or '{}')
        d['result'] = json.loads(d['result']) if d['result'] else None
        d['log'] = json.loads(d['log'] or '[]')
        d.pop('request_hash', None)
        return d

    # ---- аудит ----
    def audit(self, action, outcome, username=None, remote_addr=None, cluster=None,
              params=None, detail=None, request_id=None):
        with self._db() as db:
            db.execute('INSERT INTO audit (ts, username, remote_addr, action, cluster, params, outcome, detail, request_id) '
                       'VALUES (?,?,?,?,?,?,?,?,?)',
                       (time.time(), username, remote_addr, action, cluster,
                        json.dumps(params or {}, ensure_ascii=False), outcome,
                        (detail or '')[:2000] or None, request_id))

    def audit_list(self, limit=200):
        with self._db() as db:
            rows = db.execute('SELECT * FROM audit ORDER BY id DESC LIMIT ?', (int(limit),)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d['params'] = json.loads(d['params'] or '{}')
            out.append(d)
        return out

    # ---- события и уведомления ----
    @staticmethod
    def _insert_event(db, cluster, kind, level, code, node, text, op_id, channels, now):
        cur = db.execute('INSERT INTO events (cluster, ts, kind, level, code, node, text, op_id) VALUES (?,?,?,?,?,?,?,?)',
                         (cluster, now, kind, level, code, node, text, op_id))
        eid = cur.lastrowid
        db.executemany('INSERT INTO notifications (event_id, channel, status, next_at) VALUES (?,?,?,?)',
                       [(eid, ch, 'pending', now) for ch in channels])
        return eid

    def event_add(self, cluster, kind, level, code, text, node=None, op_id=None, channels=()):
        now = time.time()
        with self._db() as db:
            eid = self._insert_event(db, cluster, kind, level, code, node, text, op_id, channels, now)
            return dict(db.execute('SELECT * FROM events WHERE id=?', (eid,)).fetchone())

    def conditions_get(self, cluster):
        with self._db() as db:
            rows = db.execute('SELECT * FROM conditions WHERE cluster=?', (cluster,)).fetchall()
        return {r['key']: dict(r) for r in rows}

    def condition_open(self, cluster, key, cond, channels=()):
        """Событие, строка условия и уведомления — одной транзакцией."""
        now = time.time()
        with self._db() as db:
            if db.execute('SELECT 1 FROM conditions WHERE cluster=? AND key=?', (cluster, key)).fetchone():
                return None
            eid = self._insert_event(db, cluster, 'opened', cond['level'], cond['code'], cond.get('node'),
                                     cond['text'], None, channels, now)
            db.execute('INSERT INTO conditions VALUES (?,?,?,?,?,?,?,?)',
                       (cluster, key, cond['level'], cond['code'], cond.get('node'), cond['text'], now, eid))
            return eid

    def condition_close(self, cluster, key, text, channels=()):
        now = time.time()
        with self._db() as db:
            row = db.execute('SELECT * FROM conditions WHERE cluster=? AND key=?', (cluster, key)).fetchone()
            if not row:
                return None
            eid = self._insert_event(db, cluster, 'resolved', row['level'], row['code'], row['node'],
                                     text, None, channels, now)
            db.execute('DELETE FROM conditions WHERE cluster=? AND key=?', (cluster, key))
            return eid

    def watch_state_get(self, cluster):
        with self._db() as db:
            row = db.execute('SELECT * FROM watch_state WHERE cluster=?', (cluster,)).fetchone()
        return dict(row) if row else None

    def watch_state_set(self, cluster, leader, timeline, scheduled):
        with self._db() as db:
            db.execute('INSERT INTO watch_state (cluster, leader, timeline, scheduled, updated_at) VALUES (?,?,?,?,?) '
                       'ON CONFLICT(cluster) DO UPDATE SET leader=excluded.leader, timeline=excluded.timeline, '
                       'scheduled=excluded.scheduled, updated_at=excluded.updated_at',
                       (cluster, leader, timeline, int(bool(scheduled)), time.time()))

    def events_list(self, cluster, limit=100):
        with self._db() as db:
            rows = db.execute('SELECT * FROM events WHERE cluster=? ORDER BY id DESC LIMIT ?', (cluster, limit)).fetchall()
            out = [dict(r) for r in rows]
            if out:
                ids = [e['id'] for e in out]
                q = f'SELECT event_id, channel, status, attempts, last_error FROM notifications WHERE event_id IN ({",".join("?" * len(ids))})'
                by = {}
                for n in db.execute(q, ids).fetchall():
                    by.setdefault(n['event_id'], []).append(dict(n))
                for e in out:
                    e['notifications'] = [{k: v for k, v in n.items() if k != 'event_id'} for n in by.get(e['id'], [])]
        return out

    def events_trim(self, keep_days):
        cutoff = time.time() - keep_days * 86400
        with self._db() as db:
            db.execute('DELETE FROM notifications WHERE event_id IN (SELECT id FROM events WHERE ts<?)', (cutoff,))
            db.execute('DELETE FROM events WHERE ts<? AND id NOT IN (SELECT event_id FROM conditions)', (cutoff,))

    def op_recent(self, cluster, kinds, since):
        """Операция смены лидера, которая идёт сейчас или закончилась недавно."""
        with self._db() as db:
            row = db.execute(
                f'SELECT * FROM operations WHERE cluster=? AND kind IN ({",".join("?" * len(kinds))}) '
                "AND (status IN ('pending','running') OR updated_at>=?) ORDER BY id DESC LIMIT 1",
                (cluster, *kinds, since)).fetchone()
        return self._op(row) if row else None

    def notify_due(self, now, limit=20):
        with self._db() as db:
            rows = db.execute(
                'SELECT n.id, n.channel, n.attempts, e.* FROM notifications n JOIN events e ON e.id=n.event_id '
                "WHERE n.status='pending' AND n.next_at<=? ORDER BY n.id LIMIT ?", (now, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            out.append({'id': r[0], 'channel': d['channel'], 'attempts': d['attempts'],
                        'event': {k: d[k] for k in ('cluster', 'ts', 'kind', 'level', 'code', 'node', 'text', 'op_id')}
                        | {'id': r[3]}})
        return out

    def notify_sent(self, nid):
        with self._db() as db:
            db.execute("UPDATE notifications SET status='sent', attempts=attempts+1, sent_at=?, last_error=NULL WHERE id=?",
                       (time.time(), nid))

    def notify_failed(self, nid, error, next_at=None):
        """next_at=None — попытки исчерпаны."""
        with self._db() as db:
            db.execute('UPDATE notifications SET attempts=attempts+1, last_error=?, status=?, next_at=COALESCE(?, next_at) '
                       'WHERE id=?', (error[:500], 'failed' if next_at is None else 'pending', next_at, nid))

    # ---- версии глобального конфига ----
    def config_watch_get(self, cluster):
        with self._db() as db:
            row = db.execute('SELECT config_hash, config FROM watch_state WHERE cluster=?', (cluster,)).fetchone()
        if not row or row['config'] is None:
            return None, None
        return row['config_hash'], json.loads(row['config'])

    def config_watch_set(self, cluster, h, cfg):
        with self._db() as db:
            db.execute('INSERT INTO watch_state (cluster, updated_at, config_hash, config) VALUES (?,?,?,?) '
                       'ON CONFLICT(cluster) DO UPDATE SET config_hash=excluded.config_hash, config=excluded.config',
                       (cluster, time.time(), h, json.dumps(cfg, ensure_ascii=False)))

    def config_version_add(self, cluster, source, before, after, username=None, op_id=None):
        with self._db() as db:
            cur = db.execute('INSERT INTO config_versions (cluster, ts, source, username, op_id, before, after) '
                             'VALUES (?,?,?,?,?,?,?)',
                             (cluster, time.time(), source, username, op_id,
                              json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False)))
            return cur.lastrowid

    def config_versions(self, cluster, limit=50):
        with self._db() as db:
            rows = db.execute('SELECT * FROM config_versions WHERE cluster=? ORDER BY id DESC LIMIT ?',
                              (cluster, limit)).fetchall()
        return [dict(r, before=json.loads(r['before']), after=json.loads(r['after'])) for r in rows]

    def config_version(self, cluster, vid):
        with self._db() as db:
            r = db.execute('SELECT * FROM config_versions WHERE cluster=? AND id=?', (cluster, vid)).fetchone()
        return dict(r, before=json.loads(r['before']), after=json.loads(r['after'])) if r else None
