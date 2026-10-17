#!/usr/bin/env python3
"""Вход через LDAP / Active Directory.

config.yaml:
  ldap:
    enabled: true
    url: ldaps://dc1.corp.local:636          # несколько через запятую — отказоустойчивость
    start_tls: false                          # для ldap:// на 389
    ca_file: /etc/ssl/certs/corp-ca.pem       # проверка сертификата контроллера
    bind_dn: "CN=svc-pgconsole,OU=Service,DC=corp,DC=local"
    bind_password: "..."                      # или переменная PATRONI_CONSOLE_LDAP_PASSWORD
    base_dn: "DC=corp,DC=local"
    user_filter: "(&(objectClass=user)(sAMAccountName={user}))"
    nested_groups: true                       # AD: учитывать вложенные группы
    group_roles:                              # DN группы → роль консоли (берётся старшая)
      "CN=PG-Admins,OU=Groups,DC=corp,DC=local": admin
      "CN=PG-DBA,OU=Groups,DC=corp,DC=local": dba
      "CN=PG-Viewers,OU=Groups,DC=corp,DC=local": viewer

Поиск пользователя — сервисной учётной записью, проверка пароля — повторным bind
под DN пользователя. Пользователь без роли в group_roles не входит.
Нужен пакет ldap3 (pip install ldap3)."""
import os
import ssl

ROLE_LEVEL = {'viewer': 0, 'dba': 1, 'admin': 2}
_CFG = {}


def configure(config):
    global _CFG
    _CFG = dict((config or {}).get('ldap') or {})
    if _CFG.get('enabled') and not _CFG.get('bind_password'):
        _CFG['bind_password'] = os.environ.get('PATRONI_CONSOLE_LDAP_PASSWORD', '')


def enabled():
    return bool(_CFG.get('enabled'))


def _servers(ldap3):
    tls = None
    if _CFG.get('ca_file') or _CFG.get('url', '').startswith('ldaps') or _CFG.get('start_tls'):
        tls = ldap3.Tls(validate=ssl.CERT_REQUIRED if _CFG.get('ca_file') else ssl.CERT_NONE,
                        ca_certs_file=_CFG.get('ca_file') or None, version=ssl.PROTOCOL_TLS_CLIENT)
    urls = [u.strip() for u in str(_CFG.get('url', '')).split(',') if u.strip()]
    servers = [ldap3.Server(u, use_ssl=u.startswith('ldaps'), tls=tls, get_info=ldap3.NONE,
                            connect_timeout=int(_CFG.get('timeout', 5))) for u in urls]
    return ldap3.ServerPool(servers, ldap3.FIRST, active=True, exhaust=True) if len(servers) > 1 else servers[0]


def authenticate(user, password):
    """→ (role, None) при успехе, (None, 'причина') при отказе."""
    if not enabled():
        return None, 'LDAP не настроен'
    if not user or not password:
        return None, 'пустой логин или пароль'
    try:
        import ldap3
        from ldap3.utils.conv import escape_filter_chars
    except ImportError:
        return None, 'не установлен пакет ldap3 (pip install ldap3)'
    try:
        server = _servers(ldap3)
        svc = ldap3.Connection(server, user=_CFG.get('bind_dn'), password=_CFG.get('bind_password'),
                               auto_bind=False, receive_timeout=int(_CFG.get('timeout', 5)))
        if _CFG.get('start_tls'):
            svc.open(); svc.start_tls()
        if not svc.bind():
            return None, 'сервисная учётная запись LDAP не прошла bind'
        flt = str(_CFG.get('user_filter', '(sAMAccountName={user})')).replace('{user}', escape_filter_chars(user))
        svc.search(_CFG.get('base_dn', ''), flt, attributes=['memberOf'], size_limit=2)
        if len(svc.entries) != 1:
            svc.unbind()
            return None, 'пользователь не найден'
        entry = svc.entries[0]
        user_dn = entry.entry_dn
        groups = [str(g).lower() for g in (entry.memberOf.values if 'memberOf' in entry else [])]
        mapping = {str(k).lower(): v for k, v in (_CFG.get('group_roles') or {}).items()}
        role = _best_role([mapping[g] for g in groups if g in mapping])
        if role is None and _CFG.get('nested_groups'):
            # AD: членство с учётом вложенности (LDAP_MATCHING_RULE_IN_CHAIN)
            found = []
            for gdn, r in (_CFG.get('group_roles') or {}).items():
                nf = '(&(distinguishedName=%s)(memberOf:1.2.840.113556.1.4.1941:=%s))' % (
                    escape_filter_chars(user_dn), escape_filter_chars(gdn))
                svc.search(_CFG.get('base_dn', ''), nf, attributes=[], size_limit=1)
                if svc.entries:
                    found.append(r)
            role = _best_role(found)
        svc.unbind()
        if role is None:
            return None, 'нет доступа: пользователь не состоит в группах консоли'
        uc = ldap3.Connection(server, user=user_dn, password=password, auto_bind=False,
                              receive_timeout=int(_CFG.get('timeout', 5)))
        if _CFG.get('start_tls'):
            uc.open(); uc.start_tls()
        ok = uc.bind()
        uc.unbind()
        return (role, None) if ok else (None, 'неверный пароль')
    except Exception as e:
        return None, 'ошибка LDAP: %s' % e


def _best_role(roles):
    roles = [r for r in roles if r in ROLE_LEVEL]
    return max(roles, key=lambda r: ROLE_LEVEL[r]) if roles else None
