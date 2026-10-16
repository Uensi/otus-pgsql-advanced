// Patroni Console: страница статуса. Данные вставляются только через textContent.
'use strict';
(function () {
  const REFRESH_MS = 5000;
  const ROLE_RU = { leader: 'лидер', primary: 'лидер', standby_leader: 'лидер standby', replica: 'реплика',
    sync_standby: 'синхронная реплика', quorum_standby: 'кворумная реплика' };
  const LEADER = new Set(['leader', 'primary', 'standby_leader', 'master']);
  let csrf = '';
  let cluster = '';
  let timer = null;
  let role = 'viewer';
  let lastStatus = null;
  const LEVEL = { viewer: 0, operator: 1, admin: 2 };
  const ST_RU = { pending: 'в очереди', running: 'выполняется', scheduled: 'запланирована', succeeded: 'выполнена',
    warning: 'выполнена с замечаниями', failed: 'не выполнена', unknown: 'результат неизвестен',
    interrupted: 'прервана', cancelled: 'отменена' };
  const OPS = {
    switchover: { title: 'Switchover', desc: 'Плановая смена лидера. Старый лидер вернётся репликой.', fields: ['candidate_opt', 'scheduled_at'] },
    cancel_switchover: { title: 'Отмена запланированного switchover', desc: 'Удаляет расписание switchover.', fields: [] },
    pause: { title: 'Пауза кластера', desc: 'Patroni перестаёт управлять кластером, автоматический failover выключается.', fields: [] },
    resume: { title: 'Снятие паузы', desc: 'Patroni снова управляет кластером.', fields: [] },
    restart: { title: 'Рестарт PostgreSQL на ноде', desc: 'Перезапуск одной ноды через Patroni.', fields: ['node'] },
    rolling_restart: { title: 'Rolling restart', desc: 'Реплики по одной, затем switchover и рестарт бывшего лидера. Останавливается на первой ошибке.', fields: ['only_pending'] },
    config: { title: 'Изменение глобального конфига', desc: 'PATCH /config. Изменение применяется ко всему кластеру.', fields: [] },
    failover: { title: 'Failover', desc: 'Аварийное назначение лидера. Используйте, когда лидера нет.', fields: ['candidate'], danger: true },
    reinitialize: { title: 'Reinit реплики', desc: 'Удаляет данные реплики и копирует их заново с лидера.', fields: ['node_replica'], danger: true },
  };
  let opKind = null;
  let opExtra = {};
  let opKey = null;
  let lastCheck = null;
  let checkSeq = 0;

  const $ = (id) => document.getElementById(id);
  function el(tag, attrs, ...children) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === 'class') e.className = v; else e.setAttribute(k, v);
    }
    for (const c of children) if (c !== null && c !== undefined) e.append(c instanceof Node ? c : String(c));
    return e;
  }
  function bytes(n) {
    if (n === null || n === undefined) return '—';
    if (n < 1024) return n + ' Б';
    const u = ['КБ', 'МБ', 'ГБ', 'ТБ']; let v = n / 1024, i = 0;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return v.toFixed(v < 10 ? 1 : 0) + ' ' + u[i];
  }

  async function api(path, opts) {
    opts = opts || {};
    const headers = { 'Accept': 'application/json' };
    if (opts.body !== undefined) { headers['Content-Type'] = 'application/json'; headers['X-CSRF-Token'] = csrf; }
    const r = await fetch(path, { method: opts.method || 'GET', headers, credentials: 'same-origin',
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined });
    let j = null;
    try { j = await r.json(); } catch (e) { /* не JSON */ }
    if (r.status === 401 && path !== '/api/auth/login') { showLogin(); throw new Error('требуется вход'); }
    if (!r.ok || !j || !j.ok) throw new Error((j && j.error && j.error.message) || ('HTTP ' + r.status));
    return j.data;
  }

  function showLogin() {
    clearInterval(timer); timer = null;
    $('app').hidden = true; $('login').hidden = false;
  }

  async function start(me) {
    csrf = me.csrf_token;
    role = me.role;
    $('tab-ops').hidden = LEVEL[role] < LEVEL.operator;
    showTab(readTab());
    for (const b of document.querySelectorAll('.ops-bar button')) b.hidden = LEVEL[role] < LEVEL[b.dataset.role];
    $('who').textContent = me.username + ' (' + me.role + ')';
    $('login').hidden = true; $('app').hidden = false;
    const list = await api('/api/clusters');
    const sel = $('cluster'); sel.replaceChildren();
    const saved = (() => { try { return localStorage.getItem('pc.cluster'); } catch (e) { return null; } })();
    for (const c of list) sel.append(el('option', { value: c.name }, c.name));
    cluster = list.some((c) => c.name === saved) ? saved : (list[0] && list[0].name);
    sel.value = cluster;
    refresh();
    timer = setInterval(refresh, REFRESH_MS);
  }

  async function refresh() {
    const name = cluster;
    try {
      const st = await api('/api/clusters/' + encodeURIComponent(name) + '/status');
      if (name !== cluster) return;
      lastStatus = st;
      renderHealth(st); renderTopology(st); renderNodes(st);
      $('updated').textContent = 'обновлено ' + new Date(st.collected_at * 1000).toLocaleTimeString();
    } catch (e) {
      renderHealth({ health: 'critical', warnings: [{ level: 'critical', text: 'Консоль не получила статус: ' + e.message }] });
    }
    api('/api/clusters/' + encodeURIComponent(name) + '/etcd').then(renderEtcd)
      .catch((e) => $('etcd').replaceChildren(el('p', { class: 'state-bad' }, 'etcd: ' + e.message)));
    if (!$('panel-config').hidden) loadConfig();
    if (!$('tab-ops').hidden) {
      api('/api/clusters/' + encodeURIComponent(name) + '/operations').then(renderOps).catch(() => {});
    }
    api('/api/clusters/' + encodeURIComponent(name) + '/events?limit=50').then((d) => { if (name === cluster) renderEvents(d); })
      .catch((e) => $('events').replaceChildren(el('li', { class: 'state-bad' }, 'События: ' + e.message)));
    api('/api/clusters/' + encodeURIComponent(name) + '/history').then(renderHistory)
      .catch((e) => $('history').replaceChildren(el('li', { class: 'state-bad' }, e.message)));
  }

  function renderHealth(st) {
    const box = $('health');
    box.className = 'health ' + (st.health || 'ok');
    const verdict = st.health === 'ok'
      ? (st.leader ? 'Кластер работает нормально, лидер ' + st.leader : 'Кластер работает нормально')
      : st.health === 'degraded' ? 'Есть замечания, требующие внимания' : 'Кластер в аварийном состоянии';
    const ul = el('ul');
    for (const w of st.warnings || []) ul.append(el('li', { class: w.level }, (w.node ? w.node + ': ' : '') + w.text));
    box.replaceChildren(el('p', { class: 'verdict' }, verdict), (st.warnings || []).length ? ul : null);
  }

  const SVG = 'http://www.w3.org/2000/svg';
  function crown(title) {
    const svg = document.createElementNS(SVG, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('class', 'crown');
    svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', title || 'лидер');
    const t = document.createElementNS(SVG, 'title'); t.textContent = title || 'лидер (мастер)';
    const path = document.createElementNS(SVG, 'path');
    path.setAttribute('d', 'M2.5 7.5l5 4 4.5-7 4.5 7 5-4-2 10.5h-15zM4.5 19.5h15V21h-15z');
    svg.append(t, path);
    return svg;
  }

  function renderTopology(st) {
    const box = $('topology');
    const members = (st.members || []).filter((m) => m.in_dcs);
    const leader = members.find((m) => LEADER.has(m.role));
    const replicas = members.filter((m) => m !== leader);
    const head = leader
      ? el('div', { class: 'leader-row' }, el('span', { class: 'name' }, crown(), leader.name),
        el('span', { class: 'muted' }, (ROLE_RU[leader.role] || leader.role) + ', таймлайн ' + leader.timeline + (st.paused ? ', пауза' : '')))
      : el('div', { class: 'leader-row' }, el('span', { class: 'name state-bad' }, 'Лидера нет'));
    const max = Math.max(1, ...replicas.map((m) => m.lag || 0));
    const rows = replicas.map((m) => {
      const pct = m.lag ? Math.max(1, Math.round(100 * m.lag / max)) : 0;
      const bad = m.state !== 'streaming' && m.state !== 'running';
      const cls = 'replica' + (bad || m.lag_unknown ? ' crit' : (st.warnings || []).some((w) => w.node === m.name && w.code === 'lag') ? ' warn' : '');
      const fill = el('div', { class: 'fill' }); fill.style.width = (m.lag_unknown ? 100 : pct) + '%';
      return el('div', { class: cls },
        el('span', null, m.name, el('span', { class: 'muted' }, ' · ' + (ROLE_RU[m.role] || m.role || '?'))),
        el('div', { class: 'track', role: 'img', 'aria-label': 'лаг ' + bytes(m.lag) }, fill),
        el('span', { class: 'val' + (bad ? ' state-bad' : '') }, bad ? m.state : (m.lag_unknown ? 'неизвестен' : bytes(m.lag))));
    });
    box.replaceChildren(head, ...(rows.length ? rows : [el('p', { class: 'muted' }, 'Реплик нет')]));
  }

  function renderNodes(st) {
    const tb = $('nodes').querySelector('tbody');
    tb.replaceChildren(...(st.members || []).map((m) => {
      const tags = el('td');
      for (const [k, v] of Object.entries(m.tags || {})) tags.append(el('span', { class: 'tag' }, k + '=' + v));
      const state = m.reachable === false ? 'недоступна' : (m.state || '—');
      const bad = m.reachable === false || (m.state && !['running', 'streaming'].includes(m.state));
      return el('tr', null,
        el('td', null, LEADER.has(m.role) && m.in_dcs ? crown() : null, m.name,
          m.pending_restart ? el('span', { class: 'tag' }, 'нужен рестарт') : null),
        el('td', null, ROLE_RU[m.role] || m.role || '—'),
        el('td', { class: bad ? 'state-bad' : '' }, state),
        el('td', { class: 'num' }, m.timeline ?? '—'),
        el('td', { class: 'num' }, LEADER.has(m.role) ? '—' : (m.lag_unknown ? 'неизвестен' : bytes(m.lag))),
        el('td', { class: 'lsn' }, m.lsn || '—'),
        tags,
        el('td', { class: 'num' }, m.response_ms ?? '—'));
    }));
  }

  function renderEtcd(h) {
    const box = $('etcd');
    if (!h.configured) { box.replaceChildren(el('p', { class: 'muted' }, 'Адреса etcd для этого кластера не заданы в конфиге.')); return; }
    const summary = el('p', { class: h.quorum && !h.split_leader_view ? '' : 'state-bad' },
      (h.quorum ? 'Кворум есть' : 'Кворума нет') + (h.split_leader_view ? ', ноды видят разных лидеров' : '') +
      ', членов: ' + (h.members || []).length);
    const eps = (h.endpoints || []).map((e) => el('div', { class: 'etcd-ep' },
      el('div', { class: 'ep' }, e.endpoint, e.is_leader ? el('span', { class: 'tag' }, 'лидер') : null),
      e.error ? el('div', { class: 'state-bad' }, e.error)
        : el('div', { class: 'muted' }, 'v' + e.version + ', база ' + bytes(e.db_size) + ', raft ' + e.raft_term + '/' + e.raft_index +
          ((e.alarms || []).length ? ', alarm: ' + e.alarms.join(', ') : ''))));
    box.replaceChildren(summary, ...eps);
  }

  function renderHistory(rows) {
    const ol = $('history');
    if (!rows.length) { ol.replaceChildren(el('li', { class: 'muted' }, 'Переключений не было')); return; }
    ol.replaceChildren(...rows.slice(0, 15).map((r) => el('li', null,
      el('strong', null, 'Таймлайн ' + r.timeline + ' → ' + (r.timeline + 1)),
      ' ' + (r.new_leader ? 'новый лидер ' + r.new_leader : ''),
      el('div', { class: 'muted' }, (r.timestamp || '') + ' ' + (r.reason || '')))));
  }

  // ---------- конфиг ----------
  function flatten(obj, prefix) {
    const out = [];
    for (const [k, v] of Object.entries(obj || {})) {
      const p = prefix ? prefix + '.' + k : k;
      if (v && typeof v === 'object' && !Array.isArray(v) && Object.keys(v).length) out.push(...flatten(v, p));
      else out.push([p, v]);
    }
    return out;
  }
  function fmtVal(v) {
    if (v === null || v === undefined) return '—';
    return typeof v === 'object' ? JSON.stringify(v) : String(v);
  }
  function diffTable(rows) {
    if (!rows.length) return el('p', { class: 'muted' }, 'Изменений нет');
    return el('table', { class: 'diff' }, el('tbody', null, ...rows.map((r) => el('tr', null,
      el('td', null, r.path),
      el('td', { class: 'old' }, r.old === null || r.old === undefined ? '' : fmtVal(r.old)),
      el('td', { class: 'new' }, r.new === null || r.new === undefined ? 'удалить' : fmtVal(r.new))))));
  }
  function parseValue(raw) {
    const v = raw.trim();
    if (v === 'null') return null;
    if (v === 'true' || v === 'false') return v === 'true';
    if (/^-?\d+$/.test(v)) return parseInt(v, 10);
    if (/^".*"$/.test(v) || /^'.*'$/.test(v)) return v.slice(1, -1);
    if (/^[\[{]/.test(v)) { try { return JSON.parse(v); } catch (e) { throw new Error('не JSON: ' + v); } }
    return v;
  }
  function parseChanges(text) {
    const changes = {};
    text.split('\n').forEach((line, i) => {
      const l = line.trim();
      if (!l || l.startsWith('#')) return;
      const eq = l.indexOf('=');
      if (eq < 1) throw new Error('строка ' + (i + 1) + ': нужно «ключ = значение»');
      const path = l.slice(0, eq).trim().split('.');
      let node = changes;
      for (const part of path.slice(0, -1)) {
        if (!part) throw new Error('строка ' + (i + 1) + ': пустая часть ключа');
        node = node[part] = (node[part] && typeof node[part] === 'object') ? node[part] : {};
      }
      node[path[path.length - 1]] = parseValue(l.slice(eq + 1));
    });
    if (!Object.keys(changes).length) throw new Error('введите хотя бы одно изменение');
    return changes;
  }
  function renderConfig(cfg) {
    const rows = flatten(cfg).filter(([p]) => p !== 'pause').sort((a, b) => a[0].localeCompare(b[0]));
    $('cfg-table').querySelector('tbody').replaceChildren(...rows.map(([p, v]) => el('tr', null, el('td', null, p), el('td', null, fmtVal(v)))));
    $('cfg-form').hidden = role !== 'admin';
  }
  const SRC_RU = { console: 'через консоль', external: 'вне консоли' };
  function renderVersions(list) {
    if (!list.length) { $('cfg-versions').replaceChildren(el('li', { class: 'muted' }, 'Изменений пока не было')); return; }
    $('cfg-versions').replaceChildren(...list.map((v) => {
      const li = el('li', null,
        el('strong', null, 'Версия №' + v.id), ' ',
        el('span', { class: 'muted' }, new Date(v.ts * 1000).toLocaleString() + ', ' + (SRC_RU[v.source] || v.source) +
          (v.username ? ', ' + v.username : '') + (v.op_id ? ', операция №' + v.op_id : '')),
        diffTable(v.diff));
      if (role === 'admin') {
        const b = el('button', { type: 'button', class: 'link' }, 'Откатить это изменение');
        b.addEventListener('click', () => openOp('config', { rollback_to: v.id }));
        li.append(el('div', { class: 'actions' }, b));
      }
      return li;
    }));
  }
  function loadConfig() {
    const name = cluster;
    api('/api/clusters/' + encodeURIComponent(name) + '/config').then((d) => { if (name === cluster) renderConfig(d); })
      .catch((e) => $('cfg-table').querySelector('tbody').replaceChildren(el('tr', null, el('td', { colspan: '2', class: 'state-bad' }, e.message))));
    api('/api/clusters/' + encodeURIComponent(name) + '/config/versions').then((d) => { if (name === cluster) renderVersions(d); })
      .catch(() => {});
  }

  // ---------- вкладки ----------
  const TABS = ['overview', 'events', 'config', 'ops'];
  function readTab() {
    const h = location.hash.replace('#', '');
    return TABS.includes(h) ? h : 'overview';
  }
  function showTab(name) {
    if (name === 'ops' && $('tab-ops').hidden) name = 'overview';
    for (const t of TABS) {
      $('tab-' + t).setAttribute('aria-selected', String(t === name));
      $('tab-' + t).tabIndex = t === name ? 0 : -1;
      $('panel-' + t).hidden = t !== name;
    }
    if (name === 'config' && csrf) loadConfig();
  }
  function onTabKey(ev) {
    const visible = TABS.filter((t) => !$('tab-' + t).hidden);
    const i = visible.indexOf(ev.target.dataset.tab);
    const next = ev.key === 'ArrowRight' ? visible[(i + 1) % visible.length]
      : ev.key === 'ArrowLeft' ? visible[(i - 1 + visible.length) % visible.length] : null;
    if (next) { ev.preventDefault(); location.hash = next; $('tab-' + next).focus(); }
  }

  // ---------- события ----------
  const NOTIFY_RU = { pending: 'ждёт отправки', sent: 'отправлено', failed: 'не отправлено' };
  function renderEvents(d) {
    const w = d.watcher || {};
    let line;
    if (!w.running) line = 'Наблюдатель не запущен: события не записываются.';
    else if (w.error) line = 'Наблюдатель: ошибка последней проверки: ' + w.error;
    else if (w.last_run) line = 'Наблюдатель проверяет кластер каждые ' + w.interval + ' с, последняя проверка в ' +
      new Date(w.last_run * 1000).toLocaleTimeString() + '.';
    else line = 'Наблюдатель запускается.';
    line += d.channels.length ? ' Уведомления: ' + d.channels.map((c) => c.name + ' (от ' + c.min_level + ')').join(', ') + '.'
      : ' Каналы уведомлений не настроены.';
    $('watcher-line').textContent = line;
    $('notify-test').hidden = role !== 'admin' || !d.channels.length;
    const open = new Set(d.open.map((c) => c.event_id));
    const badge = $('open-count');
    badge.hidden = !d.open.length; badge.textContent = d.open.length;
    badge.className = 'count' + (d.open.some((c) => c.level === 'critical') ? '' : ' warn');
    badge.title = 'открытых проблем: ' + d.open.length;
    if (!d.events.length) { $('events').replaceChildren(el('li', { class: 'muted' }, 'Событий пока не было')); return; }
    $('events').replaceChildren(...d.events.map((e) => {
      const cls = e.kind === 'resolved' ? 'resolved' : e.level;
      const text = el('div', null, (e.node && e.kind !== 'leader_change' && !e.text.includes(e.node + ':') ? e.node + ': ' : '') + e.text);
      if (open.has(e.id)) text.append(' ', el('span', { class: 'open-now' }, 'продолжается'));
      if (e.notifications && e.notifications.length) {
        text.append(el('span', { class: 'sent' }, e.notifications.map((n) =>
          n.channel + ': ' + (NOTIFY_RU[n.status] || n.status) + (n.status === 'pending' && n.attempts ? ', попыток ' + n.attempts : '')).join(' · ')));
      }
      return el('li', { class: cls }, el('span', { class: 'when' }, new Date(e.ts * 1000).toLocaleString()), text);
    }));
  }

  async function notifyTest() {
    $('notify-test-result').textContent = '';
    try {
      await api('/api/clusters/' + encodeURIComponent(cluster) + '/notify-test', { method: 'POST', body: {} });
      $('notify-test-result').textContent = 'Отправлено в очередь, статус появится в ленте.';
      refresh();
    } catch (e) { $('notify-test-result').textContent = e.message; }
  }

  // ---------- операции ----------
  function renderOps(list) {
    const active = list.find((o) => o.status === 'running' || o.status === 'pending' || o.status === 'scheduled');
    $('active-op').hidden = !active;
    $('active-op').textContent = active ? (active.status === 'scheduled' ? 'запланирована' : 'идёт') : '';
    const tb = $('ops').querySelector('tbody');
    if (!list.length) { tb.replaceChildren(el('tr', null, el('td', { colspan: '7', class: 'muted' }, 'Операций ещё не было'))); return; }
    tb.replaceChildren(...list.slice(0, 20).map((o) => {
      const params = Object.entries(o.params || {}).filter(([k]) => k !== 'base_hash')
        .map(([k, v]) => k === 'changes' ? flatten(v).map(([p, x]) => p + '=' + fmtVal(x)).join(', ') : k + '=' + v).join(', ') || '—';
      const logList = el('ol');
      for (const l of o.log || []) logList.append(el('li', null, new Date(l.ts * 1000).toLocaleTimeString() + ' ' + l.msg));
      const last = (o.log || []).slice(-1)[0];
      return el('tr', null,
        el('td', null, o.id), el('td', null, (OPS[o.kind] || {}).title || o.kind), el('td', null, params),
        el('td', { class: 'st-' + o.status }, ST_RU[o.status] || o.status),
        el('td', null, o.created_by), el('td', null, new Date(o.created_at * 1000).toLocaleString()),
        el('td', null, el('details', null, el('summary', null, last ? last.msg : '—'), logList)));
    }));
  }

  function nodeSelect(name, label, filter, optional) {
    const sel = el('select', { name });
    if (optional) sel.append(el('option', { value: '' }, 'выберет Patroni'));
    for (const m of (lastStatus && lastStatus.members) || []) {
      if (m.unconfigured || !filter(m)) continue;
      sel.append(el('option', { value: m.name }, (LEADER.has(m.role) ? '♛ ' : '') + m.name + ' · ' + (ROLE_RU[m.role] || m.role || '?') +
        (LEADER.has(m.role) ? '' : ', лаг ' + (m.lag_unknown ? 'неизвестен' : bytes(m.lag)))));
    }
    return el('label', null, label, sel);
  }

  function buildFields(kind) {
    const box = $('op-fields'); box.replaceChildren();
    const notLeader = (m) => !LEADER.has(m.role) && m.in_dcs;
    for (const f of OPS[kind].fields) {
      if (f === 'candidate_opt') box.append(nodeSelect('candidate', 'Новый лидер', notLeader, true));
      if (f === 'candidate') box.append(nodeSelect('candidate', 'Новый лидер', notLeader, false));
      if (f === 'node') box.append(nodeSelect('node', 'Нода', () => true, false));
      if (f === 'node_replica') box.append(nodeSelect('node', 'Реплика', (m) => !LEADER.has(m.role), false));
      if (f === 'scheduled_at') box.append(el('label', null, 'Выполнить в (пусто — сейчас)', el('input', { type: 'datetime-local', name: 'scheduled_at' })));
      if (f === 'only_pending') {
        const cb = el('input', { type: 'checkbox', name: 'only_pending' }); cb.checked = true;
        box.append(el('label', { class: 'check-line' }, cb, 'Только ноды, которые ждут рестарта'));
      }
    }
  }

  function isoWithOffset(local) {
    // datetime-local не содержит пояса: добавляем пояс браузера
    const d = new Date(local);
    const off = -d.getTimezoneOffset(), sign = off >= 0 ? '+' : '-';
    const pad = (n) => String(Math.floor(Math.abs(n))).padStart(2, '0');
    return local + (local.length === 16 ? ':00' : '') + sign + pad(off / 60) + ':' + pad(off % 60);
  }

  function readParams() {
    const p = Object.assign({}, opExtra);
    for (const i of $('op-fields').querySelectorAll('select, input')) {
      if (i.type === 'checkbox') p[i.name] = i.checked;
      else if (i.name === 'scheduled_at') { if (i.value) p.scheduled_at = isoWithOffset(i.value); }
      else if (i.value) p[i.name] = i.value;
    }
    return p;
  }

  async function runCheck() {
    const seq = ++checkSeq;
    lastCheck = null; updateRunButton();
    $('op-checks').replaceChildren(el('p', { class: 'muted' }, 'Проверяю кластер…'));
    try {
      const r = await api('/api/clusters/' + encodeURIComponent(cluster) + '/operations/precheck',
        { method: 'POST', body: { kind: opKind, params: readParams() } });
      if (seq !== checkSeq) return;
      lastCheck = r;
      if (r.preview) {
        if (!opExtra.base_hash) opExtra.base_hash = r.preview.base_hash;
        $('op-preview').replaceChildren(diffTable(r.preview.diff));
      }
      const LV = { block: 'Нельзя', force: 'Опасно', warn: 'Внимание' };
      const items = r.checks.map((c) => el('li', { class: c.level }, el('strong', null, LV[c.level] + ': '), c.text));
      $('op-checks').replaceChildren(items.length ? el('ul', null, ...items) : el('p', { class: 'ok' }, 'Проверки пройдены'));
      $('op-ack-wrap').hidden = !r.needs_ack;
      $('op-force-wrap').hidden = !r.needs_force;
    } catch (e) {
      if (seq === checkSeq) $('op-checks').replaceChildren(el('p', { class: 'state-bad' }, 'Проверка не выполнена: ' + e.message));
    }
    updateRunButton();
  }

  function updateRunButton() {
    const c = lastCheck;
    const ok = c && c.can_run && $('op-confirm').value === cluster &&
      (!c.needs_ack || $('op-ack').checked) && (!c.needs_force || $('op-force').checked);
    $('op-run').disabled = !ok;
  }

  function openOp(kind, extra) {
    opKind = kind;
    opExtra = Object.assign({}, extra || {});
    $('op-preview').replaceChildren();
    opKey = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now()) + Math.random();
    const o = OPS[kind];
    $('op-title').textContent = o.title;
    $('op-desc').textContent = o.desc;
    $('op-cluster').textContent = cluster;
    $('op-confirm').value = ''; $('op-ack').checked = false; $('op-force').checked = false;
    $('op-ack-wrap').hidden = true; $('op-force-wrap').hidden = true; $('op-error').textContent = '';
    $('op-run').className = o.danger ? 'danger' : '';
    buildFields(kind);
    $('op-dialog').showModal();
    runCheck();
  }

  async function submitOp() {
    $('op-run').disabled = true; $('op-error').textContent = '';
    try {
      const headers = { 'Accept': 'application/json', 'Content-Type': 'application/json', 'X-CSRF-Token': csrf, 'Idempotency-Key': opKey };
      const r = await fetch('/api/clusters/' + encodeURIComponent(cluster) + '/operations', {
        method: 'POST', headers, credentials: 'same-origin',
        body: JSON.stringify({ kind: opKind, params: readParams(), confirm: $('op-confirm').value,
          acknowledge_warnings: $('op-ack').checked, force: $('op-force').checked }) });
      const j = await r.json();
      if (!j.ok) {
        $('op-error').textContent = j.error.message;
        if (j.error.checks) { lastCheck = null; runCheck(); }
        return;
      }
      $('op-dialog').close();
      refresh();
    } catch (e) { $('op-error').textContent = e.message; updateRunButton(); }
  }

  document.addEventListener('DOMContentLoaded', () => {
    for (const b of document.querySelectorAll('.ops-bar button')) b.addEventListener('click', () => openOp(b.dataset.op));
    $('op-fields').addEventListener('change', runCheck);
    for (const id of ['op-confirm', 'op-ack', 'op-force']) $(id).addEventListener('input', updateRunButton);
    $('op-cancel').addEventListener('click', () => $('op-dialog').close());
    $('op-run').addEventListener('click', submitOp);
    $('notify-test').addEventListener('click', notifyTest);
    for (const t of TABS) {
      $('tab-' + t).addEventListener('click', () => { location.hash = t; });
      $('tab-' + t).addEventListener('keydown', onTabKey);
    }
    window.addEventListener('hashchange', () => showTab(readTab()));
    $('cfg-form').addEventListener('submit', (ev) => {
      ev.preventDefault();
      $('cfg-error').textContent = '';
      try { openOp('config', { changes: parseChanges($('cfg-changes').value) }); } catch (e) { $('cfg-error').textContent = e.message; }
    });
    $('login-form').addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const f = new FormData(ev.target);
      $('login-error').textContent = '';
      try {
        const me = await api('/api/auth/login', { method: 'POST', body: { username: f.get('username'), password: f.get('password') } });
        ev.target.reset();
        start(me);
      } catch (e) { $('login-error').textContent = e.message; }
    });
    $('logout').addEventListener('click', async () => {
      try { await api('/api/auth/logout', { method: 'POST', body: {} }); } catch (e) { /* уже вышли */ }
      showLogin();
    });
    $('cluster').addEventListener('change', (ev) => {
      cluster = ev.target.value;
      try { localStorage.setItem('pc.cluster', cluster); } catch (e) { /* без localStorage */ }
      refresh();
    });
    api('/api/auth/me').then(start).catch(showLogin);
  });
})();
