/* gcal.js -- Google-Calendar-style all-day calendar (dark), shared by
 * leave.html and office-calendar.html. No dependencies.
 *
 *   const cal = GCal.create({
 *     root, storageKey,
 *     sources: [{id, label, color}],            // sidebar checkboxes (layers)
 *     loadEvents: async (startISO, endISO) => [{id, source, title, start, end, style, color}],
 *     onDayClick(dateISO), onEventClick(ev),
 *     onCreate(dateISO)                         // optional -> shows a Create button
 *   });
 *   cal.refresh();
 *
 * Event style: 'fill' (solid pill), 'dot' (coloured dot + text), 'ring' (hollow dot).
 */
(function () {
  const CSS = `
.gcal{--g-bg:#1f1f1f;--g-s:#282828;--g-line:#3c4043;--g-txt:#e8eaed;--g-mut:#9aa0a6;--g-acc:#8ab4f8;--g-sel:#394457;
  display:flex;flex-direction:column;background:var(--g-bg);color:var(--g-txt);border:1px solid var(--g-line);
  border-radius:16px;overflow:hidden;font-family:'Google Sans',Roboto,Inter,Arial,sans-serif;min-height:640px}
.gcal *{box-sizing:border-box}
.gc-top{display:flex;align-items:center;gap:10px;padding:12px 16px;flex-wrap:wrap}
.gc-top .gc-title{font-size:1.35rem;font-weight:400;margin-left:6px;flex:1;min-width:150px}
.gc-btn{background:transparent;color:var(--g-txt);border:1px solid var(--g-line);border-radius:20px;padding:8px 18px;
  font-size:.85rem;cursor:pointer;font-family:inherit}
.gc-btn:hover{background:rgba(255,255,255,.08)}
.gc-icon{width:36px;height:36px;border-radius:50%;border:none;background:transparent;color:var(--g-txt);cursor:pointer;font-size:1.1rem}
.gc-icon:hover{background:rgba(255,255,255,.1)}
.gc-view{position:relative}
.gc-menu{position:absolute;right:0;top:110%;background:#303134;border-radius:8px;min-width:200px;padding:8px 0;z-index:30;
  box-shadow:0 4px 16px rgba(0,0,0,.5);display:none}
.gc-menu.open{display:block}
.gc-menu div{display:flex;justify-content:space-between;padding:9px 18px;cursor:pointer;font-size:.88rem}
.gc-menu div:hover{background:rgba(255,255,255,.08)}
.gc-menu div small{color:var(--g-mut)}
.gc-menu hr{border:none;border-top:1px solid var(--g-line);margin:6px 0}
.gc-body{display:flex;flex:1;min-height:0}
.gc-side{width:240px;flex-shrink:0;padding:8px 14px 16px;border-right:1px solid var(--g-line)}
.gc-create{display:flex;align-items:center;gap:10px;background:#3c4043;color:var(--g-txt);border:none;border-radius:16px;
  padding:14px 22px;font-size:.92rem;cursor:pointer;margin:6px 0 18px;font-family:inherit}
.gc-create:hover{background:#4a4e51}
.gc-mini-h{display:flex;align-items:center;justify-content:space-between;font-size:.85rem;margin-bottom:6px}
.gc-mini{display:grid;grid-template-columns:repeat(7,1fr);gap:1px;text-align:center;font-size:.7rem}
.gc-mini .h{color:var(--g-mut);padding:3px 0}
.gc-mini .d{padding:5px 0;border-radius:50%;cursor:pointer;aspect-ratio:1;display:flex;align-items:center;justify-content:center}
.gc-mini .d:hover{background:rgba(255,255,255,.1)}
.gc-mini .d.out{color:#5f6368}
.gc-mini .d.wk{background:var(--g-sel);border-radius:0}
.gc-mini .d.today{background:var(--g-acc);color:#062e6f;font-weight:700;border-radius:50%}
.gc-layers h4{font-size:.85rem;font-weight:500;margin:20px 0 8px}
.gc-layer{display:flex;align-items:center;gap:10px;padding:6px 2px;font-size:.85rem;cursor:pointer;user-select:none}
.gc-layer b{width:16px;height:16px;border-radius:3px;border:2px solid var(--c);display:inline-flex;align-items:center;justify-content:center;font-size:.7rem;color:#111;line-height:1}
.gc-layer.on b{background:var(--c)}
.gc-main{flex:1;min-width:0;display:flex;flex-direction:column;overflow:auto}
.gc-dow{display:grid;border-bottom:1px solid var(--g-line);position:sticky;top:0;background:var(--g-bg);z-index:2}
.gc-dow div{text-align:center;font-size:.7rem;color:var(--g-mut);padding:8px 0 4px;letter-spacing:.03em;text-transform:uppercase}
.gc-dow div.today{color:var(--g-acc)}
.gc-grid{display:grid;flex:1}
.gc-cell{border-right:1px solid var(--g-line);border-bottom:1px solid var(--g-line);min-height:110px;padding:2px 3px 4px;cursor:pointer;min-width:0;overflow:hidden}
.gc-cell:hover{background:rgba(255,255,255,.03)}
.gc-cell.out .gc-num{color:#6f747a}
.gc-cell.wknd{background:rgba(255,255,255,.015)}
.gc-num{display:inline-flex;align-items:center;justify-content:center;min-width:26px;height:26px;border-radius:13px;font-size:.78rem;margin:3px 0 2px;padding:0 6px}
.gc-num:hover{background:rgba(255,255,255,.12)}
.gc-cell.today .gc-num{background:var(--g-acc);color:#062e6f;font-weight:700}
.gc-chip{display:flex;align-items:center;gap:6px;font-size:.74rem;padding:2px 6px;border-radius:5px;margin:1px 0;cursor:pointer;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.gc-chip span{overflow:hidden;text-overflow:ellipsis}
.gc-chip:hover{filter:brightness(1.15)}
.gc-chip.gc-fill{background:var(--c);color:#0b1f15;font-weight:500}
.gc-chip.gc-fill i{display:none}
.gc-chip.gc-dot i,.gc-chip.gc-ring i{width:8px;height:8px;border-radius:50%;flex-shrink:0;background:var(--c)}
.gc-chip.gc-ring i{background:transparent;border:2px solid var(--c)}
.gc-chip.gc-dot:hover,.gc-chip.gc-ring:hover{background:rgba(255,255,255,.08)}
.gc-more{font-size:.72rem;color:var(--g-mut);padding:2px 6px;cursor:pointer;border-radius:4px}
.gc-more:hover{background:rgba(255,255,255,.08)}
.gc-sched{padding:8px 24px}
.gc-sched .row{display:flex;gap:24px;padding:12px 0;border-bottom:1px solid var(--g-line)}
.gc-sched .dt{width:130px;flex-shrink:0;font-size:.85rem}
.gc-sched .dt b{display:inline-flex;align-items:center;justify-content:center;min-width:28px;height:28px;border-radius:14px;font-size:1rem;margin-right:6px}
.gc-sched .row.today .dt b{background:var(--g-acc);color:#062e6f}
.gc-sched .evs{flex:1}
.gc-empty{padding:40px;text-align:center;color:var(--g-mut);font-size:.9rem}
.gc-year{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:18px;padding:18px}
.gc-year .m h5{font-size:.9rem;font-weight:500;margin-bottom:6px;cursor:pointer}
.gc-year .m h5:hover{color:var(--g-acc)}
.gc-year .gc-mini .d{position:relative}
.gc-year .gc-mini .d.has::after{content:'';position:absolute;bottom:2px;width:4px;height:4px;border-radius:50%;background:var(--g-acc)}
@media(max-width:820px){.gc-side{display:none}.gc-cell{min-height:84px}.gc-chip{font-size:.66rem;padding:1px 4px}}
`;

  const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
  const DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const VIEWS = [['day', 'Day', 'D'], ['week', 'Week', 'W'], ['month', 'Month', 'M'], ['year', 'Year', 'Y'], ['schedule', 'Schedule', 'A'], ['4days', '4 days', 'X']];

  const pad = n => String(n).padStart(2, '0');
  const iso = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const parse = s => { const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d); };
  const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
  const sameDay = (a, b) => iso(a) === iso(b);
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[m]));
  const weekStart = d => addDays(d, -d.getDay());

  function lsGet(k, f) { try { const v = localStorage.getItem(k); return v == null ? f : v; } catch (e) { return f; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { } }

  function create(opts) {
    if (!document.getElementById('gcal-css')) {
      const s = document.createElement('style'); s.id = 'gcal-css'; s.textContent = CSS; document.head.appendChild(s);
    }
    const key = opts.storageKey || 'gcal';
    const st = {
      view: lsGet(key + '_view', 'month'), anchor: new Date(), mini: new Date(),
      events: [], hidden: new Set(JSON.parse(lsGet(key + '_hidden', '[]'))), token: 0,
    };
    if (!VIEWS.some(v => v[0] === st.view)) st.view = 'month';
    const sources = opts.sources || [];

    const root = opts.root;
    root.classList.add('gcal');
    root.innerHTML = `
      <div class="gc-top">
        <button class="gc-btn" data-a="today">Today</button>
        <button class="gc-icon" data-a="prev" aria-label="Previous">&#8249;</button>
        <button class="gc-icon" data-a="next" aria-label="Next">&#8250;</button>
        <div class="gc-title"></div>
        <div class="gc-view"><button class="gc-btn" data-a="viewmenu"></button><div class="gc-menu"></div></div>
      </div>
      <div class="gc-body">
        <div class="gc-side">
          ${opts.onCreate ? '<button class="gc-create" data-a="create"><span style="font-size:1.3rem;line-height:1">+</span> Create</button>' : ''}
          <div class="gc-minibox"></div>
          <div class="gc-layers"></div>
        </div>
        <div class="gc-main"></div>
      </div>`;
    const $ = sel => root.querySelector(sel);

    function range() {
      const a = st.anchor;
      switch (st.view) {
        case 'day': return [a, a];
        case '4days': return [a, addDays(a, 3)];
        case 'week': { const s = weekStart(a); return [s, addDays(s, 6)]; }
        case 'year': return [new Date(a.getFullYear(), 0, 1), new Date(a.getFullYear(), 11, 31)];
        case 'schedule': return [new Date(a.getFullYear(), a.getMonth(), 1), new Date(a.getFullYear(), a.getMonth() + 1, 0)];
        default: {
          const first = new Date(a.getFullYear(), a.getMonth(), 1);
          const dim = new Date(a.getFullYear(), a.getMonth() + 1, 0).getDate();
          const rows = Math.ceil((first.getDay() + dim) / 7);
          const s = weekStart(first);
          return [s, addDays(s, rows * 7 - 1)];
        }
      }
    }

    function title() {
      const a = st.anchor;
      if (st.view === 'year') return String(a.getFullYear());
      if (st.view === 'month' || st.view === 'schedule') return `${MONTHS[a.getMonth()]} ${a.getFullYear()}`;
      if (st.view === 'day') return `${a.getDate()} ${MONTHS[a.getMonth()]} ${a.getFullYear()}`;
      const [s, e] = range();
      return s.getMonth() === e.getMonth()
        ? `${MONTHS[s.getMonth()]} ${s.getFullYear()}`
        : `${MONTHS[s.getMonth()].slice(0, 3)} – ${MONTHS[e.getMonth()].slice(0, 3)} ${e.getFullYear()}`;
    }

    function visible() { return st.events.filter(e => !st.hidden.has(e.source)); }
    function eventsOn(ds) {
      return visible().filter(e => e.start <= ds && ds <= e.end);
    }
    const byUid = {};
    function chip(ev) {
      byUid[ev.uid] = ev;
      return `<div class="gc-chip gc-${ev.style || 'dot'}" style="--c:${ev.color || '#8ab4f8'}" data-ev="${ev.uid}" title="${esc(ev.title)}"><i></i><span>${esc(ev.title)}</span></div>`;
    }

    function renderMain() {
      const main = $('.gc-main');
      const todayS = iso(new Date());
      const [rs, re] = range();

      if (st.view === 'year') {
        let h = '<div class="gc-year">';
        const withEv = new Set();
        visible().forEach(e => { for (let d = parse(e.start); iso(d) <= e.end; d = addDays(d, 1)) withEv.add(iso(d)); });
        for (let m = 0; m < 12; m++) h += `<div class="m"><h5 data-gm="${m}">${MONTHS[m]}</h5>${miniHTML(st.anchor.getFullYear(), m, withEv)}</div>`;
        main.innerHTML = h + '</div>';
        return;
      }

      if (st.view === 'schedule') {
        let h = '<div class="gc-sched">', any = false;
        for (let d = rs; d <= re; d = addDays(d, 1)) {
          const ds = iso(d), evs = eventsOn(ds);
          if (!evs.length) continue;
          any = true;
          h += `<div class="row ${ds === todayS ? 'today' : ''}" data-date="${ds}"><div class="dt"><b>${d.getDate()}</b>${MONTHS[d.getMonth()].slice(0, 3).toUpperCase()}, ${DOW[d.getDay()].toUpperCase()}</div><div class="evs">${evs.map(chip).join('')}</div></div>`;
        }
        main.innerHTML = h + (any ? '' : '<div class="gc-empty">Nothing scheduled this month</div>') + '</div>';
        return;
      }

      const n = Math.round((re - rs) / 86400000) + 1;
      const cols = st.view === 'month' || st.view === 'week' ? 7 : n;
      let h = `<div class="gc-dow" style="grid-template-columns:repeat(${cols},1fr)">`;
      for (let i = 0; i < cols; i++) {
        const d = addDays(rs, i);
        h += st.view === 'month'
          ? `<div>${DOW[d.getDay()]}</div>`
          : `<div class="${iso(d) === todayS ? 'today' : ''}">${DOW[d.getDay()]} ${d.getDate()}</div>`;
      }
      h += `</div><div class="gc-grid" style="grid-template-columns:repeat(${cols},1fr);grid-auto-rows:${st.view === 'month' ? 'minmax(110px,1fr)' : 'minmax(380px,1fr)'}">`;
      const maxChips = st.view === 'month' ? 3 : 99;
      for (let i = 0; i < n; i++) {
        const d = addDays(rs, i), ds = iso(d), evs = eventsOn(ds);
        const out = st.view === 'month' && d.getMonth() !== st.anchor.getMonth();
        const label = st.view === 'month' && d.getDate() === 1 ? `${d.getDate()} ${MONTHS[d.getMonth()].slice(0, 3)}` : d.getDate();
        h += `<div class="gc-cell ${ds === todayS ? 'today' : ''} ${out ? 'out' : ''} ${d.getDay() % 6 === 0 ? 'wknd' : ''}" data-date="${ds}">
          ${st.view === 'month' ? `<span class="gc-num" data-day="${ds}">${label}</span>` : ''}
          ${evs.slice(0, maxChips).map(chip).join('')}
          ${evs.length > maxChips ? `<div class="gc-more" data-day="${ds}">${evs.length - maxChips} more</div>` : ''}
        </div>`;
      }
      main.innerHTML = h + '</div>';
    }

    function miniHTML(y, m, withEv) {
      const first = new Date(y, m, 1), dim = new Date(y, m + 1, 0).getDate();
      const todayS = iso(new Date());
      let h = '<div class="gc-mini">' + ['S', 'M', 'T', 'W', 'T', 'F', 'S'].map(x => `<div class="h">${x}</div>`).join('');
      for (let i = 0; i < first.getDay(); i++) h += '<div></div>';
      const ws = weekStart(st.anchor), we = addDays(ws, 6);
      for (let d = 1; d <= dim; d++) {
        const dt = new Date(y, m, d), ds = iso(dt);
        const inWeek = !withEv && (st.view === 'week' || st.view === 'day' || st.view === '4days') && dt >= ws && dt <= we;
        h += `<div class="d ${ds === todayS ? 'today' : ''} ${inWeek ? 'wk' : ''} ${withEv && withEv.has(ds) ? 'has' : ''}" data-mini="${ds}">${d}</div>`;
      }
      return h + '</div>';
    }

    function renderSide() {
      const y = st.mini.getFullYear(), m = st.mini.getMonth();
      $('.gc-minibox').innerHTML = `<div class="gc-mini-h"><span>${MONTHS[m]} ${y}</span>
        <span><button class="gc-icon" style="width:28px;height:28px" data-a="mprev">&#8249;</button><button class="gc-icon" style="width:28px;height:28px" data-a="mnext">&#8250;</button></span></div>${miniHTML(y, m, null)}`;
      if (sources.length) {
        $('.gc-layers').innerHTML = '<h4>Calendars</h4>' + sources.map(s =>
          `<div class="gc-layer ${st.hidden.has(s.id) ? '' : 'on'}" data-src="${s.id}" style="--c:${s.color}"><b>${st.hidden.has(s.id) ? '' : '&#10003;'}</b>${esc(s.label)}</div>`).join('');
      }
    }

    function renderTop() {
      $('.gc-title').textContent = title();
      const cur = VIEWS.find(v => v[0] === st.view);
      $('[data-a=viewmenu]').innerHTML = `${cur[1]} &#9662;`;
      $('.gc-menu').innerHTML = VIEWS.map(v => `<div data-view="${v[0]}">${v[1]}<small>${v[2]}</small></div>`).join('');
    }

    function paint() { renderTop(); renderSide(); renderMain(); }

    async function refresh() {
      const my = ++st.token;
      const [s, e] = range();
      paint();
      try {
        const evs = await opts.loadEvents(iso(s), iso(e));
        if (my !== st.token) return;
        st.events = (evs || []).map((x, i) => ({ ...x, uid: 'e' + i }));
      } catch (err) { if (my !== st.token) return; st.events = []; }
      renderMain();
    }

    function step(dir) {
      const a = st.anchor;
      if (st.view === 'day') st.anchor = addDays(a, dir);
      else if (st.view === '4days') st.anchor = addDays(a, 4 * dir);
      else if (st.view === 'week') st.anchor = addDays(a, 7 * dir);
      else if (st.view === 'year') st.anchor = new Date(a.getFullYear() + dir, a.getMonth(), 1);
      else st.anchor = new Date(a.getFullYear(), a.getMonth() + dir, 1);
      st.mini = new Date(st.anchor);
      refresh();
    }
    function goto(d, view) {
      st.anchor = typeof d === 'string' ? parse(d) : d; st.mini = new Date(st.anchor);
      if (view) setView(view, true);
      refresh();
    }
    function setView(v, silent) {
      st.view = v; lsSet(key + '_view', v);
      if (!silent) refresh();
    }

    root.addEventListener('click', e => {
      const t = e.target;
      const act = t.closest('[data-a]');
      if (act) {
        const a = act.dataset.a;
        if (a === 'today') { st.anchor = new Date(); st.mini = new Date(); refresh(); }
        else if (a === 'prev') step(-1);
        else if (a === 'next') step(1);
        else if (a === 'viewmenu') { $('.gc-menu').classList.toggle('open'); e.stopPropagation(); }
        else if (a === 'mprev') { st.mini = new Date(st.mini.getFullYear(), st.mini.getMonth() - 1, 1); renderSide(); }
        else if (a === 'mnext') { st.mini = new Date(st.mini.getFullYear(), st.mini.getMonth() + 1, 1); renderSide(); }
        else if (a === 'create') opts.onCreate && opts.onCreate(iso(st.anchor));
        return;
      }
      const vm = t.closest('[data-view]');
      if (vm) { $('.gc-menu').classList.remove('open'); setView(vm.dataset.view); return; }
      const src = t.closest('[data-src]');
      if (src) {
        const id = src.dataset.src;
        st.hidden.has(id) ? st.hidden.delete(id) : st.hidden.add(id);
        lsSet(key + '_hidden', JSON.stringify([...st.hidden]));
        renderSide(); renderMain(); return;
      }
      const mini = t.closest('[data-mini]');
      if (mini) { goto(mini.dataset.mini); return; }
      const gm = t.closest('[data-gm]');
      if (gm) { st.anchor = new Date(st.anchor.getFullYear(), Number(gm.dataset.gm), 1); setView('month'); return; }
      const evEl = t.closest('[data-ev]');
      if (evEl) { e.stopPropagation(); opts.onEventClick && opts.onEventClick(byUid[evEl.dataset.ev], e); return; }
      const dayEl = t.closest('[data-day]');
      if (dayEl) { goto(dayEl.dataset.day, 'day'); return; }
      const cell = t.closest('[data-date]');
      if (cell) opts.onDayClick && opts.onDayClick(cell.dataset.date);
    });
    document.addEventListener('click', e => { if (!e.target.closest('.gc-view')) $('.gc-menu').classList.remove('open'); });
    document.addEventListener('keydown', e => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      if (/^(INPUT|TEXTAREA|SELECT)$/.test((e.target.tagName || ''))) return;
      if (document.querySelector('.modal-bg.open')) return;
      const v = VIEWS.find(x => x[2].toLowerCase() === e.key.toLowerCase());
      if (v) setView(v[0]);
      else if (e.key === 't' || e.key === 'T') { st.anchor = new Date(); refresh(); }
    });

    refresh();
    return { refresh, goto, setView, state: st };
  }

  // ---- who is on leave: one chip per day, a list (dropdown) when several people ----
  let lastPt = { x: 200, y: 200 };
  document.addEventListener('click', e => { lastPt = { x: e.clientX, y: e.clientY }; }, true);

  /** leaves = [{name, start_date, end_date, leave_type}] -> one event per weekday that has anyone on leave.
   *  One person: "Nupur on leave"; several: "3 on leave" (click it for the names). */
  function teamLeaveEvents(leaves, color) {
    const byDay = {};
    (leaves || []).forEach(l => {
      for (let d = parse(l.start_date); iso(d) <= l.end_date; d = addDays(d, 1)) {
        if (d.getDay() === 0 || d.getDay() === 6) continue;     // weekends do not need leave
        (byDay[iso(d)] = byDay[iso(d)] || []).push({ name: String(l.name || '').split(' ')[0], half: l.leave_type === 'half' });
      }
    });
    return Object.entries(byDay).map(([ds, people]) => {
      people.sort((a, b) => a.name.localeCompare(b.name));
      const one = people.length === 1;
      return {
        source: 'team', start: ds, end: ds, style: 'fill', color, teamDay: { date: ds, people },
        title: one ? `${people[0].name} on leave${people[0].half ? ' (half day)' : ''}` : `${people.length} on leave`,
      };
    });
  }

  /** Small dropdown list near the last click. */
  function popList(title, lines) {
    const old = document.getElementById('gc-pop'); if (old) old.remove();
    const el = document.createElement('div');
    el.id = 'gc-pop';
    el.style.cssText = 'position:fixed;z-index:30000;min-width:200px;max-width:280px;padding:10px 12px;border-radius:10px;' +
      'background:var(--s1,#1b1b22);color:var(--txt,var(--text,#ececf1));border:1px solid var(--bdr,var(--border,#2c2c35));' +
      'box-shadow:0 10px 30px rgba(0,0,0,.5);font-size:.85rem;line-height:1.6';
    el.innerHTML = `<div style="font-weight:700;margin-bottom:4px">${esc(title)}</div>` +
      lines.map(t => `<div>${esc(t)}</div>`).join('');
    document.body.appendChild(el);
    const w = el.offsetWidth, h = el.offsetHeight;
    el.style.left = Math.max(8, Math.min(lastPt.x, window.innerWidth - w - 8)) + 'px';
    el.style.top = Math.max(8, Math.min(lastPt.y + 8, window.innerHeight - h - 8)) + 'px';
    const close = ev => { if (!el.contains(ev.target)) { el.remove(); document.removeEventListener('click', close, true); } };
    setTimeout(() => document.addEventListener('click', close, true), 0);
  }

  function showTeamDay(td) {
    const d = parse(td.date);
    popList(`${DOW[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]} - ${td.people.length} on leave`,
            td.people.map(p => p.name + (p.half ? ' (half day)' : '')));
  }

  window.GCal = { create, iso, parse, addDays, MONTHS, DOW, teamLeaveEvents, popList, showTeamDay };
})();
