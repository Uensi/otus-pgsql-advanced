/* Patroni Console — MODULES [modules-20260730d] */
(function(){
  var MOD_VERSION = 'modules-20261010s';
  var currentSchema = null;
  var __tuneCurrent = {};
  var __alertRules = [];
  var lastPlan = null, lastPlanQuery = '';
  window.__charts = [];

  /* Общие хелперы (cssVar/fb/esc/log/getJSON/postJSON/confirmBox) определены глобально
     в app-core.js — здесь не дублируем, чтобы не расходиться в поведении и
     пользоваться единым реестром «в полёте» (дедупликация запросов). */

  function makeChartOpts(yFmt){
    var x = { type:'time', grid:{color:cssVar('--grid')}, ticks:{color:cssVar('--muted'), maxTicksLimit:6} };
    x.time = { tooltipFormat:'HH:mm:ss' };
    var y = { beginAtZero:true, grid:{color:cssVar('--grid')}, ticks:{color:cssVar('--muted')} };
    if(yFmt) y.ticks = { callback: yFmt, color: cssVar('--muted') };
    return { responsive:true, maintainAspectRatio:false, animation:false, plugins:{legend:{display:false}}, scales:{x:x, y:y} };
  }
  function makeMultiOpts(yFmt){
    var x = { type:'time', grid:{color:cssVar('--grid')}, ticks:{color:cssVar('--muted'), maxTicksLimit:6} };
    x.time = { tooltipFormat:'HH:mm:ss' };
    var y = { beginAtZero:true, grid:{color:cssVar('--grid')}, ticks:{color:cssVar('--muted')} };
    if(yFmt) y.ticks = { callback: yFmt, color: cssVar('--muted') };
    return { responsive:true, maintainAspectRatio:false, animation:false,
             plugins:{legend:{labels:{color:cssVar('--dim'),boxWidth:12}}}, scales:{x:x, y:y} };
  }
  function makeChart(id, type, yFmt, multi){
    if(typeof Chart === 'undefined' || !document.getElementById(id)) return null;
    if(window.__charts[id]) window.__charts[id].destroy();
    var opts = multi ? makeMultiOpts(yFmt) : makeChartOpts(yFmt);
    if(type === 'bar'){ opts.scales.x = { type:'category', grid:{display:false}, ticks:{color:cssVar('--muted')} }; }
    var cfg = type === 'bar'
      ? { type:'bar', data:{labels:[], datasets:[{data:[], backgroundColor:'#3d7ab5'}]}, options:opts }
      : { type:'line', data:{datasets:[{data:[], borderColor:'#3d7ab5', tension:0.2, pointRadius:0, borderWidth:2, fill:true, backgroundColor:'rgba(61,122,181,0.08)'}]}, options:opts };
    window.__charts[id] = new Chart(document.getElementById(id).getContext('2d'), cfg);
    return window.__charts[id];
  }
  function pushLine(chart, points, field, nested){
    if(!chart) return;
    var data = points.map(function(p){
      var obj = nested ? ((p.metrics||{})[nested]||{}) : (p.metrics||{});
      return obj[field] != null ? { x: p.ts*1000, y: Number(obj[field]) } : null;
    }).filter(function(d){ return d; });
    chart.data.datasets[0].data = data;
    chart.update('none');
  }
  function pushBar(chart, obj, keys){
    if(!chart) return;
    var labels=[], values=[];
    keys.forEach(function(k){ if(obj[k]!=null){ labels.push(k); values.push(Number(obj[k])); } });
    chart.data.labels = labels; chart.data.datasets[0].data = values;
    chart.update('none');
  }
  function renderTable(bodyId, rows, cols, emptyMsg){
    var tb = document.getElementById(bodyId); if(!tb) return;
    if(!rows.length){ morph(tb, '<tr><td colspan="'+cols.length+'" class="muted center">'+(emptyMsg||'—')+'</td></tr>'); return; }
    morph(tb, rows.map(function(r){
      return '<tr>'+cols.map(function(c){ return '<td class="'+(c.class||'')+'">'+c.render(r)+'</td>'; }).join('')+'</tr>';
    }).join(''));
  }

  function pollAgentStatus(){
    if(window.__inLogin) return;
    getJSON('/api/agent/status', true).then(function(j){ if(j && j.ok) window.__agents = j.agents || {}; }).catch(function(){});
  }
  setInterval(pollAgentStatus, 5000); pollAgentStatus();
  function pollAgentNow(cb){
    getJSON('/api/agent/status', true).then(function(j){ if(j && j.ok){ window.__agents = j.agents || {}; if(cb) cb(); } });
  }

  function loadCenter(){
    return getJSON('/api/center', true).then(function(j){
      if(!j || !j.ok) return;
      var box = document.getElementById('center-grid'); if(!box) return;
      morph(box, j.data.map(function(c){
        var chips = c.members.map(function(m){
          var isL = ['leader','master','primary'].indexOf((m.role||'').toLowerCase()) >= 0;
          return '<span class="nchip-state '+(['running','streaming'].indexOf(m.state)>=0?'ok':'bad')+'" style="margin-right:6px">'+(isL?'лидер · ':'')+esc(m.name)+'</span>';
        }).join('');
        return '<div class="ocard" style="'+(c.active?'border:2px solid var(--good);':'')+'">'
          +'<h3>'+esc(c.name)+(c.active?' <span class="badge role-master">активен</span>':'')+(c.up===false?' <span class="badge role-replica">недоступен</span>':'')+'</h3>'
          +'<p class="muted">узлов: <b>'+c.nodes+'</b> · лидер: <b>'+esc(c.leader||'—')+'</b> · макс лаг: <b>'+fb(c.max_lag||0)+'</b></p>'
          +'<p class="muted">агентов онлайн: <b>'+c.agents_online+'/'+c.nodes+'</b></p>'
          +'<div style="margin:8px 0">'+chips+'</div>'
          +(!c.active ? '<button class="btn primary gate-admin" data-action="center-select" data-name="'+esc(c.name)+'">управлять</button>' : '')
          +'</div>';
      }).join('') || '<p class="muted">нет кластеров</p>');
    }).catch(function(e){ log('center: '+e.message,'err'); });
  }
  function fillClusterSwitch(){
    getJSON('/api/center/clusters', true).then(function(j){
      if(!j || !j.ok) return;
      var sel = document.getElementById('cluster-switch'); if(!sel) return;
      var role = window.__role || 'viewer';
      if(role !== 'admin'){ sel.classList.add('hidden'); return; }
      sel.classList.remove('hidden');
      var act = null;
      j.data.forEach(function(c){ if(c.active) act = c.name; });
      syncSelect(sel, j.data.map(function(c){ return c.name; }), {keep:false, value:act});
      sel.onchange = function(){
        var target = sel.value;
        confirmBox('Переключиться на кластер '+target+'?', 'Все экраны консоли начнут показывать этот кластер. На сами кластеры это не влияет.', function(){
          postJSON('/api/center/select', {name: target}).then(function(r){ if(r && r.ok) location.reload(); else log('Кластер: '+((r && r.error)||'ошибка'),'err'); });
        });
        syncSelect(sel, j.data.map(function(c){ return c.name; }), {keep:false, value:act});
      };
    }).catch(function(){});
  }

  /* ===== Хосты и инстансы (реестр + ОС-метрики + прогноз дисков) ===== */
  function fbUptime(s){
    if(s==null) return '—';
    var d = Math.floor(s/86400), h = Math.floor((s%86400)/3600), m = Math.floor((s%3600)/60);
    return (d ? d+'д ' : '')+h+'ч '+m+'м';
  }
  function svcIcon(v){
    if(!v || v==='unknown') return '<span class="muted">?</span>';
    if(v==='active' || v==='ok') return '<span style="color:var(--good)">●</span>';
    return '<span style="color:var(--crit)">●</span>';
  }
  function loadHosts(){
    var box = document.getElementById('hosts-grid');
    var fbox = document.getElementById('disk-forecasts');
    uiLoading(box);
    return getJSON('/api/hosts', true).then(function(j){
      if(!j || !j.ok || !box) return;
      var rows = j.data || [];
      if(!rows.length){ morph(box, '<p class="muted">нет агентов и standalone-инстансов</p>'); if(fbox) morph(fbox, ''); return; }
      morph(box, rows.map(function(h){
        if(h.kind === 'standalone'){
          var dbs = (h.databases||[]).map(function(d){
            return '<div class="mono" style="font-size:.85em">'+esc(d.name)+' · '+fb(d.size)+' · '+(d.conn||0)+' conn</div>';
          }).join('');
          return '<div class="ocard">'
            +'<h3>'+esc(h.name)+' <span class="badge '+(h.up?'role-master':'role-replica')+'">'+(h.up?'доступен':'нет соединения')+'</span></h3>'
            +'<p class="muted">standalone · '+esc(h.host)+':'+h.port+(h.version ? ' · PG '+esc(h.version) : '')+'</p>'
            +(h.error ? '<p class="sqlerr">'+esc(h.error)+'</p>' : dbs || '<p class="muted">нет данных</p>')
            +'</div>';
        }
        var mem = h.mem || {};
        var load = (h.loadavg||[]).join(' ');
        var disks = (h.disks||[]).map(function(d){
          var pct = d.used_pct || 0;
          var col = pct > 90 ? 'var(--crit)' : pct > 75 ? 'var(--amber)' : 'var(--good)';
          var fc = d.forecast;
          var fct = '';
          if(fc && fc.hours != null) fct = ' · закончится ~'+(fc.hours>48 ? Math.round(fc.hours/24)+'д' : Math.round(fc.hours)+'ч')+' ('+fc.slope_mb_h+' МБ/ч)';
          else if(fc && fc.note) fct = '';
          return '<div style="font-size:.85em" class="mono">'+esc(d.path)+' — <b style="color:'+col+'">'+pct+'%</b> из '+fb(d.total)+fct+'</div>';
        }).join('');
        var warns = (h.kernel_warnings||[]).map(function(w){ return '<div class="sqlerr" style="font-size:.8em">'+esc(w)+'</div>'; }).join('');
        var svcs = h.services || {};
        var svc = ['postgresql','patroni','etcd','consul'].map(function(s){
          return svcs[s] ? svcIcon(svcs[s])+' <span class="muted" style="font-size:.8em">'+s+'</span>' : '';
        }).join(' &nbsp; ');
        return '<div class="ocard">'
          +'<h3>'+esc(h.name)+' <span class="badge '+(h.online?'role-master':'role-replica')+'">'+(h.online?'онлайн':'офлайн')+'</span></h3>'
          +'<p class="muted">CPU×'+(h.cpus||'?')+' · load '+esc(load || '—')+(h.cpu_used_pct!=null ? ' · CPU '+h.cpu_used_pct+'%' : '')+(h.cpu_iowait_pct!=null ? ' (iowait '+h.cpu_iowait_pct+'%)' : '')+'</p>'
          +'<p class="muted">RAM: '+fb(mem.total||0)+' (свободно '+fb(mem.available||0)+')'+(mem.swap_used ? ' · swap '+fb(mem.swap_used) : '')+' · uptime '+fbUptime(h.uptime_s)+'</p>'
          +'<div style="margin:6px 0">'+disks+'</div>'
          +'<div style="margin:4px 0">'+svc+'</div>'
          +(h.pg_version ? '<p class="muted" style="font-size:.8em">'+esc(h.pg_version)+'</p>' : '')
          +warns
          +'<div style="margin-top:8px" class="gate-admin">'
          +'<button class="btn sm" data-action="host-svc-restart" data-node="'+esc(h.name)+'" data-svc="postgresql">restart postgresql</button> '
          +'<button class="btn sm" data-action="host-svc-restart" data-node="'+esc(h.name)+'" data-svc="patroni">restart patroni</button>'
          +'</div>'
          +'</div>';
      }).join(''));
      renderDiskForecasts(rows, fbox);
    }).catch(function(e){ log('hosts: '+e.message,'err'); });
  }
  function renderDiskForecasts(rows, fbox){
    if(!fbox) return;
    var items = [];
    rows.forEach(function(h){
      if(h.kind !== 'agent') return;
      (h.disks||[]).forEach(function(d){
        if(d.forecast && d.forecast.hours != null) items.push({host:h.name, path:d.path, used_pct:d.used_pct, free:d.free, forecast:d.forecast});
      });
    });
    if(!items.length){ morph(fbox, '<p class="muted">прогноз появится после накопления точек истории (2–3 мин)</p>'); return; }
    items.sort(function(a,b){ return a.forecast.hours - b.forecast.hours; });
    morph(fbox, '<table><thead><tr><th>Хост</th><th>Диск</th><th>Занято</th><th>Свободно</th><th>Темп роста</th><th>Прогноз заполнения</th></tr></thead><tbody>'
      + items.map(function(it){
        var hrs = it.forecast.hours, fcol = hrs < 24*7 ? 'var(--crit)' : (hrs < 24*30 ? 'var(--amber)' : 'var(--good)');
        return '<tr><td>'+esc(it.host)+'</td><td class="mono">'+esc(it.path)+'</td><td>'+it.used_pct+'%</td><td>'+fb(it.free)+'</td><td>'+it.forecast.slope_mb_h+' МБ/ч</td>'
          +'<td><b style="color:'+fcol+'">'+(it.forecast.hours>48 ? Math.round(it.forecast.hours/24)+' дней' : Math.round(it.forecast.hours)+' часов')+'</b></td></tr>';
      }).join('')+'</tbody></table>');
  }
  function hostSvc(node, svc, action){
    confirmBox(action+' службы '+svc+' на '+node+'?', 'Действие выполнит агент на хосте.', function(){
      postJSON('/api/hosts/service', {node:node, service:svc, action:action}).then(function(j){
        if(j && j.ok) log('команда '+action+' '+svc+' → '+node+' отправлена', 'ok');
        else log('service: '+((j && j.error) || 'ошибка'), 'err');
      }).catch(function(e){ log('service: '+e.message,'err'); });
    });
  }

  /* ===== мультинодовые TPS / Cache Hit ===== */
  var tpsAllChart, cacheAllChart;
  function initMultiCharts(){
    if(typeof Chart === 'undefined') return;
    if(!tpsAllChart) tpsAllChart = makeChart('m-tps-all','line', null, true);
    if(!cacheAllChart) cacheAllChart = makeChart('m-cache-all','line', function(v){ return v+'%'; }, true);
  }
  function drawSeries(chart, h, field, diff){
    if(!chart || !h || !h.length) return;
    var sl = h.slice(-60), names={}, ds=[];
    sl.forEach(function(s){ Object.keys(s.nodes||{}).forEach(function(n){ names[n]=1; }); });
    Object.keys(names).sort().forEach(function(nm){
      var pts=[];
      if(diff){
        for(var i=1;i<sl.length;i++){
          var pv=sl[i-1].nodes[nm], cu=sl[i].nodes[nm];
          if(pv && cu) pts.push({ x:sl[i].ts*1000, y:Math.max(0,(cu[field]-pv[field])/(sl[i].ts-sl[i-1].ts)) });
        }
      } else {
        sl.forEach(function(s){ if(s.nodes && s.nodes[nm]) pts.push({ x:s.ts*1000, y:s.nodes[nm][field] }); });
      }
      ds.push({ label:nm, data:pts });
    });
    syncDatasets(chart, ds);
  }
  function fetchPerf(silent){
    return getJSON('/api/perf/snapshot', true).then(function(){
      return Promise.all([getJSON('/api/perf/tps-history', true), getJSON('/api/perf/cache-history', true)]);
    }).then(function(arr){
      if(arr[0] && arr[0].ok) drawSeries(tpsAllChart, arr[0].data, 'total_xact', true);
      if(arr[1] && arr[1].ok) drawSeries(cacheAllChart, arr[1].data, 'cache_hit_ratio', false);
    }).catch(function(e){ if(!silent) log('perf: '+e.message, 'err'); });
  }

  /* ===== per-node метрики ===== */
  function fillMetricsNodes(){
    var sel = document.getElementById('metrics-node'); if(!sel) return;
    return getJSON('/api/metrics/nodes', true).then(function(j){
      if(!j || !j.ok) return;
      syncSelect(sel, j.data);
      renderMetrics();
    }).catch(function(){});
  }
  function loadMetricsScreen(){
    initMultiCharts();
    return Promise.all([fetchPerf(), fillMetricsNodes()]);
  }
  function renderMetrics(){
    var sel = document.getElementById('metrics-node'); var node = sel ? sel.value : '';
    var pts = parseInt(document.getElementById('metrics-points').value, 10) || 600;
    if(!node) return;
    getJSON('/api/metrics/history?node='+encodeURIComponent(node)+'&points='+pts, true).then(function(j){
      if(!j || !j.ok) return;
      var history = j.data || [], last = history.length ? history[history.length-1] : null;
      var st = document.getElementById('metrics-status');
      if(st) morph(st, (!last || !last.metrics) ? '<b style="color:var(--crit)">нет данных</b>'
        : 'точек: <b>'+history.length+'</b> · обновление: '+new Date(last.ts*1000).toLocaleTimeString('ru-RU'));
      var m = last ? last.metrics : {};
      if(!window.__charts['m-conn']) window.__charts['m-conn'] = makeChart('m-conn','bar');
      pushBar(window.__charts['m-conn'], m.conn || {}, ['active','idle','idle_in_tx']);
      if(!window.__charts['m-tps']) window.__charts['m-tps'] = makeChart('m-tps','line');
      pushLine(window.__charts['m-tps'], history, 'tps', 'rates');
      if(!window.__charts['m-cache']) window.__charts['m-cache'] = makeChart('m-cache','line', function(v){ return v.toFixed(1)+'%'; });
      pushLine(window.__charts['m-cache'], history, 'cache_hit_ratio', 'rates');
      if(!window.__charts['m-tuples']) window.__charts['m-tuples'] = makeChart('m-tuples','line');
      var tpts = history.map(function(p){
        var r = (p.metrics||{}).rates || {};
        return { x:p.ts*1000, y:(r.tup_inserted_per_s||0)+(r.tup_updated_per_s||0)+(r.tup_deleted_per_s||0) };
      });
      if(window.__charts['m-tuples']){ window.__charts['m-tuples'].data.datasets[0].data = tpts; window.__charts['m-tuples'].update('none'); }
      [['m-locks','locks_waiting',null],['m-deadlocks','deadlocks','db'],['m-long','long_queries',null],['m-temp','temp_files','db']].forEach(function(cfg){
        if(!window.__charts[cfg[0]]) window.__charts[cfg[0]] = makeChart(cfg[0],'line');
        pushLine(window.__charts[cfg[0]], history, cfg[1], cfg[2]);
      });
    }).catch(function(){});
  }

  function loadSchema(){
    return getJSON('/api/databases').then(function(jd){
      var dbs = (jd && jd.ok && jd.data) ? jd.data : [];
      var ctrl = document.querySelector('[data-screen="schema"] .inline-ctrl'), sel = document.getElementById('erd-db');
      if(!sel && ctrl){
        var lab = document.createElement('label');
        lab.innerHTML = '<span style="margin-right:6px">база</span>';
        sel = document.createElement('select'); sel.id = 'erd-db';
        sel.onchange = function(){ loadSchemaWithDb(sel.value); };
        lab.appendChild(sel); ctrl.insertBefore(lab, ctrl.firstChild);
      }
      syncSelect(sel, dbs);
      return loadSchemaWithDb(sel ? sel.value : (dbs[0]||''));
    }).catch(function(e){ log('базы: '+e.message, 'err'); });
  }
  function loadSchemaWithDb(db){
    var c = document.getElementById('erd-diagram'); morphReset(c); morph(c, '<div class="skeleton"><i></i><i></i><i></i></div>');
    return getJSON('/api/schema?db='+encodeURIComponent(db||'')).then(function(j){
      if(!j.ok){ morph(c, '<p class="sqlerr">'+esc(j.error)+'</p>'); return; }
      currentSchema = j.data;
      drawERD(j.data);
      morph(document.getElementById('table-list-content'), j.data.tables.map(function(t){
        return '<div class="titem" data-action="show-table" data-fn="'+esc(t.full_name)+'"><div class="tn">'+esc(t.schema)+'.'+esc(t.name)+'</div><div class="tm">'+fb(t.size)+' · '+t.columns.length+' кол.</div></div>';
      }).join(''));
      log('ERD: '+j.data.tables.length+' таблиц ✓', 'ok');
    }).catch(function(e){ morph(c, '<p class="sqlerr">'+esc(e.message)+'</p>'); });
  }
  function loadDbs(){
    var body = document.getElementById('db-overview-body');
    var stats = document.getElementById('db-stats');
    uiLoadingRow(body, 7);
    return getJSON('/api/db/overview').then(function(j){
      if(!j || !j.ok){
        if(body) morph(body, '<tr><td colspan="7" class="sqlerr">'+esc((j && j.error) || 'ошибка')+'</td></tr>');
        return;
      }
      var rows = j.data || [];
      var totalSize = 0, totalConn = 0;
      rows.forEach(function(r){ totalSize += (r.size || 0); totalConn += (r.connections || 0); });
      if(stats){
        morph(stats, '<div class="metric"><span class="mk">Баз</span><span class="mv">'+rows.length+'</span></div>'
          + '<div class="metric"><span class="mk">Суммарный размер</span><span class="mv good">'+fb(totalSize)+'</span></div>'
          + '<div class="metric"><span class="mk">Соединений</span><span class="mv">'+totalConn+'</span></div>'
          + '<div class="metric"><span class="mk">Основная БД</span><span class="mv">'+(rows[0] ? esc(rows[0].datname) : '—')+'</span></div>');
      }
      if(body){
        morph(body, rows.map(function(r){
          var cache = r.cache_hit;
          var cacheCls = (cache == null) ? '' : (cache >= 95 ? 'good' : (cache >= 80 ? 'warn' : 'bad'));
          var txns = (r.xact_commit || 0) + (r.xact_rollback || 0);
          var growthTxt = '—';
          if(r.growth_rate != null){
            var sign = r.growth_rate > 0 ? '+' : '';
            growthTxt = sign + fb(r.growth_rate) + '/ч';
          }
          return '<tr>'
            + '<td><b>'+esc(r.datname)+'</b></td>'
            + '<td class="mono">'+fb(r.size)+'</td>'
            + '<td class="mono">'+(r.connections || 0)+'</td>'
            + '<td class="mono '+cacheCls+'">'+(cache == null ? '—' : cache+'%')+'</td>'
            + '<td class="mono">'+txns.toLocaleString('ru-RU')+'</td>'
            + '<td class="mono">'+growthTxt+'</td>'
            + '<td class="muted">'+esc(r.encoding || '—')+'</td>'
            + '</tr>';
        }).join('') || '<tr><td colspan="7" class="muted center">баз нет</td></tr>');
      }
    }).catch(function(e){ if(body) morph(body, '<tr><td colspan="7" class="sqlerr">'+esc(e.message)+'</td></tr>'); });
  }
  function drawERD(sch){
    var c = document.getElementById('erd-diagram'), T=sch.tables, R=sch.relationships;
    var W=280, H=40, CH=24, P=30, GX=60, GY=100, cols=Math.max(1, Math.ceil(Math.sqrt(T.length)));
    var pos={}, sw=P*2+cols*W+(cols-1)*GX+100, sh=P*2+Math.ceil(T.length/cols)*(H+400+GY)+100;
    /* presentation-атрибуты SVG не понимают var(): подставляем конкретные цвета темы */
    var CL = { fill:cssVar('--panel'), stroke:cssVar('--cyan'), text:cssVar('--text'),
               muted:cssVar('--muted'), inset:cssVar('--inset'), line2:cssVar('--line2'), amber:cssVar('--amber') };
    T.forEach(function(t,i){ pos[t.full_name] = { x:P+(i%cols)*(W+GX), y:P+Math.floor(i/cols)*(H+400+GY) }; });
    var s = '<svg width="'+sw+'" height="'+sh+'" xmlns="http://www.w3.org/2000/svg">';
    R.forEach(function(r){
      var a=pos[r.source], b=pos[r.target];
      if(a && b){
        var x1=a.x+W/2, y1=a.y+H, x2=b.x+W/2, y2=b.y, my=(y1+y2)/2;
        s += '<path d="M '+x1+' '+y1+' C '+x1+' '+my+', '+x2+' '+my+', '+x2+' '+y2+'" stroke="'+CL.stroke+'" stroke-width="2" fill="none" marker-end="url(#ar)"/>';
      }
    });
    T.forEach(function(t){
      var p=pos[t.full_name], x=p.x, y=p.y;
      s += '<rect x="'+x+'" y="'+y+'" width="'+W+'" height="'+H+'" fill="'+CL.fill+'" stroke="'+CL.stroke+'" stroke-width="2" rx="7"/>';
      s += '<text x="'+(x+10)+'" y="'+(y+26)+'" fill="'+CL.text+'" font-weight="700" font-size="14">'+esc(t.name)+'</text>';
      s += '<text x="'+(x+W-10)+'" y="'+(y+26)+'" fill="'+CL.muted+'" font-size="10" text-anchor="end">'+fb(t.size)+'</text>';
      t.columns.forEach(function(col,ci){
        var cy=y+H+ci*CH, pk=t.primary_key.indexOf(col.name)>=0;
        s += '<rect x="'+x+'" y="'+cy+'" width="'+W+'" height="'+CH+'" fill="'+CL.inset+'" stroke="'+CL.line2+'"/>';
        s += '<text x="'+(x+8)+'" y="'+(cy+16)+'" fill="'+(pk?CL.amber:CL.text)+'" font-size="11">'+(pk?'PK ':'')+esc(col.name)+'</text>';
      });
    });
    s += '<defs><marker id="ar" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto"><path d="M0,0 L0,6 L9,3 z" fill="'+CL.stroke+'"/></marker></defs></svg>';
    morph(c, s);
  }
  function showTable(fn){
    if(!currentSchema) return;
    var t = currentSchema.tables.find(function(x){ return x.full_name === fn; });
    if(!t) return;
    morph(document.getElementById('table-list-content'), '<h3 style="color:var(--cyan)">'+esc(t.schema)+'.'+esc(t.name)+'</h3>'
      + t.columns.map(function(c){ return '<div class="titem" style="cursor:default">'+esc(c.name)+' <span class="tm">('+esc(c.type)+')</span></div>'; }).join('')
      + '<button class="btn sm" data-action="back-table">← к списку</button>');
  }
  function downloadSchema(){
    var svg = document.querySelector('#erd-diagram svg'); if(!svg){ log('нет svg', 'err'); return; }
    var cv = document.createElement('canvas'), cx = cv.getContext('2d'), img = new Image();
    img.onload = function(){
      cv.width = img.width; cv.height = img.height;
      cx.fillStyle = cssVar('--inset'); cx.fillRect(0,0,cv.width,cv.height); cx.drawImage(img,0,0);
      var a = document.createElement('a'); a.download = 'erd.png'; a.href = cv.toDataURL('image/png'); a.click();
    };
    img.src = 'data:image/svg+xml;base64,'+btoa(unescape(encodeURIComponent(new XMLSerializer().serializeToString(svg))));
  }

  function loadSlow(){
    var c = document.getElementById('slow-list'); uiLoading(c);
    return getJSON('/api/querylab/slow').then(function(j){
      if(!j.ok){ morph(c, '<p class="sqlerr">'+esc(j.error)+'</p>'); return; }
      morph(c, j.data.length ? j.data.map(function(q){
        var sysBadge = q.system
          ? '<span class="badge role-replica" title="'+esc(q.label||'служебный')+'">служебный</span>'
          : '<span class="badge role-master" title="пользовательский запрос — можно оптимизировать">пользовательский</span>';
        return '<div class="qitem" data-action="analyze" data-query="'+esc(q.query)+'">'
          + '<div class="qh">'+sysBadge+'<span class="muted" style="font-size:.78em">cache hit '+q.cache_hit+'%</span></div>'
          + '<div class="qt">'+esc(q.query)+'</div>'
          + '<div class="qstats"><span class="qs"><span class="k">вызовов</span><span class="v">'+q.calls+'</span></span>'
          + '<span class="qs"><span class="k">среднее</span><span class="v '+(q.mean_time>1000?'bad':(q.mean_time>300?'warn':'good'))+'">'+q.mean_time+'мс</span></span>'
          + '<span class="qs"><span class="k">макс</span><span class="v">'+q.max_time+'мс</span></span></div></div>';
      }).join('') : '<p class="muted">нет медленных запросов</p>');
    }).catch(function(e){ morph(c, '<p class="sqlerr">'+esc(e.message)+'</p>'); });
  }
  function loadCurrent(){
    var c = document.getElementById('current-list'); uiLoading(c);
    return getJSON('/api/querylab/current').then(function(j){
      if(!j.ok){ morph(c, '<p class="sqlerr">'+esc(j.error)+'</p>'); return; }
      morph(c, j.data.length ? j.data.map(function(q){
        return '<div class="qitem"><div class="qt">'+esc(q.query)+'</div><div class="qstats"><span class="qs">'+q.duration_ms+'мс</span><span class="qs">'+esc(q.state)+'</span></div></div>';
      }).join('') : '<p class="muted">нет активных &gt;100мс</p>');
    }).catch(function(e){ morph(c, '<p class="sqlerr">'+esc(e.message)+'</p>'); });
  }
  function analyzeCustom(){ var q = document.getElementById('custom-query').value.trim(); if(q) analyze(q); }
  function planNodeHTML(n, depth){
    if(!n) return '';
    var pad = depth * 18;
    var t = n['Node Type'] || '?';
    var rel = n['Relation Name'] ? ' <span class="muted">'+esc(n['Relation Name'])+'</span>' : '';
    var extra = [];
    if(n['Actual Rows'] != null) extra.push('rows='+n['Actual Rows']+(n['Plan Rows']!=null ? '/'+n['Plan Rows'] : ''));
    if(n['Actual Total Time'] != null) extra.push('time='+n['Actual Total Time']+'ms');
    if(n['Actual Startup Time'] != null) extra.push('start='+n['Actual Startup Time']+'ms');
    if(n['Filter']) extra.push('filter='+esc(String(n['Filter'])));
    if(n['Sort Method']) extra.push(String(n['Sort Method']));
    if(n['Join Type']) extra.push('join='+String(n['Join Type']));
    if(n['Group Key']) extra.push('group='+esc(String(n['Group Key'])));
    var h = '<div class="plan-node" style="padding-left:'+pad+'px">'
      + '<span class="pn-type">'+esc(t)+'</span>'+rel
      + (extra.length ? ' <span class="muted">'+extra.join(' · ')+'</span>' : '')
      + '</div>';
    (n['Plans'] || []).forEach(function(ch){ h += planNodeHTML(ch, depth+1); });
    return h;
  }
  function analyze(query){
    document.getElementById('analysis-panel').classList.remove('hidden');
    return postJSON('/api/analyze-query', {query:query}).then(function(j){
      if(!j.ok){ log('анализ: '+j.error, 'err'); return; }
      var d = j.data;
      lastPlan = d.plan;
      lastPlanQuery = query;
      var ab = document.getElementById('ai-explain-btn');
      if(ab) ab.disabled = !d.plan;
      // классификация: служебный / пользовательский
      var cl = d.classification || {};
      var cb = document.getElementById('an-classify');
      if(cb){
        var sysCls = cl.system ? 'role-replica' : 'role-master';
        morph(cb, '<div class="tune-box" style="margin-top:0">'
          + '<span class="badge '+sysCls+'">'+(cl.system ? 'служебный' : 'пользовательский')+'</span>'
          + ' <b>'+esc(cl.label || '')+'</b>'
          + '<div class="muted" style="font-size:.82em;margin-top:4px">'+esc(cl.explanation || '')+'</div></div>');
      }
      // сводка «что сделать»
      var sum = document.getElementById('an-summary');
      if(sum){
        var srows = d.recommendations_summary || [];
        morph(sum, srows.length
          ? srows.map(function(s){
              var col = s.sev==='high' ? 'var(--crit)' : (s.sev==='medium' ? 'var(--amber)' : 'var(--good)');
              return '<div class="ritem"><div class="rt" style="color:'+col+'">'+esc(s.text)+'</div></div>';
            }).join('')
          : (cl.system
              ? '<p class="muted">Служебный запрос — оптимизация не требуется.</p>'
              : '<p class="muted">Явных рекомендаций нет — запрос выглядит оптимальным.</p>'));
      }
      morph(document.getElementById('plan-tree'), d.plan_error
        ? '<div class="sqlerr">'+esc(d.plan_error)+'</div>'
        : (d.plan
            ? planNodeHTML(((Array.isArray(d.plan) ? d.plan[0] : d.plan) || {}).Plan || {}, 0)
            : '<p class="muted">план недоступен</p>'));
      morph(document.getElementById('plan-issues'), (d.plan_issues||[]).length ? d.plan_issues.map(function(it){
        return '<div class="ritem '+it.severity+'"><div class="rt">'+esc(it.type)+'</div><div class="rm">'+esc(it.message)+'</div>'+(it.suggestion?'<div class="rs">↳ '+esc(it.suggestion)+'</div>':'')+'</div>';
      }).join('') : '<p class="muted">проблем плана нет ✓</p>');
      morph(document.getElementById('index-recs'), (d.index_recommendations||[]).length ? d.index_recommendations.map(function(r){
        return '<div class="ritem"><b>'+esc(r.table)+'.'+esc(r.columns.join(','))+'</b><div class="rm">'+esc(r.reason||'')+'</div><div class="sql">'+esc(r.sql)+'</div></div>';
      }).join('') : '<p class="muted">рекомендаций нет ✓</p>');
      morph(document.getElementById('rewrite-recs'), (d.rewrite_suggestions||[]).length ? d.rewrite_suggestions.map(function(s){
        return '<div class="ritem"><div class="rt">'+esc(s.problem)+'</div><div class="rs">↳ '+esc(s.suggestion)+'</div></div>';
      }).join('') : '<p class="muted">запрос оптимален ✓</p>');
    }).catch(function(e){ log('анализ: '+e.message, 'err'); });
  }

  function loadLocks(user){
    var tb = document.getElementById('locks-body'), meta = document.getElementById('locks-meta');
    uiLoadingRow(tb, 9);
    loadDeadlocks();
    return getJSON('/api/dba/locks', !user).then(function(j){
      if(!j.ok){ morph(tb, '<tr><td colspan="9" class="sqlerr">'+esc(j.error)+'</td></tr>'); return; }
      var m = j.meta || {};
      if(meta) meta.textContent = 'проверено '+(m.checked||0)+'/'+(m.total||0)+' нод';
      renderTable('locks-body', (j.data||[]).filter(function(l){ return !l.error; }), [
        {render:function(r){ return esc(r.node); }},
        {class:'mono', render:function(r){ return r.pid; }},
        {render:function(r){ return esc(r.usename); }},
        {render:function(r){ return esc(r.obj); }},
        {render:function(r){ return esc(r.mode); }},
        {render:function(r){ return esc(r.state); }},
        {class:'mono', render:function(r){ return esc((r.query||'').slice(0,60)); }},
        {render:function(r){ return r.granted?'да':'ждёт'; }},
        {render:function(r){ return '<button class="btn sm gate-admin" data-action="kill" data-node="'+esc(r.node)+'" data-pid="'+r.pid+'">kill</button>'; }}
      ], 'нет блокировок ✓');
    }).catch(function(e){ morph(tb, '<tr><td colspan="9" class="sqlerr">'+esc(e.message)+'</td></tr>'); });
  }
  function loadDeadlocks(){
    return getJSON('/api/dba/summary', true).then(function(j){
      if(!j || !j.ok) return;
      renderTable('deadlocks-body', j.data.deadlocks || [], [
        {render:function(r){ return esc(r.datname); }},
        {class:'mono lag-crit', render:function(r){ return r.deadlocks; }},
        {render:function(r){ return r.stats_reset ? esc(String(r.stats_reset).slice(0,19)) : '—'; }}
      ], 'Дедлоков не было');
    }).catch(function(){});
  }
  function kill(node, pid){
    confirmBox('kill PID '+pid+' на '+node+'?', '', function(){
      postJSON('/api/dba/kill', {node:node, pid:pid}).then(function(j){
        if(j.ok){ log('PID '+pid+' убит ✓', 'ok'); setTimeout(function(){ loadLocks(true); }, 800); }
        else log('kill: '+j.error, 'err');
      }).catch(function(e){ log('kill: '+e.message, 'err'); });
    });
  }

  function loadDbaSummary(){
    return getJSON('/api/dba/summary', true).then(function(j){
      if(!j.ok){ log('dba: '+j.error, 'err'); return; }
      var d = j.data, ad = document.getElementById('dba-alerts');
      morph(ad, (d.alerts && d.alerts.length) ? d.alerts.map(function(a){
        return '<div class="alert '+a.type+'">'+esc(a.msg)+'</div>';
      }).join('') : '<div class="alert info">✓ проблем не обнаружено</div>');
      [['m-idle',d.idle,true],['m-long2',d.long,true],['m-bloat',d.bloat,false],['m-unused',d.unused,false],['m-vacuum',d.vacuum,false]].forEach(function(x){
        var el = document.getElementById(x[0]); if(!el) return;
        el.textContent = (x[1]||[]).length;
        el.className = 'mv '+((x[2] && (x[1]||[]).length>0)?'bad':((x[1]||[]).length>0?'warn':'good'));
      });
    }).catch(function(e){ log('dba: '+e.message, 'err'); });
  }
  function loadBloat(){
    return getJSON('/api/dba/bloat').then(function(j){
      if(!j.ok) return;
      renderTable('bloat-t', j.data.tables || [], [
        {render:function(r){ return esc(r.schemaname); }},
        {render:function(r){ return esc(r.t); }},
        {class:'mono', render:function(r){ return r.n_dead_tup||0; }},
        {class:'mono', render:function(r){ return r.n_live_tup||0; }},
        {render:function(r){ return r.pct+'%'; }},
        {class:'mono', render:function(r){ return fb(r.sz); }},
        {render:function(r){ return '<button class="btn sm gate-dba" data-action="vacuum" data-s="'+esc(r.schemaname)+'" data-t="'+esc(r.t)+'">vacuum</button>'; }}
      ], '—');
    }).catch(function(e){ log('bloat: '+e.message, 'err'); });
  }
  function loadVacuum(){
    return getJSON('/api/dba/summary').then(function(j){
      if(!j.ok) return;
      renderTable('vacuum-t', j.data.vacuum || [], [
        {render:function(r){ return esc(r.schemaname); }},
        {render:function(r){ return esc(r.relname); }},
        {class:'mono', render:function(r){ return r.n_dead_tup||0; }},
        {class:'mono', render:function(r){ return r.last_autovacuum||'никогда'; }}
      ], '—');
    }).catch(function(e){ log('vacuum: '+e.message, 'err'); });
  }
  function loadUnused(){
    return getJSON('/api/dba/summary').then(function(j){
      if(!j.ok) return;
      renderTable('unused-t', j.data.unused || [], [
        {render:function(r){ return esc(r.schemaname); }},
        {render:function(r){ return esc(r.t); }},
        {render:function(r){ return esc(r.ix); }},
        {class:'mono', render:function(r){ return fb(r.sz); }},
        {render:function(r){ return '<button class="btn sm gate-admin" data-action="drop" data-s="'+esc(r.schemaname)+'" data-ix="'+esc(r.ix)+'">drop</button>'; }}
      ], '—');
    }).catch(function(e){ log('unused: '+e.message, 'err'); });
  }
  function loadConn(){
    var barBox = document.getElementById('conn-bar'); uiLoading(barBox);
    return getJSON('/api/dba/connections').then(function(j){
      if(!j.ok){ morph(barBox, '<p class="sqlerr">'+esc(j.error)+'</p>'); return; }
      var d = j.data, nodes = d.by_node || [], rows=[];
      nodes.forEach(function(nd){ if(!nd.ok) return; nd.by_user.forEach(function(r){ rows.push({node:nd.name, usename:r.usename, app:r.application_name, state:r.state, cnt:r.cnt}); }); });
      morph(barBox, '<div class="muted">Всего: <b>'+d.total_all+'</b> (лимит '+d.max_connections+')</div> <button class="btn danger gate-admin" onclick="killUserSessions()">Завершить сессии</button>'
        +'<table class="mono-t" style="margin-top:10px"><thead><tr><th>нода</th><th>user</th><th>application</th><th>state</th><th>count</th></tr></thead><tbody>'
        +(rows.length ? rows.map(function(r){ return '<tr><td>'+esc(r.node)+'</td><td>'+esc(r.usename||'—')+'</td><td>'+esc(r.app||'—')+'</td><td>'+esc(r.state)+'</td><td class="mono">'+r.cnt+'</td></tr>'; }).join('') : '<tr><td colspan="5" class="muted center">нет подключений</td></tr>')
        +'</tbody></table>');
    }).catch(function(e){ morph(barBox, '<p class="sqlerr">'+esc(e.message)+'</p>'); });
  }
  function killUserSessions(){
    confirmBox('Завершить пользовательские сессии на всех нодах?', '', function(){
      postJSON('/api/dba/kill-user-sessions', {}).then(function(j){
        if(j.ok) log('завершено: '+j.data.total, 'ok'); else log('kill: '+j.error, 'err');
      }).catch(function(e){ log('kill: '+e.message, 'err'); });
    });
  }
  function vacuum(s, t){
    confirmBox('VACUUM ANALYZE '+s+'.'+t+'?', '', function(){
      postJSON('/api/dba/vacuum', {table:t, schema:s, full:false, analyze:true}).then(function(j){
        if(j.ok) log(j.data.message, 'ok'); else log('vacuum: '+j.error, 'err');
      }).catch(function(e){ log('vacuum: '+e.message, 'err'); });
    });
  }
  function dropIdx(s, ix){
    confirmBox('drop index '+s+'.'+ix+'?', '', function(){
      postJSON('/api/dba/drop-index', {index:ix, schema:s}).then(function(j){
        if(j.ok) log(j.data.message, 'ok'); else log('drop: '+j.error, 'err');
      }).catch(function(e){ log('drop: '+e.message, 'err'); });
    });
  }
  var __tuneCatalog = null;
  function tuneInput(){
    return {
      ram: parseFloat(document.getElementById('tune-ram').value) || 0,
      cpu: parseInt(document.getElementById('tune-cpu').value, 10) || 0,
      maxconn: parseInt(document.getElementById('tune-maxconn').value, 10) || 0,
      disk: (document.getElementById('tune-disk') ? document.getElementById('tune-disk').value : 'ssd') || 'ssd'
    };
  }
  function computeTune(){
    var inp = tuneInput();
    if(!inp.ram || !inp.cpu || !inp.maxconn){ log('заполните RAM, CPU и max_connections', 'warn'); return; }
    var url = '/api/tuning/catalog?ram_gb='+encodeURIComponent(inp.ram)+'&cpu='+inp.cpu
      +'&maxconn='+inp.maxconn+'&disk='+encodeURIComponent(inp.disk);
    getJSON(url).then(function(j){
      if(!j || !j.ok){ log('tuning: '+((j && j.error) || 'ошибка'), 'err'); return; }
      __tuneCatalog = j.data;
      renderTune();
    }).catch(function(e){ log('tuning: '+e.message, 'err'); });
  }
  function loadTune(){
    // текущие значения параметров с сервера (для столбца «сейчас»)
    getJSON('/api/dba/config-recs', true).then(function(j){
      if(j && j.ok) __tuneCurrent = j.data.current || {};
    }).catch(function(){});
    computeTune();
  }
  function renderTune(){
    var cat = __tuneCatalog; if(!cat) return;
    var out = document.getElementById('tune-out'); if(!out) return;
    var cur = __tuneCurrent || {};
    var cats = cat.categories || [], params = cat.params || [];
    var html = '';
    cats.forEach(function(cg){
      var plist = params.filter(function(p){ return p.category === cg.key; });
      if(!plist.length) return;
      html += '<h4 style="margin:16px 0 6px;color:var(--cyan)">'+(cg.icon ? '<span style="margin-right:6px">'+cg.icon+'</span>' : '')+esc(cg.name)+'</h4>';
      html += '<table class="tune-table"><thead><tr><th>параметр</th><th>сейчас</th><th>рекомендую</th><th>пояснение</th></tr></thead><tbody>';
      plist.forEach(function(p){
        var cv = cur[p.name];
        var cvTxt = (cv == null || cv === '') ? '—' : String(cv);
        var flag = (cv != null && cv !== '' && String(cv) !== String(p.recommended)) ? 'flag-medium' : '';
        var restart = p.restart ? ' <span class="muted" title="требует перезапуск">⟳</span>' : '';
        var dsk = p.disk_sensitive ? ' <span class="muted" title="зависит от типа диска">*</span>' : '';
        html += '<tr class="'+flag+'">'
          + '<td><b class="mono">'+esc(p.name)+'</b>'+restart+dsk+'</td>'
          + '<td class="cur mono">'+esc(cvTxt)+'</td>'
          + '<td class="rec mono">'+esc(p.recommended)+'</td>'
          + '<td class="why">'+esc(p.description)+(p.note ? '<br><span class="muted">'+esc(p.note)+'</span>' : '')+'</td>'
          + '</tr>';
      });
      html += '</tbody></table>';
    });
    morph(out, html || '<p class="muted">нет данных</p>');
    log('рекомендации пересчитаны', 'info');
  }

  function fillSqlNodes(){
    var sel = document.getElementById('sql-node'); if(!sel) return;
    var m = (window.clusterData && window.clusterData.members) || [];
    syncSelect(sel, m.map(function(x){ return {value:x.name, label:x.name+' · '+(isLeaderRole(x.role)?'лидер (запись)':'реплика (только чтение)')}; }));
    fillSqlDbs();
  }
  function fillSqlDbs(){
    var sel = document.getElementById('sql-db'); if(!sel) return;
    getJSON('/api/databases').then(function(j){
      if(!j || !j.ok) return;
      var dbs = j.data || [];
      if(!dbs.length) return;
      syncSelect(sel, dbs);
    }).catch(function(){});
  }
  function execSQL(){
    var sql = document.getElementById('sql-query').value.trim(), node = document.getElementById('sql-node').value;
    var db = document.getElementById('sql-db').value;
    if(!sql || !node) return;
    var rd = document.getElementById('sql-results'), rc = document.getElementById('sql-content'), meta = document.getElementById('sql-meta');
    rd.classList.remove('hidden'); meta.textContent = 'выполнение…'; morph(rc, '');
    postJSON('/api/querylab/exec', {sql:sql, node:node, db:db}).then(function(j){
      if(!j.ok){ morph(rc, '<div class="sqlerr">'+esc(j.error)+'</div>'); return; }
      var d = j.data; meta.textContent = node+' · '+db+' · '+d.execution_time+'мс';
      if(d.columns){
        morph(rc, '<table class="mono-t"><thead><tr>'+d.columns.map(function(c){ return '<th>'+esc(c)+'</th>'; }).join('')+'</tr></thead><tbody>'
          +d.rows.map(function(r){ return '<tr>'+d.columns.map(function(c){ var v=r[c]; return '<td>'+(v==null?'NULL':esc(String(v)))+'</td>'; }).join('')+'</tr>'; }).join('')+'</tbody></table>');
      } else morph(rc, '<div class="sqlok">'+esc(d.message||'выполнено')+'</div>');
    }).catch(function(e){ morph(rc, '<div class="sqlerr">'+esc(e.message)+'</div>'); });
  }
  function clearSQL(){ document.getElementById('sql-results').classList.add('hidden'); document.getElementById('sql-query').value = ''; }
  (function(){
    var sq = document.getElementById('sql-query');
    if(sq) sq.addEventListener('keydown', function(e){
      if((e.ctrlKey || e.metaKey) && e.key === 'Enter'){ e.preventDefault(); execSQL(); }
    });
  })();

  function opResult(name, okText){
    return function(j){
      if(j && j.ok) log(okText, 'ok'); else log(name+': '+((j && j.error) || (j && j.data && j.data.message) || 'ошибка'), 'err');
      setTimeout(function(){ if(window.fetchCluster) fetchCluster(true); }, 1500);
    };
  }
  function opFail(name){ return function(e){ log(name+': '+e.message, 'err'); }; }
  function clusterName(){ var h = document.getElementById('cluster-name'); return h ? h.textContent.trim() : ''; }
  function doSwitchover(){
    var l = document.getElementById('sw-leader').value, c = document.getElementById('sw-candidate').value;
    if(!c){ log('Нет подходящей реплики-кандидата', 'warn'); return; }
    if(l === c){ log('Кандидат совпадает с текущим лидером', 'warn'); return; }
    confirmBox('Плановое переключение: '+l+' → '+c,
      'Лидер корректно передаст роль реплике '+c+'. Соединения на запись будут разорваны на несколько секунд, приложения должны переподключиться.',
      function(){
        log('Switchover '+l+' → '+c+'…', 'info');
        postJSON('/api/switchover', {leader:l, candidate:c}).then(opResult('Switchover', 'Switchover выполнен: новый лидер '+c)).catch(opFail('Switchover'));
      }, {danger:true, okText:'Переключить'});
  }
  function doFailover(){
    var c = document.getElementById('fo-candidate').value;
    if(!c){ log('Нет подходящей реплики-кандидата', 'warn'); return; }
    confirmBox('Аварийный failover на '+c,
      'Реплика '+c+' будет принудительно назначена лидером без участия текущего лидера. Транзакции, не дошедшие до реплики, будут потеряны. Используйте, только если лидер недоступен.',
      function(){
        log('Failover на '+c+'…', 'info');
        postJSON('/api/failover', {candidate:c}).then(opResult('Failover', 'Failover выполнен: новый лидер '+c)).catch(opFail('Failover'));
      }, {danger:true, require:c, okText:'Выполнить failover'});
  }
  function doReinit(n){
    confirmBox('Пересоздать реплику '+n+'?', 'Каталог данных на '+n+' будет удалён и скопирован с лидера заново (pg_basebackup). На больших базах это долго и нагружает сеть.',
      function(){ postJSON('/api/reinitialize', {node:n, force:true}).then(opResult('Reinit '+n, 'Пересоздание '+n+' запущено')).catch(opFail('Reinit')); },
      {danger:true, require:n, okText:'Пересоздать'});
  }
  function doRestart(n){
    confirmBox('Перезапустить PostgreSQL на '+n+'?', 'Соединения с узлом будут разорваны. Если это лидер, запись будет недоступна до окончания перезапуска.',
      function(){ postJSON('/api/ops/restart', {node:n}).then(opResult('Restart '+n, 'Перезапуск '+n+' выполнен')).catch(opFail('Restart')); },
      {danger:true, okText:'Перезапустить'});
  }
  function doReload(n){ postJSON('/api/ops/reload', {node:n}).then(opResult('Reload '+n, 'Конфигурация '+n+' перечитана')).catch(opFail('Reload')); }
  function restartCluster(){
    var cn = clusterName();
    confirmBox('Перезапустить весь кластер '+cn+'?', 'Сначала по очереди перезапускаются реплики, затем лидер. Во время перезапуска лидера запись недоступна.',
      function(){
        log('Перезапуск кластера…', 'info');
        postJSON('/api/ops/restart-cluster', {}).then(function(j){
          if(j && j.ok) log(j.data.summary || 'Кластер перезапущен', 'ok'); else log('Перезапуск кластера: '+((j && j.error)||'ошибка'), 'err');
        }).catch(opFail('Перезапуск кластера'));
      }, {danger:true, require:cn, okText:'Перезапустить кластер'});
  }
  function loadHistory(){
    getJSON('/api/ops/history').then(function(j){
      var box = document.getElementById('history-box');
      if(!j.ok){ box.textContent = j.error || 'ошибка'; return; }
      var rows = j.data || [];
      if(!rows.length){ box.textContent = 'история пуста'; return; }
      if(!Array.isArray(rows) || rows.length === 0 || typeof rows[0] !== 'object'){ box.textContent = JSON.stringify(j.data, null, 2); return; }
      var first = rows[0];
      var cols = Object.keys(first).filter(function(k){ return k !== 'reason'; });
      var html = '<table class="mono-t"><thead><tr><th>'+cols.map(function(k){ return esc(k); }).join('</th><th>')+'</th><th>причина</th></tr></thead><tbody>';
      rows.forEach(function(r){
        html += '<tr>'+cols.map(function(k){ return '<td class="mono">'+esc(r[k]==null?'—':String(r[k]))+'</td>'; }).join('')+'<td class="muted">'+esc(r.reason||'—')+'</td></tr>';
      });
      html += '</tbody></table>';
      morph(box, html);
    });
  }
  function fillDelayNodes(){
    var sel = document.getElementById('delay-node'); if(!sel) return;
    var hs = document.getElementById('delay-hours');
    if(hs && hs.options.length===0){ for(var h=1;h<=24;h++){ hs.add(new Option(h+' ч', h)); } }
    var m = (window.clusterData && window.clusterData.members) || [];
    syncSelect(sel, m.filter(function(x){ return !isLeaderRole(x.role); }).map(function(x){ return x.name; }));
    refreshDelayStatus();
  }
  function refreshDelayStatus(){
    var sel = document.getElementById('delay-node'), out = document.getElementById('delay-current');
    if(!sel || !out) return;
    var node = sel.value;
    getJSON('/api/querylab/show?param=recovery_min_apply_delay&node='+encodeURIComponent(node), true).then(function(j){
      var val='0', on=false;
      if(j && j.ok && j.data){ val = (j.data.value==null?'0':String(j.data.value)); on=val && val!=='0'; }
      var ag = (window.__agents||{})[node]; var agOn = ag && ag.online;
      morph(out, (on ? 'отставание: <b style="color:var(--amber)">'+esc(val)+'</b>' : 'отставание: —')
        + ' · агент: '+(agOn ? '<b style="color:var(--good)">онлайн</b>' : '<b style="color:var(--crit)">офлайн</b>'));
    }).catch(function(){ morph(out, 'отставание: —'); });
  }
  function applyDelay(){ setReplicaDelay(document.getElementById('delay-node').value, false, parseInt(document.getElementById('delay-hours').value,10)||1); }
  function clearDelay(){ setReplicaDelay(document.getElementById('delay-node').value, true, 1); }
  function setReplicaDelay(node, reset, hours){
    if(!node) return;
    confirmBox((reset?'Снять задержку на ':'Включить задержку '+hours+'ч на ')+node+'?', '', function(){
      postJSON('/api/dba/delay-replica', {node:node, reset:reset, hours:hours}).then(function(j){
        if(j.ok){ log((reset?'задержка снята':'задержка '+hours+'ч включена')+' на '+node, 'ok'); setTimeout(refreshDelayStatus, 3000); }
        else log('delay: '+j.error, 'err');
      }).catch(function(e){ log('delay: '+e.message, 'err'); });
    });
  }

  function fillBackupNodes(){
    var sel = document.getElementById('bk-node'); if(!sel) return;
    var m = (window.clusterData && window.clusterData.members) || [];
    syncSelect(sel, m.map(function(x){ return {value:x.name, label:x.name+(isLeaderRole(x.role)?' (лидер)':' (реплика)')}; }));
    renderBackups(); bkSchedLoad();
  }
  function bkArchState(){
    var sel = document.getElementById('bk-node'), el = document.getElementById('bk-arch-state');
    if(!sel || !el) return;
    getJSON('/api/querylab/show?param=archive_mode&node='+encodeURIComponent(sel.value), true).then(function(j){
      var v = (j && j.ok && j.data) ? String(j.data.value||'off') : 'off';
      morph(el, 'WAL‑архив: '+(v==='on' ? '<b style="color:var(--good)">on</b>' : '<b>off</b>'));
    }).catch(function(){ morph(el, 'WAL‑архив: —'); });
  }
  function renderBackups(){
    var tb = document.getElementById('bk-body'); if(!tb) return;
    var sel = document.getElementById('bk-node'), node = sel ? sel.value : '';
    var ag = (window.__agents||{})[node] || {}, list = ag.backups || [];
    bkArchState();
    var st = document.getElementById('bk-status'), last = ag.last;
    if(st) morph(st, last ? 'последняя: '+(last.ok?'✓':'✗')+' '+esc((last.out||'').slice(0,200)) : 'агент: '+(ag.online?'<b style="color:var(--good)">онлайн</b>':'<b style="color:var(--crit)">офлайн</b>'));
    if(!list.length){ morph(tb, '<tr><td colspan="5" class="muted center">нет бэкапов</td></tr>'); return; }
    list = list.slice().sort(function(a, b){ return String(b.time||b.start_time||'').localeCompare(String(a.time||a.start_time||'')); });
    morph(tb, list.map(function(b){
      var name=b.backup_name||b.name||'—', fin=(b.finish_time||b.time||b.start_time) ? String(b.finish_time||b.time||b.start_time).replace('T',' ').slice(0,19) : '—';
      var typ = b.is_full == null ? '—' : (b.is_full ? 'полный' : 'дельта');
      var lsn = b.finish_lsn != null ? lsnToString(b.finish_lsn) : (b.end_lsn || '');
      var size = b.compressed_size != null ? ' <span class="muted" title="сжатый / исходный объём">'+fb(b.compressed_size)+' / '+fb(b.uncompressed_size)+'</span>' : '';
      return '<tr data-key="bk-'+esc(name)+'"><td class="mono">'+esc(name)+size+'</td><td>'+typ+'</td><td class="mono">'+esc(fin)+'</td><td class="mono">'+esc(lsn)+'</td>'
        +'<td><button class="btn sm gate-dba" data-action="bk-verify" data-name="'+esc(name)+'">проверить</button> '
        +'<button class="btn sm gate-dba" data-action="bk-clone" data-name="'+esc(name)+'">клонировать</button> '
        +'<button class="btn sm gate-dba" data-action="bk-restore" data-name="'+esc(name)+'">restore</button> '
        +'<button class="btn sm gate-dba" data-action="bk-pitr" data-name="'+esc(name)+'">PITR</button> '
        +'<button class="btn sm gate-admin" data-action="bk-delete" data-name="'+esc(name)+'">удалить</button></td></tr>';
    }).join(''));
  }
  function bkCreate(full){
    var sel = document.getElementById('bk-node'), node = sel ? sel.value : '';
    confirmBox((full?'Полный':'Дельта')+' бэкап на '+node+'?', '', function(){
      postJSON('/api/backups', {node:node, action: full?'backup_full':'backup_delta'}).then(function(j){
        if(j.ok){ log('бэкап запущен на '+node+'…','info'); setTimeout(function(){ pollAgentNow(renderBackups); }, 5000); }
        else log('backups: '+j.error,'err');
      }).catch(function(e){ log('backups: '+e.message,'err'); });
    });
  }
  function bkRotate(){
    var sel = document.getElementById('bk-node'), node = sel ? sel.value : '';
    var keep = parseInt(document.getElementById('bk-keep').value, 10) || 3;
    confirmBox('Ротация: оставить '+keep+' на '+node+'?', '', function(){
      postJSON('/api/backups', {node:node, action:'rotate', keep:keep}).then(function(j){
        if(j.ok){ log('ротация запущена','info'); setTimeout(function(){ pollAgentNow(renderBackups); }, 4000); }
        else log('backups: '+j.error,'err');
      }).catch(function(e){ log('backups: '+e.message,'err'); });
    });
  }
  function bkArch(on){
    var sel = document.getElementById('bk-node'), node = sel ? sel.value : '';
    confirmBox((on?'Включить':'Выключить')+' WAL‑архив на '+node+'?', on?'Нода перезапустится.':'', function(){
      postJSON('/api/backups', {node:node, action:'set_wal_archiving', on:on}).then(function(j){
        if(!j.ok){ log('backups: '+j.error,'err'); return; }
        log('WAL‑архив: конфиг обновлён, перезапускаю…','info');
        return postJSON('/api/ops/restart', {node:node}).then(function(r){
          if(r && r.ok) log('рестарт ✓','ok'); else log('рестарт: '+((r && r.error)||'ошибка'),'err');
        });
      }).then(function(){ setTimeout(function(){ pollAgentNow(renderBackups); }, 8000); }).catch(function(e){ log('backups: '+e.message,'err'); });
    });
  }
  function bkAction(name, action, prompt){
    var sel = document.getElementById('bk-node'), node = sel ? sel.value : '';
    var t = prompt ? window.prompt(prompt) : null;
    if(prompt && !t) return;
    var params = {node:node, action:action, name:name}; if(t) params.target_time = t;
    postJSON('/api/backups', params).then(function(j){
      if(j.ok){ log(action+' запущен…','info'); setTimeout(function(){ pollAgentNow(renderBackups); }, 5000); }
      else log('backups: '+j.error,'err');
    }).catch(function(e){ log('backups: '+e.message,'err'); });
  }
  function bkClone(name){
    var sel = document.getElementById('bk-node'), node = sel ? sel.value : '';
    var label = window.prompt('Метка клона (например "test-clone"):', 'clone');
    if(!label) return;
    confirmBox('Клонировать бэкап '+name+' на '+node+'?', 'Восстановление в отдельный каталог + подготовка конфига (порт +100, 127.0.0.1) и стартового скрипта. Живой кластер не трогается, автозапуска нет.', function(){
      postJSON('/api/backups', {node:node, action:'clone_restore', name:name, label:label}).then(function(j){
        if(!j || !j.ok){ log('clone: '+((j && j.error)||'ошибка'),'err'); return; }
        log('клон запущен…','info');
        pollCmdResult(node, j.data.cmd_id, function(r, err){
          if(r && r.ok){
            var d = r.out && r.out.data ? r.out.data : {};
            log('клон готов: '+d.path+' · порт 5532 (127.0.0.1) · старт: '+(d.start_script || '—'),'ok');
          } else log('clone: '+(err || (r && r.out) || 'ошибка'),'err');
          setTimeout(renderBackups, 2000);
        }, 220);
      }).catch(function(e){ log('clone: '+e.message,'err'); });
    });
  }
  function cloneInspect(){
    var path = (document.getElementById('clone-path') || {}).value || '';
    path = path.trim();
    if(!path){ log('укажи путь к клону','err'); return; }
    var sel = document.getElementById('bk-node'), node = sel ? sel.value : '';
    var st = document.getElementById('dump-status');
    if(st) morph(st, '<span class="muted">инспекция '+esc(path)+'…</span>');
    postJSON('/api/backups', {node:node, action:'clone_inspect', path:path}).then(function(j){
      if(!j || !j.ok){ if(st) morph(st, '<span class="sqlerr">'+esc((j&&j.error)||'ошибка')+'</span>'); return; }
      pollCmdResult(node, j.data.cmd_id, function(r, err){
        if(r && r.ok){
          var d = r.out && r.out.data ? r.out.data : {};
          var size = fb(d.size||0);
          var lines = ['инспекция: '+d.path, 'верс. PG: '+(d.version||'—'), 'размер: '+size,
            'статус каталога: '+(d.controldata && d.controldata.status || '—'),
            'pg_control верс.: '+(d.controldata && d.controldata.version || '—'),
            'recovery-sync: '+(d.recovery_signal?'да':'нет'), 'порт: '+(d.port||'—')];
          if(st) morph(st, '<b>Проверка:</b> '+lines.join(' · '));
          log('инспекция клона: '+lines.join(', '),'ok');
        } else {
          if(st) morph(st, '<span class="sqlerr">'+esc(err || (r && r.out) || 'ошибка')+'</span>');
        }
      }, 30);
    }).catch(function(e){ if(st) morph(st, '<span class="sqlerr">'+esc(e.message)+'</span>'); });
  }
  function bkSchedLoad(){
    getJSON('/api/backup-schedule', true).then(function(j){
      if(!j || !j.ok) return;
      var d = j.data;
      var on = document.getElementById('bk-sched-on'); if(on) on.checked = !!d.enabled;
      var tm = document.getElementById('bk-sched-time'); if(tm) tm.value = d.time || '02:00';
      var ty = document.getElementById('bk-sched-type'); if(ty) ty.value = d.type || 'delta';
      var ns = document.getElementById('bk-sched-node');
      if(ns){
        var m = (window.clusterData && window.clusterData.members) || [];
        syncSelect(ns, [{value:'', label:'авто (любая реплика)'}].concat(m.map(function(x){ return x.name; })), {keep:false, value:d.node || ''});
      }
    }).catch(function(){});
  }
  function bkSchedSave(){
    var b = {
      enabled: document.getElementById('bk-sched-on').checked,
      time: document.getElementById('bk-sched-time').value,
      node: document.getElementById('bk-sched-node').value,
      type: document.getElementById('bk-sched-type').value,
      keep: parseInt(document.getElementById('bk-keep').value, 10) || 3
    };
    postJSON('/api/backup-schedule', b).then(function(j){
      if(j.ok) log('расписание сохранено', 'ok'); else log('schedule: '+j.error, 'err');
    }).catch(function(e){ log('schedule: '+e.message, 'err'); });
  }
  function dumpSchedLoad(){
    Promise.all([getJSON('/api/dump-schedule', true)]).then(function(a){
      var j = a[0]; if(!j || !j.ok) return;
      var d = j.data;
      var on = document.getElementById('ds-sched-on'); if(on) on.checked = !!d.enabled;
      var tm = document.getElementById('ds-sched-time'); if(tm) tm.value = d.time || '03:00';
      var rt = document.getElementById('ds-sched-rotate'); if(rt) rt.value = d.rotate_days || 7;
      var ns = document.getElementById('ds-sched-node');
      if(ns){
        var m = (window.clusterData && window.clusterData.members) || [];
        syncSelect(ns, [{value:'', label:'авто (любая реплика)'}].concat(m.map(function(x){ return x.name; })), {keep:false, value:d.node || ''});
      }
    }).catch(function(){});
  }
  function dumpSchedSave(){
    var b = {
      enabled: document.getElementById('ds-sched-on').checked,
      time: document.getElementById('ds-sched-time').value,
      node: document.getElementById('ds-sched-node').value,
      rotate_days: parseInt(document.getElementById('ds-sched-rotate').value, 10) || 0
    };
    // остальные параметры берём из текущей формы дампа (режим, потоки, сжатие)
    var dm = document.getElementById('dump-mode'); if(dm) b.mode = dm.value;
    var dj = document.getElementById('dump-jobs'); if(dj) b.jobs = parseInt(dj.value, 10) || 2;
    var dc = document.getElementById('dump-compress'); if(dc) b.compress = parseInt(dc.value, 10) || 3;
    postJSON('/api/dump-schedule', b).then(function(j){
      if(j && j.ok) log('расписание дампов сохранено', 'ok'); else log('dumps sched: '+((j && j.error)||'ошибка'), 'err');
    }).catch(function(e){ log('dumps sched: '+e.message, 'err'); });
  }

  /* ========== ЛОГИЧЕСКИЕ ДАМПЫ (pg_dump) ========== */
  function dumpNode(){ var s = document.getElementById('bk-node'); return s ? s.value : ''; }
  function dumpDb(){ var s = document.getElementById('dump-db'); return s ? s.value : ''; }
  function pollCmdResult(node, cid, cb, attempts){
    var n = attempts || 8;
    if(!cid){ cb(null, 'агент не вернул идентификатор команды'); return; }
    (function tick(i){
      if(i > n){ cb(null, 'timeout — результат можно посмотреть по кнопке «Обновить»'); return; }
      getJSON('/api/ops/cmd-result?node='+encodeURIComponent(node)+'&id='+encodeURIComponent(cid), true).then(function(j){
        var r = j && j.ok ? j.data : null;
        if(r){ cb(r, null); }
        else setTimeout(function(){ tick(i+1); }, 4000);
      }).catch(function(e){ cb(null, e.message); });
    })(0);
  }
  function loadDumps(){
    var node = dumpNode();
    dumpSchedLoad();
    var nn = document.getElementById('dump-node-name'); if(nn) nn.textContent = node || '—';
    var db = document.getElementById('dump-db');
    if(db && !db.dataset.filled){
      db.dataset.filled = '1';
      getJSON('/api/databases', true).then(function(j){
        if(j && j.ok) syncSelect(db, j.data || []);
      }).catch(function(){});
    }
    if(!node){ var tb = document.getElementById('dump-body'); if(tb) morph(tb, '<tr><td colspan="4" class="muted center">нет ноды</td></tr>'); return; }
    var st = document.getElementById('dump-status'); if(st) st.textContent = 'запрашиваю список…';
    postJSON('/api/dumps', {node: node, action: 'list'}).then(function(j){
      if(!j || !j.ok){ if(st) morph(st, '<span class="sqlerr">'+esc(j && j.error || 'ошибка')+'</span>'); return; }
      pollCmdResult(node, j.data.cmd_id, function(r, err){
        renderDumps(r, err, st);
      }, 4);
    }).catch(function(e){ if(st) morph(st, '<span class="sqlerr">'+esc(e.message)+'</span>'); });
  }
  function renderDumps(r, err, st){
    var tb = document.getElementById('dump-body'); if(!tb) return;
    if(!st) st = document.getElementById('dump-status');
    if(err || !r || !r.ok){
      if(st) morph(st, '<span class="sqlerr">'+esc(err || (r && r.out) || 'ошибка агента')+'</span>');
      return;
    }
    if(st){
      var last = document.getElementById('bk-node');
      var ag = (window.__agents||{})[dumpNode()] || {};
      morph(st, 'агент: '+(ag.online?'<b style="color:var(--good)">онлайн</b>':'<b style="color:var(--crit)">офлайн</b>')
        +(ag.last ? ' · последняя команда: '+(ag.last.ok?'✓':'✗')+' '+esc(String(ag.last.out||'').slice(0,140)) : ''));
    }
    var list = Array.isArray(r.out) ? r.out : ((r.out||{}).data || []);
    if(!list.length){ morph(tb, '<tr><td colspan="4" class="muted center">дампов нет</td></tr>'); return; }
    morph(tb, list.map(function(d){
      var dt = d.mtime ? new Date(d.mtime*1000).toLocaleString() : '—';
      return '<tr><td class="mono">'+esc(d.name)+(d.is_dir?' <span class="muted">(dir)</span>':'')+'</td><td>'+fb(d.size||0)+'</td><td class="mono">'+esc(dt)+'</td>'
        +'<td><button class="btn sm gate-admin" data-action="dump-delete" data-name="'+esc(d.name)+'">удалить</button></td></tr>';
    }).join(''));
  }
  function dumpCreate(){
    var node = dumpNode(), db = dumpDb();
    if(!node || !db){ log('выбери ноду и БД','err'); return; }
    var mode = document.getElementById('dump-mode').value;
    var jobs = parseInt(document.getElementById('dump-jobs').value, 10) || 2;
    var compress = parseInt(document.getElementById('dump-compress').value, 10) || 3;
    confirmBox('Дамп базы '+db+' на '+node+'?', 'режим '+mode+' · -j '+jobs+' · сжатие '+compress, function(){
      var st = document.getElementById('dump-status'); if(st) st.textContent = 'дамп запущен, это может занять минуты…';
      postJSON('/api/dumps', {node:node, action:'create', db:db, mode:mode, jobs:jobs, compress:compress}).then(function(j){
        if(!j || !j.ok){ if(st) morph(st, '<span class="sqlerr">'+esc(j && j.error || 'ошибка')+'</span>'); return; }
        log('дамп '+db+' запущен на '+node+'…','info');
        pollCmdResult(node, j.data.cmd_id, function(r, err){
          if(r && r.ok) log('дамп готов: '+JSON.stringify(r.out.data || r.out).slice(0,200),'ok');
          else log('дамп: '+(err || (r && r.out) || 'ошибка'),'err');
          loadDumps();
        }, 220); // до ~15 мин на большие базы
      }).catch(function(e){ if(st) morph(st, '<span class="sqlerr">'+esc(e.message)+'</span>'); });
    });
  }
  function dumpRotate(){
    var node = dumpNode();
    var days = parseInt(document.getElementById('dump-ttl').value, 10) || 7;
    confirmBox('Ротация дампов на '+node+': удалить старше '+days+' дней?', '', function(){
      postJSON('/api/dumps', {node:node, action:'rotate', days:days}).then(function(j){
        if(!j || !j.ok){ log('rotate: '+((j && j.error)||'ошибка'),'err'); return; }
        pollCmdResult(node, j.data.cmd_id, function(r, err){
          log((err || (r && !r.ok)) ? 'rotate: '+(err || r.out) : (r && r.out || 'ok'), (err || (r && !r.ok)) ? 'err' : 'ok');
          loadDumps();
        }, 6);
      }).catch(function(e){ log('rotate: '+e.message,'err'); });
    });
  }
  function dumpDelete(name){
    var node = dumpNode();
    confirmBox('Удалить дамп '+name+'?', '', function(){
      postJSON('/api/dumps', {node:node, action:'delete', name:name}).then(function(j){
        if(!j || !j.ok){ log('delete: '+((j && j.error)||'ошибка'),'err'); return; }
        pollCmdResult(node, j.data.cmd_id, function(){ loadDumps(); }, 4);
      }).catch(function(e){ log('delete: '+e.message,'err'); });
    });
  }
  function nfsMount(){
    var node = dumpNode(), share = document.getElementById('nfs-share').value.trim(), mp = document.getElementById('nfs-mp').value;
    if(!share){ log('укажи share host:/path','err'); return; }
    confirmBox('Смонтировать '+share+' → '+mp+' на '+node+'?', '', function(){
      postJSON('/api/dumps', {node:node, action:'nfs_mount', share:share, mountpoint:mp}).then(function(j){
        if(!j || !j.ok){ log('nfs: '+((j && j.error)||'ошибка'),'err'); return; }
        pollCmdResult(node, j.data.cmd_id, function(r, err){ log((err || (r && !r.ok)) ? 'nfs: '+(err || r.out) : 'nfs ✓ '+r.out, (err || (r && !r.ok)) ? 'err' : 'ok'); }, 8);
      }).catch(function(e){ log('nfs: '+e.message,'err'); });
    });
  }
  function nfsUmount(){
    var node = dumpNode(), mp = document.getElementById('nfs-mp').value;
    confirmBox('Размонтировать '+mp+' на '+node+'?', '', function(){
      postJSON('/api/dumps', {node:node, action:'nfs_umount', mountpoint:mp}).then(function(j){
        if(!j || !j.ok){ log('nfs: '+((j && j.error)||'ошибка'),'err'); return; }
        pollCmdResult(node, j.data.cmd_id, function(r, err){ log((err || (r && !r.ok)) ? 'nfs: '+(err || r.out) : 'umount ✓', (err || (r && !r.ok)) ? 'err' : 'ok'); }, 8);
      }).catch(function(e){ log('nfs: '+e.message,'err'); });
    });
  }
  function sendDumpReport(){
    var node = dumpNode(), to = document.getElementById('report-to').value.trim();
    if(!to){ log('укажи e-mail','err'); return; }
    var rows = document.querySelectorAll('#dump-body tr');
    var body = 'Отчёт по логическим дампам ('+node+')\n';
    for(var i=0;i<rows.length;i++) body += rows[i].innerText.replace(/\n/g, ' | ')+'\n';
    postJSON('/api/dumps', {node:node, action:'report', to:to, subject:'отчёт дампов '+node, body:body}).then(function(j){
      if(j && j.ok){ log('отчёт отправлен через агента','ok'); }
      else log('report: '+((j && j.error)||'ошибка'),'err');
    }).catch(function(e){ log('report: '+e.message,'err'); });
  }

  /* ========== ОБСЛУЖИВАНИЕ: repack + планировщик + шаблоны ========== */
  function bloatLeaderDb(){
    // БД лидера из кластера (первая база, по которой снят снимок) — fallback 'postgres'
    if(window.clusterData && window.clusterData.db) return window.clusterData.db;
    var ag = window.__agents || {};
    for(var k in ag){ if(ag[k].metrics && ag[k].metrics.dbs && ag[k].metrics.dbs.length) return ag[k].metrics.dbs[0]; }
    return 'postgres';
  }
  function loadMaint(){
    return getJSON('/api/dba/bloat', true).then(function(j){
      var tb = document.getElementById('maint-bloat-t'); if(!tb) return;
      if(!j || !j.ok){ morph(tb, '<tr><td colspan="7" class="sqlerr">'+esc(j && j.error || 'ошибка')+'</td></tr>'); return; }
      var rows = (j.data && j.data.tables) || [];
      if(!rows.length){ morph(tb, '<tr><td colspan="7" class="muted center">раздутых таблиц не найдено</td></tr>'); return; }
      var db = bloatLeaderDb();
      morph(tb, rows.map(function(r){
        var pct = r.pct || 0;
        return '<tr><td>'+esc(r.schemaname||'')+'</td><td>'+esc(r.t||'')+'</td><td>'+(r.n_dead_tup||0)+'</td><td>'+(r.n_live_tup||0)+'</td>'
          +'<td><b style="color:'+(pct>40?'var(--crit)':pct>20?'var(--amber)':'var(--good)')+'">'+pct+'</b></td><td>'+fb(r.sz||0)+'</td>'
          +'<td><button class="btn sm gate-admin" data-action="maint-repack" data-db="'+esc(db)+'" data-schema="'+esc(r.schemaname||'public')+'" data-table="'+esc(r.t||'')+'">repack</button></td></tr>';
      }).join(''));
    }).catch(function(e){ log('maint: '+e.message,'err'); });
  }
  function maintRepack(db, schema, table){
    confirmBox('Дефрагментация '+schema+'.'+table+' на лидере?', 'pg_repack (онлайн) или VACUUM FULL, если pg_repack не установлен. Займёт время.', function(){
      postJSON('/api/ops/repack', {db:db, schema:schema, table:table, method:'auto'}).then(function(j){
        if(!j || !j.ok){ log('repack: '+((j && j.error)||'ошибка'),'err'); return; }
        log('repack '+schema+'.'+table+' запущен на '+j.data.node+'…','info');
        pollCmdResult(j.data.node, j.data.cmd_id, function(r, err){
          log((err || (r && !r.ok)) ? 'repack: '+(err || r.out) : '✓ '+r.out, (err || (r && !r.ok)) ? 'err' : 'ok');
          setTimeout(loadMaint, 2000);
        }, 220);
      }).catch(function(e){ log('repack: '+e.message,'err'); });
    });
  }
  function maintSchedLoad(){
    getJSON('/api/maint-schedule', true).then(function(j){
      if(!j || !j.ok) return;
      var d = j.data;
      var on = document.getElementById('maint-on'); if(on) on.checked = !!d.enabled;
      var tm = document.getElementById('maint-time'); if(tm) tm.value = d.time || '03:30';
      var pct = document.getElementById('maint-pct'); if(pct) pct.value = d.min_bloat_pct || 40;
      var n = document.getElementById('maint-n'); if(n) n.value = d.max_tables || 5;
      var md = document.getElementById('maint-method'); if(md) md.value = d.method || 'auto';
      var days = d.days || [];
      var sel = document.getElementById('maint-days');
      if(sel){ for(var i=0;i<sel.options.length;i++) sel.options[i].selected = days.indexOf(parseInt(sel.options[i].value,10)) >= 0; }
    }).catch(function(){});
  }
  function maintSchedSave(){
    var sel = document.getElementById('maint-days');
    var days = [];
    if(sel){ for(var i=0;i<sel.options.length;i++) if(sel.options[i].selected) days.push(parseInt(sel.options[i].value,10)); }
    var b = {
      enabled: document.getElementById('maint-on').checked,
      time: document.getElementById('maint-time').value,
      days: days,
      min_bloat_pct: parseInt(document.getElementById('maint-pct').value, 10) || 40,
      max_tables: parseInt(document.getElementById('maint-n').value, 10) || 5,
      method: document.getElementById('maint-method').value
    };
    postJSON('/api/maint-schedule', b).then(function(j){
      if(j && j.ok) log('планировщик сохранён', 'ok'); else log('maint-schedule: '+((j && j.error)||'ошибка'),'err');
    }).catch(function(e){ log('maint-schedule: '+e.message,'err'); });
  }
  function loadTemplates(){
    Promise.all([getJSON('/api/templates', true), getJSON('/api/templates/history', true)]).then(function(arr){
      var box = document.getElementById('tpl-out'); if(box) morph(box, '');
      if(arr[0] && arr[0].ok) renderTemplates(arr[0].data || {});
      if(arr[1] && arr[1].ok) renderTplHistory(arr[1].data || []);
    }).catch(function(e){ log('templates: '+e.message,'err'); });
  }
  function renderTemplates(d){
    var box = document.getElementById('tpl-out'); if(!box) return;
    var cur = d.current || {};
    var curParams = (cur.postgresql && cur.postgresql.parameters) || {};
    var tpls = d.templates || [];
    morph(box, tpls.map(function(t){
      var vs = Object.keys(t.params).map(function(k){
        var cv = curParams[k];
        return '<div class="mono" style="font-size:.84em">· '+esc(k)+' = <b>'+esc(t.params[k])+'</b>'+(cv == null ? ' <span class="muted">(сейчас не задан)</span>' : (String(cv) !== String(t.params[k]) ? ' <span class="muted">(сейчас '+esc(cv)+')</span>' : ' <span style="color:var(--good)">(совпадает)</span>'))+'</div>';
      }).join('');
      return '<div class="ocard"><h3>'+esc(t.name)+'</h3><p class="muted">'+esc(t.description)+'</p>'
        +'<div style="max-height:180px;overflow:auto;margin:6px 0">'+vs+'</div>'
        +'<button class="btn primary gate-admin" data-action="tpl-apply" data-key="'+esc(t.key)+'">применить</button></div>';
    }).join('') || '<p class="muted">нет шаблонов</p>');
  }
  function tplApply(key){
    confirmBox('Применить шаблон на лидере?', 'Параметры будут переданы в Patroni /config (с рестартом при необходимости).', function(){
      postJSON('/api/templates/apply', {name:key, confirm:true}).then(function(j){
        if(j && j.ok){ log('шаблон применён: '+j.data.params.length+' параметров','ok'); loadTemplates(); }
        else log('templates: '+((j && j.error)||'ошибка'),'err');
      }).catch(function(e){ log('templates: '+e.message,'err'); });
    });
  }
  function renderTplHistory(rows){
    var tb = document.getElementById('tpl-history'); if(!tb) return;
    morph(tb, rows.length ? rows.map(function(r){
      return '<tr><td class="mono">'+esc(r.time||'')+'</td><td>'+esc(r.user||'?')+'</td><td>'+esc(r.template||'')+'</td><td class="mono">'+esc(r.node||'')+'</td>'
        +'<td class="mono" style="max-width:340px;overflow-wrap:anywhere">'+esc(Object.keys(r.params||{}).slice(0,8).join(', ')+(Object.keys(r.params||{}).length>8?'…':''))+'</td></tr>';
    }).join('') : '<tr><td colspan="5" class="muted center">история пуста</td></tr>');
  }

  /* ========== УВЕДОМЛЕНИЯ (email / telegram) ========== */
  function setMulti(selId, vals){
    var sel = document.getElementById(selId); if(!sel) return;
    for(var i=0;i<sel.options.length;i++) sel.options[i].selected = (vals||[]).indexOf(sel.options[i].value) >= 0;
  }
  function getMulti(selId){
    var sel = document.getElementById(selId), out = [];
    if(!sel) return out;
    for(var i=0;i<sel.options.length;i++) if(sel.options[i].selected) out.push(sel.options[i].value);
    return out;
  }
  function loadNotifyCfg(){
    Promise.all([getJSON('/api/notify/config', true), getJSON('/api/notify/log', true)]).then(function(arr){
      if(arr[0] && arr[0].ok) fillNotifyCfg(arr[0].data || {});
      if(arr[1] && arr[1].ok) renderNotifyLog(arr[1].data || []);
    }).catch(function(e){ log('notify: '+e.message,'err'); });
  }
  function fillNotifyCfg(d){
    var e = d.email || {}, t = d.telegram || {}, r = d.routes || {};
    var on = document.getElementById('nt-email-on'); if(on) on.checked = !!e.enabled;
    var to = document.getElementById('nt-email-to'); if(to) to.value = e.to || '';
    var mt = document.getElementById('nt-email-method'); if(mt) mt.value = e.method || 'msmtp';
    var hp = document.getElementById('nt-smtp-host'); if(hp) hp.value = e.smtp_host || '';
    var pp = document.getElementById('nt-smtp-port'); if(pp) pp.value = e.smtp_port || 587;
    var pu = document.getElementById('nt-smtp-user'); if(pu) pu.value = e.smtp_user || '';
    var pw = document.getElementById('nt-smtp-pass'); if(pw) pw.value = e.smtp_pass || '';
    var fm = document.getElementById('nt-from'); if(fm) fm.value = e.from || '';
    var tg = document.getElementById('nt-tg-on'); if(tg) tg.checked = !!t.enabled;
    var tk = document.getElementById('nt-tg-token'); if(tk) tk.value = t.bot_token || '';
    var ch = document.getElementById('nt-tg-chat'); if(ch) ch.value = t.chat_id || '';
    setMulti('nt-route-critical', r.critical || ['email','telegram']);
    setMulti('nt-route-warning', r.warning || ['email']);
    var di = d.diag || {};
    var db = document.getElementById('nt-diag');
    if(db){
      var parts = [];
      if(di.installed) parts.push('<span style="color:var(--good)">✓ msmtp: установлен</span>'+(di.path?' <span class="mono" style="font-size:.8em">('+esc(di.path)+')</span>':''));
      else parts.push('<span class="sqlerr">✗ msmtp: не найден — установите msmtp или проверьте PATH сервиса</span>');
      if(di.config) parts.push('<span style="color:var(--good)">✓ конфиг: <span class="mono">'+esc(di.config)+'</span></span>');
      else parts.push('<span class="sqlerr">✗ конфиг msmtp отсутствует — создайте /etc/msmtprc</span>');
      morph(db, parts.join(' &nbsp; '));
    }
  }
  function notifySave(){
    var b = {
      email: {
        enabled: document.getElementById('nt-email-on').checked,
        to: document.getElementById('nt-email-to').value.trim(),
        method: document.getElementById('nt-email-method').value,
        smtp_host: document.getElementById('nt-smtp-host').value.trim(),
        smtp_port: parseInt(document.getElementById('nt-smtp-port').value,10) || 587,
        smtp_user: document.getElementById('nt-smtp-user').value.trim(),
        smtp_pass: document.getElementById('nt-smtp-pass').value,
        from: document.getElementById('nt-from').value.trim()
      },
      telegram: {
        enabled: document.getElementById('nt-tg-on').checked,
        bot_token: document.getElementById('nt-tg-token').value.trim(),
        chat_id: document.getElementById('nt-tg-chat').value.trim()
      },
      routes: {
        critical: getMulti('nt-route-critical'),
        warning: getMulti('nt-route-warning'),
        info: []
      }
    };
    postJSON('/api/notify/config', b).then(function(j){
      if(j && j.ok) log('настройки уведомлений сохранены','ok'); else log('notify: '+((j && j.error)||'ошибка'),'err');
    }).catch(function(e){ log('notify: '+e.message,'err'); });
  }
  function notifyTest(){
    postJSON('/api/notify/test', {severity:'warning'}).then(function(j){
      if(!j || !j.ok){ log('notify test: '+((j && j.error)||'ошибка'),'err'); return; }
      (j.data||[]).forEach(function(r){
        log('notify '+r.channel+': '+(r.ok?'✓ отправлено':'✗ '+(r.error||'')), r.ok ? 'ok' : 'err');
      });
      loadNotifyCfg();
    }).catch(function(e){ log('notify test: '+e.message,'err'); });
  }
  function renderNotifyLog(rows){
    var tb = document.getElementById('notify-log-body'); if(!tb) return;
    morph(tb, rows.length ? rows.map(function(r){
      return '<tr><td class="mono">'+esc(r.time||'')+'</td><td>'+esc(r.channel||'')+'</td><td>'+esc(r.severity||'')+'</td>'
        +'<td class="mono" style="max-width:300px;overflow-wrap:anywhere">'+esc(r.subject||'')+'</td>'
        +'<td>'+(r.ok?'<b style="color:var(--good)">ok</b>':'<span class="sqlerr">'+esc(r.error||'fail')+'</span>')+'</td></tr>';
    }).join('') : '<tr><td colspan="5" class="muted center">журнал пуст</td></tr>');
  }

  /* ========== DCS (etcd / consul) + синхронные реплики ========== */
  var dcsLoaded = false;
  function loadDcsHealth(){
    if(!dcsLoaded){
      getJSON('/api/dcs/config', true).then(function(j){
        if(j && j.ok){
          var e = document.getElementById('dcs-etcd'); if(e) e.value = (j.data.etcd||[]).join(', ');
          var c = document.getElementById('dcs-consul'); if(c) c.value = (j.data.consul||[]).join(', ');
        }
      }).catch(function(){});
      dcsLoaded = true;
    }
    getJSON('/api/dcs/health', true).then(function(j){
      var box = document.getElementById('dcs-out'); if(!box) return;
      if(!j || !j.ok){ morph(box, '<span class="sqlerr">'+esc(j && j.error || 'ошибка')+'</span>'); return; }
      var d = j.data || {};
      var html = '';
      if(!d.configured) html += '<p class="muted">DCS не настроен — укажи адреса etcd/consul выше и сохрани (admin).</p>';
      (d.etcd||[]).forEach(function(e){
        html += '<div>etcd <b>'+esc(e.url)+'</b>: '+(e.ok ? '<b style="color:var(--good)">healthy</b>' : '<b style="color:var(--crit)">недоступен</b>')
          +' · members: '+(e.members||[]).map(function(m){ return esc(m.name)+(e.leader && m.id===e.leader ? ' (лидер)' : ''); }).join(', ') || '—'
          +(e.error ? ' <span class="sqlerr">'+esc(e.error)+'</span>' : '')+'</div>';
      });
      (d.consul||[]).forEach(function(e){
        html += '<div>consul <b>'+esc(e.url)+'</b>: '+(e.ok ? '<b style="color:var(--good)">ok</b>' : '<b style="color:var(--crit)">недоступен</b>')
          +' · leader: '+esc(e.leader || '—')
          +' · members: '+(e.members||[]).map(function(m){ return esc(m.name)+'('+esc(m.status)+')'; }).join(', ') || '—'
          +(e.error ? ' <span class="sqlerr">'+esc(e.error)+'</span>' : '')+'</div>';
      });
      var s = d.sync || {};
      if(s.ok){
        html += '<div style="margin-top:6px">synchronous_standby_names: <b class="mono">'+esc(s.synchronous_standby_names || '(пусто)')+'</b></div>';
        var reps = s.replicas || [];
        html += reps.length ? '<table class="mono-t" style="margin-top:6px"><thead><tr><th>app</th><th>state</th><th>sync_state</th><th>лаг</th></tr></thead><tbody>'
          + reps.map(function(r){
            return '<tr><td>'+esc(r.app||'')+'</td><td>'+esc(r.state||'')+'</td><td><b style="color:'+(r.sync_state==='sync'||r.sync_state==='quorum'?'var(--good)':r.sync_state==='potential'?'var(--amber)':'var(--dim)')+'">'+esc(r.sync_state||'')+'</b></td><td>'+fb(r.lag_bytes||0)+'</td></tr>';
          }).join('') + '</tbody></table>'
          : '<p class="muted">реплик в pg_stat_replication нет</p>';
      } else {
        html += '<div style="margin-top:6px"><span class="sqlerr">'+esc(s.error||'нет данных о синхронных репликах')+'</span></div>';
      }
      morph(box, html);
    }).catch(function(e){ var box = document.getElementById('dcs-out'); if(box) morph(box, '<span class="sqlerr">'+esc(e.message)+'</span>'); });
  }
  function dcsCfgSave(){
    var e = document.getElementById('dcs-etcd').value.split(',').map(function(s){ return s.trim(); }).filter(Boolean);
    var c = document.getElementById('dcs-consul').value.split(',').map(function(s){ return s.trim(); }).filter(Boolean);
    postJSON('/api/dcs/config', {etcd:e, consul:c}).then(function(j){
      if(j && j.ok){ log('DCS-конфиг сохранён','ok'); loadDcsHealth(); }
      else log('dcs: '+((j && j.error)||'ошибка'),'err');
    }).catch(function(e){ log('dcs: '+e.message,'err'); });
  }
  function dcsDiscover(){
    var box = document.getElementById('dcs-out');
    if(box) morph(box, '<span class="muted">обнаружение… опрос агентов</span>');
    postJSON('/api/dcs/discover', {}, true).then(function(j){
      if(!j || !j.ok){ if(box) morph(box, '<span class="sqlerr">'+esc((j && j.error)||'ошибка')+'</span>'); return; }
      var d = j.data || {}, f = d.found || {};
      var html = '<b style="color:var(--good)">найдено:</b> ' +
        (f.etcd||[]).map(function(u){ return 'etcd '+esc(u); }).join(', ')
        + ((f.etcd||[]).length ? '' : 'etcd — ')
        + ' · ' + (f.consul||[]).map(function(u){ return 'consul '+esc(u); }).join(', ')
        + ((f.consul||[]).length ? '' : 'consul —');
      var pn = d.per_node || {}; var keys = Object.keys(pn);
      if(keys.length){
        html += '<div class="muted" style="margin-top:4px">по нодам:'+keys.map(function(k){
          return ' <b>'+esc(k)+'</b> ('+esc(pn[k].path||'')+')';
        }).join(',')+'</div>';
      } else html += '<div class="muted" style="margin-top:4px">онлайн-агенты не ответили — проверь, что агент запущен (root) и в patroni.yml есть DCS.</div>';
      var e = document.getElementById('dcs-etcd'), c = document.getElementById('dcs-consul');
      if(e && (f.etcd||[]).length) e.value = (f.etcd||[]).join(', ');
      if(c && (f.consul||[]).length) c.value = (f.consul||[]).join(', ');
      html += ' <button class="btn sm gate-admin" onclick="dcsCfgSave()">применить найденное</button>';
      if(box) morph(box, html);
    }).catch(function(e){ if(box) morph(box, '<span class="sqlerr">'+esc(e.message)+'</span>'); });
  }

  /* ========== ГЛОБАЛЬНЫЙ КОНФИГ PATRONI ЧЕРЕЗ DCS ========== */
  function dcsCfgLoad(){
    var box = document.getElementById('dcs-cfg-msg');
    if(box) morph(box, '<span class="muted">чтение из DCS…</span>');
    getJSON('/api/dcs/patroni-config', true).then(function(j){
      if(!j || !j.ok){
        if(box) morph(box, '<span class="sqlerr">'+esc((j && (j.error || 'не найден ключ config в DCS'))||'ошибка')+'</span>');
        return;
      }
      var d = j.data || {};
      var ta = document.getElementById('dcs-cfg-text');
      if(ta) ta.value = d.config || '';
      var key = document.getElementById('dcs-cfg-key');
      if(key) key.value = d.key || '';
      var meta = document.getElementById('dcs-cfg-meta');
      if(meta) morph(meta, '· <b>'+esc(d.backend||'')+'</b> · '+esc(d.url||'')+' · revision/ключ: '+esc(d.key||''));
      if(box) morph(box, '<span style="color:var(--good)">✓ прочитано из DCS ('+esc(d.backend||'')+')</span>');
    }).catch(function(e){ if(box) morph(box, '<span class="sqlerr">'+esc(e.message)+'</span>'); });
  }

  function dcsCfgApply(){
    var ta = document.getElementById('dcs-cfg-text');
    var key = document.getElementById('dcs-cfg-key');
    if(!ta || !ta.value.trim()){ log('конфиг пуст — нечего записывать','err'); return; }
    var keyVal = (key && key.value || '').trim();
    var box = document.getElementById('dcs-cfg-msg');
    if(box) morph(box, '<span class="muted">запись в DCS…</span>');
    postJSON('/api/dcs/patroni-config', {config: ta.value, key: keyVal || undefined}).then(function(j){
      if(j && j.ok){
        if(box) morph(box, '<span style="color:var(--good)">✓ записано в DCS: '+esc(j.data.key||'')+' ('+esc(j.data.backend||'')+')</span>');
        log('глобальный конфиг Patroni записан в DCS','ok');
      } else {
        if(box) morph(box, '<span class="sqlerr">'+esc((j && j.error)||'ошибка записи')+'</span>');
        log('запись конфига в DCS: '+((j && j.error)||'ошибка'),'err');
      }
    }).catch(function(e){ if(box) morph(box, '<span class="sqlerr">'+esc(e.message)+'</span>'); });
  }

  /* ========== AI-АССИСТЕНТ ========== */
  function aiAdd(msg, reply, topic){
    var box = document.getElementById('ai-chat'); if(!box) return;
    var div = document.createElement('div');
    div.className = 'ai-msg '+(msg ? 'user' : '');
    div.innerHTML = (msg ? '<b>Вы:</b> '+esc(msg)+'<br>' : '')
      + (topic ? '<span class="muted" style="font-size:.78em">['+esc(topic)+']</span> ' : '')
      + '<span class="ai-reply">'+esc(reply||'')+'</span>';
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
    return div;
  }
  function aiExplainPlan(){
    if(!lastPlan){ log('сначала выполни EXPLAIN', 'warn'); return; }
    var q = lastPlanQuery || '';
    var reply = 'разбираю план…';
    var box = document.getElementById('ai-chat');
    // переключаемся на AI-экран и показываем прогресс
    var chip = document.querySelector('.chip[data-screen="ai"]');
    if(chip) chip.click();
    aiAdd('объясни план: '+(q.slice(0,60)||'(EXPLAIN)'), '…');
    postJSON('/api/assistant/explain', {query:q, plan:lastPlan}).then(function(j){
      var last = document.querySelector('#ai-chat .ai-msg:last-child');
      if(last) last.remove();
      if(j && j.ok) aiAdd('объясни план: '+(q.slice(0,60)||'(EXPLAIN)'), j.text || j.data && j.data.text || '', 'explain');
      else aiAdd('', 'ошибка: '+((j && j.error)||'нет ответа'));
    }).catch(function(e){ var last = document.querySelector('#ai-chat .ai-msg:last-child'); if(last) last.remove(); aiAdd('', 'ошибка: '+e.message); });
  }
  function aiSend(){
    var inp = document.getElementById('ai-input'), msg = (inp ? inp.value : '').trim();
    if(!msg) return;
    if(inp) inp.value = '';
    aiAdd(msg, '…');
    postJSON('/api/assistant/chat', {message: msg}).then(function(j){
      var last = document.querySelector('#ai-chat .ai-msg:last-child');
      if(last) last.remove();
      if(j && j.ok) aiAdd(msg, j.data.reply, j.data.topic);
      else aiAdd(msg, 'ошибка: '+((j && j.error)||'нет ответа'));
    }).catch(function(e){ var last = document.querySelector('#ai-chat .ai-msg:last-child'); if(last) last.remove(); aiAdd(msg, 'ошибка: '+e.message); });
  }
  function aiAsk(q){ var inp = document.getElementById('ai-input'); if(inp) inp.value = q; aiSend(); }
  function aiAdvice(){
    var pending = aiAdd('', 'собираю советы по тюнингу…');
    postJSON('/api/assistant/config-advice', {}).then(function(j){
      if(pending) pending.remove();
      if(j && j.ok) aiAdd('', (j.data.tips||[]).join('\n'), 'config');
      else aiAdd('', 'ошибка: '+((j && j.error)||'нет ответа'));
    }).catch(function(e){ aiAdd('', 'ошибка: '+e.message); });
  }
  function aiDiagnose(){
    var pending = aiAdd('', 'провожу диагностику…');
    postJSON('/api/assistant/diagnose', {}).then(function(j){
      if(pending) pending.remove();
      if(!j || !j.ok){ aiAdd('', 'ошибка: '+((j && j.error)||'нет ответа')); return; }
      var d = j.data || {}, issues = d.issues || [];
      var lines = ['**Диагностика кластера**', '', '**'+d.summary+'**', ''];
      var sevTxt = {critical:'[критично]', warning:'[важно]', info:'[инфо]'};
      issues.forEach(function(i){
        lines.push((sevTxt[i.sev]||'•')+' '+i.what+(i.detail ? ' — '+i.detail : ''));
      });
      if(!issues.length) lines.push('— активных проблем нет.');
      aiAdd('', lines.join('\n'), 'diagnose');
    }).catch(function(e){ aiAdd('', 'ошибка: '+e.message); });
  }
  function loadAiHistory(){
    getJSON('/api/assistant/history', true).then(function(j){
      if(!j || !j.ok) return;
      var d = j.data.dialog || [];
      var box = document.getElementById('ai-chat'); if(!box) return;
      box.innerHTML = '';
      if(!d.length){ box.innerHTML = '<div class="ai-msg instruct">История пуста — задай вопрос или выбери тему слева.</div>'; return; }
      d.forEach(function(r){
        aiAdd(r.msg || '', r.reply || '', r.topic);
      });
    }).catch(function(e){ log('ai: '+e.message,'err'); });
  }
  function clearAi(){
    postJSON('/api/assistant/clear', {}).then(function(){
      var box = document.getElementById('ai-chat');
      if(box) box.innerHTML = '<div class="ai-msg instruct">Диалог очищен.</div>';
    }).catch(function(){});
  }

  function loadAudit(){
    return getJSON('/api/audit', true).then(function(j){
      var tb = document.getElementById('audit-body'); if(!tb) return;
      if(!j || !j.ok){ morph(tb, '<tr><td colspan="4" class="sqlerr">'+esc(j && j.error || 'ошибка')+'</td></tr>'); return; }
      var rows = (j.data || []).slice().reverse();
      morph(tb, rows.length ? rows.map(function(r){
        return '<tr><td class="mono">'+esc(r.time||'')+'</td><td>'+esc(r.user||'')+'</td><td>'+esc(r.method||'')+'</td><td class="mono">'+esc(r.path||'')+'</td></tr>';
      }).join('') : '<tr><td colspan="4" class="muted center">журнал пуст</td></tr>');
    }).catch(function(e){ log('audit: '+e.message,'err'); });
  }

  /* ========== ALERTS ========== */
  function loadAlerts(){
    return Promise.all([getJSON('/api/alerts/rules', true), getJSON('/api/alerts/history', true)]).then(function(arr){
      if(arr[0] && arr[0].ok){ __alertRules = arr[0].data || []; renderAlertRules(__alertRules); }
      if(arr[1] && arr[1].ok) renderAlertHistory(arr[1].data || []);
    }).catch(function(e){ log('alerts: '+e.message,'err'); });
  }
  function renderAlertRules(rows){
    var tb = document.getElementById('alerts-rules-body'); if(!tb) return;
    morph(tb, (rows||[]).length ? rows.map(function(r){
      return '<tr><td>'+esc(r.name)+'</td><td class="mono">'+esc(r.metric)+'</td>'
        +'<td class="mono">'+esc(r.operator)+' <input type="number" class="al-thr" data-id="'+esc(r.id)+'" value="'+esc(r.threshold)+'" style="width:80px;margin:0;padding:3px 6px"></td>'
        +'<td>'+esc(r.severity)+'</td>'
        +'<td><button class="btn sm gate-dba" data-action="alert-toggle" data-id="'+esc(r.id)+'">'+(r.enabled?'выкл':'вкл')+'</button></td>'
        +'<td><button class="btn sm gate-dba" data-action="alert-delete" data-id="'+esc(r.id)+'">удалить</button></td></tr>';
    }).join('') : '<tr><td colspan="6" class="muted center">нет правил</td></tr>');
    /* обработчик порога навешивается один раз (делегирование): morph сохраняет
       те же input-элементы, повторная привязка дублировала бы запросы */
    if(!tb.__thrBound){
      tb.__thrBound = true;
      tb.addEventListener('change', function(e){
        var inp = e.target.closest && e.target.closest('.al-thr'); if(!inp) return;
        var v = parseFloat(inp.value);
        if(isNaN(v)){ log('Порог должен быть числом', 'warn'); return; }
        postJSON('/api/alerts/rules', {id:inp.getAttribute('data-id'), threshold:v}).then(function(j){
          if(j && j.ok){ log('Порог обновлён', 'ok'); loadAlerts(); } else log('Алерты: '+((j && j.error)||'ошибка'), 'err');
        });
      });
    }
  }
  function renderAlertHistory(rows){
    var tb = document.getElementById('alerts-history-body'); if(!tb) return;
    var r = (rows||[]).slice().reverse();
    morph(tb, r.length ? r.map(function(x){
      var c = x.type==='firing' ? 'var(--crit)' : (x.type==='resolved' ? 'var(--good)' : 'var(--cyan)');
      return '<tr><td class="mono">'+esc(x.time||'')+'</td><td>'+esc(x.rule||'')+'</td><td style="color:'+c+'">'+esc(x.type||'')+'</td><td>'+esc(x.severity||'')+'</td><td class="mono">'+(x.value==null?'—':x.value)+'</td></tr>';
    }).join('') : '<tr><td colspan="5" class="muted center">история пуста</td></tr>');
  }
  function alertRuleSave(){
    var b = {
      name: document.getElementById('al-name').value.trim() || 'правило',
      metric: document.getElementById('al-metric').value,
      operator: document.getElementById('al-op').value,
      threshold: parseFloat(document.getElementById('al-threshold').value) || 0,
      severity: document.getElementById('al-severity').value,
      enabled: true
    };
    postJSON('/api/alerts/rules', b).then(function(j){
      if(j.ok){ log('правило добавлено ✓','ok'); loadAlerts(); } else log('alerts: '+j.error,'err');
    }).catch(function(e){ log('alerts: '+e.message,'err'); });
  }
  function alertToggle(id){
    var r = __alertRules.find(function(x){ return x.id === id; });
    if(!r) return;
    postJSON('/api/alerts/rules', {id:id, enabled: !r.enabled}).then(function(j){ if(j.ok) loadAlerts(); });
  }
  function alertDelete(id){
    confirmBox('Удалить правило?', '', function(){
      postJSON('/api/alerts/rules/delete', {id:id}).then(function(j){ if(j.ok) loadAlerts(); });
    });
  }
  function updateAlertIndicator(){
    if(window.__inLogin) return;
    getJSON('/api/alerts/active', true).then(function(j){
      var ind = document.getElementById('alert-ind'); var cnt = document.getElementById('alert-count');
      if(!ind) return;
      var n = (j && j.ok) ? (j.data||[]).length : 0;
      if(cnt) cnt.textContent = n;
      ind.classList.toggle('hidden', n === 0);
    }).catch(function(){});
  }
  setInterval(updateAlertIndicator, 10000);

  /* ========== ADMIN ========== */
  function loadAdmin(){
    return Promise.all([
      getJSON('/api/admin/extensions', true),
      getJSON('/api/admin/roles', true),
      getJSON('/api/admin/tablespaces', true),
      getJSON('/api/admin/nodes', true)
    ]).then(function(arr){
      if(arr[0] && arr[0].ok) renderAdminExt(arr[0].data);
      if(arr[1] && arr[1].ok) renderAdminRoles(arr[1].data);
      if(arr[2] && arr[2].ok) renderAdminTs(arr[2].data);
      if(arr[3] && arr[3].ok){
        var nodes = arr[3].data || [];
        ['admin-diff-a','admin-diff-b','admin-pat-a','admin-pat-b'].forEach(function(id, i){
          var sel = document.getElementById(id); if(!sel) return;
          var idx = /-b$/.test(id) ? 1 : 0;
          syncSelect(sel, nodes, {value: nodes[idx]});
        });
      }
    }).catch(function(e){ log('admin: '+e.message,'err'); });
  }
  function renderAdminExt(d){
    var tb = document.getElementById('admin-ext-installed');
    if(tb) morph(tb, (d.installed||[]).map(function(r){
      return '<tr><td class="mono">'+esc(r.extname)+'</td><td>'+esc(r.extversion)+'</td><td>'+esc(r.schema)+'</td><td><button class="btn sm gate-admin" data-action="admin-ext-drop" data-name="'+esc(r.extname)+'">удалить</button></td></tr>';
    }).join('') || '<tr><td colspan="4" class="muted center">—</td></tr>');
    var ta = document.getElementById('admin-ext-available');
    if(ta) morph(ta, (d.available||[]).filter(function(r){ return !r.installed_version; }).map(function(r){
      return '<tr><td class="mono">'+esc(r.name)+'</td><td>'+esc(r.default_version)+'</td><td class="muted">'+esc((r.comment||'').slice(0,60))+'</td><td><button class="btn sm gate-admin" data-action="admin-ext-create" data-name="'+esc(r.name)+'">установить</button></td></tr>';
    }).join('') || '<tr><td colspan="4" class="muted center">—</td></tr>');
  }
  function renderAdminRoles(rows){
    var tb = document.getElementById('admin-roles-body'); if(!tb) return;
    morph(tb, (rows||[]).map(function(r){
      var flags = [];
      if(r.rolsuper) flags.push('super');
      if(r.rolcreatedb) flags.push('createdb');
      if(r.rolcreaterole) flags.push('createrole');
      if(r.rolreplication) flags.push('repl');
      if(!r.rolcanlogin) flags.push('nologin');
      return '<tr><td class="mono">'+esc(r.rolname)+'</td><td>'+flags.join(', ')+'</td><td>'+(r.rolconnlimit!=null?r.rolconnlimit:'—')+'</td><td class="muted">'+esc((r.member_of||[]).join(', ')||'—')+'</td><td><button class="btn sm gate-admin" data-action="admin-role-drop" data-name="'+esc(r.rolname)+'">удалить</button></td></tr>';
    }).join('') || '<tr><td colspan="5" class="muted center">—</td></tr>');
  }
  function renderAdminTs(rows){
    var tb = document.getElementById('admin-ts-body'); if(!tb) return;
    morph(tb, (rows||[]).map(function(r){
      return '<tr><td class="mono">'+esc(r.spcname)+'</td><td class="mono">'+esc(r.location||'')+'</td><td class="mono">'+fb(r.size)+'</td><td><button class="btn sm gate-admin" data-action="admin-ts-drop" data-name="'+esc(r.spcname)+'">удалить</button></td></tr>';
    }).join('') || '<tr><td colspan="4" class="muted center">—</td></tr>');
  }
  function adminExtCreate(name){
    confirmBox('Установить расширение '+name+'?', '', function(){
      postJSON('/api/admin/extensions', {action:'create', name:name}).then(function(j){
        if(j.ok){ log('расширение '+name+' установлено ✓','ok'); loadAdmin(); } else log('admin: '+j.error,'err');
      }).catch(function(e){ log('admin: '+e.message,'err'); });
    });
  }
  function adminExtDrop(name){
    confirmBox('Удалить расширение '+name+' (CASCADE)?', '', function(){
      postJSON('/api/admin/extensions', {action:'drop', name:name}).then(function(j){
        if(j.ok){ log('расширение '+name+' удалено ✓','ok'); loadAdmin(); } else log('admin: '+j.error,'err');
      }).catch(function(e){ log('admin: '+e.message,'err'); });
    });
  }
  function adminRoleCreate(){
    var name = document.getElementById('admin-role-name').value.trim();
    if(!name){ log('имя роли обязательно','warn'); return; }
    var b = {
      action:'create', name:name,
      password: document.getElementById('admin-role-pass').value,
      login: document.getElementById('admin-role-login').checked,
      superuser: document.getElementById('admin-role-super').checked,
      createdb: document.getElementById('admin-role-createdb').checked,
      createrole: document.getElementById('admin-role-createrole').checked,
      replication: document.getElementById('admin-role-repl').checked
    };
    confirmBox('Создать роль '+name+'?', '', function(){
      postJSON('/api/admin/roles', b).then(function(j){
        if(j.ok){ log('роль '+name+' создана ✓','ok'); loadAdmin(); } else log('admin: '+j.error,'err');
      }).catch(function(e){ log('admin: '+e.message,'err'); });
    });
  }
  function adminRoleDrop(name){
    confirmBox('Удалить роль '+name+'?', '', function(){
      postJSON('/api/admin/roles', {action:'drop', name:name}).then(function(j){
        if(j.ok){ log('роль '+name+' удалена ✓','ok'); loadAdmin(); } else log('admin: '+j.error,'err');
      }).catch(function(e){ log('admin: '+e.message,'err'); });
    });
  }
  function adminRoleGrant(){
    var member = document.getElementById('admin-grant-member').value.trim();
    var role = document.getElementById('admin-grant-role').value.trim();
    if(!member || !role){ log('укажите роль-члена и роль-группу','warn'); return; }
    postJSON('/api/admin/roles', {action:'grant', name:member, role:role}).then(function(j){
      if(j.ok){ log('GRANT '+role+' TO '+member+' ✓','ok'); loadAdmin(); } else log('admin: '+j.error,'err');
    }).catch(function(e){ log('admin: '+e.message,'err'); });
  }
  function adminTsCreate(){
    var name = document.getElementById('admin-ts-name').value.trim();
    var loc = document.getElementById('admin-ts-loc').value.trim();
    if(!name || !loc){ log('имя и location обязательны','warn'); return; }
    confirmBox('Создать табличное пространство '+name+'?', 'Каталог '+loc+' должен существовать на сервере и принадлежать postgres.', function(){
      postJSON('/api/admin/tablespaces', {action:'create', name:name, location:loc}).then(function(j){
        if(j.ok){ log('tablespace '+name+' создан ✓','ok'); loadAdmin(); } else log('admin: '+j.error,'err');
      }).catch(function(e){ log('admin: '+e.message,'err'); });
    });
  }
  function adminTsDrop(name){
    confirmBox('Удалить табличное пространство '+name+'?', '', function(){
      postJSON('/api/admin/tablespaces', {action:'drop', name:name}).then(function(j){
        if(j.ok){ log('tablespace '+name+' удалён ✓','ok'); loadAdmin(); } else log('admin: '+j.error,'err');
      }).catch(function(e){ log('admin: '+e.message,'err'); });
    });
  }
  function adminDiff(){
    var a = document.getElementById('admin-diff-a').value, b = document.getElementById('admin-diff-b').value;
    var tb = document.getElementById('admin-diff-body'); if(!tb) return;
    morph(tb, '<tr><td colspan="5" class="empty">сравниваю…</td></tr>');
    getJSON('/api/admin/config-diff?a='+encodeURIComponent(a)+'&b='+encodeURIComponent(b)).then(function(j){
      if(!j.ok){ morph(tb, '<tr><td colspan="5" class="sqlerr">'+esc(j.error)+'</td></tr>'); return; }
      var rows = j.data.diff || [];
      morph(tb, rows.length ? rows.map(function(r){
        return '<tr><td class="mono">'+esc(r.name)+'</td><td class="mono">'+esc(r.a==null?'—':r.a)+'</td><td class="muted">'+esc(r.source_a||'')+'</td><td class="mono">'+esc(r.b==null?'—':r.b)+'</td><td class="muted">'+esc(r.source_b||'')+'</td></tr>';
      }).join('') : '<tr><td colspan="5" class="muted center">конфигурации идентичны ✓</td></tr>');
      log('diff '+a+' ↔ '+b+': различий '+rows.length, 'info');
    }).catch(function(e){ morph(tb, '<tr><td colspan="5" class="sqlerr">'+esc(e.message)+'</td></tr>'); });
  }
  function adminPatroniDiff(){
    var a = document.getElementById('admin-pat-a').value, b = document.getElementById('admin-pat-b').value;
    var tb = document.getElementById('admin-pat-body'); if(!tb) return;
    morph(tb, '<tr><td colspan="3" class="empty">сравниваю…</td></tr>');
    getJSON('/api/admin/patroni-diff?a='+encodeURIComponent(a)+'&b='+encodeURIComponent(b)).then(function(j){
      if(!j.ok){ morph(tb, '<tr><td colspan="3" class="sqlerr">'+esc(j.error)+'</td></tr>'); return; }
      var rows = j.data.diff || [];
      var head = '<tr><td colspan="3" class="muted center">одинаковых путей: <b>'+j.data.same+'</b> · различий: <b>'+rows.length+'</b></td></tr>';
      morph(tb, head + (rows.length ? rows.map(function(r){
        var flag = r.only_a ? ' <span style="color:var(--amber)">(только в A)</span>' : (r.only_b ? ' <span style="color:var(--amber)">(только в B)</span>' : '');
        return '<tr style="background:rgba(196,138,29,.08)"><td class="mono">'+esc(r.path)+flag+'</td><td class="mono">'+esc(r.a==null?'—':String(r.a))+'</td><td class="mono">'+esc(r.b==null?'—':String(r.b))+'</td></tr>';
      }).join('') : '<tr><td colspan="3" class="muted center">patroni.yml идентичны ✓</td></tr>'));
      log('patroni diff '+a+' ↔ '+b+': '+rows.length+' различий', rows.length ? 'warn' : 'ok');
    }).catch(function(e){ morph(tb, '<tr><td colspan="3" class="sqlerr">'+esc(e.message)+'</td></tr>'); });
  }

  /* ========== REPORTS (PWR) ========== */
  function fmtTime(ts){ return new Date(ts*1000).toLocaleString('ru-RU'); }
  function loadReports(){
    return getJSON('/api/reports/snapshots', true).then(function(j){
      if(!j || !j.ok) return;
      var snaps = j.data || [];
      var f = document.getElementById('rep-from'), t = document.getElementById('rep-to');
      if(!f || !t) return;
      var opts = snaps.map(function(s){ return {value:String(s.ts), label:fmtTime(s.ts)}; });
      syncSelect(f, opts, {value: snaps.length ? String(snaps[0].ts) : null});
      syncSelect(t, opts, {keep:false, value: snaps.length ? String(snaps[snaps.length-1].ts) : null});
    }).catch(function(e){ log('reports: '+e.message,'err'); });
  }
  function reportNow(){
    postJSON('/api/reports/snapshot', {}).then(function(j){
      if(j.ok){ log('снимок снят ✓','ok'); loadReports(); } else log('reports: '+j.error,'err');
    }).catch(function(e){ log('reports: '+e.message,'err'); });
  }
  function reportGenerate(){
    var f = document.getElementById('rep-from').value, t = document.getElementById('rep-to').value;
    var out = document.getElementById('report-out'); morph(out, '<p class="muted">формирование…</p>');
    getJSON('/api/reports/generate?from='+encodeURIComponent(f)+'&to='+encodeURIComponent(t)).then(function(j){
      if(!j.ok){ morph(out, '<p class="sqlerr">'+esc(j.error)+'</p>'); return; }
      var d = j.data;
      var h = '<div class="metric-grid">'
        + '<div class="metric"><span class="mk">TPS (средний)</span><span class="mv good">'+d.tps+'</span></div>'
        + '<div class="metric"><span class="mk">Cache Hit</span><span class="mv '+(d.cache_hit>95?'good':'warn')+'">'+d.cache_hit+'%</span></div>'
        + '<div class="metric"><span class="mk">Rollback %</span><span class="mv">'+d.rollback_ratio+'%</span></div>'
        + '<div class="metric"><span class="mk">Deadlocks</span><span class="mv '+(d.deadlocks>0?'bad':'good')+'">'+d.deadlocks+'</span></div>'
        + '<div class="metric"><span class="mk">Temp files</span><span class="mv">'+d.temp_files+'</span></div>'
        + '<div class="metric"><span class="mk">Соединения</span><span class="mv">'+d.conn_start+' → '+d.conn_end+'</span></div>'
        + '</div>';
      h += '<h4 style="margin:14px 0 6px">Темп кортежей / сек</h4><p class="muted">ins '+d.rates.ins+' · upd '+d.rates.upd+' · del '+d.rates.del+' · ret '+d.rates.ret+'</p>';
      h += '<h4 style="margin:14px 0 6px">Рост баз</h4>';
      h += '<table class="mono-t"><thead><tr><th>БД</th><th>Δ размер</th><th>текущий</th></tr></thead><tbody>'
        + (d.db_growth||[]).map(function(g){ return '<tr><td>'+esc(g.name)+'</td><td class="mono">'+(g.delta>=0?'+':'')+fb(g.delta)+'</td><td class="mono">'+fb(g.now)+'</td></tr>'; }).join('')
        + '</tbody></table>';
      h += '<h4 style="margin:14px 0 6px">Топ запросов (по суммарному времени)</h4>';
      h += '<table class="mono-t"><thead><tr><th>запрос</th><th>вызовов</th><th>total мс</th><th>mean мс</th></tr></thead><tbody>'
        + (d.top_queries||[]).map(function(q){ return '<tr><td>'+esc(q.query)+'</td><td class="mono">'+q.calls+'</td><td class="mono">'+q.total+'</td><td class="mono">'+q.mean+'</td></tr>'; }).join('')
        + '</tbody></table>';
      morph(out, h);
      log('отчёт: '+fmtTime(d.from)+' – '+fmtTime(d.to)+' ('+d.snapshots+' снимков)', 'ok');
    }).catch(function(e){ morph(out, '<p class="sqlerr">'+esc(e.message)+'</p>'); });
  }

  /* ========== SLOTS ========== */
  function loadSlots(){
    var out = document.getElementById('slots-out'); if(!out) return Promise.resolve();
    uiLoading(out);
    return getJSON('/api/repl/slots', true).then(function(j){
      if(!j || !j.ok){ morph(out, '<p class="sqlerr">'+esc(j && j.error || 'ошибка')+'</p>'); return; }
      var html = '';
      (j.data||[]).forEach(function(nd){
        html += '<div class="ocard" style="margin-bottom:14px"><h3>'+esc(nd.node)+(nd.ok?'':' <span class="badge role-replica">нет связи</span>')+'</h3>';
        if(!nd.ok){ html += '<p class="muted">'+esc(nd.error||'узел недоступен')+'</p></div>'; return; }
        html += '<h4 style="margin:8px 0 6px">Слоты</h4>';
        if((nd.slots||[]).length){
          html += '<table class="mono-t"><thead><tr><th>Имя</th><th>Тип</th><th>Активен</th><th>Лаг</th><th>restart_lsn</th><th></th></tr></thead><tbody>';
          nd.slots.forEach(function(s){
            var st = s.danger ? ' style="background:rgba(204,74,60,.12)"' : '';
            html += '<tr'+st+'><td class="mono">'+esc(s.slot_name)+(s.danger?' (риск)':'')+'</td><td>'+esc(s.slot_type)+'</td><td>'+(s.active?'<b style="color:var(--good)">да</b>':'<b style="color:var(--crit)">нет</b>')+'</td><td class="mono">'+fb(s.lag_bytes)+'</td><td class="mono">'+esc(s.restart_lsn||'—')+'</td>'
              +'<td>'+((!s.active)?'<button class="btn sm gate-admin" data-action="slot-drop" data-node="'+esc(nd.node)+'" data-name="'+esc(s.slot_name)+'">drop</button>':'')+'</td></tr>';
          });
          html += '</tbody></table>';
        } else html += '<p class="muted">нет слотов</p>';
        html += '<h4 style="margin:10px 0 6px">Активные реплики</h4>';
        if((nd.repl||[]).length){
          html += '<table class="mono-t"><thead><tr><th>Клиент</th><th>application</th><th>state</th><th>sent lag</th><th>replay lag</th></tr></thead><tbody>';
          nd.repl.forEach(function(r){
            html += '<tr><td class="mono">'+esc(r.client)+'</td><td>'+esc(r.application_name||'—')+'</td><td>'+esc(r.state)+'</td><td class="mono">'+fb(r.sent_lag||0)+'</td><td class="mono">'+fb(r.replay_lag||0)+'</td></tr>';
          });
          html += '</tbody></table>';
        } else html += '<p class="muted">нет активных реплик</p>';
        html += '</div>';
      });
      morph(out, html || '<p class="muted">нет данных</p>');
    }).catch(function(e){ morph(out, '<p class="sqlerr">'+esc(e.message)+'</p>'); });
  }
  function slotDrop(node, name){
    confirmBox('Удалить слот '+name+' на '+node+'?', 'Доступно только для НЕактивных слотов.', function(){
      postJSON('/api/repl/slots/drop', {node:node, name:name}).then(function(j){
        if(j.ok){ log('слот '+name+' удалён ✓','ok'); loadSlots(); } else log('slots: '+j.error,'err');
      }).catch(function(e){ log('slots: '+e.message,'err'); });
    });
  }

  window.__loaders = {
    center: loadCenter, metrics: loadMetricsScreen, schema: loadSchema, analyzer: function(){},
    locks: function(){ loadLocks(true); }, dba: loadDbaSummary, querylab: fillSqlNodes, ops: fillDelayNodes,
    db: loadDbs,
    backups: function(){ fillBackupNodes(); loadDumps(); }, audit: loadAudit, admin: loadAdmin, alerts: function(){ loadAlerts(); loadNotifyCfg(); }, reports: loadReports, slots: loadSlots,
    hosts: loadHosts, maint: function(){ loadMaint(); maintSchedLoad(); },
    tune: function(){ loadTune(); loadTemplates(); },
    ai: loadAiHistory
  };
  window.__pollers = {
    metrics: function(){ fetchPerf(true); renderMetrics(); return Promise.resolve(); },
    locks: function(){ return loadLocks(); }, dba: function(){ var t=window.__dbaTab||'summary'; if(t==='summary') return loadDbaSummary(); },
    db: function(){ return loadDbs(); },
    backups: function(){ return new Promise(function(res){ pollAgentNow(function(){ renderBackups(); res(); }); }); },
    alerts: function(){ return loadAlerts(); }, slots: function(){ return loadSlots(); },
    hosts: function(){ return loadHosts(); },
    center: function(){ return loadCenter(); },
    audit: function(){ return loadAudit(); }
  };
  window.__actions = {
    'center-select': function(t){ postJSON('/api/center/select', {name:t.getAttribute('data-name')}).then(function(r){ if(r && r.ok) location.reload(); }); },
    analyze: function(t){ analyze(t.getAttribute('data-query')||''); },
    'show-table': function(t){ showTable(t.getAttribute('data-fn')||''); },
    'back-table': function(){ if(currentSchema) morph(document.getElementById('table-list-content'), currentSchema.tables.map(function(t){ return '<div class="titem" data-action="show-table" data-fn="'+esc(t.full_name)+'"><div class="tn">'+esc(t.schema)+'.'+esc(t.name)+'</div><div class="tm">'+fb(t.size)+' · '+t.columns.length+' кол.</div></div>'; }).join('')); },
    kill: function(t){ kill(t.getAttribute('data-node'), t.getAttribute('data-pid')); },
    vacuum: function(t){ vacuum(t.getAttribute('data-s'), t.getAttribute('data-t')); },
    drop: function(t){ dropIdx(t.getAttribute('data-s'), t.getAttribute('data-ix')); },
    reinit: function(t){ doReinit(t.getAttribute('data-node')); },
    restart: function(t){ doRestart(t.getAttribute('data-node')); },
    reload: function(t){ doReload(t.getAttribute('data-node')); },
    'bk-verify': function(t){ bkAction(t.getAttribute('data-name'), 'verify_backup'); },
    'bk-delete': function(t){ confirmBox('Удалить бэкап?', '', function(){ bkAction(t.getAttribute('data-name'), 'delete_backup'); }); },
    'bk-restore': function(t){ confirmBox('Restore бэкап?', 'В ОТДЕЛЬНЫЙ каталог, живой кластер не трогается.', function(){ bkAction(t.getAttribute('data-name'), 'restore'); }); },
    'bk-clone': function(t){ bkClone(t.getAttribute('data-name')); },
    'bk-pitr': function(t){ bkAction(t.getAttribute('data-name'), 'pitr', 'Целевое время (YYYY-MM-DD HH:MM:SS):'); },
    'admin-ext-create': function(t){ adminExtCreate(t.getAttribute('data-name')); },
    'admin-ext-drop': function(t){ adminExtDrop(t.getAttribute('data-name')); },
    'admin-role-drop': function(t){ adminRoleDrop(t.getAttribute('data-name')); },
    'admin-ts-drop': function(t){ adminTsDrop(t.getAttribute('data-name')); },
    'alert-toggle': function(t){ alertToggle(t.getAttribute('data-id')); },
    'alert-delete': function(t){ alertDelete(t.getAttribute('data-id')); },
    'slot-drop': function(t){ slotDrop(t.getAttribute('data-node'), t.getAttribute('data-name')); },
    'host-svc-restart': function(t){ hostSvc(t.getAttribute('data-node'), t.getAttribute('data-svc'), 'restart'); },
    'dump-delete': function(t){ dumpDelete(t.getAttribute('data-name')); },
    'maint-repack': function(t){ maintRepack(t.getAttribute('data-db'), t.getAttribute('data-schema'), t.getAttribute('data-table')); },
    'tpl-apply': function(t){ tplApply(t.getAttribute('data-key')); }
  };

  var dbaTabs = document.getElementById('dba-tabs');
  if(dbaTabs){
    dbaTabs.addEventListener('click', function(e){
      var dt = e.target.closest('.seg'); if(!dt) return;
      var name = dt.getAttribute('data-dba');
      document.querySelectorAll('#dba-tabs .seg').forEach(function(t){ t.classList.remove('active'); });
      dt.classList.add('active');
      document.querySelectorAll('.dba-pane').forEach(function(p){ p.classList.toggle('hidden', p.getAttribute('data-dba')!==name); });
      window.__dbaTab = name;
      var map = { bloat:loadBloat, vacuum:loadVacuum, unused:loadUnused, conn:loadConn, summary:loadDbaSummary };
      if(map[name]) map[name]();
    });
  }
  var anTabs = document.getElementById('an-tabs');
  if(anTabs){
    anTabs.addEventListener('click', function(e){
      var at = e.target.closest('.seg'); if(!at) return;
      var name = at.getAttribute('data-an');
      document.querySelectorAll('#an-tabs .seg').forEach(function(t){ t.classList.remove('active'); });
      at.classList.add('active');
      document.querySelectorAll('.an-pane').forEach(function(p){ p.classList.toggle('hidden', p.getAttribute('data-an')!==name); });
    });
  }
  var adminTabs = document.getElementById('admin-tabs');
  if(adminTabs){
    adminTabs.addEventListener('click', function(e){
      var at = e.target.closest('.seg'); if(!at) return;
      var name = at.getAttribute('data-admin');
      document.querySelectorAll('#admin-tabs .seg').forEach(function(t){ t.classList.remove('active'); });
      at.classList.add('active');
      document.querySelectorAll('.admin-pane').forEach(function(p){ p.classList.toggle('hidden', p.getAttribute('data-admin')!==name); });
    });
  }
  var alertsTabs = document.getElementById('alerts-tabs');
  if(alertsTabs){
    alertsTabs.addEventListener('click', function(e){
      var at = e.target.closest('.seg'); if(!at) return;
      var name = at.getAttribute('data-al');
      document.querySelectorAll('#alerts-tabs .seg').forEach(function(t){ t.classList.remove('active'); });
      at.classList.add('active');
      document.querySelectorAll('.al-pane').forEach(function(p){ p.classList.toggle('hidden', p.getAttribute('data-al')!==name); });
    });
  }
  var tuneTabs = document.getElementById('tune-tabs');
  if(tuneTabs){
    tuneTabs.addEventListener('click', function(e){
      var at = e.target.closest('.seg'); if(!at) return;
      var name = at.getAttribute('data-tune');
      document.querySelectorAll('#tune-tabs .seg').forEach(function(t){ t.classList.remove('active'); });
      at.classList.add('active');
      document.querySelectorAll('.tune-pane').forEach(function(p){ p.classList.toggle('hidden', p.getAttribute('data-tune')!==name); });
      if(name === 'templates') loadTemplates();
    });
  }
  var tuneDisk = document.getElementById('tune-disk');
  if(tuneDisk){
    tuneDisk.addEventListener('change', function(){ if(__tuneCatalog) computeTune(); });
  }

  Object.assign(window, {
    loadSchema: loadSchema, downloadSchema: downloadSchema, loadSlow: loadSlow, loadCurrent: loadCurrent, analyzeCustom: analyzeCustom,
    loadDbs: loadDbs,
    loadLocks: loadLocks, loadBloat: loadBloat, loadVacuum: loadVacuum, loadUnused: loadUnused, loadConn: loadConn, loadTune: loadTune,
    computeTune: computeTune, execSQL: execSQL, clearSQL: clearSQL, doSwitchover: doSwitchover, doFailover: doFailover,
    restartCluster: restartCluster, loadHistory: loadHistory, killUserSessions: killUserSessions,
    setReplicaDelay: setReplicaDelay, applyDelay: applyDelay, clearDelay: clearDelay, refreshDelayStatus: refreshDelayStatus,
    renderBackups: renderBackups, renderMetrics: renderMetrics, loadCenter: loadCenter, loadAudit: loadAudit, loadAdmin: loadAdmin, loadAlerts: loadAlerts,
    bkCreate: bkCreate, bkRotate: bkRotate, bkArch: bkArch, bkSchedSave: bkSchedSave,
    loadDumps: loadDumps, dumpCreate: dumpCreate, dumpRotate: dumpRotate, nfsMount: nfsMount, nfsUmount: nfsUmount, sendDumpReport: sendDumpReport, dumpSchedSave: dumpSchedSave, cloneInspect: cloneInspect,
    loadMaint: loadMaint, maintSchedSave: maintSchedSave, loadTemplates: loadTemplates,
    loadNotifyCfg: loadNotifyCfg, notifySave: notifySave, notifyTest: notifyTest,
    loadDcsHealth: loadDcsHealth, dcsCfgSave: dcsCfgSave, dcsDiscover: dcsDiscover,
    dcsCfgLoad: dcsCfgLoad, dcsCfgApply: dcsCfgApply,
    aiSend: aiSend, aiAsk: aiAsk, aiAdvice: aiAdvice, aiDiagnose: aiDiagnose, loadAiHistory: loadAiHistory, clearAi: clearAi, aiExplainPlan: aiExplainPlan,
    adminExtCreate: adminExtCreate, adminExtDrop: adminExtDrop, adminRoleCreate: adminRoleCreate, adminRoleDrop: adminRoleDrop,
    adminRoleGrant: adminRoleGrant, adminTsCreate: adminTsCreate, adminTsDrop: adminTsDrop, adminDiff: adminDiff, adminPatroniDiff: adminPatroniDiff,
    alertRuleSave: alertRuleSave, alertToggle: alertToggle, alertDelete: alertDelete,
    loadReports: loadReports, reportNow: reportNow, reportGenerate: reportGenerate,
    loadSlots: loadSlots, slotDrop: slotDrop, loadHosts: loadHosts
  });

  fillClusterSwitch();
  /* DCS опрашиваем не на каждом тике кластера (3 с), а раз в 15 с */
  var dcsLast = 0;
  window.__postClusterLoad = function(){
    var s = window.activeScreen;
    if((!s || s === 'cluster') && Date.now() - dcsLast > 15000){ dcsLast = Date.now(); try { loadDcsHealth(); } catch(e){} }
  };
  updateAlertIndicator();
  window.__modulesReady = function(){ log('модули готовы ✓', 'ok'); };
  log('модули загружены ('+MOD_VERSION+')', 'info');
})();

