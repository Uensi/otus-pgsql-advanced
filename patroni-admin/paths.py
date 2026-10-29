"""Единый каталог данных консоли (журналы, история, расписания, настройки из UI).

PATRONI_CONSOLE_DATA=/var/lib/patroni-console — куда писать. Не задан — как раньше,
рядом с кодом (а расписания — рядом с config.yaml). В Docker это смонтированный том:
данные переживают пересоздание контейнера."""
import os
import shutil

CODE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('PATRONI_CONSOLE_DATA') or ''
SEED_FILES = ('alerts_rules.json',)


def data_path(name, legacy_dir=None):
    if not DATA_DIR:
        return os.path.join(legacy_dir or CODE_DIR, name)
    os.makedirs(DATA_DIR, exist_ok=True)
    p = os.path.join(DATA_DIR, name)
    # файлы, поставляемые с консолью (например, правила алертов по умолчанию),
    # копируются в каталог данных при первом обращении
    seed = os.path.join(CODE_DIR, name)
    # только файлы, поставляемые с консолью; рабочие журналы (аудит и т.п.) не копируются
    if name in SEED_FILES and not os.path.exists(p) and os.path.isfile(seed):
        try: shutil.copyfile(seed, p)
        except OSError: pass
    return p
