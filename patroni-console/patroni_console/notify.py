"""Отправка уведомлений из outbox (таблица notifications).

Событие и строки уведомлений пишутся одной транзакцией, отправщик забирает
их отдельно и повторяет с нарастающей паузой. Поэтому падение канала или
перезапуск консоли не теряют уведомления. Повтор возможен (упали между
отправкой и отметкой в базе): webhook получает event_id для дедупликации.
"""
import logging
import threading
import time

import requests

log = logging.getLogger('patroni_console.notify')

RANK = {'info': 0, 'warning': 1, 'critical': 2}
MAX_ATTEMPTS = 6
ICON = {'critical': '🔴', 'warning': '🟠', 'info': '🔵'}


class DeliveryError(Exception):
    pass


def channels_for(level, channels):
    return [c.name for c in channels if RANK[level] >= RANK[c.min_level]]


def message(event):
    icon = '✅' if event['kind'] == 'resolved' else ICON.get(event['level'], '')
    n = event.get('node')
    node = f' {n}:' if n and event['kind'] != 'leader_change' and f'{n}:' not in event['text'] else ''
    return f"{icon} [{event['cluster']}]{node} {event['text']}"


class Notifier:
    def __init__(self, store, channels, session_factory=None, clock=time.time):
        self.store = store
        self.channels = {c.name: c for c in channels}
        self.session_factory = session_factory or requests.Session
        self.clock = clock
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    def _send(self, ch, event):
        s = self.session_factory()
        if ch.type == 'webhook':
            headers = {'Authorization': f'Bearer {ch.token}'} if ch.token else {}
            body = {k: event[k] for k in ('id', 'cluster', 'ts', 'kind', 'level', 'code', 'node', 'text', 'op_id')}
            body['event_id'] = body.pop('id')
            body['message'] = message(event)
            r = s.post(ch.url, json=body, headers=headers, timeout=ch.timeout, allow_redirects=False)
        else:
            r = s.post(f'{ch.url}/bot{ch.token}/sendMessage', timeout=ch.timeout, allow_redirects=False,
                       json={'chat_id': ch.chat_id, 'text': message(event), 'disable_web_page_preview': True})
        if not 200 <= r.status_code < 300:
            raise DeliveryError(f'получатель ответил HTTP {r.status_code}')

    @staticmethod
    def _safe_error(ch, e):
        """Текст ошибки без секретов. Он виден всем пользователям в ленте событий, поэтому
        ни URL (в нём бывает секрет webhook или токен бота), ни тело ответа сюда не попадают."""
        if isinstance(e, DeliveryError):
            return str(e)
        if isinstance(e, requests.Timeout):
            return 'таймаут'
        if isinstance(e, requests.ConnectionError):
            return 'нет связи с получателем'
        return type(e).__name__

    def drain(self):
        """Отправляет всё, что пора отправить. Возвращает число успешных отправок."""
        sent = 0
        for n in self.store.notify_due(self.clock()):
            ch = self.channels.get(n['channel'])
            if ch is None:
                self.store.notify_failed(n['id'], 'канал удалён из конфига')
                continue
            try:
                self._send(ch, n['event'])
                self.store.notify_sent(n['id'])
                sent += 1
            except Exception as e:
                err = self._safe_error(ch, e)
                attempts = n['attempts'] + 1
                next_at = None if attempts >= MAX_ATTEMPTS else self.clock() + min(30 * 2 ** (attempts - 1), 1800)
                self.store.notify_failed(n['id'], err, next_at)
                log.warning('уведомление не отправлено', extra={'channel': ch.name, 'attempt': attempts,
                                                                'error': err, 'gave_up': next_at is None})
        return sent

    def wake(self):
        self._wake.set()

    def run_forever(self):
        while not self._stop.is_set():
            try:
                self.drain()
            except Exception:
                log.exception('отправщик уведомлений: ошибка')
            self._wake.wait(5)
            self._wake.clear()

    def start(self):
        if self._thread is None and self.channels:
            self._thread = threading.Thread(target=self.run_forever, name='notifier', daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
