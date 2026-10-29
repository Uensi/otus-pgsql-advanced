/* Patroni Console — DBA [dba-20261017a]
   Экраны: Риски (wraparound, RPO, слоты, лаг во времени), Активные сессии (ASH),
   Производительность (запросы и регрессии, контрольные точки/WAL, долгие операции),
   Аудит индексов, Журналы PostgreSQL, Безопасность, Обновления, еженедельный отчёт.
   Все обновления — через morph()/syncSelect() из ui-kit.js (без мерцания). */
(function(){
  'use strict';

  var LV = {ok:'норма', info:'к сведению', warn:'внимание', bad:'авария', unknown:'нет данных'};
  function lvTag(l){ return '<span class="lv lv-'+(l||'unknown')+'">'+(LV[l]||LV.unknown)+'</span>'; }
  function pbar(pct, level){
    var p = Math.max(0, Math.min(100, Number(pct)||0));
    return '<span class="pbar '+(level||'')+'"><i style="width:'+p.toFixed(1)+'%"></i></span>';
  }
  function num(n){ return n == null ? '—' : Number(n).toLocaleString('ru-RU'); }
  function dur(s){
    if(s == null) return '—';
    s = Math.round(s);
    if(s < 60) return s+' с';
    if(s < 3600) return Math.floor(s/60)+' мин '+(s%60)+' с';
    if(s < 86400) return Math.floor(s/3600)+' ч '+Math.floor((s%3600)/60)+' мин';
    return Math.floor(s/86400)+' дн '+Math.floor((s%86400)/3600)+' ч';
  }
  function ago(ts){ return ts ? dur(Date.now()/1000 - ts)+' назад' : '—'; }
  function tsRu(ts){ return ts ? new Date(ts*1000).toLocaleString('ru-RU') : '—'; }
  function sqlBox(sql){
    return '<div class="sqlfix"><code>'+esc(sql)+'</code><button class="btn sm" data-action="copy" data-text="'+esc(sql)+'">Копировать</button></div>';
  }
  function members(){ return (window.clusterData && window.clusterData.members) || []; }
  function nodeOptions(withAll){
    var list = members().map(function(m){ return {value:m.name, label:m.name+(isLeaderRole(m.role)?' (лидер)':'')}; });
    return withAll ? [{value:'', label:'все узлы'}].concat(list) : list;
  }
  function leaderName(){ var l = members().filter(function(m){ return isLeaderRole(m.role); })[0]; return l ? l.name : ''; }
  function errBox(e){ return '<div class="sqlerr">'+esc((e && e.message) || e || 'ошибка')+'</div>'; }
  function waitCmd(node, cid, cb, maxTries, url){
    var n = 0;
    (function tick(){
      n++;
      getJSON((url || '/api/ops/cmd-result')+'?node='+encodeURIComponent(node)+'&id='+encodeURIComponent(cid)).then(function(j){
        if(j && j.ok && j.data){ cb(j.data, null); return; }
        if(j && !j.ok){ cb(null, j.error || 'ошибка'); return; }
        if(n >= (maxTries||90)){ cb(null, 'агент не ответил вовремя — результат появится позже'); return; }
        setTimeout(tick, 2000);
      }).catch(function(e){ cb(null, e.message); });
    })();
  }

  /* ================================================================ РИСКИ */
  var riskData = null;
  function fetchRisk(force){
    if(window.__inLogin) return Promise.resolve();
    return getJSON('/api/risk/summary'+(force?'?force=1':''), true).then(function(j){
      if(!j || !j.ok) return;
      riskData = j.data;
      window.__riskItems = j.data.items || [];
      if(window.activeScreen === 'risk') renderRisk();
    });
  }
  setInterval(function(){ if(!document.hidden && window.activeScreen !== 'risk') fetchRisk(); }, 60000);
  setTimeout(fetchRisk, 1500);

  var AREA = {
    wraparound:{t:'Wraparound XID', p:'risk-wrap-panel'}, backups:{t:'Бэкапы и WAL', p:'risk-backup-panel'},
    slots:{t:'Слоты репликации', p:'risk-slots-panel'}, replication:{t:'Лаг во времени', p:'risk-repl-panel'}
  };
  function renderRisk(){
    var d = riskData; if(!d) return;
    var w = d.wraparound || {}, b = d.backups || {}, s = d.slots || {}, r = d.replication || {};
    var line = {
      wraparound: w.ok ? ('макс. '+w.max_pct+'% в '+esc(w.worst_db||'—')+(w.days_left!=null?' · ~'+w.days_left+' дн. до остановки':'')) : esc(w.error||'нет данных'),
      backups: b.last_backup_age_h!=null ? 'последний '+b.last_backup_age_h+' ч назад' : 'бэкапов нет',
      slots: (s.nodes||[]).reduce(function(a, n){ return a + (n.slots||[]).length; }, 0)+' слотов',
      replication: r.ok ? ('макс. '+Math.round(r.max_replay_lag_s||0)+' с') : esc(r.error||'нет данных')
    };
    morph(document.getElementById('risk-tiles'), Object.keys(AREA).map(function(k){
      var lv = (d[k]||{}).level || 'unknown';
      return '<a class="rtile '+lv+'" href="#risk" data-action="risk-jump" data-panel="'+AREA[k].p+'"><span class="rt-t">'+AREA[k].t+'</span>'
        +'<span class="rt-v">'+(LV[lv]||'—')+'</span><span class="rt-s">'+line[k]+'</span></a>';
    }).join(''));
    renderWrap(w); renderBackups(b); renderSlots(s); renderRepl(r);
  }
  function renderWrap(w){
    var box = document.getElementById('risk-wrap');
    morph(document.getElementById('risk-wrap-meta'), w.ok
      ? 'расход XID: '+(w.xid_per_sec!=null ? num(w.xid_per_sec)+'/с' : 'оценивается…')+' · autovacuum_freeze_max_age '+num((w.settings||{}).autovacuum_freeze_max_age) : '');
    if(!w.ok){ morph(box, errBox(w.error)); return; }
    var h = '<div class="table-wrap"><table><thead><tr><th>База</th><th data-hint="Возраст datfrozenxid: сколько транзакций назад база была полностью «заморожена». Предел — 2,1 млрд.">Возраст XID</th><th></th>'
      +'<th data-hint="То же для идентификаторов MultiXact (разделяемые блокировки строк, SELECT FOR SHARE, внешние ключи)">MultiXact</th>'
      +'<th data-hint="Оценка по текущему расходу XID, если VACUUM FREEZE не будет выполнен">До остановки записи</th><th class="num">Размер</th></tr></thead><tbody>';
    (w.dbs||[]).forEach(function(d){
      var pct = Math.max(d.xid_pct, d.mxid_pct);
      var lv = pct >= 75 ? 'bad' : (pct >= 50 ? 'warn' : 'ok');
      h += '<tr data-key="xd-'+esc(d.datname)+'"><td><b>'+esc(d.datname)+'</b></td><td class="num">'+num(d.xid_age)+'</td><td style="min-width:160px">'+pbar(d.xid_pct, lv)+' <span class="'+(lv==='ok'?'':'lag-'+(lv==='bad'?'crit':'warn'))+'">'+d.xid_pct+'%</span></td>'
        +'<td class="num">'+d.mxid_pct+'%</td><td class="num">'+(d.days_left!=null ? num(d.days_left)+' дн.' : '—')+'</td><td class="num">'+fb(d.size)+'</td></tr>';
    });
    h += '</tbody></table></div>';
    var top = w.top_tables || [];
    if(top.length){
      h += '<h4 class="subh">Таблицы, которые держат возраст базы</h4><p class="muted small">VACUUM (FREEZE) замораживает старые строки и сбрасывает возраст. На больших таблицах идёт долго, нагружает диск; запускается в фоне на лидере.</p>'
        + '<div class="table-wrap"><table><thead><tr><th>Таблица</th><th class="num">Возраст XID</th><th class="num">Размер</th><th>Последний автовакуум</th><th></th></tr></thead><tbody>';
      top.forEach(function(t){
        h += '<tr data-key="xt-'+esc(t.db+'.'+t.schema+'.'+t.table)+'"><td class="mono">'+esc(t.db)+' · '+esc(t.schema)+'.'+esc(t.table)+'</td><td class="num">'+num(t.xid_age)+'</td><td class="num">'+fb(t.size)+'</td>'
          +'<td>'+esc(t.last_autovacuum ? String(t.last_autovacuum).slice(0,19) : 'не было')+'</td>'
          +'<td><button class="btn sm gate-dba" data-action="risk-freeze" data-db="'+esc(t.db)+'" data-schema="'+esc(t.schema)+'" data-table="'+esc(t.table)+'">VACUUM FREEZE</button></td></tr>';
      });
      h += '</tbody></table></div>';
    }
    morph(box, h);
  }
  function renderBackups(b){
    var box = document.getElementById('risk-backup');
    var a = b.archiver || {}, st = a.stat || {};
    var archLv = (a.mode||'off') === 'off' ? 'warn' : ((st.last_failed_time && (!st.last_archived_time || st.last_failed_time > st.last_archived_time)) ? 'bad' : 'ok');
    var h = '<div class="metric-grid">'
      + '<div class="metric" data-hint="Время с момента последнего бэкапа wal-g (полного или дельта)"><span class="mk">Последний бэкап</span><span class="mv '+(b.last_backup_age_h==null?'bad':(b.last_backup_age_h>26?'warn':'good'))+'">'+(b.last_backup_age_h!=null ? b.last_backup_age_h+' ч' : 'нет')+'</span></div>'
      + '<div class="metric"><span class="mk">Последний полный</span><span class="mv">'+(b.last_full_age_h!=null ? b.last_full_age_h+' ч' : '—')+'</span></div>'
      + '<div class="metric" data-hint="pg_stat_archiver: отправка WAL-сегментов в хранилище. Без неё PITR невозможен."><span class="mk">Архивация WAL</span><span class="mv '+(archLv==='ok'?'good':(archLv==='bad'?'bad':'warn'))+'">'+((a.mode||'off')==='off'?'выкл.':(archLv==='bad'?'падает':'работает'))+'</span></div>'
      + '<div class="metric" data-hint="Сегменты WAL, ожидающие отправки (.ready в pg_wal/archive_status)"><span class="mk">Очередь архивации</span><span class="mv">'+(a.ready!=null ? a.ready : '—')+'</span></div>'
      + '<div class="metric"><span class="mk">Бэкапов в хранилище</span><span class="mv">'+(b.backups_total||0)+'</span></div>'
      + '</div>';
    if((b.issues||[]).length) h += '<ul class="issue-list">'+b.issues.map(function(i){ return '<li class="'+i.level+'">'+esc(i.text)+'</li>'; }).join('')+'</ul>';
    if(a.mode && a.mode !== 'off'){
      h += '<p class="muted small" style="margin-top:8px">Заархивировано '+num(st.archived_count)+' сегментов, последний '+esc(st.last_archived_wal||'—')+' в '+esc(String(st.last_archived_time||'—').slice(0,19))
        + (st.failed_count ? ' · ошибок с '+esc(String(st.stats_reset||'').slice(0,10))+': '+num(st.failed_count)+' (последняя: '+esc(st.last_failed_wal||'')+', '+esc(String(st.last_failed_time||'').slice(0,19))+')' : '')+'</p>';
    }
    h += '<h4 class="subh">Проверки восстановления</h4><div id="rt-status" class="muted small"></div>';
    var tests = (b.restore_tests||[]).slice().reverse();
    if(!tests.length) h += '<p class="empty">Ни одной проверки. Нажмите «Проверить восстановление»: бэкап без проверенного восстановления — это надежда, а не бэкап.</p>';
    else {
      h += '<div class="table-wrap"><table><thead><tr><th>Когда</th><th>Узел</th><th>Бэкап</th><th class="num">Скачивание</th><th class="num">Восстановление</th><th class="num">Объём</th><th>Данные на момент</th><th>Результат</th></tr></thead><tbody>';
      tests.forEach(function(t){
        var x = t.data || {};
        h += '<tr><td>'+esc(t.time||'')+'</td><td>'+esc(t.node||'')+'</td><td class="mono">'+esc(x.backup||'—')+'</td><td class="num">'+dur(x.fetch_s)+'</td><td class="num">'+dur(x.recovery_s)+'</td><td class="num">'+fb(x.size)+'</td>'
          +'<td>'+esc(x.consistent_at ? String(x.consistent_at).slice(0,19) : '—')+'</td><td>'+(t.ok ? lvTag('ok').replace('норма','успешно') : '<span class="lv lv-bad" title="'+esc(t.error||'')+'">ошибка</span>')+'</td></tr>';
      });
      h += '</tbody></table></div>';
    }
    var keep = document.getElementById('rt-status'); var keepTxt = keep ? keep.innerHTML : '';
    morph(box, h);
    var st2 = document.getElementById('rt-status'); if(st2 && keepTxt) st2.innerHTML = keepTxt;
  }
  function renderSlots(s){
    var box = document.getElementById('risk-slots');
    var rows = [];
    (s.nodes||[]).forEach(function(n){ (n.slots||[]).forEach(function(x){ rows.push({n:n, x:x}); }); });
    if(!rows.length){ morph(box, '<p class="empty">Слотов репликации нет.</p>'); return; }
    var lim = ((s.nodes||[])[0]||{}).max_slot_wal_keep_size;
    var h = '<p class="muted small">max_slot_wal_keep_size = <b class="mono">'+esc(lim==null?'—':lim)+'</b>'+((lim==='-1'||lim==='-1MB')?' — слот может удерживать WAL без ограничений, пока не кончится диск':'')+'</p>'
      + '<div class="table-wrap"><table><thead><tr><th>Узел</th><th>Слот</th><th>Тип</th><th>Активен</th>'
      + '<th data-hint="reserved — норма; extended — держит WAL сверх max_wal_size; unreserved — скоро потеряет WAL; lost — WAL уже удалён, слот непригоден">wal_status</th>'
      + '<th class="num">Удерживает WAL</th><th>Оценка</th></tr></thead><tbody>';
    rows.forEach(function(r){
      var x = r.x;
      h += '<tr data-key="sl-'+esc(r.n.node+'-'+x.slot_name)+'"><td>'+esc(r.n.node)+'</td><td class="mono">'+esc(x.slot_name)+'</td><td>'+esc(x.slot_type)+(x.database?' · '+esc(x.database):'')+'</td>'
        +'<td>'+(x.active?'<span class="st-ok">да</span>':'<span class="st-warn">нет</span>')+'</td><td class="mono">'+esc(x.wal_status||'—')+'</td>'
        +'<td class="num">'+fb(x.retained_bytes)+'</td><td>'+lvTag(x.level)+(x.why?' <span class="small">'+esc(x.why)+'</span>':'')+'</td></tr>';
    });
    morph(box, h + '</tbody></table></div>');
  }
  function renderRepl(r){
    var box = document.getElementById('risk-repl');
    if(!r.ok){ morph(box, errBox(r.error)); return; }
    if(!(r.replicas||[]).length){ morph(box, '<p class="empty">Лидер не видит ни одной реплики в pg_stat_replication.</p>'); return; }
    var h = '<div class="table-wrap"><table><thead><tr><th>Реплика</th><th>Адрес</th><th>Состояние</th><th>Режим</th>'
      +'<th class="num" data-hint="Время до записи WAL на диск реплики (без fsync)">write</th><th class="num" data-hint="Время до гарантированной записи (fsync) на реплике — важно для synchronous_commit">flush</th>'
      +'<th class="num" data-hint="Время до применения изменений — через столько данные видны на реплике">replay</th><th class="num">Отставание, байт</th><th>Оценка</th></tr></thead><tbody>';
    r.replicas.forEach(function(x){
      function s(v){ return v==null ? '<span class="muted" title="нет активности записи">—</span>' : (v < 1 ? (v*1000).toFixed(0)+' мс' : v.toFixed(1)+' с'); }
      h += '<tr data-key="rp-'+esc(x.application_name)+'"><td><b>'+esc(x.application_name)+'</b></td><td class="mono">'+esc(x.client||'—')+'</td><td>'+esc(x.state)+'</td><td>'+esc(x.sync_state)+'</td>'
        +'<td class="num">'+s(x.write_lag_s)+'</td><td class="num">'+s(x.flush_lag_s)+'</td><td class="num"><b>'+s(x.replay_lag_s)+'</b></td><td class="num">'+fb(x.replay_lag_bytes)+'</td><td>'+lvTag(x.level)+'</td></tr>';
    });
    morph(box, h + '</tbody></table></div>');
  }
  function riskFreeze(t){
    var db = t.getAttribute('data-db'), sc = t.getAttribute('data-schema'), tb = t.getAttribute('data-table');
    confirmBox('VACUUM (FREEZE) '+sc+'.'+tb+' в базе '+db+'?',
      'Команда заморозит старые строки и сбросит возраст таблицы. Выполняется в фоне на лидере; на больших таблицах — часы и заметная нагрузка на диск. Блокировок записи не берёт.',
      function(){
        postJSON('/api/risk/freeze', {db:db, schema:sc, table:tb}).then(function(j){
          if(j && j.ok) log(j.data.message, 'ok'); else log('VACUUM FREEZE: '+((j && j.error)||'ошибка'), 'err');
        }).catch(function(e){ log('VACUUM FREEZE: '+e.message, 'err'); });
      }, {okText:'Запустить'});
  }
  function riskRestoreTest(){
    confirmBox('Проверить восстановление из последнего бэкапа?',
      'Агент на реплике развернёт бэкап во временный каталог, применит WAL, поднимет PostgreSQL на отдельном порту (только 127.0.0.1), проверит чтение баз и удалит копию. Нужно свободное место размером с базу. Может занять от минут до часов.',
      function(){
        postJSON('/api/risk/restore-test', {}).then(function(j){
          if(!j || !j.ok){ log('Проверка восстановления: '+((j && j.error)||'ошибка'), 'err'); return; }
          var node = j.data.node, t0 = Date.now();
          log('Проверка восстановления запущена на '+node, 'info');
          var st = document.getElementById('rt-status');
          var timer = setInterval(function(){ var s2 = document.getElementById('rt-status'); if(s2) s2.textContent = 'Идёт проверка на '+node+': '+dur((Date.now()-t0)/1000); }, 1000);
          waitCmd(node, j.data.cmd_id, function(res, err){
            clearInterval(timer);
            var s3 = document.getElementById('rt-status'); if(s3) s3.textContent = '';
            if(res && res.ok){
              var x = (res.out||{}).data || {};
              log('Восстановление проверено: '+(x.backup||'')+' за '+dur(x.duration_s)+', данные на '+String(x.consistent_at||'—').slice(0,19), 'ok');
            } else log('Проверка восстановления не прошла: '+(err || (res && res.out) || 'ошибка'), 'err');
            fetchRisk(true);
          }, 3600);
        }).catch(function(e){ log('Проверка восстановления: '+e.message, 'err'); });
      }, {okText:'Запустить проверку'});
  }

  /* ================================================================ ПОДКЛЮЧЕНИЕ К УЗЛАМ */
  function fetchConn(){
    if(window.__inLogin) return Promise.resolve();
    return getJSON('/api/diag/connectivity', true).then(function(j){
      if(!j || !j.ok) return;
      var d = j.data;
      window.__connItems = d.issues || [];
      var box = document.getElementById('conn-diag'); if(!box) return;
      if(!(d.issues || []).length){ morph(box, ''); return; }
      var bad = d.issues.some(function(i){ return i.level === 'bad'; });
      var h = '<details class="panel conn-diag"'+(bad?' open':'')+'><summary><b>Подключение консоли к узлам</b> '
        + '<span class="cnt '+(bad?'warn':'ok')+'">'+d.issues.length+' '+(bad?'проблем':'замечаний')+'</span>'
        + ' <span class="muted small">PostgreSQL: пользователь '+esc(d.pg_user)+', порт '+esc(d.pg_port)+', база '+esc(d.pg_db)+'</span></summary>'
        + '<div class="table-wrap" style="margin-top:10px"><table><thead><tr><th>Узел (config.yaml)</th><th>Адрес</th><th>REST Patroni</th><th>Имя в Patroni</th><th>PostgreSQL</th><th>Агент</th></tr></thead><tbody>'
        + d.nodes.map(function(n){
            return '<tr data-key="cd-'+esc(n.config_name)+'"><td><b>'+esc(n.config_name)+'</b></td><td class="mono">'+esc(n.host)+':'+esc(n.rest_port)+'</td>'
              + '<td>'+(n.rest_ok ? '<span class="st-ok">доступен</span>' : '<span class="st-bad" title="'+esc(n.rest_error||'')+'">нет</span>')+'</td>'
              + '<td class="'+(n.member_name && n.member_name !== n.config_name ? 'lag-warn' : '')+'">'+esc(n.member_name || '—')+'</td>'
              + '<td>'+(n.pg_ok ? '<span class="st-ok">ок '+esc(n.pg_version||'')+'</span>' : '<span class="st-bad">нет</span>')+'</td>'
              + '<td>'+(n.agent_online ? '<span class="st-ok">на связи</span>' : '<span class="muted">—</span>')+'</td></tr>';
          }).join('') + '</tbody></table></div>'
        + '<ul class="issue-list">'+d.issues.map(function(i){ return '<li class="'+i.level+'">'+esc(i.text)+'<div class="muted small">'+esc(i.fix)+'</div></li>'; }).join('')+'</ul>'
        + '<p class="muted small">Ту же проверку с сервера консоли: <span class="mono">python3 tools/check_config.py config.yaml</span></p></details>';
      morph(box, h);
    }).catch(function(){});
  }
  setTimeout(fetchConn, 800);
  setInterval(function(){ if(!document.hidden && (window.activeScreen === 'cluster' || (window.__connItems||[]).length)) fetchConn(); }, 60000);

  /* ================================================================ ASH */
  var ashChart = null;
  var WAIT_COLORS = {CPU:'#3f9b68', IO:'#3d7ab5', Lock:'#cc4a3c', LWLock:'#7b6bb5', IdleTx:'#c48a1d', Client:'#8a95a3',
                     IPC:'#2f9aa0', Timeout:'#b5a33d', BufferPin:'#a3527b', Extension:'#5c6b7a', Activity:'#b0b8c3'};
  var WAIT_ORDER = ['CPU','IO','Lock','LWLock','IPC','IdleTx','Client','Timeout','BufferPin','Extension','Activity'];
  function initAshChart(){
    var c = document.getElementById('ash-chart');
    if(ashChart || !c || typeof Chart === 'undefined') return;
    ashChart = new Chart(c.getContext('2d'), {type:'bar', data:{datasets:[]}, options:{
      responsive:true, maintainAspectRatio:false, animation:false,
      interaction:{mode:'index', intersect:false},
      plugins:{legend:{labels:{color:cssVar('--dim'), boxWidth:12}},
               tooltip:{callbacks:{label:function(x){ return x.dataset.label+': '+x.parsed.y.toFixed(2)+' сесс.'; }}}},
      scales:{x:{type:'time', stacked:true, grid:{display:false}, ticks:{color:cssVar('--muted'), maxTicksLimit:8}, time:{tooltipFormat:'HH:mm:ss'}},
              y:{stacked:true, beginAtZero:true, title:{display:true, text:'активных сессий', color:cssVar('--muted')}, grid:{color:cssVar('--grid')}, ticks:{color:cssVar('--muted')}}}
    }});
    window.__charts['ash-chart'] = ashChart;
  }
  function loadAsh(){
    initAshChart();
    syncSelect(document.getElementById('ash-node'), nodeOptions(true));
    var node = document.getElementById('ash-node').value, min = document.getElementById('ash-minutes').value;
    return getJSON('/api/ash?node='+encodeURIComponent(node)+'&minutes='+min, true).then(function(j){
      if(!j || !j.ok){ log('ASH: '+((j && j.error)||'ошибка'), 'err'); return; }
      renderAsh(j.data);
    });
  }
  function renderAsh(d){
    var present = {};
    d.timeline.forEach(function(b){ Object.keys(b.classes).forEach(function(k){ present[k] = 1; }); });
    var classes = WAIT_ORDER.filter(function(k){ return present[k]; }).concat(Object.keys(present).filter(function(k){ return WAIT_ORDER.indexOf(k) < 0; }));
    if(ashChart){
      var cur = {}; ashChart.data.datasets.forEach(function(x){ cur[x.label] = x; });
      ashChart.data.datasets = classes.map(function(k){
        var ds = cur[k] || {label:k, backgroundColor:WAIT_COLORS[k]||'#999', borderWidth:0, barPercentage:1.0, categoryPercentage:0.92};
        ds.data = d.timeline.map(function(b){ return {x:b.ts*1000 + d.bucket_s*500, y:b.classes[k]||0}; });
        return ds;
      });
      ashChart.update('none');
    }
    var peak = 0; d.timeline.forEach(function(b){ if(b.aas > peak) peak = b.aas; });
    var topEv = (d.top_events||[])[0];
    morph(document.getElementById('ash-tiles'),
      '<div class="metric" data-hint="Average Active Sessions — сколько сессий в среднем одновременно работали или ждали. Сравнивайте с числом ядер CPU."><span class="mk">Средняя нагрузка (AAS)</span><span class="mv">'+d.avg_active+'</span></div>'
      +'<div class="metric"><span class="mk">Пик</span><span class="mv">'+peak.toFixed(1)+'</span></div>'
      +'<div class="metric"><span class="mk">Главное ожидание</span><span class="mv small-mv">'+esc(topEv ? topEv.key : '—')+'</span></div>'
      +'<div class="metric"><span class="mk">Снимков</span><span class="mv">'+num(d.samples)+'</span></div>');
    morph(document.getElementById('ash-meta'), 'шаг '+d.bucket_s+' с · история с '+(d.history_from ? new Date(d.history_from*1000).toLocaleTimeString('ru-RU') : '—')
      + (Object.keys(d.errors||{}).length ? ' · нет данных с '+Object.keys(d.errors).join(', ') : ''));
    function share(rows, keyHdr){
      if(!rows.length) return '<p class="empty">Активных сессий за период не было.</p>';
      return '<table><thead><tr><th>'+keyHdr+'</th><th style="width:45%">Доля времени</th><th class="num">AAS</th></tr></thead><tbody>'
        + rows.map(function(r){ var cls = r.key.split(':')[0]; return '<tr data-key="'+esc(keyHdr+r.key)+'"><td><span class="wdot" style="background:'+(WAIT_COLORS[cls]||'var(--muted)')+'"></span>'+esc(r.key)+'</td><td>'+pbar(r.pct)+' '+r.pct+'%</td><td class="num">'+r.aas+'</td></tr>'; }).join('')
        + '</tbody></table>';
    }
    morph(document.getElementById('ash-events'), share(d.top_events||[], 'Ожидание'));
    morph(document.getElementById('ash-who'), share(d.top_users||[], 'Пользователь') + share(d.top_dbs||[], 'База'));
    var q = d.top_queries || [];
    morph(document.getElementById('ash-queries'), q.length ? '<div class="table-wrap"><table><thead><tr><th>Запрос</th><th>База / пользователь</th><th>Главное ожидание</th><th style="width:22%">Доля</th><th class="num">AAS</th><th></th></tr></thead><tbody>'
      + q.map(function(x, i){
        return '<tr data-key="aq-'+i+'"><td class="qcell mono" title="'+esc(x.query)+'">'+esc(x.query)+'</td><td>'+esc(x.db)+' / '+esc(x.user)+'</td><td>'+esc(x.top_event)+'</td><td>'+pbar(x.pct)+' '+x.pct+'%</td><td class="num">'+x.aas+'</td>'
          +'<td><button class="btn sm" data-action="analyze-q" data-query="'+esc(x.query)+'">Анализ</button></td></tr>';
      }).join('') + '</tbody></table></div>' : '<p class="empty">Нет данных.</p>');
  }

  /* ================================================================ ПРОИЗВОДИТЕЛЬНОСТЬ */
  var perfTab = 'stmt', perfLast = {};
  function loadPerf(force){
    var now = Date.now();
    if(perfTab === 'stmt' && (force || !perfLast.stmt || now - perfLast.stmt > 60000)){ perfLast.stmt = now; return loadStmt(); }
    if(perfTab === 'ckpt' && (force || !perfLast.ckpt || now - perfLast.ckpt > 60000)){ perfLast.ckpt = now; return loadCkpt(); }
    if(perfTab === 'prog') return loadProgress();
    return Promise.resolve();
  }
  function loadStmt(){
    var h = document.getElementById('stmt-hours').value;
    return getJSON('/api/statements/report?hours='+h, true).then(function(j){
      var box = document.getElementById('stmt-top');
      if(!j || !j.ok){ morph(document.getElementById('stmt-regr'), ''); morph(box, '<p class="empty">'+esc((j && j.error)||'нет данных')+'</p>'); return; }
      morph(document.getElementById('stmt-meta'), tsRu(j.from)+' — '+tsRu(j.to)+' · узел '+esc(j.node||'')+' · суммарно '+dur(j.total_ms/1000));
      var rg = j.regressions || [];
      morph(document.getElementById('stmt-regr'), rg.length ? '<div class="alert warning"><b>Регрессии:</b> у '+rg.length+' запрос(ов) среднее время выросло в 2+ раза по сравнению с предыдущим периодом — возможна смена плана, рост данных или блокировки.</div>'
        + '<div class="table-wrap" style="margin-bottom:14px"><table><thead><tr><th>Запрос</th><th class="num">Было, мс</th><th class="num">Стало, мс</th><th class="num">Рост</th><th class="num">Вызовов</th><th></th></tr></thead><tbody>'
        + rg.map(function(x){ return '<tr><td class="qcell mono" title="'+esc(x.query)+'">'+esc(x.query)+'</td><td class="num">'+x.prev_mean_ms+'</td><td class="num lag-crit">'+x.mean_ms+'</td><td class="num">×'+x.ratio+'</td><td class="num">'+num(x.calls)+'</td><td><button class="btn sm" data-action="analyze-q" data-query="'+esc(x.query)+'">Анализ</button></td></tr>'; }).join('')
        + '</tbody></table></div>' : '');
      morph(box, '<div class="table-wrap"><table><thead><tr><th>Запрос</th><th class="num">Вызовов</th><th class="num" data-hint="Среднее время выполнения; в скобках — в предыдущем таком же периоде">Среднее, мс</th><th class="num">Всего</th><th style="width:16%">Доля времени</th>'
        + '<th class="num" data-hint="Блоков прочитано с диска (не из shared_buffers) за период">С диска</th><th class="num" data-hint="Блоков записано во временные файлы — не хватило work_mem">Temp</th><th></th></tr></thead><tbody>'
        + (j.top||[]).map(function(x){
          var tr = x.ratio != null ? (x.ratio >= 1.5 ? ' <span class="lag-crit">(было '+x.prev_mean_ms+')</span>' : ' <span class="muted">('+x.prev_mean_ms+')</span>') : '';
          return '<tr data-key="st-'+esc(x.qid)+'"><td class="qcell mono" title="'+esc(x.query)+'">'+esc(x.query)+'</td><td class="num">'+num(x.calls)+'</td><td class="num">'+x.mean_ms+tr+'</td><td class="num">'+dur(x.total_ms/1000)+'</td>'
            + '<td>'+pbar(x.pct)+' '+x.pct+'%</td><td class="num">'+num(x.blks_read)+'</td><td class="num '+(x.temp_w?'lag-warn':'')+'">'+num(x.temp_w)+'</td>'
            + '<td><button class="btn sm" data-action="analyze-q" data-query="'+esc(x.query)+'">Анализ</button></td></tr>';
        }).join('') + '</tbody></table></div>');
    });
  }
  function loadCkpt(){
    return getJSON('/api/perf/checkpoints', true).then(function(j){
      var box = document.getElementById('ckpt-out');
      if(!j || !j.ok){ morph(box, errBox(j && j.error)); return; }
      var r = j.rates, st = j.settings || {};
      var h = '';
      if(r){
        h += '<div class="metric-grid" style="margin-bottom:12px">'
          + '<div class="metric" data-hint="Скорость генерации WAL — объём изменений в базе"><span class="mk">WAL в секунду</span><span class="mv">'+fb(r.wal_bytes_per_s)+'</span></div>'
          + '<div class="metric" data-hint="При checkpoint_timeout '+esc(st.checkpoint_timeout||'')+' норма — около '+(st.checkpoint_timeout==='5min'?'12':'нескольких')+' в час"><span class="mk">Контрольных точек в час</span><span class="mv">'+r.checkpoints_per_h+'</span></div>'
          + '<div class="metric"><span class="mk">Запись одной КТ</span><span class="mv">'+dur((r.write_ms_per_ckpt||0)/1000)+'</span></div>'
          + '<div class="metric" data-hint="Долгий fsync — признак медленного хранилища"><span class="mk">fsync одной КТ</span><span class="mv">'+((r.sync_ms_per_ckpt||0)/1000).toFixed(2)+' с</span></div>'
          + '</div><p class="muted small">Окно '+dur(r.window_s)+'.</p>';
      } else h += '<p class="muted small">Счётчики с момента сброса статистики; скорости появятся через 2–3 минуты наблюдения.</p>';
      h += '<ul class="issue-list">'+(j.recommendations||[]).map(function(x){ return '<li class="'+x.level+'">'+esc(x.text)+'</li>'; }).join('')+'</ul>';
      h += '<h4 class="subh">Настройки</h4><div class="table-wrap"><table class="mono-t"><tbody>'
        + Object.keys(st).map(function(k){ return '<tr><td>'+esc(k)+'</td><td>'+esc(st[k]==null?'—':st[k])+'</td></tr>'; }).join('') + '</tbody></table></div>';
      morph(box, h);
    });
  }
  function loadProgress(){
    return getJSON('/api/progress', true).then(function(j){
      var box = document.getElementById('prog-out');
      if(!j || !j.ok){ morph(box, errBox(j && j.error)); return; }
      var rows = j.data || [];
      morph(box, rows.length ? '<div class="table-wrap"><table><thead><tr><th>Узел</th><th>Операция</th><th>Объект</th><th>Фаза</th><th style="width:20%">Прогресс</th><th class="num">Идёт</th><th class="num">Осталось ~</th><th>Кто</th></tr></thead><tbody>'
        + rows.map(function(x){
          return '<tr data-key="pg-'+esc(x.node+'-'+x.pid+'-'+x.kind)+'"><td>'+esc(x.node)+'</td><td><b>'+esc(x.kind)+'</b></td><td class="mono">'+esc((x.datname?x.datname+' · ':'')+(x.target||''))+'</td><td>'+esc(x.phase||'')+'</td>'
            + '<td>'+(x.pct!=null ? pbar(x.pct)+' '+x.pct+'%' : '<span class="muted">не оценивается</span>')+'</td><td class="num">'+dur(x.dur_s)+'</td><td class="num">'+(x.eta_s!=null?dur(x.eta_s):'—')+'</td><td>'+esc(x.usename||'')+'</td></tr>';
        }).join('') + '</tbody></table></div>' : '<p class="empty">Долгих операций сейчас нет.</p>');
    });
  }

  /* ================================================================ АУДИТ ИНДЕКСОВ */
  var IDX = [
    ['invalid', 'Невалидные индексы', 'Остались после неудачного CREATE INDEX CONCURRENTLY: не используются в запросах, но обновляются при каждой записи.'],
    ['duplicate', 'Дубликаты', 'Полностью совпадают с другим индексом той же таблицы — лишняя запись и место.'],
    ['overlap', 'Перекрытые индексы', 'Колонки индекса — начало другого индекса: тот же поиск обслужит более широкий индекс. Проверьте, не нужен ли узкий индекс для скорости.'],
    ['fk_no_index', 'Внешние ключи без индекса', 'Удаление/обновление строки в родительской таблице сканирует дочернюю целиком и держит блокировки.'],
    ['unused', 'Неиспользуемые', 'Ни одного сканирования с момента сброса статистики. Проверьте на всех узлах (реплики могут использовать индекс для отчётов).']
  ];
  function loadIdx(){
    var sel = document.getElementById('idx-db');
    return getJSON('/api/databases', true).then(function(j){
      if(j && j.ok) syncSelect(sel, j.data || []);
      var box = document.getElementById('idx-out'); uiLoading(box);
      return getJSON('/api/indexes/audit?db='+encodeURIComponent(sel.value||''), true).then(function(r){
        if(!r || !r.ok){ morph(box, errBox(r && r.error)); return; }
        morph(document.getElementById('idx-meta'), 'можно освободить ~'+fb(r.reclaimable_bytes)+(r.stats_reset?' · статистика с '+esc(String(r.stats_reset).slice(0,10)):''));
        morph(box, IDX.map(function(c){
          var rows = r[c[0]] || [];
          var h = '<details class="idx-cat" '+(rows.length?'open':'')+'><summary><b>'+c[1]+'</b> <span class="cnt '+(rows.length?'warn':'ok')+'">'+rows.length+'</span></summary><p class="muted small">'+c[2]+'</p>';
          if(!rows.length) return h + '<p class="empty">Не найдено.</p></details>';
          h += '<div class="table-wrap"><table><thead><tr><th>Таблица</th><th>'+(c[0]==='fk_no_index'?'Ограничение / колонки':'Индекс')+'</th><th class="num">Размер</th><th>Исправление</th></tr></thead><tbody>';
          rows.forEach(function(x){
            var what = c[0]==='fk_no_index' ? esc(x.constraint)+' ('+esc(x.columns)+') → '+esc(x.references)
              : esc(x.index)+(x.duplicate_of?' = '+esc(x.duplicate_of):'')+(x.covered_by?' ⊂ '+esc(x.covered_by):'');
            h += '<tr><td class="mono">'+esc(x.schema)+'.'+esc(x.table)+'</td><td class="mono" title="'+esc(x.def||'')+'">'+what+'</td><td class="num">'+fb(x.size!=null?x.size:x.table_size)+'</td><td>'+sqlBox(x.fix)+'</td></tr>';
          });
          return h + '</tbody></table></div></details>';
        }).join(''));
      });
    });
  }

  /* ================================================================ ЖУРНАЛЫ */
  function loadLogs(){
    syncSelect(document.getElementById('logs-node'), nodeOptions(false), {value:leaderName()});
    var node = document.getElementById('logs-node').value;
    return getJSON('/api/logs/settings?node='+encodeURIComponent(node), true).then(function(j){
      var box = document.getElementById('logs-settings');
      if(!j || !j.ok){ morph(box, errBox(j && j.error)); return; }
      var bad = (j.settings||[]).filter(function(s){ return !s.ok; });
      morph(box, '<details class="idx-cat" '+(bad.length?'open':'')+'><summary><b>Настройки журналирования</b> <span class="cnt '+(bad.length?'warn':'ok')+'">'+(bad.length ? bad.length+' выключено' : 'всё включено')+'</span></summary>'
        + '<p class="muted small">Без этих настроек часть разделов отчёта будет пустой. Формат строки: <span class="mono">'+esc((j.info||{}).log_line_prefix||'')+'</span></p>'
        + '<div class="table-wrap"><table><thead><tr><th>Параметр</th><th>Сейчас</th><th>Рекомендуется</th><th>Что даёт</th></tr></thead><tbody>'
        + j.settings.map(function(s){ return '<tr><td class="mono">'+esc(s.name)+'</td><td class="mono '+(s.ok?'':'lag-warn')+'">'+esc(s.current==null?'—':s.current)+'</td><td class="mono">'+esc(s.recommended)+'</td><td>'+esc(s.why)+'</td></tr>'; }).join('')
        + '</tbody></table></div></details>');
    });
  }
  function logsScan(){
    var node = document.getElementById('logs-node').value, mb = document.getElementById('logs-mb').value;
    var out = document.getElementById('logs-out');
    morph(out, '<p class="muted">Агент читает журнал на '+esc(node)+'…</p>');
    postJSON('/api/logs/scan', {node:node, max_mb:parseInt(mb, 10)}).then(function(j){
      if(!j || !j.ok){ morph(out, errBox(j && j.error)); return; }
      waitCmd(j.data.node, j.data.cmd_id, function(d, err){
        if(err){ morph(out, errBox(err)); return; }
        renderLogs(d);
        log('Журнал '+node+' разобран: '+num(d.lines)+' строк', 'ok');
      }, 90, '/api/logs/result');
    }).catch(function(e){ morph(out, errBox(e)); });
  }
  function renderLogs(d){
    var ck = d.checkpoints || {}, tf = d.temp_files || {};
    var h = '<div class="metric-grid" style="margin:12px 0">'
      + '<div class="metric"><span class="mk">Период</span><span class="mv small-mv">'+esc(d.first||'—')+'<br>'+esc(d.last||'—')+'</span></div>'
      + '<div class="metric"><span class="mk">Ошибок (ERROR/FATAL)</span><span class="mv '+(((d.by_severity||{}).ERROR||0)+((d.by_severity||{}).FATAL||0)?'warn':'good')+'">'+num(((d.by_severity||{}).ERROR||0)+((d.by_severity||{}).FATAL||0)+((d.by_severity||{}).PANIC||0))+'</span></div>'
      + '<div class="metric"><span class="mk">Deadlock</span><span class="mv '+(d.deadlocks?'bad':'good')+'">'+num(d.deadlocks)+'</span></div>'
      + '<div class="metric" data-hint="Вынужденные (wal) контрольные точки — признак малого max_wal_size"><span class="mk">Контрольные точки</span><span class="mv small-mv">'+num(ck.time)+' по времени<br>'+num(ck.wal)+' по объёму WAL</span></div>'
      + '<div class="metric"><span class="mk">Временные файлы</span><span class="mv small-mv">'+num(tf.count)+' шт.<br>'+fb(tf.bytes)+'</span></div>'
      + '</div><p class="muted small">Файл '+esc(d.file||'')+' ('+fb(d.file_size)+'), прочитано '+fb(d.read_bytes)+', '+num(d.lines)+' строк.</p>';
    function table(title, hint, rows, cols){
      if(!rows || !rows.length) return '';
      return '<h4 class="subh">'+title+'</h4>'+(hint?'<p class="muted small">'+hint+'</p>':'')+'<div class="table-wrap"><table><thead><tr>'+cols.map(function(c){ return '<th class="'+(c.n?'num':'')+'">'+c.h+'</th>'; }).join('')+'</tr></thead><tbody>'
        + rows.map(function(r){ return '<tr>'+cols.map(function(c){ return '<td class="'+(c.n?'num':'')+(c.q?' qcell mono':'')+'"'+(c.q?' title="'+esc(c.v(r))+'"':'')+'>'+(c.raw?c.v(r):esc(c.v(r)))+'</td>'; }).join('')+'</tr>'; }).join('')
        + '</tbody></table></div>';
    }
    h += table('Неудачные входы', 'Повторяющиеся неудачи — подбор пароля или приложение со старым паролем.', d.auth_failures, [{h:'Пользователь / адрес', v:function(r){ return r.user; }}, {h:'Попыток', n:1, v:function(r){ return num(r.count); }}]);
    h += table('Ошибки', 'Сгруппированы по тексту (числа и строки заменены на ?).', d.errors, [{h:'Уровень', v:function(r){ return r.severity; }}, {h:'Сообщение', q:1, v:function(r){ return r.example; }}, {h:'Раз', n:1, v:function(r){ return num(r.count); }}, {h:'Последний раз', v:function(r){ return r.last||''; }}]);
    h += table('Медленные запросы', 'По log_min_duration_statement.', d.slow_queries, [{h:'Запрос', q:1, v:function(r){ return r.example; }}, {h:'Раз', n:1, v:function(r){ return num(r.count); }}, {h:'Среднее, мс', n:1, v:function(r){ return num(r.avg_ms); }}, {h:'Макс, мс', n:1, v:function(r){ return num(r.max_ms); }}]);
    h += table('Временные файлы', 'Запросу не хватило work_mem — сортировка или хеш ушли на диск.', tf.top, [{h:'Запрос', q:1, v:function(r){ return r.query; }}, {h:'Файлов', n:1, v:function(r){ return num(r.count); }}, {h:'Всего', n:1, v:function(r){ return fb(r.bytes); }}, {h:'Макс', n:1, v:function(r){ return fb(r.max); }}]);
    h += table('Ожидания блокировок', 'По log_lock_waits (дольше deadlock_timeout).', d.lock_waits, [{h:'Время', v:function(r){ return r.time||''; }}, {h:'PID', n:1, v:function(r){ return r.pid; }}, {h:'Режим', v:function(r){ return r.mode; }}, {h:'Объект', v:function(r){ return r.object; }}, {h:'Ждал, мс', n:1, v:function(r){ return num(Math.round(r.ms)); }}]);
    h += table('Самые долгие автовакуумы', '', d.autovacuum, [{h:'Таблица', v:function(r){ return r.table; }}, {h:'Тип', v:function(r){ return r.kind; }}, {h:'Длительность', n:1, v:function(r){ return dur(r.elapsed_s); }}, {h:'Когда', v:function(r){ return r.time||''; }}]);
    morph(document.getElementById('logs-out'), h);
  }

  /* ================================================================ БЕЗОПАСНОСТЬ */
  function loadSecurity(){
    return getJSON('/api/security/audit', true).then(function(j){
      var box = document.getElementById('sec-out');
      if(!j || !j.ok){ morph(box, errBox(j && j.error)); return; }
      var sc = j.score, lv = sc >= 85 ? 'ok' : (sc >= 60 ? 'warn' : 'bad');
      morph(document.getElementById('sec-meta'), 'узел '+esc(j.node)+' · проверено '+new Date().toLocaleTimeString('ru-RU'));
      var cnt = {bad:0, warn:0, info:0}; (j.findings||[]).forEach(function(f){ cnt[f.level] = (cnt[f.level]||0)+1; });
      var h = '<div class="sec-head"><div class="score '+lv+'"><b>'+sc+'</b><span>из 100</span></div><div><div class="sec-verdict">'
        + (lv==='ok' ? 'Серьёзных проблем не найдено' : (lv==='warn' ? 'Есть что исправить' : 'Есть критичные уязвимости конфигурации'))+'</div>'
        + '<div class="muted">критичных '+cnt.bad+' · предупреждений '+cnt.warn+' · к сведению '+cnt.info+'</div></div></div>';
      h += (j.findings||[]).map(function(f, i){
        return '<div class="finding '+f.level+'" data-key="sf-'+i+'"><div class="f-h">'+lvTag(f.level)+' <b>'+esc(f.title)+'</b></div>'
          + (f.detail ? '<p>'+esc(f.detail)+'</p>' : '')
          + (f.fix ? sqlBox(f.fix) : '')
          + ((f.items||[]).length ? '<details'+(f.items.length<=6?' open':'')+'><summary class="small">'+f.items.length+' объект(ов)</summary><ul class="f-items">'+f.items.map(function(x){ return '<li class="mono">'+esc(x)+'</li>'; }).join('')+'</ul></details>' : '')
          + '</div>';
      }).join('') || '<p class="empty">Находок нет.</p>';
      morph(box, h);
    });
  }

  /* ================================================================ ОБНОВЛЕНИЯ */
  function loadUpgrade(){
    var sel = document.getElementById('upg-target');
    return getJSON('/api/upgrade/check'+(sel.value ? '?target='+encodeURIComponent(sel.value) : ''), true).then(function(j){
      var box = document.getElementById('upg-out');
      if(!j || !j.ok){ morph(box, errBox(j && j.error)); return; }
      var cur = parseInt(j.current_major||'0', 10);
      syncSelect(sel, (j.available_targets||[]).filter(function(v){ return parseInt(v,10) > cur; }).map(function(v){ return {value:v, label:'PostgreSQL '+v}; }), {value:j.target});
      morph(document.getElementById('upg-meta'), 'данные о версиях от '+esc(j.versions_updated||'—')+' (файл pg_versions.json)');
      var h = '<div class="table-wrap"><table><thead><tr><th>Узел</th><th>Версия</th><th>Последняя минорная</th><th>Поддержка до</th><th>Контрольные суммы</th></tr></thead><tbody>';
      var behind = false, eol = false;
      (j.nodes||[]).forEach(function(n){
        if(!n.ok){ h += '<tr><td>'+esc(n.node)+'</td><td colspan="4" class="sqlerr">'+esc(n.error||'')+'</td></tr>'; return; }
        var d = n.data; behind = behind || d.behind; eol = eol || d.eol_passed || d.eol_soon;
        h += '<tr><td>'+esc(n.node)+'</td><td class="mono">'+esc(d.version)+'</td><td class="mono '+(d.behind?'lag-warn':'')+'">'+esc(d.latest||'—')+(d.behind?' — доступно обновление':'')+'</td>'
          + '<td class="'+(d.eol_passed?'lag-crit':(d.eol_soon?'lag-warn':''))+'">'+esc(d.eol||'—')+(d.eol_passed?' — поддержка закончилась':(d.eol_soon?' — меньше года':''))+'</td><td>'+esc(d.checksums||'—')+'</td></tr>';
      });
      h += '</tbody></table></div>';
      if(behind) h += '<div class="alert warning">Минорные выпуски содержат исправления ошибок и уязвимостей. '+esc(j.minor_plan)+'</div>';
      if(eol) h += '<div class="alert critical">Версия без поддержки (или поддержка заканчивается в течение года) не получает исправлений безопасности — запланируйте переход на новую мажорную версию.</div>';
      if(j.target){
        h += '<h4 class="subh">Проверки перед pg_upgrade на PostgreSQL '+esc(j.target)+'</h4><ul class="pre-list">'
          + (j.prechecks||[]).map(function(c){ return '<li class="'+c.level+'">'+lvTag(c.level)+' <b>'+esc(c.title)+'</b> — '+esc(c.detail||'')
            + ((c.items||[]).length ? '<ul class="f-items">'+c.items.map(function(x){ return '<li class="mono">'+esc(x)+'</li>'; }).join('')+'</ul>' : '')+'</li>'; }).join('') + '</ul>';
        h += '<h4 class="subh">План мажорного обновления кластера Patroni</h4><ol class="plan">'+(j.major_plan||[]).map(function(s){ return '<li>'+esc(s)+'</li>'; }).join('')+'</ol>';
      }
      morph(box, h);
    });
  }

  /* ================================================================ ЕЖЕНЕДЕЛЬНЫЙ ОТЧЁТ */
  function weeklyLoad(){
    return getJSON('/api/report/weekly/schedule', true).then(function(j){
      if(!j || !j.ok) return;
      var d = j.data;
      document.getElementById('wk-on').checked = !!d.enabled;
      document.getElementById('wk-day').value = String(d.weekday);
      document.getElementById('wk-time').value = d.time || '09:00';
      morph(document.getElementById('wk-meta'), d.last_sent ? 'последняя отправка '+esc(d.last_sent) : 'ещё не отправлялся');
    });
  }
  function weeklySave(){
    postJSON('/api/report/weekly/schedule', {enabled:document.getElementById('wk-on').checked,
      weekday:parseInt(document.getElementById('wk-day').value, 10), time:document.getElementById('wk-time').value}).then(function(j){
      if(j && j.ok) log('Расписание отчёта сохранено', 'ok'); else log('Отчёт: '+((j && j.error)||'ошибка'), 'err');
    });
  }
  function weeklySend(){
    postJSON('/api/report/weekly/send', {}).then(function(j){
      if(j && j.ok) log('Еженедельный отчёт отправлен', 'ok'); else log('Отчёт: '+((j && j.error)||'ошибка'), 'err');
      weeklyLoad();
    });
  }

  /* ================================================================ РЕГИСТРАЦИЯ */
  var loaders = window.__loaders || (window.__loaders = {});
  var pollers = window.__pollers || (window.__pollers = {});
  var actions = window.__actions || (window.__actions = {});
  var prevReports = loaders.reports;
  Object.assign(loaders, {
    risk: function(){ return fetchRisk(true).then(renderRisk); },
    ash: loadAsh, perf: function(){ return loadPerf(true); }, logs: loadLogs, security: loadSecurity, upgrade: loadUpgrade,
    reports: function(){ weeklyLoad(); return prevReports ? prevReports() : null; }
  });
  Object.assign(pollers, {
    risk: function(){ return fetchRisk(); }, ash: loadAsh, perf: function(){ return loadPerf(false); }
  });
  Object.assign(actions, {
    'risk-freeze': riskFreeze,
    'risk-jump': function(t){ var p = document.getElementById(t.getAttribute('data-panel')); if(p) p.scrollIntoView({behavior:'smooth', block:'start'}); },
    'copy': function(t){
      var txt = t.getAttribute('data-text') || '';
      (navigator.clipboard ? navigator.clipboard.writeText(txt) : Promise.reject()).then(function(){ log('Скопировано в буфер обмена', 'ok'); },
        function(){ var ta = document.createElement('textarea'); ta.value = txt; document.body.appendChild(ta); ta.select(); try{ document.execCommand('copy'); log('Скопировано в буфер обмена', 'ok'); }catch(e){} ta.remove(); });
    },
    'analyze-q': function(t){
      var q = t.getAttribute('data-query') || '';
      showScreen('analyzer');
      var seg = document.querySelector('#an-tabs .seg[data-an="custom"]'); if(seg) seg.click();
      var ta = document.getElementById('custom-query'); if(ta) ta.value = q;
      log('Запрос перенесён в анализатор. Для запросов с параметрами $1 подставьте значения.', 'info');
    }
  });
  var bind = function(id, ev, fn){ var el = document.getElementById(id); if(el) el.addEventListener(ev, fn); };
  bind('ash-node', 'change', loadAsh); bind('ash-minutes', 'change', loadAsh);
  bind('stmt-hours', 'change', loadStmt);
  bind('logs-node', 'change', loadLogs);
  bind('upg-target', 'change', loadUpgrade);
  bind('idx-db', 'change', loadIdx);
  var pt = document.getElementById('perf-tabs');
  if(pt) pt.addEventListener('click', function(e){
    var b = e.target.closest('.seg'); if(!b) return;
    perfTab = b.getAttribute('data-perf');
    pt.querySelectorAll('.seg').forEach(function(x){ x.classList.toggle('active', x === b); });
    document.querySelectorAll('.perf-pane').forEach(function(p){ p.classList.toggle('hidden', p.getAttribute('data-perf') !== perfTab); });
    loadPerf(true);
  });
  var dt = document.getElementById('dba-tabs');
  if(dt) dt.addEventListener('click', function(e){
    var b = e.target.closest('.seg'); if(b && b.getAttribute('data-dba') === 'idx') loadIdx();
  });
  /* подсказка на форме входа, если включён вход через домен */
  fetch('/api/auth/providers').then(function(r){ return r.json(); }).then(function(j){
    var f = document.getElementById('login-foot');
    if(f && j && j.ok && j.data.ldap) f.textContent = 'Можно войти: '+(j.data.ldap_hint || 'доменная учётная запись')+'.';
  }).catch(function(){});

  Object.assign(window, {riskRestoreTest:riskRestoreTest, logsScan:logsScan, loadSecurity:loadSecurity,
                         weeklySave:weeklySave, weeklySend:weeklySend, fetchRisk:fetchRisk});
})();
