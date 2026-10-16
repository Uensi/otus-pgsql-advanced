"""Наблюдатель: раз в interval секунд снимает состояние каждого кластера,
открывает и закрывает «условия» (проблемы) и пишет события.

Условие — это предупреждение уровня warning/critical из снимка статуса или
проблема etcd. Ключ условия — код + нода, поэтому изменение текста (например,
величины лага) не порождает новых событий. Чтобы не реагировать на дребезг,
условие открывается, только если продержалось `confirm` проверок подряд,
и закрывается, только если `confirm` проверок подряд его нет.

Открытые условия и последний известный лидер хранятся в SQLite, поэтому
перезапуск консоли не приводит к повторным уведомлениям.
"""
import logging
import threading
import time

from . import dynconf
from . import status as status_mod
from .patroni import PatroniError
from .notify import channels_for

log = logging.getLogger('patroni_console.watcher')

LEADER_OPS = ('switchover', 'failover', 'rolling_restart')


def conditions_from(snap, etcd):
    conds = {}
    for w in snap.get('warnings') or []:
        if w['level'] in ('warning', 'critical'):
            conds[f"{w['code']}:{w.get('node') or ''}"] = {
                'level': w['level'], 'code': w['code'], 'node': w.get('node'), 'text': w['text']}
    if etcd and etcd.get('configured'):
        if not etcd.get('quorum'):
            conds['etcd_quorum:'] = {'level': 'critical', 'code': 'etcd_quorum', 'node': None,
                                    'text': 'etcd: нет кворума'}
        if etcd.get('split_leader_view'):
            conds['etcd_split_leader:'] = {'level': 'critical', 'code': 'etcd_split_leader', 'node': None,
                                          'text': 'etcd: endpoint-ы называют разных лидеров raft'}
        for ep in etcd.get('endpoints') or []:
            if ep.get('error'):
                conds[f"etcd_endpoint:{ep['endpoint']}"] = {
                    'level': 'warning', 'code': 'etcd_endpoint', 'node': ep['endpoint'],
                    'text': f"etcd {ep['endpoint']}: {ep['error']}"}
            for a in ep.get('alarms') or []:
                conds[f"etcd_alarm:{a}"] = {'level': 'critical', 'code': 'etcd_alarm', 'node': None,
                                           'text': f'etcd: alarm {a}'}
    return conds


class Watcher:
    def __init__(self, store, config, patroni_client, etcd_client, pool=None, clock=time.time, on_event=None):
        self.store = store
        self.config = config
        self.patroni_client = patroni_client      # cluster -> PatroniClient
        self.etcd_client = etcd_client            # cluster -> EtcdClient
        self.pool = pool
        self.clock = clock
        self.on_event = on_event or (lambda: None)  # разбудить отправщик уведомлений
        self._seen = {}       # cluster -> {key: подряд замечено}
        self._missing = {}    # cluster -> {key: подряд не замечено}
        self.state = {}       # cluster -> {'last_run', 'error', 'ms'}
        self._stop = threading.Event()
        self._thread = None
        self._last_trim = 0

    # ---- один проход по кластеру ----
    def tick(self, cluster):
        t0 = time.monotonic()
        try:
            pc = self.patroni_client(cluster)
            snap = status_mod.collect(cluster, pc, self.pool)
            etcd = self.etcd_client(cluster).health() if cluster.etcd.endpoints else None
            self.evaluate(cluster, snap, etcd)
            self.check_config(cluster, snap, pc)
            self.state[cluster.name] = {'last_run': self.clock(), 'error': None,
                                        'ms': round((time.monotonic() - t0) * 1000)}
        except Exception as e:  # наблюдатель не должен умирать из-за одного кластера
            log.exception('наблюдатель: ошибка проверки кластера', extra={'cluster': cluster.name})
            self.state[cluster.name] = {'last_run': self.clock(), 'error': str(e)[:300], 'ms': None}

    def _channels(self, level):
        return channels_for(level, self.config.channels)

    def evaluate(self, cluster, snap, etcd):
        """Сравнивает снимок с сохранённым состоянием и пишет события. Возвращает id новых событий."""
        name = cluster.name
        confirm = self.config.watch.confirm
        seen = self._seen.setdefault(name, {})
        missing = self._missing.setdefault(name, {})
        current = conditions_from(snap, etcd)
        opened = self.store.conditions_get(name)
        new_events = []

        for key, c in current.items():
            missing.pop(key, None)
            if key in opened:
                continue
            seen[key] = seen.get(key, 0) + 1
            if seen[key] >= confirm:
                eid = self.store.condition_open(name, key, c, self._channels(c['level']))
                seen.pop(key, None)
                if eid:
                    new_events.append(eid)
        for key in list(seen):
            if key not in current:
                seen.pop(key)
        for key, row in opened.items():
            if key in current:
                continue
            missing[key] = missing.get(key, 0) + 1
            if missing[key] >= confirm:
                eid = self.store.condition_close(name, key, 'устранено: ' + row['text'], self._channels(row['level']))
                missing.pop(key, None)
                if eid:
                    new_events.append(eid)

        new_events += self._leader(cluster, snap)
        if new_events:
            self.on_event()
        return new_events

    def _leader(self, cluster, snap):
        name = cluster.name
        leader = snap.get('leader')
        if not leader:
            return []          # отсутствие лидера — отдельное условие no_leader
        timeline = max(snap.get('timelines') or [0]) or None
        scheduled = bool(snap.get('scheduled_switchover'))
        prev = self.store.watch_state_get(name)
        self.store.watch_state_set(name, leader, timeline, scheduled)
        if not prev or not prev['leader'] or prev['leader'] == leader:
            return []
        since = self.clock() - max(3 * self.config.watch.interval, 60)
        op = next((o for o in self.store.ops_recent(name, LEADER_OPS, since)
                   if self._explains(o, prev['leader'], leader)), None)
        tl = f', таймлайн {timeline}' if timeline else ''
        if op:
            level, op_id = 'info', op['id']
            text = f"лидер сменился: {prev['leader']} → {leader}{tl}. Это операция №{op['id']} ({op['kind']}, {op['created_by']})"
        elif prev['scheduled']:
            level, op_id = 'info', None
            text = f"лидер сменился по расписанию: {prev['leader']} → {leader}{tl}"
        else:
            level, op_id = 'critical', None
            text = f"незапланированная смена лидера (failover): {prev['leader']} → {leader}{tl}"
        ev = self.store.event_add(name, 'leader_change', level, 'leader_change', text, node=leader,
                                  op_id=op_id, channels=self._channels(level))
        return [ev['id']]

    @staticmethod
    def _explains(op, old, new):
        """Объясняет ли операция консоли именно эту смену лидера.

        Идущая операция объясняет любую смену. Завершённая — только ту, которую она
        сама сделала: иначе авария через минуту после switchover сошла бы за него.
        Каждая операция объясняет не больше одной смены (это отбирает store.ops_recent):
        если Patroni после аварии выбрал того же лидера, что и прошлый switchover,
        тот switchover уже «израсходован»."""
        if op['status'] in ('pending', 'running'):
            return True
        res = op.get('result') or {}
        return res.get('new_leader') == new and res.get('old_leader') in (None, old)

    def check_config(self, cluster, snap, client):
        """Замечает изменения глобального конфига, сделанные не через консоль (patronictl edit-config и т.п.)."""
        name = cluster.name
        order = sorted(cluster.nodes, key=lambda n: n.name != snap.get('leader'))
        cfg = None
        for n in order:
            try:
                cfg = client.dynamic_config(n)
                break
            except PatroniError:
                continue
        if cfg is None:
            return None
        cfg = {k: v for k, v in cfg.items() if k != 'pause'}
        h = dynconf.config_hash(cfg)
        old_h, old = self.store.config_watch_get(name)
        if old_h == h:
            return None
        active = self.store.op_recent(name, ('config',), self.clock())
        if active and active['status'] in ('pending', 'running'):
            return None            # операция консоли ещё идёт: решим на следующей проверке
        self.store.config_watch_set(name, h, cfg)
        if old is None:
            return None
        # своё изменение узнаём по содержимому: текущий конфиг совпадает с последней версией консоли
        last = next(iter(self.store.config_versions(name, 1)), None)
        if last and last['source'] == 'console' and \
                dynconf.config_hash({k: v for k, v in last['after'].items() if k != 'pause'}) == h:
            return None
        # точка отсчёта — последнее известное состояние: после изменения из консоли это его результат
        base = {k: v for k, v in last['after'].items() if k != 'pause'} if last and last['source'] == 'console' else old
        rows = dynconf.diff(base, cfg)
        if not rows:
            return None
        vid = self.store.config_version_add(name, 'external', base, cfg)
        summary = '; '.join(f"{r['path']}: {r['old']} → {r['new']}" for r in rows[:5])
        more = f' и ещё {len(rows) - 5}' if len(rows) > 5 else ''
        ev = self.store.event_add(name, 'info', 'warning', 'config_external',
                                  f'глобальный конфиг изменён вне консоли (версия №{vid}): {summary}{more}',
                                  channels=self._channels('warning'))
        self.on_event()
        return ev['id']

    # ---- фоновый цикл ----
    def run_forever(self):
        interval = self.config.watch.interval
        while not self._stop.is_set():
            start = time.monotonic()
            for c in self.config.clusters.values():
                if self._stop.is_set():
                    break
                self.tick(c)
            if time.time() - self._last_trim > 3600:
                try:
                    self.store.housekeeping(self.config.watch.keep_days)
                    self._last_trim = time.time()
                except Exception:
                    log.exception('наблюдатель: не удалось почистить старые события')
            self._stop.wait(max(0.5, interval - (time.monotonic() - start)))

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self.run_forever, name='watcher', daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
