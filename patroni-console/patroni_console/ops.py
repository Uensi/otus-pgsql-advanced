"""Операции с кластером: проверки перед запуском и исполнитель с контролем результата.

Правила:
  * перед каждой операцией строится свежий снимок кластера и список проверок;
    проверка уровня `block` запрещает запуск, `warn` требует явного согласия,
    `force` — роли admin и флага force;
  * изменяющий запрос к Patroni отправляется ровно один раз и никогда не повторяется;
    если ответ не пришёл, операция переходит в контроль результата по снимкам;
  * успех операции определяется по состоянию кластера, а не по коду ответа.
"""
import logging
import time
from datetime import datetime, timezone

from . import dynconf
from . import status as status_mod
from .patroni import PatroniError, PatroniUnknownOutcome

log = logging.getLogger(__name__)

LEADER_ROLES = status_mod.LEADER_ROLES
HEALTHY = status_mod.REPLICA_OK_STATES
DEFAULT_MAX_LAG_ON_FAILOVER = 1048576   # значение Patroni по умолчанию

KINDS = {
    # kind: минимальная роль
    'switchover': 'operator',
    'cancel_switchover': 'operator',
    'pause': 'operator',
    'resume': 'operator',
    'restart': 'operator',
    'rolling_restart': 'operator',
    'failover': 'admin',
    'reinitialize': 'admin',
    'config': 'admin',
}


class Timings:
    poll = 2.0               # период опроса при контроле результата
    leader_change = 120.0    # ждать смены лидера
    rejoin = 180.0           # ждать возврата старого лидера репликой
    restart = 300.0          # ждать готовности ноды после рестарта
    reinit = 3600.0          # ждать окончания reinit
    pause = 30.0
    config = 30.0            # ждать, пока новый конфиг станет виден в /config


def _c(level, code, text):
    return {'level': level, 'code': code, 'text': text}


def _member(snap, name):
    return next((m for m in snap['members'] if m['name'] == name), None)


def _leader(snap):
    return _member(snap, snap['leader']) if snap.get('leader') else None


def _eligible(m):
    tags = m.get('tags') or {}
    return (m.get('in_dcs') and m.get('role') not in LEADER_ROLES and m.get('state') in HEALTHY
            and not tags.get('nofailover') and tags.get('failover_priority') != 0)


def _parse_time(value):
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else None


def _fmt(n):
    return status_mod.human_bytes(n) if n is not None else 'неизвестен'


# ---------------- проверки ----------------
def checks(kind, snap, dyn, params):
    """Возвращает список проверок. Никаких побочных эффектов."""
    fn = {
        'switchover': _check_switchover, 'failover': _check_failover,
        'cancel_switchover': _check_cancel, 'pause': _check_pause, 'resume': _check_resume,
        'restart': _check_restart, 'rolling_restart': _check_rolling, 'reinitialize': _check_reinit,
        'config': _check_config,
    }[kind]
    out = fn(snap, dyn or {}, params)
    if not snap.get('view_from'):
        out.insert(0, _c('block', 'no_dcs_view', 'нет данных о составе кластера: ни одна нода не ответила'))
    return out


def _check_leader_healthy(snap, out):
    ld = _leader(snap)
    if not ld:
        out.append(_c('block', 'no_leader', 'в кластере нет единственного лидера'))
        return None
    if not ld.get('reachable') or ld.get('state') != 'running':
        out.append(_c('block', 'leader_unhealthy', f'лидер {ld["name"]} недоступен или не в состоянии running'))
    return ld


def _check_candidate(snap, dyn, name, out, sync_required):
    m = _member(snap, name)
    if not m or not m.get('in_dcs'):
        out.append(_c('block', 'candidate_unknown', f'кандидата {name} нет в кластере'))
        return None
    if m.get('role') in LEADER_ROLES:
        out.append(_c('block', 'candidate_is_leader', f'{name} уже лидер'))
        return None
    tags = m.get('tags') or {}
    if tags.get('nofailover') or tags.get('failover_priority') == 0:
        out.append(_c('block', 'candidate_nofailover', f'у {name} тег nofailover или failover_priority=0'))
    if m.get('state') not in HEALTHY:
        out.append(_c('block', 'candidate_state', f'{name} в состоянии {m.get("state")}'))
    if not m.get('reachable'):
        out.append(_c('block', 'candidate_unreachable', f'REST API {name} недоступен'))
    max_lag = dyn.get('maximum_lag_on_failover', DEFAULT_MAX_LAG_ON_FAILOVER)
    if m.get('lag') is None:
        out.append(_c('block', 'candidate_lag_unknown', f'лаг {name} неизвестен'))
    elif max_lag is not None and m['lag'] > max_lag:
        out.append(_c('block', 'candidate_lag', f'лаг {name} {_fmt(m["lag"])} больше maximum_lag_on_failover {_fmt(max_lag)}'))
    ld = _leader(snap)
    if ld and m.get('timeline') is not None and ld.get('timeline') is not None and m['timeline'] != ld['timeline']:
        out.append(_c('block', 'candidate_timeline', f'таймлайн {name} ({m["timeline"]}) отличается от лидера ({ld["timeline"]})'))
    if sync_required and m.get('role') not in ('sync_standby', 'quorum_standby'):
        out.append(_c('block', 'candidate_not_sync', f'включён synchronous_mode, а {name} не синхронная реплика'))
    if m.get('pending_restart'):
        out.append(_c('warn', 'candidate_pending_restart', f'{name} ждёт рестарта для применения параметров'))
    return m


def _check_switchover(snap, dyn, p):
    out = []
    _check_leader_healthy(snap, out)
    if snap.get('scheduled_switchover'):
        out.append(_c('block', 'already_scheduled', 'уже запланирован switchover; сначала отмените его'))
    cand = p.get('candidate')
    sync = bool(dyn.get('synchronous_mode'))
    if snap.get('paused'):
        if not cand:
            out.append(_c('block', 'paused_needs_candidate', 'кластер на паузе: укажите кандидата явно'))
        else:
            out.append(_c('warn', 'paused', 'кластер на паузе: автоматического failover не будет'))
    if cand:
        _check_candidate(snap, dyn, cand, out, sync)
    else:
        pool = [m for m in snap['members'] if _eligible(m)]
        if sync:
            pool = [m for m in pool if m.get('role') in ('sync_standby', 'quorum_standby')]
        if not pool:
            out.append(_c('block', 'no_candidates', 'нет реплики, пригодной для switchover'))
    if p.get('scheduled_at'):
        t = _parse_time(p['scheduled_at'])
        if not t:
            out.append(_c('block', 'bad_time', 'время switchover — ISO 8601 с часовым поясом, напр. 2026-10-17T03:00:00+03:00'))
        elif t <= datetime.now(timezone.utc):
            out.append(_c('block', 'time_in_past', 'время switchover уже прошло'))
    return out


def _check_failover(snap, dyn, p):
    out = []
    cand = p.get('candidate')
    if not cand:
        return [_c('block', 'candidate_required', 'для failover кандидат обязателен')]
    ld = _leader(snap)
    if ld and ld.get('reachable') and ld.get('state') == 'running':
        out.append(_c('force', 'leader_healthy',
                      f'лидер {ld["name"]} работает. Для планового переключения используйте switchover; '
                      'принудительный failover при живом лидере может потерять транзакции'))
    m = _check_candidate(snap, dyn, cand, out, sync_required=False)
    # при failover лаг и таймлайн — не запрет, а оценка потерь: Patroni сам решит
    for c in out:
        if c['code'] in ('candidate_lag', 'candidate_lag_unknown', 'candidate_timeline'):
            c['level'] = 'warn'
    if m and m.get('lag'):
        out.append(_c('warn', 'data_loss', f'{cand} отстаёт на {_fmt(m["lag"])}: эти данные могут быть потеряны'))
    if snap.get('paused'):
        out.append(_c('warn', 'paused', 'кластер на паузе'))
    return out


def _check_cancel(snap, dyn, p):
    if not snap.get('scheduled_switchover'):
        return [_c('block', 'nothing_scheduled', 'запланированного switchover нет')]
    return []


def _check_pause(snap, dyn, p):
    out = []
    if snap.get('paused'):
        out.append(_c('block', 'already_paused', 'кластер уже на паузе'))
    out.append(_c('warn', 'no_auto_failover', 'пока кластер на паузе, Patroni не переключит лидера при аварии'))
    return out


def _check_resume(snap, dyn, p):
    if not snap.get('paused'):
        return [_c('block', 'not_paused', 'кластер не на паузе')]
    out = []
    for m in snap['members']:
        if m.get('in_dcs') and m.get('state') not in HEALTHY:
            out.append(_c('warn', 'unhealthy_member', f'{m["name"]} в состоянии {m.get("state")}: после снятия паузы Patroni начнёт его чинить'))
    return out


def _check_restart(snap, dyn, p):
    out = []
    node = p.get('node')
    m = _member(snap, node)
    if not m or m.get('unconfigured'):
        return [_c('block', 'node_unknown', f'ноды {node} нет в конфиге')]
    if not m.get('reachable'):
        out.append(_c('block', 'node_unreachable', f'REST API {node} недоступен'))
    if m.get('role') in LEADER_ROLES:
        out.append(_c('force', 'restart_leader',
                      f'{node} — лидер: рестарт остановит запись. Лучше rolling restart (сначала switchover)'))
    elif not [x for x in snap['members'] if x.get('in_dcs') and x.get('state') in HEALTHY and x['name'] != node]:
        out.append(_c('warn', 'last_healthy', 'других здоровых нод нет'))
    if not m.get('pending_restart'):
        out.append(_c('warn', 'not_pending', f'{node} не ждёт рестарта'))
    return out


def _check_rolling(snap, dyn, p):
    out = []
    _check_leader_healthy(snap, out)
    for m in snap['members']:
        if m.get('in_dcs') and not m.get('reachable'):
            out.append(_c('block', 'node_unreachable', f'REST API {m["name"]} недоступен'))
        elif m.get('in_dcs') and m.get('state') not in HEALTHY | {'running'}:
            out.append(_c('block', 'node_unhealthy', f'{m["name"]} в состоянии {m.get("state")}'))
    targets = _rolling_targets(snap, p.get('only_pending', True))
    if not targets['replicas'] and not targets['leader']:
        out.append(_c('block', 'nothing_to_do', 'ни одна нода не ждёт рестарта'))
    if targets['leader'] and not [m for m in snap['members'] if _eligible(m)]:
        out.append(_c('block', 'no_candidates', 'лидер ждёт рестарта, но реплики для switchover нет'))
    if snap.get('paused'):
        out.append(_c('block', 'paused', 'кластер на паузе'))
    return out


def _check_reinit(snap, dyn, p):
    out = []
    node = p.get('node')
    m = _member(snap, node)
    if not m or m.get('unconfigured'):
        return [_c('block', 'node_unknown', f'ноды {node} нет в конфиге')]
    if m.get('role') in LEADER_ROLES:
        out.append(_c('block', 'reinit_leader', 'reinit лидера невозможен'))
    if not m.get('reachable'):
        out.append(_c('block', 'node_unreachable', f'REST API {node} недоступен'))
    if not _leader(snap):
        out.append(_c('block', 'no_leader', 'нет лидера, с которого копировать данные'))
    out.append(_c('warn', 'data_wiped', f'каталог данных {node} будет удалён и скопирован заново с лидера'))
    return out


def _check_config(snap, dyn, p):
    out = dynconf.checks(dyn, p['changes'], p.get('base_hash'))
    if p.get('rollback_to'):
        out.append(_c('warn', 'rollback', f'откат конфига к состоянию до версии №{p["rollback_to"]}'))
    if not snap.get('leader'):
        out.append(_c('warn', 'no_leader', 'в кластере нет лидера: изменение применится, когда лидер появится'))
    return out


def _rolling_targets(snap, only_pending):
    reps = [m['name'] for m in snap['members']
            if m.get('in_dcs') and not m.get('unconfigured') and m.get('role') not in LEADER_ROLES
            and (m.get('pending_restart') or not only_pending)]
    ld = _leader(snap)
    lead = bool(ld and (ld.get('pending_restart') or not only_pending))
    return {'replicas': reps, 'leader': ld['name'] if lead else None}


def gate(cks, acknowledge_warnings, force, role):
    """Можно ли запускать: (ok, код_ошибки, сообщение)."""
    if any(c['level'] == 'block' for c in cks):
        return False, 'precheck_failed', 'проверки не пройдены'
    if any(c['level'] == 'force' for c in cks):
        if role != 'admin':
            return False, 'forbidden', 'операция требует роли admin'
        if not force:
            return False, 'force_required', 'подтвердите принудительное выполнение (force)'
    if any(c['level'] == 'warn' for c in cks) and not acknowledge_warnings:
        return False, 'warnings_not_acknowledged', 'подтвердите, что предупреждения прочитаны'
    return True, None, None


# ---------------- исполнитель ----------------
STATUS_RU = {'succeeded': 'выполнена', 'warning': 'выполнена с замечаниями', 'failed': 'не выполнена',
             'unknown': 'результат неизвестен', 'interrupted': 'прервана', 'cancelled': 'отменена'}


class Runner:
    def __init__(self, store, cluster, client, timings=None, sleep=time.sleep, clock=time.monotonic):
        self.store = store
        self.cluster = cluster
        self.client = client
        self.t = timings or Timings()
        self.sleep = sleep
        self.clock = clock

    def snap(self):
        return status_mod.collect(self.cluster, self.client)

    def node(self, name):
        return next(n for n in self.cluster.nodes if n.name == name)

    def say(self, op_id, msg):
        log.info(msg, extra={'cluster': self.cluster.name})
        self.store.op_update(op_id, log_line=msg)

    def run(self, op):
        op_id = op['id']
        self.store.op_update(op_id, status='running', log_line='начало')
        try:
            status, result = getattr(self, '_' + op['kind'])(op_id, op['params'])
        except Exception as e:  # noqa: BLE001 — операция обязана завершиться с понятным статусом
            log.exception('операция упала', extra={'cluster': self.cluster.name})
            status, result = 'failed', {'error': f'внутренняя ошибка: {e.__class__.__name__}: {e}'}
        self.store.op_update(op_id, status=status, result=result, log_line=f'итог: {STATUS_RU.get(status, status)}')
        self.store.audit(f'op:{op["kind"]}', 'ok' if status in ('succeeded', 'scheduled', 'cancelled') else 'error',
                         op['created_by'], cluster=self.cluster.name,
                         params={'operation_id': op_id, **op['params']}, detail=f'{status}: {result}')
        return status, result

    # ---- ожидания ----
    def wait(self, op_id, what, timeout, pred):
        deadline = self.clock() + timeout
        last = None
        while True:
            try:
                last = self.snap()
                if pred(last):
                    return True, last
            except Exception as e:  # noqa: BLE001
                self.say(op_id, f'{what}: снимок не получен ({e.__class__.__name__})')
            if self.clock() >= deadline:
                return False, last
            self.sleep(self.t.poll)

    def wait_node_healthy(self, op_id, name, timeout):
        def ok(s):
            m = _member(s, name)
            if not m or not m.get('in_dcs') or m.get('pending_restart'):
                return False
            if m.get('role') in LEADER_ROLES:
                return m.get('state') == 'running'
            return m.get('state') in HEALTHY and m.get('lag') is not None and m['lag'] <= self.cluster.max_lag_bytes
        return self.wait(op_id, f'ожидание {name}', timeout, ok)

    def send(self, op_id, what, fn, *args, **kw):
        """Отправить изменяющий запрос один раз. Возвращает ('ok'|'refused'|'unknown', тело)."""
        try:
            body = fn(*args, **kw)
            self.say(op_id, f'{what}: Patroni ответил: {body}')
            return 'ok', body
        except PatroniUnknownOutcome as e:
            self.say(op_id, f'{what}: ответа нет, результат проверяю по состоянию кластера ({e})')
            return 'unknown', None
        except PatroniError as e:
            self.say(op_id, f'{what}: Patroni отказал: {e} {e.body or ""}')
            return 'refused', {'status': e.status, 'body': e.body, 'error': str(e)}

    # ---- switchover / failover ----
    def _change_leader(self, op_id, kind, params):
        before = self.snap()
        old = before['leader']
        cand = params.get('candidate')
        old_tl = max(before['timelines'] or [0])
        if kind == 'switchover':
            target = self.node(old)
            outcome, body = self.send(op_id, 'switchover', self.client.switchover, target, old, cand,
                                      params.get('scheduled_at'))
            if outcome == 'ok' and params.get('scheduled_at'):
                return 'scheduled', {'scheduled_at': params['scheduled_at'], 'response': body}
        else:
            target = self.node(cand)
            outcome, body = self.send(op_id, 'failover', self.client.failover, target, cand)
        if outcome == 'refused':
            return 'failed', body

        def leader_changed(s):
            ld = _leader(s)
            return bool(ld and ld['name'] != old and (not cand or ld['name'] == cand) and ld.get('state') == 'running')

        changed, s = self.wait(op_id, 'смена лидера', self.t.leader_change, leader_changed)
        if not changed:
            now = (s or {}).get('leader')
            st = 'unknown' if outcome == 'unknown' else 'failed'
            return st, {'error': f'лидер не сменился за {int(self.t.leader_change)} с', 'leader_now': now}
        new = s['leader']
        new_tl = max(s['timelines'] or [0])
        self.say(op_id, f'новый лидер {new}, таймлайн {old_tl} → {new_tl}')
        if not old:
            return 'succeeded', {'old_leader': None, 'new_leader': new, 'timeline': new_tl}

        def rejoined(x):
            m = _member(x, old)
            return bool(m and m.get('in_dcs') and m.get('role') not in LEADER_ROLES and m.get('state') in HEALTHY)

        back, s2 = self.wait(op_id, f'возврат {old} репликой', self.t.rejoin, rejoined)
        res = {'old_leader': old, 'new_leader': new, 'timeline': new_tl}
        if not back:
            m = _member(s2 or s, old) or {}
            res['warning'] = f'{old} не вернулся репликой за {int(self.t.rejoin)} с (состояние {m.get("state")})'
            return 'warning', res
        return 'succeeded', res

    def _switchover(self, op_id, p):
        return self._change_leader(op_id, 'switchover', p)

    def _failover(self, op_id, p):
        return self._change_leader(op_id, 'failover', p)

    def _cancel_switchover(self, op_id, p):
        s = self.snap()
        node = self.node(s['leader'] or s['view_from'])
        outcome, body = self.send(op_id, 'отмена switchover', self.client.cancel_scheduled_switchover, node)
        if outcome == 'refused':
            return 'failed', body
        ok, _ = self.wait(op_id, 'отмена', self.t.pause, lambda x: not x.get('scheduled_switchover'))
        return ('cancelled', {'response': body}) if ok else ('unknown', {'error': 'расписание не исчезло'})

    # ---- пауза ----
    def _set_pause(self, op_id, value):
        s = self.snap()
        node = self.node(s['leader'] or s['view_from'])
        outcome, body = self.send(op_id, 'пауза' if value else 'снятие паузы',
                                  self.client.patch_config, node, {'pause': True if value else None})
        if outcome == 'refused':
            return 'failed', body
        ok, _ = self.wait(op_id, 'проверка паузы', self.t.pause, lambda x: bool(x.get('paused')) == value)
        return ('succeeded', {'paused': value}) if ok else ('unknown', {'error': 'состояние паузы не подтвердилось'})

    def _pause(self, op_id, p):
        return self._set_pause(op_id, True)

    def _resume(self, op_id, p):
        return self._set_pause(op_id, False)

    # ---- глобальный конфиг ----
    def _config(self, op_id, p):
        s = self.snap()
        node = self.node(s['leader'] or s['view_from'])
        before = self.client.dynamic_config(node)
        expected = dynconf.merge(before, p['changes'])
        outcome, body = self.send(op_id, 'PATCH /config', self.client.patch_config, node, p['changes'])
        if outcome == 'refused':
            return 'failed', body
        deadline = self.clock() + self.t.config
        after = None
        while True:
            try:
                after = self.client.dynamic_config(node)
            except PatroniError as e:
                self.say(op_id, f'чтение /config: {e}')
            if after is not None and not dynconf.diff({k: v for k, v in expected.items() if k != 'pause'},
                                                      {k: v for k, v in after.items() if k != 'pause'}):
                break
            if self.clock() >= deadline:
                left = dynconf.diff(expected, after or {})
                return ('unknown' if outcome == 'unknown' else 'failed'), {
                    'error': 'конфиг в DCS не совпал с ожидаемым', 'mismatch': left[:20]}
            self.sleep(self.t.poll)
        op = self.store.op_get(op_id)
        vid = self.store.config_version_add(self.cluster.name, 'console', before, after,
                                            username=op['created_by'] if op else None, op_id=op_id)
        rows = dynconf.diff(before, after)
        self.say(op_id, f'конфиг применён, версия №{vid}: ' + '; '.join(f"{r['path']}: {r['old']} → {r['new']}" for r in rows[:10]))
        params = {r['path'].split('.', 2)[2] for r in rows if r['path'].startswith('postgresql.parameters.')}
        result = {'version': vid, 'changed': rows}
        if params & dynconf.RESTART:
            result['needs_restart'] = sorted(params & dynconf.RESTART)
            self.say(op_id, 'для части параметров нужен рестарт: запустите rolling restart')
        return 'succeeded', result

    # ---- рестарты ----
    def _restart_one(self, op_id, name):
        outcome, body = self.send(op_id, f'рестарт {name}', self.client.restart, self.node(name))
        if outcome == 'refused':
            return False, body
        ok, s = self.wait_node_healthy(op_id, name, self.t.restart)
        if not ok:
            m = _member(s or {'members': []}, name) or {}
            return False, {'error': f'{name} не пришёл в норму за {int(self.t.restart)} с', 'state': m.get('state')}
        self.say(op_id, f'{name} готов')
        return True, None

    def _restart(self, op_id, p):
        ok, err = self._restart_one(op_id, p['node'])
        return ('succeeded', {'node': p['node']}) if ok else ('failed', err)

    def _rolling_restart(self, op_id, p):
        s = self.snap()
        targets = _rolling_targets(s, p.get('only_pending', True))
        done = []
        self.say(op_id, f'план: реплики {targets["replicas"] or "—"}, лидер {targets["leader"] or "не нужен"}')
        for name in targets['replicas']:
            ok, err = self._restart_one(op_id, name)
            if not ok:
                return 'failed', {'done': done, 'failed_on': name, **(err or {})}
            done.append(name)
        moved = {}
        if targets['leader']:
            old = targets['leader']
            s = self.snap()
            pool = sorted((m for m in s['members'] if _eligible(m)),
                          key=lambda m: (m.get('role') not in ('sync_standby', 'quorum_standby'), m.get('lag') or 0))
            if not pool:
                return 'failed', {'done': done, 'error': 'нет кандидата для switchover перед рестартом лидера'}
            self.say(op_id, f'switchover {old} → {pool[0]["name"]} перед рестартом')
            st, res = self._change_leader(op_id, 'switchover', {'candidate': pool[0]['name']})
            if st not in ('succeeded',):
                return 'failed', {'done': done, 'failed_on': f'switchover: {st}', **(res or {})}
            moved = {'old_leader': res['old_leader'], 'new_leader': res['new_leader']}
            ok, err = self._restart_one(op_id, old)
            if not ok:
                return 'failed', {'done': done, 'failed_on': old, **moved, **(err or {})}
            done.append(old)
        return 'succeeded', {'restarted': done, **moved}

    def _reinitialize(self, op_id, p):
        name = p['node']
        outcome, body = self.send(op_id, f'reinit {name}', self.client.reinitialize, self.node(name), False)
        if outcome == 'refused':
            return 'failed', body
        ok, s = self.wait_node_healthy(op_id, name, self.t.reinit)
        if not ok:
            m = _member(s or {'members': []}, name) or {}
            return 'failed', {'error': f'{name} не пришёл в норму за {int(self.t.reinit)} с', 'state': m.get('state')}
        return 'succeeded', {'node': name}
