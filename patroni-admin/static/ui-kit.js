/* Patroni Console — UI-KIT [uikit-20261017a]
   Базовые примитивы интерфейса, общие для ядра и модулей:

   morph(el, html)    — бесшовное обновление DOM. Вместо el.innerHTML = html
                        сравнивает старое и новое дерево и меняет только то,
                        что реально изменилось. Сохраняются фокус, курсор
                        в полях ввода, выбранные значения select, прокрутка,
                        hover и выделение текста — экран не «дёргается».
                        Ключ строки: id или data-key (переупорядочивание без
                        пересоздания узлов).
   syncSelect(sel, items, opts) — обновить список option, не сбрасывая выбор.
   toast(msg, type)   — всплывающее уведомление о результате действия.
   hints              — подсказки по [data-hint] (одна плавающая подсказка,
                        не обрезается контейнерами с overflow).
   Без зависимостей и сборки: консоль работает в закрытой сети. */
(function(){
  'use strict';

  var tpl = document.createElement('template');

  function keyOf(n){
    if(n.nodeType !== 1) return null;
    return n.getAttribute('data-key') || n.id || null;
  }
  function sameKind(a, b){
    if(a.nodeType !== b.nodeType) return false;
    if(a.nodeType !== 1) return true;
    return a.nodeName === b.nodeName;
  }
  function isEditing(el){
    return el === document.activeElement && /^(INPUT|TEXTAREA|SELECT)$/.test(el.nodeName);
  }

  function syncAttrs(o, n){
    var na = n.attributes, oa = o.attributes, i, a;
    for(i = 0; i < na.length; i++){
      a = na[i];
      if(o.getAttribute(a.name) !== a.value) o.setAttribute(a.name, a.value);
    }
    for(i = oa.length - 1; i >= 0; i--){
      a = oa[i];
      if(!n.hasAttribute(a.name)) o.removeAttribute(a.name);
    }
    /* значения полей ввода — свойства, а не атрибуты: не трогаем поле,
       в котором пользователь сейчас печатает */
    if(o.nodeName === 'INPUT' && !isEditing(o)){
      if(o.type === 'checkbox' || o.type === 'radio'){ if(o.checked !== n.hasAttribute('checked')) o.checked = n.hasAttribute('checked'); }
      else { var nv = n.getAttribute('value') || ''; if(o.value !== nv) o.value = nv; }
    }
    if(o.nodeName === 'OPTION'){
      var sel = n.hasAttribute('selected');
      if(o.selected !== sel && sel) o.selected = true;
    }
  }

  function morphNode(o, n){
    if(o.nodeType === 3 || o.nodeType === 8){
      if(o.nodeValue !== n.nodeValue) o.nodeValue = n.nodeValue;
      return;
    }
    syncAttrs(o, n);
    /* canvas (Chart.js) и явно помеченные узлы живут своей жизнью */
    if(o.nodeName === 'CANVAS' || o.hasAttribute('data-morph-skip')) return;
    if(o.nodeName === 'TEXTAREA'){
      if(!isEditing(o) && o.value !== n.value) o.value = n.value;
      return;
    }
    morphChildren(o, n);
  }

  function morphChildren(oldParent, newParent){
    var keyed = {}, c, k;
    for(c = oldParent.firstChild; c; c = c.nextSibling){
      k = keyOf(c); if(k) keyed[k] = c;
    }
    var cur = oldParent.firstChild;
    var nc = newParent.firstChild;
    while(nc){
      var next = nc.nextSibling;
      var nk = keyOf(nc), match = null;
      if(nk && keyed[nk] && keyed[nk].nodeName === nc.nodeName){
        match = keyed[nk]; delete keyed[nk];
      } else if(cur && !keyOf(cur) && !nk && sameKind(cur, nc)){
        match = cur;
      }
      if(match){
        if(match !== cur) oldParent.insertBefore(match, cur);
        else cur = cur.nextSibling;
        morphNode(match, nc);
      } else {
        oldParent.insertBefore(nc, cur);   /* nc переносится из шаблона */
      }
      nc = next;
    }
    /* хвост: всё, что не нашло пары, удаляем */
    while(cur){
      var nx = cur.nextSibling;
      oldParent.removeChild(cur);
      cur = nx;
    }
    if(oldParent.nodeName === 'SELECT' && !isEditing(oldParent)){
      var want = null;
      for(c = oldParent.firstChild; c; c = c.nextSibling){ if(c.nodeType === 1 && c.hasAttribute('selected')) want = c.value; }
      if(want !== null && oldParent.value !== want) oldParent.value = want;
    }
  }

  /* Главная функция. Быстрый путь: html не изменился — DOM не трогаем вообще. */
  function morph(el, html){
    if(!el) return;
    html = html == null ? '' : String(html);
    if(el.__morphHtml === html) return;
    el.__morphHtml = html;
    if(!el.firstChild){ el.innerHTML = html; return; }
    tpl.innerHTML = html;
    morphChildren(el, tpl.content);
  }
  /* Принудительный сброс (например, после ручной правки el.innerHTML). */
  function morphReset(el){ if(el) el.__morphHtml = null; }

  /* «Скелет» загрузки показываем только при первом заполнении блока:
     повторный опрос не заменяет данные на «загрузка…» и обратно. */
  function loading(el, html){
    if(!el || el.__morphHtml != null) return;
    el.innerHTML = html || '<div class="skeleton"><i></i><i></i><i></i></div>';
  }
  function loadingRow(el, cols){
    loading(el, '<tr class="skeleton-row"><td colspan="'+(cols||1)+'"><div class="skeleton"><i></i><i></i></div></td></tr>');
  }

  /* items: массив строк или {value,label,disabled}. opts.keep — сохранить выбор,
     opts.value — значение по умолчанию, если прежнего выбора нет. */
  function syncSelect(sel, items, opts){
    if(!sel) return;
    opts = opts || {};
    var list = (items || []).map(function(it){
      return (typeof it === 'object' && it) ? it : {value:String(it), label:String(it)};
    });
    var sig = JSON.stringify(list);
    var prev = sel.value;
    if(sel.__sig !== sig){
      sel.__sig = sig;
      while(sel.options.length) sel.remove(0);
      list.forEach(function(it){
        var o = new Option(it.label != null ? it.label : it.value, it.value);
        if(it.disabled) o.disabled = true;
        sel.add(o);
      });
    }
    var has = function(v){ return list.some(function(it){ return String(it.value) === String(v); }); };
    if(prev && has(prev) && opts.keep !== false){ if(sel.value !== prev) sel.value = prev; }
    else if(opts.value != null && has(opts.value)) sel.value = opts.value;
    else if(list.length && !has(sel.value)) sel.value = list[0].value;
  }

  /* ---------- уведомления ---------- */
  function toast(msg, type, ms){
    var box = document.getElementById('toasts');
    if(!box){ box = document.createElement('div'); box.id = 'toasts'; box.className = 'toasts'; box.setAttribute('aria-live','polite'); document.body.appendChild(box); }
    var t = document.createElement('div');
    t.className = 'toast ' + (type || 'info');
    t.textContent = msg;
    t.setAttribute('role', type === 'err' ? 'alert' : 'status');
    t.addEventListener('click', function(){ dismiss(); });
    box.appendChild(t);
    while(box.children.length > 4) box.removeChild(box.firstChild);
    var timer = setTimeout(dismiss, ms || (type === 'err' ? 8000 : 4000));
    function dismiss(){ clearTimeout(timer); t.classList.add('out'); setTimeout(function(){ if(t.parentNode) t.parentNode.removeChild(t); }, 200); }
  }

  /* ---------- подсказки ---------- */
  var tip = null, tipFor = null;
  function showTip(el){
    var text = el.getAttribute('data-hint'); if(!text) return;
    if(!tip){ tip = document.createElement('div'); tip.className = 'hint-tip'; tip.setAttribute('role','tooltip'); document.body.appendChild(tip); }
    tip.textContent = text;
    tip.style.left = '0px'; tip.style.top = '0px';
    tip.classList.add('on');
    var r = el.getBoundingClientRect(), tr = tip.getBoundingClientRect();
    var x = Math.min(Math.max(8, r.left + r.width/2 - tr.width/2), window.innerWidth - tr.width - 8);
    var y = r.bottom + 8;
    if(y + tr.height > window.innerHeight - 8) y = r.top - tr.height - 8;
    tip.style.left = Math.round(x) + 'px'; tip.style.top = Math.round(y) + 'px';
    tipFor = el;
  }
  function hideTip(){ if(tip) tip.classList.remove('on'); tipFor = null; }
  document.addEventListener('mouseover', function(e){
    var el = e.target.closest && e.target.closest('[data-hint]');
    if(el && el !== tipFor) showTip(el); else if(!el && tipFor) hideTip();
  });
  document.addEventListener('focusin', function(e){ var el = e.target.closest && e.target.closest('[data-hint]'); if(el) showTip(el); });
  document.addEventListener('focusout', hideTip);
  window.addEventListener('scroll', hideTip, true);

  window.morph = morph;
  window.morphReset = morphReset;
  window.uiLoading = loading;
  window.uiLoadingRow = loadingRow;
  window.syncSelect = syncSelect;
  window.toast = toast;
})();
