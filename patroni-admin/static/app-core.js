/* Patroni Console — CORE [core-20261017a]
   Ядро: навигация, опрос кластера, лента узлов, сводка здоровья, график лага,
   авторизация, подтверждения. Все обновления DOM идут через morph() (ui-kit.js):
   перерисовывается только то, что изменилось, — без мерцания. */
var APP_VERSION = 'core-20261017a';
var REFRESH = 3000;
var clusterData = null, lsnData = {};
window.__agents = window.__agents || {};
var lagChart = null;
var isPaused = false;
var inflight = {};

var LEADER_ROLES = ['leader','master','primary','standby_leader'];
function isLeaderRole(r){ return LEADER_ROLES.indexOf(String(r||'').toLowerCase()) >= 0; }
/* Patroni 3.x: лидер — running, реплика — streaming (раньше тоже running).
   Промежуточные состояния не авария, но требуют внимания. */
var STATE_OK = ['running','streaming'];
var STATE_TRANSIENT = ['starting','stopping','restarting','in archive recovery','creating replica','initializing new cluster','running custom bootstrap script','starting after demotion'];
function stateLevel(st){
  st = String(st||'').toLowerCase();
  if(STATE_OK.indexOf(st) >= 0) return 'ok';
  if(STATE_TRANSIENT.indexOf(st) >= 0) return 'warn';
  return 'bad';
}
var STATE_RU = {
  'running':'работает', 'streaming':'поток WAL', 'stopped':'остановлен', 'starting':'запускается',
  'stopping':'останавливается', 'restarting':'перезапуск', 'in archive recovery':'восстановление из архива',
  'creating replica':'создаётся реплика', 'crashed':'аварийно остановлен', 'start failed':'не запустился', 'unknown':'неизвестно'
};
function stateRu(st){ return STATE_RU[String(st||'').toLowerCase()] || st || '—'; }

var LAG_WARN = 10485760, LAG_CRIT = 104857600;   /* 10 МБ / 100 МБ WAL */

function cssVar(name){ return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#888'; }
function lsnToString(v){
  if(v==null||v===''||v==='-') return '—';
  try{
    if(typeof v==='string' && v.indexOf('/')>=0) return v.toUpperCase();
    var n=BigInt(v);
    return ((n>>32n)&0xFFFFFFFFn).toString(16).toUpperCase()+'/'+(n&0xFFFFFFFFn).toString(16).toUpperCase().padStart(8,'0');
  }catch(e){ return (typeof v==='string' && v) ? v : '—'; }
}
function fb(b){
  if(b==null||b===''||b==='-') return '—';
  var n=Number(b); if(isNaN(n)||n<0) return '—';
  if(n<1024) return n+' B';
  if(n<1048576) return (n/1024).toFixed(1)+' KB';
  if(n<1073741824) return (n/1024/1024).toFixed(1)+' MB';
  if(n<1099511627776) return (n/1073741824).toFixed(2)+' GB';
  return (n/1099511627776).toFixed(2)+' TB';
}
function esc(t){
  if(t==null) return '';
  return String(t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#039;');
}
function setHTML(el, html){ morph(el, html); }
function lagNum(x){ return typeof x === 'number' ? x : 0; }
function lagClass(b){ var n=Number(b||0); if(n===0) return 'lag-zero'; if(n<LAG_WARN) return ''; if(n<LAG_CRIT) return 'lag-warn'; return 'lag-crit'; }
function timeRu(d){ return (d || new Date()).toLocaleTimeString('ru-RU'); }

/* ---------- журнал + уведомления ---------- */
var logCount = 0, bootTs = Date.now(), toastSeen = {};
window.__inflight = inflight;
function log(msg, type){
  var box = document.getElementById('op-log'); if(!box) return;
  var line = document.createElement('div');
  line.className = 'log-line ' + (type || 'info');
  var t = document.createElement('span'); t.className = 'log-t'; t.textContent = timeRu();
  var m = document.createElement('span'); m.className = 'log-m'; m.textContent = msg;
  line.appendChild(t); line.appendChild(m);
  box.insertBefore(line, box.firstChild);
  while(box.childNodes.length > 400) box.removeChild(box.lastChild);
  logCount++;
  var lc = document.getElementById('log-count'); if(lc) lc.textContent = logCount;
  /* результат действий дублируем всплывающим уведомлением; одинаковые
     сообщения (например, повторная ошибка опроса) не чаще раза в минуту */
  if((type === 'ok' || type === 'err' || type === 'warn') && Date.now() - bootTs > 2500 && window.toast){
    var now = Date.now();
    if(!toastSeen[msg] || now - toastSeen[msg] > 60000){ toastSeen[msg] = now; toast(msg, type); }
  }
}

/* ---------- HTTP ---------- */
function setConn(ok){
  var ci = document.getElementById('conn-ind'); if(!ci) return;
  ci.classList.toggle('ok', ok); ci.classList.toggle('err', !ok);
  var b = ci.querySelector('b'); if(b) b.textContent = ok ? 'Связь' : 'Нет связи';
  ci.setAttribute('data-hint', ok ? 'Консоль получает данные от сервера' : 'Сервер консоли не отвечает — данные на экране могут быть устаревшими');
}
function isUnauthorized(j){ return !!j && j.ok === false && j.error === 'unauthorized'; }
function parseResp(r){
  var ct = r.headers.get('content-type') || '';
  if(ct.indexOf('application/json') < 0) throw new Error(r.status===404?'эндпоинт не найден (404)':'сервер вернул не JSON (HTTP '+r.status+')');
  return r.json();
}
/* allowSkip: если такой же запрос уже в полёте, второй вызывающий получает
   тот же промис (а не дубль запроса и не пустой ответ). */
function getJSON(url, allowSkip){
  if(allowSkip && inflight[url]) return inflight[url];
  var p = fetch(url, {headers:{'X-Requested-With':'patroni-console'}, credentials:'same-origin'}).then(function(r){ setConn(true); return parseResp(r); }).then(function(j){
    if(isUnauthorized(j) && !window.__inLogin){ showLogin(); }
    delete inflight[url]; return j;
  }, function(e){ delete inflight[url]; if(e instanceof TypeError) setConn(false); throw e; });
  inflight[url] = p;
  return p;
}
function postJSON(url, body){
  var opts = { method:'POST', credentials:'same-origin',
               headers:{'Content-Type':'application/json','X-Requested-With':'patroni-console'},
               body:JSON.stringify(body) };
  return fetch(url, opts).then(function(r){ setConn(true); return parseResp(r); }).then(function(j){
    if(isUnauthorized(j) && !window.__inLogin){ showLogin(); }
    return j;
  }, function(e){ if(e instanceof TypeError) setConn(false); throw e; });
}
function handleResp(j, name, ok){
  if(j && j.ok) log(ok, 'ok'); else log(name+': '+((j && j.error)||'ошибка'), 'err');
}

/* ---------- графики ---------- */
function fmtBytes(v){ return fb(v); }
var PALETTE = ['#3d7ab5','#3f9b68','#c48a1d','#cc4a3c','#7b6bb5','#2f9aa0'];
function makeLagOptions(){
  var x = { type:'time', grid:{color:cssVar('--grid')}, ticks:{color:cssVar('--muted'), maxTicksLimit:8} };
  x.time = { tooltipFormat:'HH:mm:ss', displayFormats:{second:'HH:mm:ss', minute:'HH:mm'} };
  var y = { beginAtZero:true, grid:{color:cssVar('--grid')}, ticks:{color:cssVar('--muted')} };
  y.ticks.callback = fmtBytes;
  return { responsive:true, maintainAspectRatio:false, animation:false, interaction:{mode:'index', intersect:false},
           plugins:{legend:{labels:{color:cssVar('--dim'),boxWidth:12}}, tooltip:{callbacks:{label:function(c){ return c.dataset.label+': '+fb(c.parsed.y); }}}},
           scales:{x:x, y:y} };
}
function setChartColors(chart){
  if(!chart) return;
  var g = cssVar('--grid'), m = cssVar('--muted'), d = cssVar('--dim');
  try{
    chart.options.scales.x.ticks.color = m; chart.options.scales.x.grid.color = g;
    chart.options.scales.y.ticks.color = m; chart.options.scales.y.grid.color = g;
    if(chart.options.plugins.legend.labels) chart.options.plugins.legend.labels.color = d;
    chart.update('none');
  }catch(e){}
}
/* Обновляет наборы данных графика «на месте»: тот же объект dataset получает
   новые точки. Легенда и цвета не пересоздаются, график не мигает. */
function syncDatasets(chart, series, style){
  if(!chart) return;
  var cur = chart.data.datasets, byLabel = {};
  cur.forEach(function(d){ byLabel[d.label] = d; });
  var out = series.map(function(s, i){
    var d = byLabel[s.label];
    if(!d){
      var color = PALETTE[i % PALETTE.length];
      d = Object.assign({ label:s.label, data:[], borderColor:color, backgroundColor:color+'22', tension:0.3, fill:false, pointRadius:0, pointHoverRadius:4, borderWidth:2 }, style || {});
    }
    d.data = s.data;
    return d;
  });
  var changed = out.length !== cur.length || out.some(function(d, i){ return d !== cur[i]; });
  if(changed) chart.data.datasets = out;
  chart.update('none');
}
window.syncDatasets = syncDatasets;
window.CHART_PALETTE = PALETTE;

/* ---------- экраны и навигация ---------- */
var SCREENS = {
  center:{loaded:false, pollMs:15000},
  cluster:{loaded:false}, metrics:{loaded:false, pollMs:3000},
  schema:{loaded:false}, analyzer:{loaded:false}, locks:{loaded:false, pollMs:10000},
  db:{loaded:false, pollMs:15000},
  dba:{loaded:false, pollMs:60000}, querylab:{loaded:false}, ops:{loaded:false},
  backups:{loaded:false, pollMs:15000}, slots:{loaded:false, pollMs:15000}, reports:{loaded:false},
  alerts:{loaded:false, pollMs:10000}, admin:{loaded:false}, audit:{loaded:false, pollMs:30000},
  hosts:{loaded:false, pollMs:30000}, maint:{loaded:false}, tune:{loaded:false}, ai:{loaded:false},
  risk:{loaded:false, pollMs:30000}, ash:{loaded:false, pollMs:10000}, perf:{loaded:false, pollMs:3000},
  logs:{loaded:false}, security:{loaded:false}, upgrade:{loaded:false}
};
var activeScreen = 'cluster';

function loadScreen(name){
  var s = SCREENS[name]; if(!s || s.loaded) return; s.loaded = true;
  var p = null;
  if(name === 'cluster'){ p = fetchCluster(); }
  else {
    var loader = (window.__loaders || {})[name];
    if(typeof loader === 'function'){ try{ p = loader(); }catch(e){ log('экран '+name+': '+e.message, 'err'); } }
  }
  if(p && typeof p.catch === 'function'){ p.catch(function(e){ log('экран '+name+': '+e.message, 'err'); }); }
}
var groupOf = {
  center:'mon', cluster:'mon', risk:'mon', metrics:'mon', reports:'mon', alerts:'mon',
  db:'db', schema:'db', analyzer:'db', querylab:'db', locks:'db', ash:'db',
  tune:'opt', perf:'opt',
  dba:'ops', maint:'ops', hosts:'ops', backups:'ops', slots:'ops', ops:'ops', logs:'ops',
  ai:'adm', admin:'adm', audit:'adm', security:'adm', upgrade:'adm'
};
var openGroup = null;
var groupLast = {mon:'cluster', db:'db', opt:'tune', ops:'dba', adm:'ai'};
var SHORT = {
  cluster:'Кластер', center:'Все кластеры', metrics:'Метрики', reports:'Отчёты', alerts:'Алерты',
  db:'Базы данных', schema:'Схема / ERD', analyzer:'Анализатор', querylab:'SQL‑консоль', locks:'Блокировки',
  tune:'Параметры', dba:'DBA', maint:'Обслуживание', hosts:'Хосты', backups:'Бэкапы', slots:'Слоты', ops:'Операции',
  ai:'Ассистент', admin:'Админ', audit:'Аудит',
  risk:'Риски', ash:'Активные сессии', perf:'Производительность', logs:'Журналы', security:'Безопасность', upgrade:'Обновления'
};

function setExpanded(g, on){
  var b = g.querySelector('.nav-group-btn');
  if(b) b.setAttribute('aria-expanded', on ? 'true' : 'false');
}
function closeGroups(){
  var gs = document.querySelectorAll('.nav-group');
  for(var i=0;i<gs.length;i++){ gs[i].classList.remove('open'); setExpanded(gs[i], false); }
  openGroup = null;
}
function toggleGroup(g){
  if(openGroup === g){ closeGroups(); return; }
  closeGroups();
  g.classList.add('open'); setExpanded(g, true);
  openGroup = g;
}
function updateNav(){
  var groups = document.querySelectorAll('.nav-group');
  for(var i=0;i<groups.length;i++){
    var g = groups[i], grp = g.getAttribute('data-group');
    var label = g.querySelector('.nav-group-label');
    var txt = SHORT[groupLast[grp]] || '';
    if(label && label.textContent !== txt) label.textContent = txt;
    g.classList.toggle('active', grp === groupOf[activeScreen]);
  }
}

function showScreen(name, noHash){
  if(!SCREENS[name]) name = 'cluster';
  activeScreen = name;
  var screens = document.querySelectorAll('.screen');
  for(var i=0;i<screens.length;i++) screens[i].classList.toggle('active', screens[i].getAttribute('data-screen')===name);
  var chips = document.querySelectorAll('.chip');
  for(var j=0;j<chips.length;j++){
    var on = chips[j].getAttribute('data-screen')===name;
    chips[j].classList.toggle('active', on);
    if(on) chips[j].setAttribute('aria-current','page'); else chips[j].removeAttribute('aria-current');
  }
  var grp = groupOf[name];
  if(grp) groupLast[grp] = name;
  updateNav();
  closeGroups();
  loadScreen(name);
  /* экран в адресе: ссылку можно отправить коллеге, F5 не сбрасывает экран */
  if(!noHash && location.hash !== '#'+name){ try{ history.replaceState(null, '', '#'+name); }catch(e){} }
  /* при возврате на экран сразу освежаем данные, а не ждём следующего тика */
  screenLast[name] = 0;
}

var lastCluster = 0; var screenLast = {};
function heartbeat(){
  if(document.hidden) return;
  var now = Date.now();
  if(now - lastCluster >= REFRESH){ lastCluster = now; fetchCluster(true); }
  var s = SCREENS[activeScreen];
  if(!s || !s.loaded || !s.pollMs) return;
  var poller = (window.__pollers || {})[activeScreen];
  if(typeof poller !== 'function') return;
  var back = s._back || 1;
  if(!screenLast[activeScreen] || now - screenLast[activeScreen] >= s.pollMs*back){
    screenLast[activeScreen] = now;
    Promise.resolve(poller()).then(function(){ s._back = 1; }, function(){ s._back = Math.min(8, (s._back||1)*2); });
  }
}

function fetchCluster(silent){
  return getJSON('/api/cluster', true).then(function(jC){
    if(jC === null) return;
    if(!jC.ok) throw new Error(jC.error);
    clusterData = jC.data;
    return getJSON('/api/lsn', true).then(function(jL){ if(jL && jL.ok) lsnData = jL.data || {}; });
  }).then(function(){
    if(!clusterData) return;
    window.__lsnData = lsnData;
    renderRibbon(); renderCluster(); renderHealth(); updateTelemetry();
    var u = document.getElementById('cluster-updated');
    if(u) u.textContent = 'обновлено в '+timeRu();
    fetchLag();
    if(window.__postClusterLoad){ try { window.__postClusterLoad(); } catch(e){ log('postCluster: '+e.message,'err'); } }
  }).catch(function(e){
    if(!silent) log('кластер: '+e.message, 'err');
    renderHealth(e.message);
  });
}

/* Вычисленное состояние узлов: одна точка правды для ленты, таблицы и сводки. */
function memberInfo(x){
  var L = lsnData[x.name] || {};
  var isL = isLeaderRole(x.role);
  var st = x.state || L.state || 'unknown';
  var lag = isL ? null : (typeof x.lag === 'number' ? x.lag : (typeof L.lag === 'number' ? L.lag : null));
  var ag = (window.__agents||{})[x.name];
  var delay = (ag && ag.delay) || L.delay || '0';
  return {
    name:x.name, host:x.host, port:x.port, role:x.role || 'unknown', isLeader:isL,
    state:st, level:stateLevel(st), lag:lag, lagUnknown: !isL && lag === null,
    timeline: L.timeline || x.timeline || '—',
    pending: !!(L.pending_restart || x.pending_restart), pendingWhy: L.pending_restart_reason || '',
    agentOn: !!(ag && ag.online), hasAgent: !!ag,
    delay: delay, delayed: (!isL) && delay && delay!=='0' && delay!=='?',
    L:L
  };
}
function members(){ return ((clusterData && clusterData.members) || []).map(memberInfo); }

function renderRibbon(){
  var el = document.getElementById('cluster-ribbon'); if(!el || !clusterData) return;
  var html = members().map(function(x){
    var cls = 'nchip'+(x.isLeader?' master':'')+(x.level==='bad'?' down':(x.level==='warn'?' transit':''));
    var h = '<div class="'+cls+'" data-key="rb-'+esc(x.name)+'">';
    h += '<span class="nchip-role">'+(x.isLeader?'лидер':'реплика')+'</span>';
    h += '<span class="nchip-name">'+esc(x.name)+'</span>';
    h += '<span class="nchip-state '+x.level+'" data-hint="Состояние Patroni: '+esc(x.state)+'">'+esc(stateRu(x.state))+'</span>';
    if(!x.isLeader) h += '<span class="nchip-lag '+lagClass(x.lag)+'" data-hint="Отставание реплики от лидера в байтах WAL">'+(x.lagUnknown?'лаг ?':'лаг '+fb(x.lag))+'</span>';
    if(x.delayed) h += '<span class="pending" data-hint="Отложенная реплика: recovery_min_apply_delay">задержка '+esc(x.delay)+'</span>';
    if(x.pending) h += '<span class="pending" data-hint="'+esc(x.pendingWhy || 'Изменены параметры, которые применяются только после перезапуска')+'">нужен рестарт</span>';
    h += '<span class="nchip-agent '+(x.agentOn?'ok':'bad')+'" data-hint="Агент на хосте: '+(x.agentOn?'на связи':'нет связи — бэкапы, дампы и ОС-метрики недоступны')+'"></span>';
    return h + '</div>';
  }).join('');
  morph(el, html);
}

function updateTelemetry(){
  var m = members(), ld = null, reps = 0, maxLag = 0;
  m.forEach(function(x){ if(x.isLeader) ld = x; else reps++; if(x.lag > maxLag) maxLag = x.lag; });
  var tl = document.getElementById('t-leader');
  var tr = document.getElementById('t-replicas');
  var tlg = document.getElementById('t-lag');
  if(tl) tl.textContent = ld ? ld.name : 'нет';
  if(tl) tl.classList.toggle('bad', !ld);
  if(tr) tr.textContent = String(reps);
  if(tlg){
    tlg.textContent = fb(maxLag);
    tlg.classList.toggle('bad', maxLag > LAG_CRIT);
    tlg.classList.toggle('warn', maxLag > LAG_WARN && maxLag <= LAG_CRIT);
    tlg.classList.toggle('good', maxLag <= LAG_WARN);
  }
}

/* Сводка здоровья: одна фраза «что с кластером» + список причин, если что-то не так.
   Оценка в терминах DBA: есть ли лидер, все ли реплики принимают WAL, какой лаг,
   включён ли автоматический failover. */
function renderHealth(err){
  var box = document.getElementById('cluster-health'); if(!box) return;
  if(err && !clusterData){
    morph(box, '<div class="health bad"><div class="health-title">Нет данных от Patroni</div>'
      +'<ul class="health-list"><li>'+esc(err)+'</li><li>Проверьте доступность REST API Patroni (порт 8008) с сервера консоли и список узлов в config.yaml.</li></ul></div>');
    return;
  }
  var m = members(); if(!m.length){ morph(box, ''); return; }
  var issues = [], level = 'ok';
  function add(lv, text){ issues.push({lv:lv, text:text}); if(lv === 'bad' || (lv === 'warn' && level === 'ok')) level = lv; }
  var leaders = m.filter(function(x){ return x.isLeader; });
  var reps = m.filter(function(x){ return !x.isLeader; });
  if(!leaders.length) add('bad', 'Лидер не выбран: кластер не принимает запись. Проверьте DCS (etcd/Consul) и журналы Patroni.');
  if(leaders.length > 1) add('bad', 'Несколько лидеров одновременно ('+leaders.map(function(x){return x.name;}).join(', ')+') — возможен split-brain.');
  m.forEach(function(x){
    if(x.level === 'bad') add('bad', x.name+': '+stateRu(x.state)+'.');
    else if(x.level === 'warn') add('warn', x.name+': '+stateRu(x.state)+'.');
    if(!x.isLeader && x.lag > LAG_CRIT) add('bad', x.name+' отстаёт на '+fb(x.lag)+' WAL — при failover возможна потеря данных.');
    else if(!x.isLeader && x.lag > LAG_WARN) add('warn', x.name+' отстаёт на '+fb(x.lag)+' WAL.');
    if(x.lagUnknown && x.level === 'ok') add('warn', x.name+': лаг неизвестен.');
    if(x.pending) add('warn', x.name+': ожидает перезапуска для применения параметров.');
    if(x.hasAgent && !x.agentOn) add('warn', 'Агент на '+x.name+' не на связи: бэкапы и ОС-метрики недоступны.');
  });
  if(clusterData.pause === true) add('warn', 'Кластер на паузе: автоматический failover отключён.');
  /* риски потери данных (wraparound, бэкапы, слоты, лаг во времени) — из /api/risk/summary */
  (window.__riskItems || []).forEach(function(i){ if(i.level === 'bad' || i.level === 'warn') add(i.level, i.text); });
  var healthyReps = reps.filter(function(x){ return x.level === 'ok'; }).length;
  var maxLag = 0; reps.forEach(function(x){ if(x.lag > maxLag) maxLag = x.lag; });
  var title = level === 'ok' ? 'Кластер исправен' : (level === 'warn' ? 'Требует внимания' : 'Авария');
  var facts = [
    'лидер <b>'+(leaders[0] ? esc(leaders[0].name) : 'нет')+'</b>',
    'реплик в строю <b>'+healthyReps+' из '+reps.length+'</b>',
    'макс. лаг <b>'+fb(maxLag)+'</b>',
    'автофейловер <b>'+(clusterData.pause === true ? 'выключен' : 'включён')+'</b>'
  ];
  var html = '<div class="health '+level+'"><div class="health-row"><div class="health-title">'+title+'</div>'
    + '<div class="health-facts">'+facts.map(function(f){ return '<span>'+f+'</span>'; }).join('')+'</div></div>';
  if(issues.length){
    issues.sort(function(a,b){ return (a.lv==='bad'?0:1) - (b.lv==='bad'?0:1); });
    html += '<ul class="health-list">'+issues.map(function(i){ return '<li class="'+i.lv+'">'+esc(i.text)+'</li>'; }).join('')+'</ul>';
  }
  morph(box, html + '</div>');
}

function renderCluster(){
  var m = members();
  var tb = document.getElementById('nodes-body'); if(!tb) return;
  var ld = null;
  m.forEach(function(x){ if(x.isLeader) ld = x.name; });
  var html = m.map(function(x){
    var L = x.L;
    var recv = x.isLeader ? (L.location||'—') : (L.received||'—');
    var rep = x.isLeader ? (L.location||'—') : (L.replayed||'—');
    var h = '<tr data-key="node-'+esc(x.name)+'">';
    h += '<td><strong>'+esc(x.name)+'</strong>'+(x.isLeader?' <span class="role-tag">лидер</span>':'')+'</td>';
    h += '<td class="mono">'+esc(x.host)+':'+esc(x.port)+'</td>';
    h += '<td><span class="badge '+(x.isLeader?'role-master':'role-replica')+'">'+esc(x.role)+'</span></td>';
    h += '<td class="st-'+x.level+'" title="'+esc(x.state)+'">'+esc(stateRu(x.state))+'</td>';
    h += '<td class="mono num">'+esc(x.timeline)+'</td>';
    h += '<td class="lsn '+(x.isLeader?'lsn-master':'')+'">'+lsnToString(recv)+'</td>';
    h += '<td class="lsn '+(x.isLeader?'lsn-master':'')+'">'+lsnToString(rep)+'</td>';
    h += '<td class="num '+lagClass(x.lag)+'">'+(x.isLeader?'—':(x.lagUnknown?'?':fb(x.lag)))+'</td>';
    h += '<td>'+(x.delayed ? '<span class="pending">'+esc(x.delay)+'</span>' : '<span class="no-pend">—</span>')+'</td>';
    h += '<td><span class="nchip-agent '+(x.agentOn?'ok':'bad')+'"></span> '+(x.agentOn?'на связи':'нет')+'</td>';
    h += '<td class="row-actions">'
      + (x.isLeader ? '' : '<button class="btn sm gate-admin" data-action="reinit" data-node="'+esc(x.name)+'" data-hint="Пересоздать реплику с нуля (pg_basebackup с лидера). Данные реплики будут удалены.">reinit</button>')
      + '<button class="btn sm gate-admin" data-action="restart" data-node="'+esc(x.name)+'" data-hint="Перезапустить PostgreSQL на узле через Patroni">restart</button>'
      + '<button class="btn sm gate-admin" data-action="reload" data-node="'+esc(x.name)+'" data-hint="Перечитать конфигурацию без перезапуска (pg_reload_conf)">reload</button></td>';
    return h + '</tr>';
  }).join('');
  morph(tb, html || '<tr><td colspan="11" class="empty">Patroni не вернул ни одного узла</td></tr>');

  /* списки для switchover/failover: обновляем, не сбрасывая выбор пользователя */
  var names = m.map(function(x){ return x.name; });
  var cands = m.filter(function(x){ return !x.isLeader && !x.delayed; }).map(function(x){ return x.name; });
  syncSelect(document.getElementById('sw-leader'), names, {keep:false, value:ld});
  syncSelect(document.getElementById('sw-candidate'), cands);
  syncSelect(document.getElementById('fo-candidate'), cands);

  isPaused = clusterData.pause === true;
  var pb = document.getElementById('pause-status');
  var pbtn = document.getElementById('btn-pause');
  if(pb && pbtn){
    morph(pb, isPaused ? '<span class="dot"></span>Пауза: автофейловер выкл.' : '<span class="dot"></span>Автофейловер вкл.');
    pb.className = isPaused ? 'pill pill-warn' : 'pill pill-ok';
    pb.setAttribute('data-hint', isPaused
      ? 'Patroni на паузе (maintenance mode): при падении лидера переключения не будет'
      : 'Patroni сам переключит лидера на здоровую реплику при аварии');
    var t = isPaused ? 'Снять паузу' : 'Пауза';
    if(pbtn.textContent !== t) pbtn.textContent = t;
    pbtn.className = isPaused ? 'btn danger gate-admin' : 'btn gate-admin';
  }
  renderLagBadges();
}
function renderLagBadges(){
  var box = document.getElementById('lag-badges'); if(!box || !clusterData) return;
  morph(box, members().filter(function(x){ return !x.isLeader; }).map(function(x){
    return '<span class="lag-badge" data-key="lb-'+esc(x.name)+'">'+esc(x.name)+' <b class="'+lagClass(x.lag)+'">'+(x.lagUnknown?'?':fb(x.lag))+'</b></span>';
  }).join(''));
}

function initLagChart(){
  var c = document.getElementById('lag-chart'); if(!c) return;
  if(typeof Chart === 'undefined'){ log('Chart.js не загрузился — графики недоступны (см. static/vendor)', 'warn'); return; }
  lagChart = new Chart(c.getContext('2d'), { type:'line', data:{datasets:[]}, options:makeLagOptions() });
  setChartColors(lagChart);
}
function fetchLag(){
  return getJSON('/api/lag-history', true).then(function(j){
    if(j && j.ok && j.data && j.data.length) updateLag(j.data);
  }).catch(function(){});
}
var lagClearedAt = 0;
function updateLag(h){
  if(!lagChart) return;
  var max = parseInt(document.getElementById('lag-points').value, 10) || 60;
  var sl = h.filter(function(p){ return p.ts*1000 > lagClearedAt; }).slice(-max);
  var names = {};
  sl.forEach(function(p){ Object.keys(p.nodes||{}).forEach(function(k){ if(!isLeaderRole(p.nodes[k].role)) names[k]=1; }); });
  var allZero = true;
  var series = Object.keys(names).sort().map(function(nm){
    var pts = [];
    sl.forEach(function(p){ if(p.nodes && p.nodes[nm]){ var y = lagNum(p.nodes[nm].lag); if(y>0) allZero=false; pts.push({ x:p.ts*1000, y:y }); } });
    return {label:nm, data:pts};
  });
  syncDatasets(lagChart, series);
  var flag = document.getElementById('sync-flag'); if(flag) flag.classList.toggle('hidden', !(allZero && series.length));
}
function clearChart(){ lagClearedAt = Date.now(); if(lagChart){ lagChart.data.datasets.forEach(function(d){ d.data = []; }); lagChart.update('none'); } }

function onDocClick(ev){
  var inside = ev.target.closest && ev.target.closest('.nav-group');
  if(!inside && openGroup) closeGroups();
  var t = ev.target.closest && ev.target.closest('[data-action]'); if(!t) return;
  var a = t.getAttribute('data-action');
  var reg = window.__actions || {};
  var fn = reg[a];
  if(typeof fn === 'function'){ try{ fn(t); }catch(err){ log('действие '+a+': '+err.message, 'err'); } }
  else log('действие «'+a+'» недоступно (модуль не загружен)', 'warn');
}

/* Подтверждение действия. opts.danger — красная кнопка; opts.require — строка,
   которую нужно ввести вручную (защита от случайного failover/рестарта кластера). */
var modalPrevFocus = null;
function confirmBox(title, text, fn, opts){
  opts = opts || {};
  var m = document.getElementById('modal'); if(!m){ if(typeof fn==='function') fn(); return; }
  document.getElementById('modal-title').textContent = title;
  document.getElementById('modal-text').textContent = text || '';
  var req = document.getElementById('modal-require'), inp = document.getElementById('modal-input');
  var btn = document.getElementById('modal-confirm');
  btn.className = 'btn ' + (opts.danger ? 'danger solid' : 'primary');
  btn.textContent = opts.okText || 'Подтвердить';
  if(opts.require){
    req.classList.remove('hidden');
    document.getElementById('modal-require-word').textContent = opts.require;
    inp.value = ''; btn.disabled = true;
    inp.oninput = function(){ btn.disabled = inp.value.trim() !== opts.require; };
  } else { req.classList.add('hidden'); btn.disabled = false; inp.oninput = null; }
  modalPrevFocus = document.activeElement;
  m.classList.remove('hidden');
  setTimeout(function(){ (opts.require ? inp : btn).focus(); }, 0);
  btn.onclick = function(){ if(btn.disabled) return; closeModal(); if(typeof fn==='function') fn(); };
}
function closeModal(){
  var m = document.getElementById('modal'); if(m) m.classList.add('hidden');
  if(modalPrevFocus && modalPrevFocus.focus){ try{ modalPrevFocus.focus(); }catch(e){} }
}

/* ===================== AUTH ===================== */
window.__role = 'viewer';
function applyRole(){
  document.body.classList.remove('is-viewer','is-dba','is-admin');
  document.body.classList.add('is-' + window.__role);
}
function showLogin(){
  window.__inLogin = true;
  var o = document.getElementById('login-overlay'); if(o) o.classList.remove('hidden');
  var u = document.getElementById('login-user'); if(u) setTimeout(function(){ u.focus(); }, 0);
}
function doLogin(){
  var u = document.getElementById('login-user').value, p = document.getElementById('login-pass').value;
  var err = document.getElementById('login-err'), btn = document.getElementById('login-submit');
  if(!u || !p){ err.textContent = 'Введите логин и пароль'; return; }
  btn.disabled = true; err.textContent = '';
  postJSON('/api/auth/login', {user:u, password:p}).then(function(j){
    if(j.ok) location.reload();
    else { err.textContent = j.error || 'Неверный логин или пароль'; btn.disabled = false; }
  }).catch(function(e){ err.textContent = 'Сервер недоступен: '+e.message; btn.disabled = false; });
}
function doLogout(){ postJSON('/api/auth/logout', {}).then(function(){ location.reload(); }); }
function initAuth(){
  getJSON('/api/auth/me', true).then(function(j){
    var user = (j && j.ok && j.data) ? j.data.user : null;
    if(!user){ showLogin(); return; }
    window.__role = j.data.role || 'viewer';
    applyRole();
    var uc = document.getElementById('user-chip'); if(uc) uc.classList.remove('hidden');
    var un = document.getElementById('user-name');
    var ROLE_RU = {viewer:'просмотр', dba:'DBA', admin:'администратор'};
    if(un) un.textContent = user + ' · ' + (ROLE_RU[window.__role] || window.__role);
    var bl = document.getElementById('btn-logout'); if(bl) bl.classList.remove('hidden');
    if(j.data.demo){ var d = document.getElementById('demo-flag'); if(d) d.classList.remove('hidden'); }
  }).catch(function(){ showLogin(); });
  var ls = document.getElementById('login-submit'); if(ls) ls.addEventListener('click', doLogin);
  var lp = document.getElementById('login-pass'); if(lp) lp.addEventListener('keydown', function(e){ if(e.key === 'Enter') doLogin(); });
  var lu = document.getElementById('login-user'); if(lu) lu.addEventListener('keydown', function(e){ if(e.key === 'Enter' && lp) lp.focus(); });
  var bl = document.getElementById('btn-logout'); if(bl) bl.addEventListener('click', doLogout);
}

function onPauseClick(){
  var ns = !isPaused;
  confirmBox(ns ? 'Поставить кластер на паузу?' : 'Снять паузу?',
    ns ? 'Patroni перестанет автоматически переключать лидера при аварии (maintenance mode). Используйте на время ручных работ с узлами.'
       : 'Patroni снова будет автоматически переключать лидера при аварии.',
    function(){
      log(ns?'Ставлю на паузу…':'Снимаю паузу…', 'info');
      postJSON('/api/pause', {pause:ns}).then(function(j){
        if(j.ok) log(ns?'Кластер на паузе':'Пауза снята', 'ok'); else handleResp(j, 'Пауза', '');
        setTimeout(function(){ fetchCluster(); }, 1000);
      }).catch(function(e){ log('Пауза: '+e.message, 'err'); });
    }, {danger:ns, okText: ns ? 'Поставить на паузу' : 'Снять паузу'});
}
function onRefreshClick(){
  fetchCluster();
  screenLast[activeScreen] = 0;
  var poller = (window.__pollers || {})[activeScreen];
  if(typeof poller === 'function'){ try{ poller(true); }catch(e){} }
}
function applyTheme(next){
  document.documentElement.setAttribute('data-theme', next);
  try{ localStorage.setItem('patroni_theme', next); }catch(e){}
  if(window.__charts) Object.keys(window.__charts).forEach(function(k){ setChartColors(window.__charts[k]); });
  setChartColors(lagChart);
  var bt = document.getElementById('btn-theme');
  if(bt) bt.setAttribute('aria-label', next === 'graphite' ? 'Светлая тема' : 'Тёмная тема');
}
function onThemeClick(){
  var cur = document.documentElement.getAttribute('data-theme');
  applyTheme(cur === 'graphite' ? 'paper' : 'graphite');
}

function bindCore(){
  document.addEventListener('click', onDocClick);
  document.addEventListener('keydown', function(e){
    if(e.key === 'Escape'){
      if(openGroup) closeGroups();
      var m = document.getElementById('modal'); if(m && !m.classList.contains('hidden')) closeModal();
    }
    if(e.key === 'Enter' && e.target && e.target.id === 'modal-input'){
      var b = document.getElementById('modal-confirm'); if(b && !b.disabled) b.click();
    }
  });
  var sb = document.getElementById('screenbar');
  if(sb) sb.addEventListener('click', function(e){
    var gb = e.target.closest('.nav-group-btn');
    if(gb){ var g = gb.closest('.nav-group'); if(g) toggleGroup(g); return; }
    var c = e.target.closest('.chip');
    if(c){ showScreen(c.getAttribute('data-screen')); }
  });
  var bp = document.getElementById('btn-pause'); if(bp) bp.addEventListener('click', onPauseClick);
  var br = document.getElementById('btn-refresh'); if(br) br.addEventListener('click', onRefreshClick);
  var bt = document.getElementById('btn-theme'); if(bt) bt.addEventListener('click', onThemeClick);
  var lp = document.getElementById('lag-points'); if(lp) lp.addEventListener('change', fetchLag);
  window.addEventListener('hashchange', function(){ var n = location.hash.slice(1); if(n && n !== activeScreen) showScreen(n, true); });
  /* вернулись на вкладку — данные обновляются сразу, без ожидания тика */
  document.addEventListener('visibilitychange', function(){ if(!document.hidden){ lastCluster = 0; screenLast[activeScreen] = 0; heartbeat(); } });
}

window.confirmBox = confirmBox;
window.closeModal = closeModal;
window.clearChart = clearChart;
window.isLeaderRole = isLeaderRole;

/* Старт после загрузки всех скриптов (модули регистрируют загрузчики экранов
   позже ядра — экран из адреса #hosts должен найти свой загрузчик). */
function boot(){
  initAuth();
  bindCore();
  initLagChart();
  showScreen((location.hash || '').slice(1) || 'cluster', true);
  if(activeScreen !== 'cluster') fetchCluster(true);
  updateNav();
  setInterval(heartbeat, 1000);
  log('ядро загружено ('+APP_VERSION+')', 'info');
  var note = document.getElementById('boot-note');
  if(note){ note.classList.add('gone'); }
  if(window.__modulesReady) window.__modulesReady();
}
if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
