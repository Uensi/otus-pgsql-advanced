#!/usr/bin/env python3
"""Собирает docs/screenshots/index.html — галерею скриншотов экранов консоли."""
import html
import os

D = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'docs', 'screenshots')
CAP = {
    'login': ('Вход', 'Форма входа: локальные пользователи и (если включено) LDAP / Active Directory.'),
    'cluster': ('Кластер', 'Сводка здоровья одной фразой и причины, узлы с LSN и лагом, график лага, DCS. Обновляется без мерцания.'),
    'cluster-dark': ('Кластер — тёмная тема', 'Тема по системной настройке или переключателю в шапке.'),
    'all-clusters': ('Все кластеры', 'Сводка по всем кластерам из config.yaml и переключение активного.'),
    'risk': ('Риски', 'Wraparound XID с прогнозом до остановки записи, таблицы, держащие возраст, и VACUUM FREEZE; RPO и архивация WAL.'),
    'risk-slots-lag': ('Риски — слоты и лаг', 'Проверки восстановления, слоты по wal_status и удерживаемому WAL, лаг реплик в секундах.'),
    'metrics': ('Метрики', 'TPS, cache hit, соединения, кортежи, блокировки, deadlocks, долгие запросы, временные файлы.'),
    'reports-pwr-weekly': ('Отчёты (PWR)', 'Отчёт за период по снимкам статистики и настройки еженедельного отчёта.'),
    'weekly-report': ('Еженедельный отчёт', 'Страница для печати / PDF: риски, бэкапы, алерты, рост баз, диски, запросы, безопасность, версии.'),
    'alerts': ('Алерты и уведомления', 'Правила (включая риски: XID, бэкап, архивация, слоты, лаг), e-mail и Telegram.'),
    'databases': ('Базы данных', 'Размер, соединения, cache hit, транзакции, рост в час.'),
    'schema-erd': ('Схема / ERD', 'Диаграмма таблиц и связей, экспорт в PNG.'),
    'analyzer': ('Анализатор запросов', 'Сводка «что сделать», дерево EXPLAIN, проблемы плана, рекомендации индексов, переписывание.'),
    'sql-console': ('SQL-консоль', 'Выполнение запросов на выбранном узле (опасные команды заблокированы).'),
    'locks': ('Блокировки', 'Текущие блокировки на всех узлах и deadlocks по базам.'),
    'ash': ('Активные сессии (ASH)', 'Средняя нагрузка по классам ожиданий во времени, топ ожиданий, пользователей и запросов.'),
    'ash-dark': ('ASH — тёмная тема', ''),
    'tune': ('Параметры', 'Рекомендации под железо (RAM, CPU, соединения, тип дисков) со сравнением «сейчас → рекомендую».'),
    'tune-templates': ('Шаблоны конфигурации', 'Пресеты для 1С, OLTP, OLAP со сравнением с текущими параметрами.'),
    'perf-statements': ('Запросы и регрессии', 'История pg_stat_statements: топ за период и запросы, ставшие медленнее в 2+ раза.'),
    'perf-checkpoints': ('Контрольные точки и WAL', 'Скорость WAL, доля вынужденных КТ, запись бэкендами, FPI — с рекомендациями.'),
    'perf-progress': ('Долгие операции', 'Прогресс VACUUM, CREATE INDEX и других операций с оценкой оставшегося времени.'),
    'dba-summary': ('DBA — сводка', 'Зависшие транзакции, долгие запросы, раздутые таблицы, неиспользуемые индексы.'),
    'dba-bloat': ('DBA — bloat', 'Таблицы с мёртвыми строками и запуск VACUUM.'),
    'dba-vacuum': ('DBA — vacuum', 'Таблицы, ждущие VACUUM.'),
    'dba-unused': ('DBA — индексы', 'Неиспользуемые индексы.'),
    'dba-connections': ('DBA — соединения', 'Соединения по узлам, пользователям и приложениям.'),
    'dba-index-audit': ('Аудит индексов', 'Невалидные, дубли, перекрытые, FK без индекса, неиспользуемые — с готовым SQL.'),
    'maintenance': ('Обслуживание', 'Раздутые таблицы, pg_repack / VACUUM FULL и планировщик в окно обслуживания.'),
    'hosts': ('Хосты', 'ОС-метрики агентов, службы, предупреждения ядра и прогноз заполнения дисков.'),
    'backups': ('Бэкапы wal-g', 'Полные и дельта-бэкапы, проверка, клон, restore, PITR, расписание, WAL-архив.'),
    'dumps': ('Логические дампы', 'pg_dump, ротация, расписание, NFS, инспекция клона, отчёт по e-mail.'),
    'slots': ('Слоты репликации', 'Слоты и активные реплики по узлам.'),
    'operations': ('Операции', 'Switchover, failover, отложенная реплика, перезапуск кластера, история переключений.'),
    'operations-dcs-config': ('Глобальный конфиг Patroni', 'Чтение и запись конфигурации в DCS (etcd/Consul).'),
    'logs': ('Журналы PostgreSQL', 'Проверка настроек логирования и сводка разбора журнала.'),
    'logs-details': ('Журналы — детали', 'Неудачные входы, ошибки, медленные запросы, временные файлы, ожидания блокировок.'),
    'assistant': ('Ассистент DBA', 'Диагностика по текущему состоянию и ответы на вопросы (локально, без внешних API).'),
    'admin-extensions': ('Админ — расширения', ''),
    'admin-roles': ('Админ — роли', ''),
    'admin-tablespaces': ('Админ — табличные пространства', ''),
    'admin-config-diff': ('Админ — diff параметров', 'Различия pg_settings между узлами.'),
    'admin-patroni-diff': ('Админ — diff patroni.yml', 'Различия конфигурации Patroni между узлами.'),
    'security': ('Аудит безопасности', 'Оценка 0–100, находки с объяснением и SQL исправления.'),
    'upgrade': ('Обновления', 'Отставание от минорной версии, EOL, проверки перед pg_upgrade и план для Patroni.'),
    'audit': ('Аудит действий', 'Журнал входов и изменяющих действий пользователей.'),
    'mobile-cluster': ('Телефон — кластер', 'Адаптивная вёрстка.'),
    'mobile-risk': ('Телефон — риски', ''),
}

CSS = """
:root{--bg:#eef0f3;--panel:#fff;--text:#1b2430;--muted:#65707f;--line:#d5dbe3;--accent:#2c6290}
@media (prefers-color-scheme: dark){:root{--bg:#171b20;--panel:#1f242b;--text:#e6e9ee;--muted:#8a95a3;--line:#3a424d;--accent:#7db1dd}}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{padding:24px 20px 8px;max-width:1200px;margin:0 auto} h1{margin:0 0 4px;font-size:1.5rem} header p{margin:0;color:var(--muted)}
nav{max-width:1200px;margin:12px auto 0;padding:0 20px;display:flex;flex-wrap:wrap;gap:6px}
nav a{font-size:.82em;color:var(--accent);text-decoration:none;border:1px solid var(--line);border-radius:4px;padding:2px 8px;background:var(--panel)}
main{max-width:1200px;margin:16px auto 40px;padding:0 20px;display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:16px}
figure{margin:0;background:var(--panel);border:1px solid var(--line);border-radius:6px;overflow:hidden;scroll-margin-top:12px}
img{display:block;width:100%;height:auto;border-bottom:1px solid var(--line)}
figcaption{padding:10px 12px;font-size:.9em;color:var(--muted)} figcaption b{color:var(--text)}
@media (max-width:420px){main{grid-template-columns:1fr;padding:0 16px} nav,header{padding-left:16px;padding-right:16px}}
"""


def main():
    files = sorted(f for f in os.listdir(D) if f.endswith('.jpg'))
    figs, toc = [], []
    for f in files:
        key = f[3:-4]
        title, desc = CAP.get(key, (key, ''))
        figs.append(f'<figure id="{key}"><a href="{f}" target="_blank"><img src="{f}" alt="{html.escape(title)}" loading="lazy"></a>'
                    f'<figcaption><b>{html.escape(title)}</b>{(" — " + html.escape(desc)) if desc else ""}</figcaption></figure>')
        toc.append(f'<a href="#{key}">{html.escape(title)}</a>')
    page = ('<!DOCTYPE html>\n<html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>Patroni Console — экраны</title><style>{CSS}</style></head><body>'
            f'<header><h1>Patroni Console — все экраны</h1><p>Демо-режим (PATRONI_CONSOLE_DEMO=1), {len(files)} скриншотов. '
            'Нажмите на изображение, чтобы открыть его в полном размере.</p></header>'
            f'<nav>{"".join(toc)}</nav><main>{"".join(figs)}</main></body></html>')
    with open(os.path.join(D, 'index.html'), 'w', encoding='utf-8') as fh:
        fh.write(page)
    print(f'{len(files)} скриншотов → {os.path.join(D, "index.html")}')


if __name__ == '__main__':
    main()
