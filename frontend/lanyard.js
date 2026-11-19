/* Lanyard rail: the team hangs from a rail as ID badges (vanilla port of the
 * 21st.dev "team-lanyard-rail" component). Badges sway, swing when the mouse
 * brushes past, and flip on tap. Used by dashboard.html's Live Attendance panel.
 *
 *   LanyardRail.render(container, people, {
 *     me: "emp003",            // whose badge gets the Check In / Check Out buttons
 *     today: true,             // false = viewing a past date (no actions)
 *     onAction: (kind) => {},  // "checkin" | "checkout" | "undo" | "set-checkin" | "set-checkout"
 *   })
 *
 * people: [{id, name, role, inTime, outTime, inMap, outMap, inMapMissing,
 *           outMapMissing, total, working, mood, moodReason}]
 * Calling render() again (60 s refresh) keeps which badge is flipped and does
 * not replay the intro sway.
 */
(function () {
  const RATIO = 96 / 62;
  const TONES = ["#3b82f6", "#22c55e", "#f59e0b", "#a855f7", "#f43f5e", "#14b8a6", "#ff6a00", "#6366f1"];
  const DROPS = [6, 20, 0, 13, 22, 4, 16, 9, 11, 18];
  const REST_Y = [9, -10, 7, -8, 10, -7, 8, -9, 6, -6];
  const REST_Z = [-1.6, 1.2, -2, 1.5, -1, 1.8, -1.3, 2, -1.1, 1.4];
  const MOODS = { awesome: "🤩", happy: "😊", mid: "😐", sad: "😞", stressed: "😫" };

  const state = { flipped: null, introDone: false, sim: [], raf: 0, hoverX: 0, hoverT: 0, hoverHit: -1, people: [], opts: {} };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, m => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[m]);
  }
  function hash(t) { let h = 2166136261; for (let i = 0; i < t.length; i++) { h ^= t.charCodeAt(i); h = Math.imul(h, 16777619); } return h >>> 0; }
  function rnd(seed) { let a = seed || 1; return () => { a = (a + 0x6d2b79f5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; }
  function barcode(seed) {
    const next = rnd(seed ^ 0x9e3779b9); let x = 0, d = "";
    while (x < 92) { const w = 1 + Math.floor(next() * 3); d += `M${x} 0h${w}v20h-${w}z`; x += w + 1 + Math.floor(next() * 2.2); }
    return { d, width: x };
  }
  function initials(name) { return String(name || "?").split(/\s+/).map(p => p[0] || "").join("").slice(0, 2).toUpperCase(); }
  function hhmm(t) { return t ? String(t).slice(0, 5) : ""; }
  function reduced() { return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches); }

  function injectCss() {
    if (document.getElementById("lanyard-css")) return;
    const s = document.createElement("style");
    s.id = "lanyard-css";
    s.textContent = `
.ly-stage{position:relative;width:100%;overflow-x:auto;scrollbar-width:thin;scrollbar-color:#ff6a1a transparent;overflow-y:hidden;user-select:none;-webkit-user-select:none;padding-bottom:6px}
.ly-inner{position:relative;margin:0 auto;background:radial-gradient(rgba(255,255,255,.06) 1px,transparent 1.3px) 0 0/18px 18px}
.ly-rail{position:absolute;left:0;right:0;border-radius:99px;z-index:1;
  background:linear-gradient(to bottom,#cfcfd6 0%,#fff 20%,#b4b4bd 40%,#8a8a94 64%,#4a4a52 100%);box-shadow:0 1px 0 rgba(0,0,0,.4),0 6px 10px rgba(0,0,0,.35)}
.ly-rail::after{content:"";position:absolute;inset:0;border-radius:99px;opacity:.35;mix-blend-mode:overlay;
  background:repeating-linear-gradient(90deg,transparent 0 2px,rgba(255,255,255,.5) 2px 3px,transparent 3px 7px)}
.ly-cap{position:absolute;width:16px;height:28px;border-radius:3px;z-index:2;
  background:linear-gradient(to right,#d6d6dc,#8a8a94 55%,#45454c);box-shadow:0 3px 6px rgba(0,0,0,.4)}
.ly-item{position:absolute;top:0;pointer-events:none}
.ly-swing{position:absolute;inset:0;will-change:transform}
.ly-ring{position:absolute;z-index:3;border-radius:50%;border:2px solid #c9c9d1;box-shadow:inset 1px 0 0 rgba(0,0,0,.25),1px 1px 2px rgba(0,0,0,.3)}
.ly-strap{position:absolute;color:var(--ly-tone);
  background:repeating-linear-gradient(135deg,rgba(255,255,255,.16) 0 1px,transparent 1px 3px),
             repeating-linear-gradient(45deg,rgba(0,0,0,.13) 0 1px,transparent 1px 3px),
             linear-gradient(90deg,color-mix(in oklab,currentColor 68%,black) 0%,currentColor 16%,color-mix(in oklab,currentColor 86%,white) 48%,currentColor 80%,color-mix(in oklab,currentColor 66%,black) 100%)}
.ly-strap::before{content:"";position:absolute;top:0;bottom:0;left:1.5px;right:1.5px;border-left:1px dashed rgba(255,255,255,.35);border-right:1px dashed rgba(255,255,255,.35)}
.ly-crimp{position:absolute;border-radius:2px;background:linear-gradient(to right,#55555c,#e7e7ec 38%,#9a9aa3 70%,#4a4a52);box-shadow:0 1px 1.5px rgba(0,0,0,.4)}
.ly-reel{position:absolute;border-radius:50%;background:radial-gradient(circle at 34% 30%,#f2f2f5 0%,#bdbdc6 58%,#7d7d86 100%);box-shadow:inset 0 -1px 2px rgba(0,0,0,.4),0 2px 3px rgba(0,0,0,.35)}
.ly-reel::after{content:"";position:absolute;left:50%;top:50%;width:32%;height:32%;transform:translate(-50%,-50%);border-radius:50%;background:linear-gradient(to right,#55555c,#e7e7ec 38%,#9a9aa3 70%,#4a4a52)}
.ly-cord{position:absolute;width:1.5px;background:rgba(230,230,240,.6)}
.ly-clip{position:absolute;clip-path:polygon(24% 0,76% 0,100% 72%,94% 100%,6% 100%,0 72%);background:linear-gradient(180deg,#6b6b73,#2c2c32)}
.ly-clipstem{position:absolute;border-radius:0 0 2px 2px;background:linear-gradient(180deg,#5b5b63,#2c2c32)}
.ly-shadow{position:absolute;border-radius:10px;background:rgba(0,0,0,.6);filter:blur(8px);transform:translate(5px,6px)}
.ly-card{position:absolute;pointer-events:auto;cursor:pointer;border:0;padding:0;background:none;outline:none;border-radius:.85em;perspective:900px;font-family:inherit;color:#ececf1;text-align:left}
.ly-card{transition:transform .35s cubic-bezier(.3,1.45,.5,1)}.ly-card[aria-pressed=true]{transform:scale(1.14)}
.ly-card:focus-visible{outline:2px solid #ff8a1f;outline-offset:4px}
.ly-flip{position:absolute;inset:0;display:block;transform-style:preserve-3d;transition:transform .75s cubic-bezier(.3,1.45,.5,1)}
.ly-face{position:absolute;inset:0;overflow:hidden;border-radius:.85em;border:1px solid rgba(255,255,255,.12);
  background:linear-gradient(180deg,#26262c,#18181d);backface-visibility:hidden;-webkit-backface-visibility:hidden;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.08)}
.ly-face.front{display:flex;flex-direction:column;align-items:center;padding:0 .8em .7em;transform:translateZ(1px)}
.ly-face.back{transform:rotateY(180deg) translateZ(1px)}
.ly-gloss{position:absolute;inset:0;pointer-events:none;background:linear-gradient(100deg,transparent 40%,rgba(255,255,255,.10) 48%,rgba(255,255,255,.03) 52%,transparent 58%)}
.ly-band{position:absolute;left:0;right:0;top:0;color:var(--ly-tone);
  background:repeating-linear-gradient(135deg,transparent 0 .45em,rgba(255,255,255,.10) .45em .56em),
             linear-gradient(180deg,color-mix(in oklab,currentColor 80%,white),currentColor 72%,color-mix(in oklab,currentColor 86%,black))}
.ly-band::after{content:"";position:absolute;left:0;right:0;bottom:0;height:1px;background:rgba(0,0,0,.2)}
.ly-hole{position:absolute;left:50%;top:.72em;height:.56em;width:2.7em;transform:translateX(-50%);border-radius:99px;background:#0d0d10;
  box-shadow:inset 0 .09em .2em rgba(0,0,0,.6),0 .05em 0 rgba(255,255,255,.25)}
.ly-photo{position:relative;margin-top:2.05em;width:5.3em;height:5.3em;flex:none;border-radius:50%;background:#1d1d22;padding:.2em;box-shadow:0 .15em .5em rgba(0,0,0,.5)}
.ly-photo>span{position:relative;width:100%;height:100%;border-radius:50%;overflow:hidden;background:#33333b;color:#c9c9d3;
  font-weight:700;font-size:1.6em;display:flex;align-items:center;justify-content:center}
.ly-photo img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.ly-dot{position:absolute;right:.25em;bottom:.25em;width:.95em;height:.95em;border-radius:50%;border:.18em solid #1d1d22;background:#6b6b78}
.ly-dot.on{background:#22d3a0;box-shadow:0 0 .6em rgba(34,211,160,.8)}
.ly-name{margin-top:.55em;width:100%;text-align:center;font-weight:700;font-size:1.08em;line-height:1.2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ly-role{margin-top:.15em;width:100%;text-align:center;font-size:.8em;color:#9b9bab;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ly-stat{margin-top:.5em;font-size:.62em;font-weight:700;letter-spacing:.16em;text-transform:uppercase;color:#8a8a99;white-space:nowrap}
.ly-stat.on{color:#22d3a0}.ly-stat.out{color:#ffb15c}
.ly-bar{margin-top:auto;width:100%;border-top:1px dashed rgba(255,255,255,.15);padding-top:.5em}
.ly-bar svg{display:block;width:100%;height:1.6em;color:rgba(236,236,241,.8)}
.ly-bar div{margin-top:.25em;text-align:center;font-family:ui-monospace,monospace;font-size:.52em;letter-spacing:.32em;color:#8a8a99}
.ly-back{position:absolute;left:.85em;right:.85em;top:2.6em}
.ly-back b{display:block;font-size:.95em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ly-back small{display:block;font-size:.72em;color:#9b9bab;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ly-rows{margin-top:.7em;display:flex;flex-direction:column;gap:.38em;font-size:.74em}
.ly-rows div{display:flex;justify-content:space-between;gap:.4em;border-bottom:1px dashed rgba(255,255,255,.08);padding-bottom:.3em}
.ly-rows span{color:#9b9bab}.ly-rows em{font-style:normal;font-weight:600}
.ly-rows a{color:#ff9a2e;text-decoration:underline;font-size:.85em;margin-left:.35em;font-weight:500}
.ly-mood{margin-top:.55em;font-size:.72em;color:#c9c9d3}
.ly-acts{position:absolute;left:.7em;right:.7em;bottom:.75em;display:flex;flex-direction:column;gap:.35em}
.ly-btn{display:block;text-align:center;pointer-events:auto;border-radius:.6em;padding:.62em .4em;font-family:inherit;font-weight:800;font-size:.8em;cursor:pointer;color:#fff;
  background:linear-gradient(90deg,rgba(255,255,255,.1),rgba(255,106,0,.85) 85%,#ff6a00);border:1px solid rgba(255,140,30,.5);box-shadow:0 6px 16px rgba(255,90,0,.35)}
.ly-btn.green{background:linear-gradient(135deg,#34e0b0,#14b886);border-color:rgba(60,240,190,.5);color:#05261c;box-shadow:0 6px 16px rgba(34,211,160,.3)}
.ly-btn.ghost{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.14);box-shadow:none;font-weight:600;font-size:.68em;padding:.5em .4em}
.ly-btn:disabled{opacity:.6;cursor:wait}
.ly-hint{position:absolute;left:.85em;right:.85em;bottom:.85em;font-size:.6em;color:#7d7d8c;text-align:center}
.ly-tapme{position:absolute;left:50%;top:-1.9em;transform:translateX(-50%);white-space:nowrap;font-size:.62em;font-weight:700;letter-spacing:.08em;
  color:#ffb15c;background:rgba(255,106,0,.14);border:1px solid rgba(255,140,30,.35);border-radius:99px;padding:.2em .7em;pointer-events:none}
.ly-caption{text-align:center;margin-top:6px;min-height:2.4em}
.ly-caption p{font-size:.88rem;font-weight:600;margin:0}
.ly-caption small{font-size:.78rem;color:var(--muted,#8a8a99)}
@media (prefers-reduced-motion: reduce){.ly-flip{transition:none}}
`;
    document.head.appendChild(s);
  }

  function geometry(W, n) {
    const u = W / 124, H = W * RATIO;
    const railR = Math.max(3, 4.2 * u), railY = 14 + 16 * u + railR, ring = 9 * u, pivotY = railY + ring * 0.8;
    const strapW = Math.max(8, 11 * u), crimp = 6 * u, reel = 21 * u, cordRest = 12 * u, clipH = 13 * u, clipW = 30 * u;
    const items = [];
    for (let i = 0; i < n; i++) {
      const strap = (56 + DROPS[i % DROPS.length]) * u;
      const reelTop = pivotY + strap + crimp * 0.6;
      const clipTop = reelTop + reel * 0.82 + cordRest;
      const cardTop = clipTop + clipH * 0.62;
      items.push({ strap, reelTop, clipTop, cardTop, pend: cardTop + H / 2 - pivotY });
    }
    const bottom = Math.max(0, ...items.map(it => it.cardTop + H));
    return { u, W, H, railR, railY, ring, pivotY, strapW, crimp, reel, cordRest, clipH, clipW, items, height: Math.ceil(bottom + 16 * u + 8) };
  }

  function restTransform(j) { return `rotateY(${REST_Y[j % REST_Y.length]}deg) rotateZ(${REST_Z[j % REST_Z.length]}deg)`; }

  function faces(p, opts) {
    const bars = barcode(hash(p.id + ":" + p.name));
    const digits = String(p.id || "").replace(/\D/g, "").padStart(3, "0");
    const mine = !!opts.me && p.id === opts.me;
    const stat = !p.inTime ? `<div class="ly-stat">${opts.today ? "Not in yet" : "No check-in"}</div>`
      : p.outTime ? `<div class="ly-stat out">Out ${esc(hhmm(p.outTime))}</div>`
        : `<div class="ly-stat on">In ${esc(hhmm(p.inTime))}</div>`;
    const front = `<span class="ly-face front">
      <span class="ly-band" style="height:4.3em"></span><span class="ly-hole"></span>
      <span class="ly-photo"><span>${esc(initials(p.name))}<img src="avatars/${esc(p.id)}.jpg" alt="" draggable="false" onerror="this.remove()"></span><i class="ly-dot ${p.working ? "on" : ""}"></i></span>
      <span class="ly-name">${esc(p.name)}</span>
      <span class="ly-role">${esc(p.role || "")}</span>
      ${stat}
      <span class="ly-bar"><svg viewBox="0 0 ${bars.width} 20" preserveAspectRatio="none" aria-hidden="true"><path d="${bars.d}" fill="currentColor"/></svg><div>MMGA · ${esc(digits)}</div></span>
      <span class="ly-gloss"></span>
    </span>`;

    const map = (u) => u ? `<a href="${esc(u)}" target="_blank" rel="noopener" data-link="1">map</a>` : "";
    let acts = "";
    if (mine && opts.today) {
      const b = (kind, label, cls) => `<span role="button" tabindex="0" class="ly-btn ${cls || ""}" data-act="${kind}">${label}</span>`;
      if (!p.inTime) acts = b("checkin", "Check In", "green");
      else if (!p.outTime) acts = b("checkout", "Check Out") + (p.inMapMissing ? b("set-checkin", "Add check-in location", "ghost") : "");
      else acts = b("checkout", "Check Out again") + b("undo", "Undo checkout", "ghost") + (p.outMapMissing ? b("set-checkout", "Add check-out location", "ghost") : "");
    }
    const outTxt = p.outTime ? esc(hhmm(p.outTime)) + map(p.outMap) : (p.inTime && opts.today ? "Working" : "—");
    const back = `<span class="ly-face back">
      <span class="ly-band" style="height:2.15em"></span><span class="ly-hole"></span>
      <span class="ly-back"><b>${esc(p.name)}</b><small>${esc(p.role || "")}</small>
        <span class="ly-rows" style="display:flex">
          <div><span>In</span><em>${p.inTime ? esc(hhmm(p.inTime)) + map(p.inMap) : "—"}</em></div>
          <div><span>Out</span><em>${outTxt}</em></div>
          <div><span>Total</span><em>${esc(p.total || "—")}</em></div>
        </span>
        ${p.mood ? `<span class="ly-mood" style="display:block" data-mood="1">${MOODS[p.mood] || ""} ${esc(p.mood)}${p.moodReason ? " (tap)" : ""}</span>` : ""}
      </span>
      ${acts ? `<span class="ly-acts">${acts}</span>` : `<span class="ly-hint">${mine ? "Viewing a past date" : "Tap to turn back"}</span>`}
      <span class="ly-gloss"></span>
    </span>`;
    return front + back;
  }

  function render(container, people, opts) {
    opts = opts || {};
    injectCss();
    // The signed-in person's badge goes in the middle of the row, so it can be
    // scrolled to the centre of the screen.
    people = people.slice();
    const meIdx = opts.me ? people.findIndex(p => p.id === opts.me) : -1;
    if (meIdx >= 0) { const [me] = people.splice(meIdx, 1); people.splice(Math.floor(people.length / 2), 0, me); }
    const n = Math.max(1, people.length);
    const width = Math.max(300, container.clientWidth || 900);
    // 4 badges fit on screen (2 on a phone); the rest scroll sideways.
    const visible = Math.min(n, width < 640 ? 2 : 4);
    const pitch = (width - 2) / visible;
    const W = Math.round(Math.max(104, Math.min(168, pitch / 1.4)) * 4) / 4;
    const innerW = Math.max(width - 2, pitch * n);
    const g = geometry(W, people.length);
    const start = innerW / 2 - (pitch * (people.length - 1)) / 2;
    const base = W / 11.2;

    if (state.flipped && !people.some(p => p.id === state.flipped)) state.flipped = null;

    let html = `<div class="ly-inner" style="width:${innerW}px;height:${g.height}px">
      <div class="ly-rail" style="top:${g.railY - g.railR}px;height:${g.railR * 2}px"></div>
      <div class="ly-cap" style="left:6px;top:${g.railY - 14}px"></div><div class="ly-cap" style="right:6px;top:${g.railY - 14}px"></div>`;
    people.forEach((p, i) => {
      const it = g.items[i], tone = TONES[i % TONES.length];
      const cardOffset = it.cardTop - it.clipTop;
      const flipped = state.flipped === p.id;
      const mine = opts.me && p.id === opts.me && opts.today;
      html += `<div class="ly-item" style="left:${start + i * pitch - W / 2}px;width:${W}px;height:${g.height}px;--ly-tone:${tone};z-index:${flipped ? 40 : 10 + i}">
        <span class="ly-ring" style="left:${W / 2 - 4.5 * g.u}px;top:${g.railY - g.ring}px;width:${9 * g.u}px;height:${g.ring * 2}px"></span>
        <div class="ly-swing" style="transform-origin:${W / 2}px ${g.pivotY}px">
          <span class="ly-strap" style="left:${W / 2 - g.strapW / 2}px;top:${g.pivotY - 2}px;width:${g.strapW}px;height:${it.strap + 2}px"></span>
          <span class="ly-crimp" style="left:${W / 2 - g.strapW / 2 - 1.5}px;top:${g.pivotY + it.strap - g.crimp * 0.4}px;width:${g.strapW + 3}px;height:${g.crimp}px"></span>
          <span class="ly-reel" style="left:${W / 2 - g.reel / 2}px;top:${it.reelTop}px;width:${g.reel}px;height:${g.reel}px"></span>
          <span class="ly-cord" style="left:${W / 2 - 0.75}px;top:${it.reelTop + g.reel * 0.82}px;height:${g.cordRest}px"></span>
          <div class="ly-hang" style="position:absolute;left:0;top:${it.clipTop}px;width:${W}px;height:${cardOffset + g.H}px;transform-origin:${W / 2}px 0">
            <span class="ly-shadow" style="left:0;top:${cardOffset}px;width:${W}px;height:${g.H}px"></span>
            <button type="button" class="ly-card" data-i="${i}" aria-pressed="${flipped}" aria-label="${esc(p.name)}, ${esc(p.role || "")}"
              style="left:0;top:${cardOffset}px;width:${W}px;height:${g.H}px;font-size:${base}px">
              <span class="ly-flip" style="transform:${flipped ? "rotateY(180deg) rotateZ(0deg)" : restTransform(i)}">${faces(p, opts)}</span>
            </button>
            <span class="ly-clipstem" style="left:${W / 2 - g.clipW * 0.21}px;top:${g.clipH * 0.7}px;width:${g.clipW * 0.42}px;height:${cardOffset - g.clipH * 0.7 + base}px"></span>
            <span class="ly-clip" style="left:${W / 2 - g.clipW / 2}px;top:0;width:${g.clipW}px;height:${g.clipH}px"></span>
          </div>
        </div>
        ${mine && !flipped ? `<span class="ly-tapme" style="font-size:${base}px;top:${g.railY - g.ring - 4}px;transform:translate(-50%,-100%)">${!p.inTime ? "Tap to check in" : (!p.outTime ? "Tap to check out" : "You")}</span>` : ""}
      </div>`;
    });
    html += `</div>`;

    const oldStage = container.querySelector(".ly-stage");
    const scroll = oldStage ? oldStage.scrollLeft : 0;
    container.innerHTML = `<div class="ly-stage">${html}</div><div class="ly-caption"><p id="ly-cap1"></p><small id="ly-cap2"></small></div>`;
    const stage = container.querySelector(".ly-stage");
    const myPos = opts.me ? people.findIndex(p => p.id === opts.me) : -1;
    if (!state.centered && myPos >= 0) {
      state.centered = true;
      stage.scrollLeft = Math.max(0, start + myPos * pitch - stage.clientWidth / 2);
    } else {
      stage.scrollLeft = scroll;
    }

    const items = Array.from(stage.querySelectorAll(".ly-item"));
    state.sim = items.map((el, i) => {
      const old = state.sim[i] || {};
      return { el, swing: el.querySelector(".ly-swing"), hang: el.querySelector(".ly-hang"),
        a: old.a || 0, v: old.v || 0, k: 28 * (g.items[0].pend / g.items[i].pend) };
    });
    state.people = people;
    state.opts = opts;
    caption();

    stage.querySelectorAll(".ly-card").forEach((card) => {
      const i = Number(card.dataset.i);
      const handle = (ev) => {
        if (ev.target.closest("[data-link]")) return; // map link opens normally
        const act = ev.target.closest("[data-act]");
        if (act) {
          ev.preventDefault(); ev.stopPropagation();
          if (act.getAttribute("aria-disabled") === "true") return;
          act.setAttribute("aria-disabled", "true"); act.style.opacity = ".6";
          if (opts.onAction) opts.onAction(act.dataset.act);
          // re-enable if the location popup was cancelled (a real action re-renders anyway)
          setTimeout(() => { if (act.isConnected) { act.removeAttribute("aria-disabled"); act.style.opacity = ""; } }, 2500);
          return;
        }
        const mood = ev.target.closest("[data-mood]");
        if (mood && people[i].moodReason && typeof window.showMoodReason === "function") {
          ev.stopPropagation();
          mood.setAttribute("data-name", people[i].name); mood.setAttribute("data-mood", people[i].mood); mood.setAttribute("data-reason", people[i].moodReason);
          window.showMoodReason(mood, ev);
          return;
        }
        flip(i);
      };
      card.addEventListener("click", handle);
      card.addEventListener("keydown", (ev) => {
        if ((ev.key === "Enter" || ev.key === " ") && ev.target.closest("[data-act]")) { ev.preventDefault(); handle(ev); }
      });
    });
    stage.onmousemove = (ev) => hover(ev);
    stage.onmouseleave = () => { state.hoverHit = -1; };
    // Scrolling the rail (finger swipe on a phone, scrollbar/trackpad on a
    // laptop) makes the badges swing against the direction of travel.
    let lastScroll = stage.scrollLeft;
    stage.addEventListener("scroll", () => {
      const dx = stage.scrollLeft - lastScroll;
      lastScroll = stage.scrollLeft;
      if (!dx) return;
      const amt = Math.max(-7, Math.min(7, dx * 0.35));
      state.sim.forEach((_, i) => impulse(i, amt * (0.8 + (i % 3) * 0.15)));
    }, { passive: true });

    if (!state.keyBound) {
      state.keyBound = true;
      document.addEventListener("keydown", (e) => {
        if (e.key !== "Escape" || !state.flipped) return;
        const i = state.people.findIndex(p => p.id === state.flipped);
        if (i >= 0) flip(i);
      });
    }

    if (!state.introDone) {
      state.introDone = true;
      if (!reduced()) items.forEach((_, i) => setTimeout(() => impulse(i, (i % 2 ? -1 : 1) * (30 - (i % 4) * 3)), 60 + i * 70));
    }
  }

  function caption() {
    const c1 = document.getElementById("ly-cap1"), c2 = document.getElementById("ly-cap2");
    if (!c1) return;
    const p = state.people.find(x => x.id === state.flipped);
    const mine = p && p.id === state.opts.me;
    if (p) {
      c1.textContent = `${p.name} — ${p.role || ""}`;
      c2.textContent = mine && state.opts.today ? "Use the button on your badge, or tap again to turn it back" : "Tap again or press Esc to turn it back";
    } else {
      c1.textContent = state.opts.me && state.opts.today ? "Tap your badge to check in or out" : "Tap a badge to see their times";
      c2.textContent = "Tap anyone's badge to see when they came in and left";
    }
  }

  function flip(i) {
    const p = state.people[i];
    state.flipped = state.flipped === p.id ? null : p.id;
    state.sim.forEach((s, j) => {
      const on = state.people[j].id === state.flipped;
      s.el.querySelector(".ly-flip").style.transform = on ? "rotateY(180deg) rotateZ(0deg)" : restTransform(j);
      s.el.style.zIndex = on ? 40 : 10 + j;
      s.el.querySelector(".ly-card").setAttribute("aria-pressed", on);
      const tap = s.el.querySelector(".ly-tapme");
      if (tap) tap.style.display = on ? "none" : "";
    });
    impulse(i, i % 2 ? 9 : -9);
    if (i > 0) impulse(i - 1, 6);
    if (i < state.sim.length - 1) impulse(i + 1, -6);
    caption();
  }

  function hover(ev) {
    const now = performance.now();
    const vx = (ev.clientX - state.hoverX) / Math.max(1, now - state.hoverT);
    state.hoverX = ev.clientX; state.hoverT = now;
    let hit = -1;
    state.sim.forEach((s, i) => {
      const r = s.el.querySelector(".ly-card").getBoundingClientRect();
      if (ev.clientX >= r.left - 4 && ev.clientX <= r.right + 4 && ev.clientY >= r.top - 40 && ev.clientY <= r.bottom) hit = i;
    });
    if (hit >= 0 && hit !== state.hoverHit) impulse(hit, Math.max(-24, Math.min(24, -vx * 22)));
    state.hoverHit = hit;
  }

  function impulse(i, amt) {
    const s = state.sim[i];
    if (!s || reduced()) return;
    s.v += amt;
    if (!state.raf) { state.last = 0; state.raf = requestAnimationFrame(step); }
  }

  function step(now) {
    const dt = state.last ? Math.min(0.05, (now - state.last) / 1000) : 1 / 60;
    state.last = now;
    const sub = 4, h = dt / sub;
    let busy = false;
    state.sim.forEach((s) => {
      for (let k = 0; k < sub; k++) {
        s.v += (-s.k * s.a - 2.2 * s.v) * h;
        s.a = Math.max(-18, Math.min(18, s.a + s.v * h));
      }
      if (!s.el.isConnected) return;
      s.swing.style.transform = `rotate(${s.a.toFixed(3)}deg)`;
      s.hang.style.transform = `rotate(${(-s.a * 0.35).toFixed(3)}deg)`;
      if (Math.abs(s.a) > 0.02 || Math.abs(s.v) > 0.05) busy = true;
      else { s.a = 0; s.v = 0; }
    });
    state.raf = busy ? requestAnimationFrame(step) : 0;
  }

  window.LanyardRail = { render };
})();
