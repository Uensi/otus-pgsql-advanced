"""Клиент REST API Patroni 4 (только то, что нужно консоли).

Ответ Patroni никогда не «проглатывается»: при ошибке поднимается PatroniError
с кодом HTTP и телом ответа, чтобы оператор видел настоящую причину отказа.
"""
import logging

import requests

log = logging.getLogger(__name__)


class PatroniError(Exception):
    def __init__(self, node, message, status=None, body=None):
        super().__init__(f'{node}: {message}')
        self.node = node
        self.status = status
        self.body = body


class PatroniUnknownOutcome(PatroniError):
    """Запрос ушёл, ответа нет (таймаут чтения, обрыв). Состояние неизвестно:
    повторять нельзя, нужно смотреть на кластер."""


class PatroniClient:
    def __init__(self, cluster, timeout=5.0, session=None, action_timeout=90.0):
        self.cluster = cluster
        self.timeout = timeout
        self.action_timeout = action_timeout
        self.session = session or requests.Session()
        if cluster.rest_username:
            self.session.auth = (cluster.rest_username, cluster.rest_password or '')
        self.session.verify = cluster.tls.verify
        if cluster.tls.cert:
            self.session.cert = cluster.tls.cert

    def _request(self, node, method, path, timeout=None, **kw):
        url = node.url + path
        try:
            r = self.session.request(method, url, timeout=timeout or self.timeout, **kw)
        except (requests.ConnectTimeout, requests.exceptions.SSLError) as e:
            raise PatroniError(node.name, f'нет связи: {e.__class__.__name__}') from e
        except (requests.ReadTimeout, requests.exceptions.ChunkedEncodingError) as e:
            if method == 'GET':
                raise PatroniError(node.name, f'таймаут ответа: {e.__class__.__name__}') from e
            raise PatroniUnknownOutcome(node.name, 'запрос отправлен, ответ не получен') from e
        except requests.ConnectionError as e:
            # соединение не установлено — запрос до Patroni не дошёл
            raise PatroniError(node.name, f'нет связи: {e.__class__.__name__}') from e
        body = None
        if r.content:
            try:
                body = r.json()
            except ValueError:
                body = r.text.strip()
        # GET /patroni на реплике отдаёт 200, но /primary и т.п. дают 503 — здесь
        # используем только эндпоинты, где 2xx означает успех.
        if not r.ok:
            raise PatroniError(node.name, f'HTTP {r.status_code}', status=r.status_code, body=body)
        return body

    def node_status(self, node):
        """GET /patroni — состояние конкретной ноды."""
        return self._request(node, 'GET', '/patroni')

    def cluster_view(self, node):
        """GET /cluster — состав кластера глазами этой ноды (данные из DCS)."""
        return self._request(node, 'GET', '/cluster')

    def history(self, node):
        """GET /history — история смены таймлайнов."""
        return self._request(node, 'GET', '/history')

    def dynamic_config(self, node):
        """GET /config — глобальная (динамическая) конфигурация из DCS."""
        return self._request(node, 'GET', '/config')

    # ---- изменяющие вызовы: НИКОГДА не повторяются автоматически ----
    def _act(self, node, method, path, payload=None):
        return self._request(node, method, path, timeout=self.action_timeout,
                             **({'json': payload} if payload is not None else {}))

    def switchover(self, node, leader, candidate=None, scheduled_at=None):
        body = {'leader': leader}
        if candidate:
            body['candidate'] = candidate
        if scheduled_at:
            body['scheduled_at'] = scheduled_at
        return self._act(node, 'POST', '/switchover', body)

    def cancel_scheduled_switchover(self, node):
        return self._act(node, 'DELETE', '/switchover')

    def failover(self, node, candidate):
        return self._act(node, 'POST', '/failover', {'candidate': candidate})

    def patch_config(self, node, patch):
        return self._act(node, 'PATCH', '/config', patch)

    def restart(self, node, restart_pending=False):
        return self._act(node, 'POST', '/restart', {'restart_pending': True} if restart_pending else {})

    def reinitialize(self, node, force=False):
        return self._act(node, 'POST', '/reinitialize', {'force': bool(force)})
