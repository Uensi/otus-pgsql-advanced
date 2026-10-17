"""Agent Patroni Console — универсальный + сбор метрик. Запуск: sudo python3 agent.py"""
import os, re, json, shlex, shutil, subprocess, time, signal, pwd, socket, yaml, requests, psycopg2
from collections import deque

# --- секреты из окружения ---
DASHBOARD = os.environ.get('PATRONI_CONSOLE_URL', 'http://127.0.0.1:5000')
KEY = os.environ.get('PATRONI_CONSOLE_KEY', '')
PG_PASS = os.environ.get('PG_PASSWORD', '')
if not KEY:
    print('[agent] ВНИМАНИЕ: PATRONI_CONSOLE_KEY не задан')
    exit(1)
if not PG_PASS:
    print('[agent] ВНИМАНИЕ: PG_PASSWORD не задан')

RESTORE_BASE = os.environ.get('PATRONI_AGENT_RESTORE_BASE', '/var/lib/pgsql/restore')
# стенд/контейнер без root: wal-g и pg_ctl запускаются от текущего пользователя
NONROOT = os.environ.get('PATRONI_AGENT_NONROOT') == '1'
INTERVAL = 5

# ---------- автообнаружение ----------
def find_patroni_yml():
    try:
        out = subprocess.run(['ps', '-eo', 'args'], capture_output=True, text=True).stdout
        for line in out.splitlines():
            if 'patroni' in line and ('.yml' in line or '.yaml' in line):
                for tok in line.split():
                    if tok.endswith(('.yml', '.yaml')) and os.path.isfile(tok): return tok
    except Exception: pass
    for p in ['/etc/patroni/patroni.yml', '/etc/patroni.yml', '/etc/patroni/config.yml', '/var/lib/pgsql/patroni.yml']:
        if os.path.isfile(p): return p
    return None

# несколько экземпляров Patroni на хосте (или нестандартный путь) — укажите явно
PATRONI_YML = os.environ.get('PATRONI_AGENT_PATRONI_YML') or find_patroni_yml() or '/etc/patroni/patroni.yml'
try:
    with open(PATRONI_YML) as f: PCFG = yaml.safe_load(f) or {}
except Exception: PCFG = {}

NODE = PCFG.get('name') or socket.gethostname()
PGDATA = (PCFG.get('postgresql', {}) or {}).get('data_dir') or '/var/lib/pgsql/17/data'
_auth = (PCFG.get('authentication', {}) or {}).get('superuser', {}) or {}
PG_USER = _auth.get('username', 'postgres')
_listen = ((PCFG.get('postgresql', {}) or {}).get('listen') or '0.0.0.0:5432')
PG_PORT = int(str(_listen).rsplit(':', 1)[-1])
# адрес для локального подключения: из listen (если PostgreSQL слушает конкретный IP), иначе 127.0.0.1
_lhost = str(_listen).rsplit(':', 1)[0].split(',')[0].strip()
PG_HOST = os.environ.get('PATRONI_AGENT_PG_HOST') or ('127.0.0.1' if _lhost in ('', '*', '0.0.0.0', '::', 'localhost') else _lhost)

WALG_BINS = ['/usr/local/bin/walg', '/usr/local/bin/wal-g', '/usr/bin/wal-g', '/usr/bin/walg']
WALG_CFG_PREF = [p for p in [os.environ.get('PATRONI_AGENT_WALG_CONFIG')] if p] + \
    ['/var/lib/pgsql/.walg_no_compress.json', '/var/lib/pgsql/.walg.json', '/etc/walg.json']
last_backups = []
_prev_stats = {'ts': 0, 'row': None}

def find_walg():
    for p in WALG_BINS:
        if os.path.isfile(p) and os.access(p, os.X_OK): return p
    return shutil.which('wal-g') or shutil.which('walg')

def walg_cfg_path():
    for p in WALG_CFG_PREF:
        if os.path.isfile(p): return p
    return None

def walg_pgdata():
    cfg = walg_cfg_path()
    try:
        with open(cfg) as f: return json.load(f).get('PGDATA', PGDATA)
    except Exception: return PGDATA

def walg_env():
    env = dict(os.environ)
    env['PGUSER'] = PG_USER; env['PGDATABASE'] = 'postgres'; env['PGPASSWORD'] = PG_PASS
    env['PGHOST'] = PG_HOST; env['PGPORT'] = str(PG_PORT)
    return env

def run_walg(args, timeout=7200):
    binp = find_walg()
    if not binp: raise RuntimeError('walg не найден')
    cfg = walg_cfg_path(); env = walg_env()
    if NONROOT:
        r = subprocess.run([binp] + (['--config', cfg] if cfg else []) + [str(a) for a in args],
                           capture_output=True, text=True, timeout=timeout, env=env)
        if r.returncode != 0:
            raise RuntimeError(f'walg {" ".join(map(str, args))}: {r.stderr.strip() or r.stdout.strip()}')
        return r.stdout
    if os.geteuid() != 0: raise RuntimeError('нужен root: sudo python3 agent.py')
    envfile = f'/tmp/walg_env.{os.getpid()}.sh'
    try:
        with open(envfile, 'w') as f:
            for k, v in env.items():
                if k.startswith(('WALG_', 'PG', 'AWS')):
                    f.write("export %s='%s'\n" % (k, str(v).replace("'", "'\\''")))
        os.chmod(envfile, 0o600); os.chown(envfile, pwd.getpwnam('postgres').pw_uid, -1)
        # все аргументы проходят shlex.quote: никакая внешняя строка не попадёт в shell-команду как код
        args_str = ' '.join(shlex.quote(str(a)) for a in args)
        cfg_arg = f'--config {shlex.quote(cfg)} ' if cfg else ''
        cmd = f"su - postgres -s /bin/bash -c \"ulimit -n 65536; set -a; source {shlex.quote(envfile)}; set +a; {binp} {cfg_arg}{args_str}\""
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError(f'walg {args_str}: {r.stderr.strip() or r.stdout.strip()}')
        return r.stdout
    finally:
        try: os.remove(envfile)
        except Exception: pass

def walg_list():
    # без --detail wal-g не сообщает is_full, start/finish_time, LSN — консоль не отличила бы полный от дельты
    out = run_walg(['backup-list', '--detail', '--json'])
    try: res = json.loads(out) or []
    except Exception:
        res = []
        for line in out.splitlines():
            line = line.strip()
            if line.startswith('{'):
                try: res.append(json.loads(line))
                except Exception: pass
    # wal-g 3.x не отдаёт is_full: дельта видна по имени base_<WAL>_D_<базовый WAL>
    for b in res:
        name = b.get('backup_name') or ''
        if b.get('is_full') is None:
            b['is_full'] = '_D_' not in name
        if '_D_' in name:
            b['delta_from'] = 'base_' + name.split('_D_', 1)[1]
    return res

def walg_backup(full=False):
    ensure_pg_hba()
    # ВНИМАНИЕ: у wal-g «-f» — это сокращение --full; дельта получается без флага
    # (и только при WALG_DELTA_MAX_STEPS > 0 в конфиге wal-g, иначе wal-g делает полный)
    args = ['backup-push', walg_pgdata()] + (['--full'] if full else [])
    try:
        run_walg(args)
    except RuntimeError as e:
        # Бэкап с реплики начинается с последней точки рестарта. Если её не было после
        # предыдущего бэкапа, wal-g отказывается делать дельту («Finish LSN … greater than
        # current LSN»). CHECKPOINT на реплике создаёт точку рестарта по последней КТ лидера.
        if 'greater than current LSN' not in str(e):
            raise
        try: pg_exec('CHECKPOINT')
        except Exception: pass
        try:
            run_walg(args)
        except RuntimeError as e2:
            if 'greater than current LSN' in str(e2):
                raise RuntimeError('на реплике ещё нет новой точки рестарта после предыдущего бэкапа: '
                                   'выполните CHECKPOINT на лидере или повторите после очередной контрольной точки '
                                   '(checkpoint_timeout). Подробности: ' + str(e2)[-300:])
            raise
    return walg_list()

def walg_rotate(keep):
    run_walg(['delete', 'retain', 'FULL', str(keep), '--confirm']); return walg_list()

def walg_delete(name):
    run_walg(['delete', 'target', name, '--confirm']); return walg_list()

def walg_verify():
    out = run_walg(['wal-verify', 'integrity', '--json'])
    try: return json.loads(out)
    except Exception: return {'raw': out}

def walg_restore(name, target_time=None):
    td = os.path.join(RESTORE_BASE, time.strftime('%Y%m%d_%H%M%S'))
    os.makedirs(td, exist_ok=True)
    run_walg(['backup-fetch', td, name])
    _prepare_recovery(td, target_time)
    return td

def _prepare_recovery(td, target_time=None):
    """Базовой копии wal-g всегда нужен WAL до точки согласованности: без
    restore_command и recovery.signal восстановленный каталог не запустится.
    Без target_time восстанавливаемся до первой согласованной точки (immediate)."""
    cfg = walg_cfg_path()
    cfg_arg = f'--config {shlex.quote(cfg)} ' if cfg else ''
    with open(os.path.join(td, 'postgresql.auto.conf'), 'a') as f:
        f.write("\n# --- восстановление (Patroni Console) ---\n")
        f.write(f"restore_command = '{find_walg()} {cfg_arg}wal-fetch %f %p'\n")
        if target_time:
            f.write(f"recovery_target_time = '{target_time}'\n")
        else:
            f.write("recovery_target = 'immediate'\n")
        f.write("recovery_target_action = 'promote'\n")
        f.write("primary_conninfo = ''\n")
        # копия не должна отправлять WAL своей новой линии времени в хранилище боевого кластера
        f.write("archive_mode = off\n")
    try: os.remove(os.path.join(td, 'standby.signal'))
    except FileNotFoundError: pass
    open(os.path.join(td, 'recovery.signal'), 'w').close()

def clone_restore(name, target_time=None, label=None):
    """Клон: восстановление wal-g в отдельный каталог + подготовка конфига и стартового
    скрипта. БЕЗ автоматического запуска — чтобы не трогать прод. Возвращает метаданные."""
    td = walg_restore(name, target_time)
    meta = {'path': td, 'backup': name, 'target_time': target_time,
            'created': time.strftime('%Y-%m-%d %H:%M:%S'), 'label': label or 'clone',
            'started': False}
    # конфиг для независимого запуска (порт сдвинут, сокет локальный)
    try:
        with open(os.path.join(td, 'postgresql.auto.conf'), 'a') as f:
            f.write(f"\nport = {PG_PORT + 100}\nlisten_addresses = '127.0.0.1'\n")
    except Exception: pass
    # стартовый скрипт (запуск вручную при необходимости)
    try:
        scr = os.path.join(td, 'start_clone.sh')
        with open(scr, 'w') as f:
            f.write('#!/bin/bash\n'
                    f'# Клон из бэкапа {name}{(" (PITR " + target_time + ")") if target_time else ""}\n'
                    f'set -e\n'
                    f'chown -R postgres:postgres {td}\n'
                    f'echo "Запуск клона (порт {PG_PORT + 100}, 127.0.0.1). Убедитесь, что каталог свободен:"\n'
                    f'su - postgres -c "/usr/pgsql-*/bin/pg_ctl -D {td} start"\n')
        os.chmod(scr, 0o755)
    except Exception: pass
    meta['start_script'] = scr if 'scr' in dir() else None
    return meta

def clone_inspect(path):
    """Проверка восстановленного клона: версия, контрольные данные, размер, конфиг.
    Безопасно — только чтение. Path должен быть внутри RESTORE_BASE."""
    real = os.path.realpath(path or '')
    if not real.startswith(os.path.realpath(RESTORE_BASE)):
        raise RuntimeError('путь вне каталога восстановления')
    out = {'path': real, 'exists': os.path.isdir(real), 'size': None, 'version': None,
           'controldata': None, 'recovery_signal': False, 'port': None, 'error': None}
    if not out['exists']:
        out['error'] = 'каталог не найден'
        return out
    try:
        total = 0
        for _r, _d, files in os.walk(real):
            for f in files:
                try: total += os.path.getsize(os.path.join(_r, f))
                except Exception: pass
        out['size'] = total
    except Exception as e:
        out['error'] = f'size: {e}'
    # версия из PG_VERSION + controldata
    try:
        with open(os.path.join(real, 'PG_VERSION')) as f:
            out['version'] = f.read().strip()
    except Exception: pass
    try:
        out['controldata'] = pg_controldata(real)
    except Exception as e:
        out['error'] = (out['error'] or '') + f' | controldata: {e}'
    out['recovery_signal'] = os.path.exists(os.path.join(real, 'recovery.signal')) or \
        os.path.exists(os.path.join(real, 'standby.signal'))
    # порт из postgresql.auto.conf / postgresql.conf
    for cfg in ('postgresql.auto.conf', 'postgresql.conf'):
        p = os.path.join(real, cfg)
        try:
            with open(p) as f:
                for line in f:
                    if line.strip().startswith('port'):
                        out['port'] = line.split('=')[1].strip().strip(',').strip()
        except Exception: continue
    return out

def pg_controldata(pgdata):
    """pg_controldata: банер имени бинарника + статус каталога."""
    ver = ''
    try:
        with open(os.path.join(pgdata, 'PG_VERSION')) as f: ver = f.read().strip()
    except Exception: pass
    cands = [b for b in [_find_pg_bin('pg_controldata', ver) if ver else None, shutil.which('pg_controldata')] if b]
    for c in cands:
        try:
            r = subprocess.run([c, pgdata], capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                lines = r.stdout.splitlines()
                head = {'version': None, 'status': None}
                for ln in lines:
                    if 'pg_control version number' in ln: head['version'] = ln.split(':')[-1].strip()
                    if 'Database cluster state' in ln: head['status'] = ln.split(':')[-1].strip()
                return head
        except Exception:
            continue
    return None

def set_wal_archiving(on):
    with open(PATRONI_YML) as f: cfg = yaml.safe_load(f)
    params = cfg.setdefault('postgresql', {}).setdefault('parameters', {})
    if on:
        wcfg = walg_cfg_path() or ''
        params['archive_mode'] = 'on'
        params['archive_command'] = (f"{find_walg()} --config {wcfg} wal-push %p") if wcfg else f"{find_walg()} wal-push %p"
    else:
        params['archive_mode'] = 'off'; params['archive_command'] = '/bin/true'
    with open(PATRONI_YML, 'w') as f: yaml.safe_dump(cfg, f)
    reload_patroni(); time.sleep(2)
    try: return pg_exec("SHOW archive_mode")
    except Exception: return 'restart-required'

def ensure_pg_hba():
    try:
        c = pg_conn(); cur = c.cursor(); cur.execute("SHOW hba_file"); hba = cur.fetchone()[0]; cur.close(); c.close()
    except Exception as e:
        print('[agent] не узнал hba_file:', e); return
    lines = [f"host replication {PG_USER} 127.0.0.1/32 md5", f"host replication {PG_USER} 127.0.0.1/32 scram-sha-256"]
    try:
        with open(hba) as f: content = f.read()
        if 'replication' in content and '127.0.0.1/32' in content and PG_USER in content: return
        with open(hba, 'a') as f: f.write("\n" + "\n".join(lines) + "\n")
        c = pg_conn(); cur = c.cursor(); cur.execute("SELECT pg_reload_conf()"); cur.close(); c.close()
    except Exception as e:
        print('[agent] не могу править pg_hba:', e)

def pg_conn():
    variants = [
        dict(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASS, dbname='postgres', connect_timeout=3),
        dict(host='127.0.0.1', port=PG_PORT, user=PG_USER, password=PG_PASS, dbname='postgres', connect_timeout=3),
        dict(host='localhost', port=PG_PORT, user=PG_USER, password=PG_PASS, dbname='postgres', connect_timeout=3),
    ]
    last = None
    for v in variants:
        try:
            c = psycopg2.connect(**v); c.autocommit = True; return c
        except Exception as e: last = e
    raise RuntimeError(f'нет подключения к PostgreSQL: {last}')

def pg_exec(sql):
    c = pg_conn()
    try:
        cur = c.cursor(); cur.execute(sql)
        if cur.description: return cur.fetchone()[0]
        return None
    finally: c.close()

def pg_exec_rows(sql):
    c = pg_conn()
    try:
        cur = c.cursor(); cur.execute(sql)
        if not cur.description: return []
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally: c.close()

# ---------- логические дампы (разд. 3 спеки: параллельный pg_dump, ротация, отчёты) ----------
DUMP_BASE = os.environ.get('PATRONI_AGENT_DUMP_BASE', '/var/lib/pgsql/dumps')
_RE_DBNAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,62}$')
_RE_MAIL = re.compile(r'^[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}$')
_RE_NFS_SHARE = re.compile(r'^[\w.-]+:/[\w./-]*$')
_RE_MOUNT_OPTS = re.compile(r'^[A-Za-z0-9_=.,-]+$')
_ALLOWED_MOUNTS = ('/backup', '/var/lib/pgsql/backup', '/var/lib/pgsql/dumps-nfs')

_BIN_DIR = ((PCFG.get('postgresql', {}) or {}).get('bin_dir') or '')
PG_DUMP = shlex.quote(os.path.join(_BIN_DIR, 'pg_dump')) if _BIN_DIR and os.path.isfile(os.path.join(_BIN_DIR, 'pg_dump')) else 'pg_dump'

def _dump_env(db):
    env = dict(os.environ)
    env['PGUSER'] = PG_USER; env['PGDATABASE'] = db; env['PGPASSWORD'] = PG_PASS
    env['PGHOST'] = PG_HOST; env['PGPORT'] = str(PG_PORT)
    return env

def run_dump_cmd(args_str, db, timeout=14400):
    """Как и в run_walg: envfile + su - postgres. args_str уже собран из shlex.quote."""
    env = _dump_env(db)
    if NONROOT:
        r = subprocess.run(['bash', '-o', 'pipefail', '-c', args_str], capture_output=True, text=True, timeout=timeout, env=env)
        if r.returncode != 0:
            raise RuntimeError((r.stderr.strip() or r.stdout.strip())[:600])
        return r.stdout
    if os.geteuid() != 0: raise RuntimeError('нужен root: sudo python3 agent.py')
    envfile = f'/tmp/pgdump_env.{os.getpid()}.sh'
    try:
        with open(envfile, 'w') as f:
            for k, v in env.items():
                if k.startswith('PG'):
                    f.write("export %s='%s'\n" % (k, str(v).replace("'", "'\\''")))
        os.chmod(envfile, 0o600); os.chown(envfile, pwd.getpwnam('postgres').pw_uid, -1)
        cmd = f"su - postgres -s /bin/bash -c \"ulimit -n 65536; set -a; source {shlex.quote(envfile)}; set +a; {args_str}\""
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError((r.stderr.strip() or r.stdout.strip())[:600])
        return r.stdout
    finally:
        try: os.remove(envfile)
        except Exception: pass

def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''): h.update(chunk)
    return h.hexdigest()

def _dir_size(path):
    size = 0
    for root, _, files in os.walk(path):
        for fn in files:
            try: size += os.path.getsize(os.path.join(root, fn))
            except Exception: pass
    return size

def dump_create(db, mode='custom', jobs=2, compress=3):
    """Параллельный/сжатый дамп. mode: custom (pg_dump -Fc), dir (-Fd -j), sql-zstd."""
    if not _RE_DBNAME.match(db or ''): raise RuntimeError(f'некорректное имя БД: {db}')
    if mode not in ('custom', 'dir', 'sql-zstd'): raise RuntimeError('неизвестный режим дампа')
    jobs = max(1, min(int(jobs), 8))
    os.makedirs(DUMP_BASE, exist_ok=True)
    try: os.chown(DUMP_BASE, pwd.getpwnam('postgres').pw_uid, -1)
    except Exception: pass
    ts = time.strftime('%Y%m%d_%H%M%S')
    t0 = time.time()
    if mode == 'custom':
        outp = os.path.join(DUMP_BASE, f'{db}_{ts}.dump')
        run_dump_cmd(f"{PG_DUMP} {shlex.quote(db)} -Fc -Z {max(0, min(int(compress), 9))} -f {shlex.quote(outp)}", db)
    elif mode == 'dir':
        outp = os.path.join(DUMP_BASE, f'{db}_{ts}.d')
        run_dump_cmd(f"{PG_DUMP} {shlex.quote(db)} -Fd -j {jobs} -f {shlex.quote(outp)}", db)
    else:
        outp = os.path.join(DUMP_BASE, f'{db}_{ts}.sql.zst')
        run_dump_cmd(f"{PG_DUMP} {shlex.quote(db)} | zstd -{max(1, min(int(compress), 19))} -q -o {shlex.quote(outp)}", db)
    size = _dir_size(outp) if os.path.isdir(outp) else os.path.getsize(outp)
    meta = {'name': os.path.basename(outp), 'db': db, 'mode': mode, 'size': size,
            'created': time.strftime('%Y-%m-%d %H:%M:%S'), 'duration': round(time.time() - t0, 1),
            'node': NODE}
    if mode != 'dir':
        try: meta['sha256'] = _sha256(outp)
        except Exception: pass
    return meta

def dump_list():
    out = []
    if not os.path.isdir(DUMP_BASE): return out
    for name in sorted(os.listdir(DUMP_BASE), reverse=True):
        p = os.path.join(DUMP_BASE, name)
        try:
            size = _dir_size(p) if os.path.isdir(p) else os.path.getsize(p)
            out.append({'name': name, 'size': size, 'mtime': os.path.getmtime(p), 'is_dir': os.path.isdir(p)})
        except Exception: pass
    return out

def dump_rotate(days=7):
    cutoff = time.time() - int(days) * 86400
    removed = []
    if os.path.isdir(DUMP_BASE):
        for name in os.listdir(DUMP_BASE):
            p = os.path.join(DUMP_BASE, name)
            try:
                if os.path.getmtime(p) < cutoff:
                    if os.path.isdir(p): shutil.rmtree(p)
                    else: os.remove(p)
                    removed.append(name)
            except Exception: pass
    return removed

def dump_delete(name):
    if not name or '/' in name or '\\' in name or name.startswith('..'): raise RuntimeError('некорректное имя файла')
    p = os.path.join(DUMP_BASE, name)
    if not os.path.exists(p): raise RuntimeError('файл не найден')
    if os.path.isdir(p): shutil.rmtree(p)
    else: os.remove(p)
    return name

def _check_mountpoint(mp):
    if mp not in _ALLOWED_MOUNTS and not any(mp.startswith(m + '/') for m in _ALLOWED_MOUNTS):
        raise RuntimeError(f'точка монтирования {mp} вне разрешённого списка: {", ".join(_ALLOWED_MOUNTS)}')

def nfs_mount(share, mountpoint, options=''):
    if not _RE_NFS_SHARE.match(share or ''): raise RuntimeError('некорректный NFS share (host:/path)')
    _check_mountpoint(mountpoint)
    if not os.path.isdir(mountpoint): os.makedirs(mountpoint)
    args = ['mount', '-t', 'nfs']
    if options:
        if not _RE_MOUNT_OPTS.match(options): raise RuntimeError('небезопасные опции монтирования')
        args += ['-o', options]
    r = subprocess.run(args + [share, mountpoint], capture_output=True, text=True, timeout=180)
    if r.returncode != 0: raise RuntimeError(f'mount: {(r.stderr or r.stdout).strip()[:300]}')
    return f'mounted {share} -> {mountpoint}'

def nfs_umount(mountpoint):
    _check_mountpoint(mountpoint)
    r = subprocess.run(['umount', mountpoint], capture_output=True, text=True, timeout=120)
    if r.returncode != 0: raise RuntimeError(f'umount: {(r.stderr or r.stdout).strip()[:300]}')
    return f'umounted {mountpoint}'

def send_report(to, subject, body):
    """Отправка отчёта через системный msmtp (/etc/msmtprc)."""
    if not _RE_MAIL.match(to or ''): raise RuntimeError('некорректный адрес получателя')
    msg = (f"From: patroni-console@{socket.gethostname()}\nTo: {to}\n"
           f"Subject: [Patroni Console] {subject}\n"
           f"Date: {time.strftime('%a, %d %b %Y %H:%M:%S')}\n"
           f"Content-Type: text/plain; charset=utf-8\n\n{body}\n")
    r = subprocess.run(['msmtp', '-t'], input=msg.encode('utf-8', 'replace'),
                       capture_output=True, timeout=90)
    if r.returncode != 0:
        raise RuntimeError(f'msmtp: {(r.stderr or r.stdout).decode("utf-8", "replace").strip()[:300]}')
    return f'sent to {to}'

# ---------- обслуживание: pg_repack / VACUUM FULL (разд. 12 спеки) ----------
def find_pg_repack():
    for p in ['/usr/pgsql-17/bin/pg_repack', '/usr/pgsql-16/bin/pg_repack', '/usr/pgsql-15/bin/pg_repack',
              '/usr/pgsql-14/bin/pg_repack', '/usr/bin/pg_repack', '/usr/local/bin/pg_repack']:
        if os.path.isfile(p) and os.access(p, os.X_OK): return p
    return shutil.which('pg_repack')

def repack_table(db, schema, table, method='auto'):
    """Онлайн-дефрагментация: pg_repack (если есть) или VACUUM FULL (автозамена)."""
    if not _RE_DBNAME.match(db or ''): raise RuntimeError(f'некорректное имя БД: {db}')
    if not _RE_DBNAME.match(schema or ''): raise RuntimeError(f'некорректная схема: {schema}')
    if not _RE_DBNAME.match(table or ''): raise RuntimeError(f'некорректная таблица: {table}')
    if method not in ('auto', 'repack', 'vacuum'):
        raise RuntimeError('метод должен быть auto/repack/vacuum')
    rep = find_pg_repack()
    if method == 'repack' and not rep:
        raise RuntimeError('pg_repack не установлен')
    qt = f'{shlex.quote(schema)}.{shlex.quote(table)}'
    if method == 'vacuum' or (method == 'auto' and not rep):
        run_dump_cmd(f'psql {shlex.quote(db)} -v ON_ERROR_STOP=1 -c "VACUUM FULL {qt}"', db, timeout=14400)
        return f'VACUUM FULL {schema}.{table} выполнен'
    use_repack = '--no-superuser-check' if rep else ''
    run_dump_cmd(f'{shlex.quote(rep)} -d {shlex.quote(db)} --table {qt} {use_repack}', db, timeout=14400)
    return f'pg_repack {schema}.{table} выполнен'

# ---------- ОС-метрики (разд. 4, 7 спеки) ----------
WATCH_PATHS = None  # заполняется после определения PGDATA

def _read_proc_stat():
    with open('/proc/stat') as f:
        return [int(x) for x in f.readline().split()[1:]]

def _disk_usage(path):
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - st.f_bfree * st.f_frsize
        return {'path': path, 'total': int(total), 'used': int(used), 'free': int(free),
                'used_pct': round(100.0 * used / total, 1) if total else 0}
    except Exception:
        return None

def _meminfo():
    info = {}
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                k, _, v = line.partition(':')
                info[k.strip()] = int(v.strip().split()[0]) * 1024  # kB -> байты
    except Exception:
        return {}
    return {'total': info.get('MemTotal', 0), 'available': info.get('MemAvailable', 0),
            'cached': info.get('Cached', 0), 'swap_total': info.get('SwapTotal', 0),
            'swap_used': max(0, info.get('SwapTotal', 0) - info.get('SwapFree', 0))}

def _sysctl(name):
    try:
        with open('/proc/sys/' + name.replace('.', '/')) as f: return f.read().strip()
    except Exception: return None

def _hugepages_mode():
    try:
        with open('/sys/kernel/mm/transparent_hugepage/enabled') as f: return f.read().strip()
    except Exception: return None

def _io_scheduler(dev):
    try:
        with open(f'/sys/block/{dev}/queue/scheduler') as f: return f.read().strip()
    except Exception: return None

def _find_data_disk_dev():
    """Блочное устройство, на котором лежит PGDATA."""
    try:
        st = os.stat(PGDATA)
        major, minor = os.major(st.st_dev), os.minor(st.st_dev)
        with open('/proc/partitions') as f:
            for line in f.readlines()[2:]:
                p = line.split()
                if len(p) >= 4 and int(p[0]) == major and int(p[1]) == minor:
                    name = p[3]
                    while name and name[-1].isdigit(): name = name[:-1]
                    return name or None
    except Exception: pass
    return None

def _service_state(name):
    # msmtp — это cli-утилита, а не systemd-сервис: проверяем наличие бинарника
    if name == 'msmtp':
        found = shutil.which('msmtp') or any(
            os.path.isfile(p) for p in ('/usr/bin/msmtp', '/usr/local/bin/msmtp', '/bin/msmtp'))
        return 'active' if found else 'inactive'
    try:
        r = subprocess.run(['systemctl', 'is-active', name], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or 'unknown'
    except Exception: return 'unknown'

def _pg_version():
    try: return str(pg_exec("SELECT version()"))
    except Exception: return None

def collect_os_metrics():
    m = {'ts': time.time(), 'hostname': socket.gethostname(), 'cpus': os.cpu_count() or 1}
    try: m['loadavg'] = [round(x, 2) for x in os.getloadavg()]
    except Exception: m['loadavg'] = []
    try:
        with open('/proc/uptime') as f: m['uptime_s'] = float(f.read().split()[0])
    except Exception: pass
    try:
        t1 = _read_proc_stat(); time.sleep(0.25); t2 = _read_proc_stat()
        idle = (t2[3] + (t2[4] if len(t2) > 4 else 0)) - (t1[3] + (t1[4] if len(t1) > 4 else 0))
        total = sum(t2) - sum(t1)
        if total > 0:
            m['cpu_used_pct'] = round(100.0 * (total - idle) / total, 1)
            if len(t2) > 4 and len(t1) > 4:
                m['cpu_iowait_pct'] = round(100.0 * (t2[4] - t1[4]) / total, 1)
    except Exception: pass
    m['mem'] = _meminfo()
    paths = ['/', PGDATA] + [p for p in ('/var/lib/pgsql', '/var/lib/pgsql/backup', '/backup') if os.path.isdir(p)]
    seen = set(); disks = []
    for p in paths:
        rp = os.path.realpath(p)
        if rp in seen: continue
        seen.add(rp)
        d = _disk_usage(p)
        if d: disks.append(d)
    m['disks'] = disks
    m['kernel'] = {'vm.dirty_bytes': _sysctl('vm.dirty_bytes'),
                   'vm.dirty_background_bytes': _sysctl('vm.dirty_background_bytes'),
                   'vm.overcommit_memory': _sysctl('vm.overcommit_memory'),
                   'vm.swappiness': _sysctl('vm.swappiness'),
                   'transparent_hugepage': _hugepages_mode()}
    dev = _find_data_disk_dev()
    if dev: m['kernel']['io_scheduler_' + dev] = _io_scheduler(dev)
    m['services'] = {s: _service_state(s) for s in ('postgresql', 'patroni', 'etcd', 'consul', 'msmtp')}
    m['pg_version'] = _pg_version()
    return m

def system_service(service, action):
    """Управление системными службами (белый список, только через systemctl)."""
    allowed = {'postgresql', 'patroni', 'etcd', 'consul', 'msmtp'}
    if not (service in allowed or service.startswith('postgresql-')):
        raise RuntimeError(f'служба {service} не в белом списке')
    if action not in ('status', 'start', 'stop', 'restart', 'enable'):
        raise RuntimeError(f'действие {action} не разрешено')
    r = subprocess.run(['systemctl', action, shlex.quote(service)], capture_output=True, text=True, timeout=120)
    if r.returncode != 0 and action != 'status':
        raise RuntimeError(f'systemctl {action} {service}: {(r.stderr or r.stdout).strip()[:300]}')
    return (r.stdout or '').strip() or 'ok'

def collect_metrics():
    try:
        m = {}
        rows = pg_exec_rows("""
            SELECT sum(xact_commit)::bigint AS xact_commit, sum(xact_rollback)::bigint AS xact_rollback,
                   sum(blks_hit)::bigint AS blks_hit, sum(blks_read)::bigint AS blks_read,
                   sum(tup_returned)::bigint AS tup_returned, sum(tup_fetched)::bigint AS tup_fetched,
                   sum(tup_inserted)::bigint AS tup_inserted, sum(tup_updated)::bigint AS tup_updated,
                   sum(tup_deleted)::bigint AS tup_deleted, sum(conflicts)::bigint AS conflicts,
                   sum(deadlocks)::bigint AS deadlocks, sum(temp_files)::bigint AS temp_files,
                   sum(temp_bytes)::bigint AS temp_bytes
            FROM pg_stat_database WHERE datname NOT LIKE 'template%'
        """)
        if rows: m['db'] = {k: int(v or 0) for k, v in rows[0].items()}
        conn = pg_exec_rows("SELECT state, count(*) AS c FROM pg_stat_activity GROUP BY state")
        cstate = {r['state'] or 'null': int(r['c']) for r in conn}
        m['conn'] = {
            'total': sum(cstate.values()),
            'active': cstate.get('active', 0),
            'idle': cstate.get('idle', 0),
            'idle_in_tx': cstate.get('idle in transaction', 0) + cstate.get('idle in transaction (aborted)', 0),
        }
        try:
            r = pg_exec_rows("SELECT count(*) FROM pg_stat_activity WHERE state='active' AND now()-query_start > interval '60 seconds'")
            m['long_queries'] = int(r[0]['count']) if r else 0
        except Exception: m['long_queries'] = 0
        try:
            r = pg_exec_rows("SELECT count(*) FROM pg_locks WHERE NOT granted")
            m['locks_waiting'] = int(r[0]['count']) if r else 0
        except Exception: m['locks_waiting'] = 0
        try:
            r = pg_exec_rows("SELECT archived_count, failed_count FROM pg_stat_archiver")
            m['archiver'] = {k: int(v or 0) for k, v in r[0].items()} if r else {}
        except Exception: m['archiver'] = {}
        try:
            r = pg_exec_rows("SELECT datname, pg_database_size(datname) AS size FROM pg_database WHERE datistemplate=false ORDER BY 2 DESC LIMIT 10")
            m['dbs'] = [{'name': row['datname'], 'size': int(row['size'])} for row in r]
        except Exception: m['dbs'] = []
        try:
            r = pg_exec_rows("SELECT client_addr, pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS lag_bytes FROM pg_stat_replication")
            m['replication'] = [{'client': str(row['client_addr']), 'lag_bytes': int(row['lag_bytes'])} for row in r]
        except Exception: m['replication'] = []
        now = time.time()
        prev = _prev_stats.get('row')
        dt = now - _prev_stats.get('ts', 0)
        rates = {}
        if prev and dt > 2:
            def rate(k): return (m['db'].get(k, 0) - prev.get(k, 0)) / dt
            rates = {
                'tps': rate('xact_commit') + rate('xact_rollback'),
                'commits_per_s': rate('xact_commit'), 'rollbacks_per_s': rate('xact_rollback'),
                'blks_hit_per_s': rate('blks_hit'), 'blks_read_per_s': rate('blks_read'),
                'tup_inserted_per_s': rate('tup_inserted'), 'tup_updated_per_s': rate('tup_updated'),
                'tup_deleted_per_s': rate('tup_deleted'),
            }
            total_blks = (m['db'].get('blks_hit', 0) - prev.get('blks_hit', 0)) + (m['db'].get('blks_read', 0) - prev.get('blks_read', 0))
            hits = m['db'].get('blks_hit', 0) - prev.get('blks_hit', 0)
            rates['cache_hit_ratio'] = round(100.0 * hits / total_blks, 2) if total_blks > 0 else 0
        _prev_stats['ts'] = now; _prev_stats['row'] = m['db'].copy() if m.get('db') else None
        m['rates'] = rates
        try:
            r = pg_exec("SELECT pg_is_in_recovery()")
            m['in_recovery'] = bool(r)
        except Exception: m['in_recovery'] = None
        return m
    except Exception as e:
        return {'error': str(e)}

def edit_patroni_yml(hours):
    with open(PATRONI_YML) as f: cfg = yaml.safe_load(f)
    cfg.setdefault('postgresql', {}).setdefault('parameters', {})
    cfg.setdefault('tags', {})
    if hours is None:
        cfg['postgresql']['parameters'].pop('recovery_min_apply_delay', None)
        cfg['tags']['nofailover'] = False; cfg['tags'].pop('nosync', None)
    else:
        cfg['postgresql']['parameters']['recovery_min_apply_delay'] = f"{hours}h"
        cfg['tags']['nofailover'] = True; cfg['tags']['nosync'] = True
    with open(PATRONI_YML, 'w') as f: yaml.safe_dump(cfg, f)

def reload_patroni():
    try:
        out = subprocess.run(['pgrep', '-f', 'patroni'], capture_output=True, text=True)
        pids = [int(x) for x in out.stdout.split() if x.strip()]
        for pid in pids: os.kill(pid, signal.SIGHUP)
        return f'HUP -> {pids}'
    except Exception:
        subprocess.run(['pkill', '-HUP', '-f', 'patroni'], check=False); return 'pkill -HUP'

def set_delay(hours):
    edit_patroni_yml(hours); reload_patroni(); time.sleep(3)
    return pg_exec("SHOW recovery_min_apply_delay")

def show_delay():
    try: return pg_exec("SHOW recovery_min_apply_delay")
    except Exception: return None

def api(path, payload, quiet=False):
    payload = dict(payload); payload['key'] = KEY; payload['node'] = NODE
    try:
        r = requests.post(DASHBOARD + path, json=payload, timeout=10)
        j = r.json()
        if not quiet and not j.get('ok'): print(f"[agent] {path} -> {j.get('error')}")
        return j
    except Exception as e:
        if not quiet: print(f"[agent] {path} -> недоступен: {e}")
        return {'ok': False, 'error': str(e)}

def discover_dcs():
    """Авто-обнаружение DCS: читает etcd/consul из patroni.yml и отдаёт адреса."""
    paths = [PATRONI_YML, '/etc/patroni/patroni.yml', '/etc/patroni.yml', '/opt/patroni/patroni.yml']
    cfg = None; used = None
    for path in paths:
        try:
            with open(path) as f: cfg = yaml.safe_load(f) or {}; used = path; break
        except Exception: continue
    if cfg is None:
        raise RuntimeError('patroni.yml не найден ни в одном из путей')
    result = {'path': used, 'etcd': [], 'consul': []}
    # Patroni < 3: top-level etcd/consul; >= 3: cluster.dcs
    dcs = cfg.get('etcd') or cfg.get('consul') or (cfg.get('cluster', {}) or {}).get('dcs') or {}
    scheme = dcs.get('scheme') if isinstance(dcs, dict) else None
    hosts = (dcs.get('hosts') or []) if isinstance(dcs, dict) else []
    if isinstance(hosts, list):
        urls = []
        for h in hosts:
            if isinstance(h, str):
                urls.append(('http://' + h) if not h.startswith('http') else h)
        if scheme == 'consul':
            result['consul'] = urls
        else:
            result['etcd'] = urls
    # отдельная секция consul (Patroni < 3, top-level consul без scheme внутри)
    if not scheme and cfg.get('consul') and isinstance(cfg['consul'], dict) and 'host' in cfg['consul']:
        host = cfg['consul']['host']
        port = cfg['consul'].get('port', 8500)
        result['consul'] = [host if host.startswith('http') else f'http://{host}:{port}']
    return result

def do_command(cmd):
    global last_backups
    act = cmd.get('action'); p = cmd.get('params', {}); cid = cmd.get('id')
    try:
        if act == 'set_recovery_delay': val = set_delay(p.get('hours', 1)); out = f'delay={val}'
        elif act == 'remove_recovery_delay': val = set_delay(None); out = f'delay={val}'
        elif act == 'backup_full': last_backups = walg_backup(True); out = f'full done, {len(last_backups)}'
        elif act == 'backup_delta': last_backups = walg_backup(False); out = f'delta done, {len(last_backups)}'
        elif act == 'list_backups': last_backups = walg_list(); out = f'{len(last_backups)} backups'
        elif act == 'rotate': last_backups = walg_rotate(p.get('keep', 3)); out = f'rotated keep {p.get("keep",3)}'
        elif act == 'delete_backup': last_backups = walg_delete(p.get('name')); out = f'deleted {p.get("name")}'
        elif act == 'verify_backup': out = json.dumps(walg_verify())[:500]
        elif act == 'set_wal_archiving': out = f'archive_mode={set_wal_archiving(bool(p.get("on")))}'
        elif act == 'restore': out = f'restored to {walg_restore(p.get("name"))}'
        elif act == 'pitr': out = f'pitr prepared at {walg_restore(p.get("name"), p.get("target_time"))}'
        elif act == 'clone_restore': out = {'data': clone_restore(p.get('name'), p.get('target_time'), p.get('label'))}
        elif act == 'clone_inspect': out = {'data': clone_inspect(p.get('path'))}
        elif act == 'read_patroni_config':
            paths = [PATRONI_YML, '/etc/patroni/patroni.yml', '/etc/patroni.yml', '/opt/patroni/patroni.yml']
            data = None; used = None
            for path in paths:
                try:
                    with open(path) as f: data = yaml.safe_load(f); used = path; break
                except Exception: continue
            if data is None: raise RuntimeError('patroni.yml не найден ни в одном из путей')
            out = {'data': {'config': data, 'path': used}}
        elif act == 'dcs_discover':
            out = {'data': discover_dcs()}
        elif act == 'os_metrics':
            out = {'data': collect_os_metrics()}
        elif act == 'service':
            out = f'{p.get("action")} {p.get("service")}: {system_service(p.get("service"), p.get("action"))}'
        elif act == 'dump_create': out = {'data': dump_create(p.get('db'), p.get('mode', 'custom'), p.get('jobs', 2), p.get('compress', 3))}
        elif act == 'dump_list': out = {'data': dump_list()}
        elif act == 'dump_rotate': out = f'удалено по TTL: {", ".join(dump_rotate(p.get("days", 7))) or "ничего"}'
        elif act == 'dump_delete': out = f'удалён {dump_delete(p.get("name"))}'
        elif act == 'nfs_mount': out = nfs_mount(p.get('share'), p.get('mountpoint'), p.get('options') or '')
        elif act == 'nfs_umount': out = nfs_umount(p.get('mountpoint'))
        elif act == 'send_report': out = send_report(p.get('to'), p.get('subject') or 'отчёт', p.get('body') or '')
        elif act == 'repack_table': out = repack_table(p.get('db'), p.get('schema'), p.get('table'), p.get('method') or 'auto')
        elif act == 'log_tail': out = {'data': log_tail(p.get('max_mb', 8))}
        elif act == 'restore_test': out = {'data': restore_test(p.get('target_time'), p.get('keep'))}
        else: out = 'unknown action'
        print(f"[agent] {act} -> ok")
        api('/api/agent/result', {'cmd_id': cid, 'action': act, 'ok': True, 'out': out}, quiet=True)
    except Exception as e:
        print(f"[agent] {act} НЕ выполнен: {e}")
        api('/api/agent/result', {'cmd_id': cid, 'action': act, 'ok': False, 'out': str(e)}, quiet=True)

# ---------- журнал PostgreSQL ----------
def log_tail(max_mb=8):
    """Хвост текущего файла журнала (разбор — на стороне консоли)."""
    max_bytes = max(1, min(int(max_mb or 8), 64)) * 1024 * 1024
    rows = pg_exec_rows("SELECT pg_current_logfile() AS f, current_setting('data_directory') AS d, "
                        "current_setting('logging_collector') AS lc")
    r = rows[0] if rows else {}
    if not r.get('f'):
        raise RuntimeError('logging_collector выключен или журнал не в файле (pg_current_logfile() пуст)')
    path = r['f'] if os.path.isabs(r['f']) else os.path.join(r['d'], r['f'])
    size = os.path.getsize(path)
    with open(path, 'rb') as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
            f.readline()   # начинаем с целой строки
        text = f.read().decode('utf-8', 'replace')
    return {'file': path, 'size': size, 'text': text}

# ---------- проверка восстановления ----------
def _find_pg_bin(name, version):
    import glob
    v = str(version).split('.')[0]
    for pat in (f'/usr/pgsql-{v}/bin/{name}', f'/usr/lib/postgresql/{v}/bin/{name}',
                f'/opt/pgpro/*-{v}/bin/{name}', f'/usr/local/pgsql/bin/{name}'):
        for p in glob.glob(pat):
            if os.access(p, os.X_OK): return p
    return shutil.which(name)

def _run_as_pg(argv, timeout=900):
    if NONROOT or os.geteuid() != 0:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    cmd = ' '.join(shlex.quote(a) for a in argv)
    return subprocess.run(['su', '-', 'postgres', '-s', '/bin/bash', '-c', cmd], capture_output=True, text=True, timeout=timeout)

def restore_test(target_time=None, keep=False):
    """Разворачивает последний бэкап во временный каталог, поднимает PostgreSQL на
    порту PG_PORT+200 (только 127.0.0.1), проверяет, что база открывается и данные
    читаются, затем останавливает и удаляет каталог. Живой кластер не затрагивается."""
    t0 = time.time()
    lst = walg_list()
    if not lst: raise RuntimeError('нет бэкапов wal-g')
    latest = sorted(lst, key=lambda b: b.get('time') or b.get('start_time') or '')[-1]
    name = latest.get('backup_name')
    td = os.path.join(RESTORE_BASE, 'rtest_' + time.strftime('%Y%m%d_%H%M%S'))
    os.makedirs(td, exist_ok=True)
    port = PG_PORT + 200
    res = {'backup': name, 'path': td, 'port': port, 'target_time': target_time, 'ok': False}
    pg_ctl = None
    try:
        run_walg(['backup-fetch', td, name])
        res['fetch_s'] = round(time.time() - t0, 1)
        res['size'] = _dir_size(td)
        _prepare_recovery(td, target_time)
        with open(os.path.join(td, 'postgresql.auto.conf'), 'a') as f:
            f.write(f"port = {port}\nlisten_addresses = '127.0.0.1'\nunix_socket_directories = '{td}'\n"
                    "archive_mode = off\nhot_standby = off\nshared_buffers = '128MB'\n"
                    "max_wal_senders = 0\nmax_replication_slots = 0\nwal_level = minimal\n")
        for fn in ('postmaster.pid', 'postmaster.opts'):
            try: os.remove(os.path.join(td, fn))
            except FileNotFoundError: pass
        os.chmod(td, 0o700)
        if not NONROOT and os.geteuid() == 0:
            subprocess.run(['chown', '-R', 'postgres:postgres', td], check=True)
        with open(os.path.join(td, 'PG_VERSION')) as f: ver = f.read().strip()
        res['version'] = ver
        pg_ctl = _find_pg_bin('pg_ctl', ver)
        if not pg_ctl: raise RuntimeError(f'pg_ctl для PostgreSQL {ver} не найден')
        t1 = time.time()
        r = _run_as_pg([pg_ctl, '-D', td, '-l', os.path.join(td, 'restore_test.log'), '-w', '-t', '900', 'start'])
        if r.returncode != 0:
            raise RuntimeError('PostgreSQL не запустился: ' + (r.stderr or r.stdout)[-1500:] + _tail(os.path.join(td, 'restore_test.log')))
        c = None
        for _ in range(900):
            try:
                c = psycopg2.connect(host='127.0.0.1', port=port, user=PG_USER, password=PG_PASS, dbname='postgres', connect_timeout=3)
                c.autocommit = True
                cur = c.cursor(); cur.execute('SELECT pg_is_in_recovery()')
                if not cur.fetchone()[0]: break
                c.close(); c = None
            except Exception:
                c = None
            time.sleep(1)
        if c is None: raise RuntimeError('база не вышла из режима восстановления за 15 минут' + _tail(os.path.join(td, 'restore_test.log')))
        res['recovery_s'] = round(time.time() - t1, 1)
        cur = c.cursor()
        cur.execute('SELECT pg_last_xact_replay_timestamp()::text')
        res['last_xact_replayed'] = cur.fetchone()[0]
        cur.execute("SELECT datname, pg_database_size(datname) FROM pg_database WHERE datallowconn ORDER BY 2 DESC")
        res['databases'] = [{'name': d, 'size': sz} for d, sz in cur.fetchall()]
        checked = []
        for d in res['databases'][:5]:
            try:
                cc = psycopg2.connect(host='127.0.0.1', port=port, user=PG_USER, password=PG_PASS, dbname=d['name'], connect_timeout=5)
                k = cc.cursor()
                k.execute("SELECT count(*) FROM pg_class WHERE relkind = 'r'")
                n = k.fetchone()[0]
                checked.append({'db': d['name'], 'tables': n, 'ok': True}); cc.close()
            except Exception as e:
                checked.append({'db': d['name'], 'ok': False, 'error': str(e)[:300]})
        res['checked'] = checked
        c.close()
        res['consistent_at'] = _recovered_to(os.path.join(td, 'restore_test.log')) or res.get('last_xact_replayed') \
            or latest.get('finish_time') or latest.get('time')
        res['ok'] = all(x['ok'] for x in checked)
        if not res['ok']: raise RuntimeError('не все базы читаются: ' + json.dumps(checked, ensure_ascii=False)[:1500])
        return res
    finally:
        if pg_ctl:
            try: _run_as_pg([pg_ctl, '-D', td, '-m', 'fast', '-w', 'stop'], timeout=120)
            except Exception: pass
        res['duration_s'] = round(time.time() - t0, 1)
        if not keep and os.path.realpath(td).startswith(os.path.realpath(RESTORE_BASE) + os.sep):
            shutil.rmtree(td, ignore_errors=True)

def _tail(path, n=1500):
    try:
        with open(path, errors='replace') as f: return '\n' + f.read()[-n:]
    except Exception: return ''

def _recovered_to(logpath):
    """Момент, на который восстановлены данные (из журнала восстановления)."""
    try:
        with open(logpath, errors='replace') as f: text = f.read()
        m = re.findall(r'last completed transaction was at log time (.+)', text) or \
            re.findall(r'recovery stopping (?:before|after) .*? time (.+)', text)
        return m[-1].strip() if m else None
    except Exception: return None

def main():
    global last_backups
    print(f"[agent] старт: node={NODE} yml={PATRONI_YML} pgdata={PGDATA} walg={find_walg()} cfg={walg_cfg_path()}")
    try: host_info = collect_os_metrics()
    except Exception as e: host_info = {'error': str(e)}
    while True:
        r = api('/api/agent/register', {'caps': ['patroni', 'walg', 'os', 'dumps', 'nfs', 'mail'], 'host': host_info})
        if r.get('ok'): print("[agent] зарегистрирован ✓"); break
        time.sleep(3)
    ensure_pg_hba()
    poll_n = 0
    while True:
        poll_n += 1
        metrics = None
        if poll_n % 3 == 0:
            try: metrics = collect_metrics()
            except Exception as e: metrics = {'error': str(e)}
        if poll_n % 12 == 1:
            try: last_backups = walg_list()
            except Exception: pass
        payload = {'delay': show_delay(), 'backups': last_backups, 'metrics': metrics}
        if poll_n % 6 == 0:
            try: payload['os'] = collect_os_metrics()
            except Exception: pass
        r = api('/api/agent/poll', payload, quiet=True)
        if r.get('ok'):
            for cmd in r.get('commands', []): do_command(cmd)
        time.sleep(INTERVAL)

if __name__ == '__main__':
    main()
