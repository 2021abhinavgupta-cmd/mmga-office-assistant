/**
 * whatsnew.js -- the "What's new in Lumina" popup. Loaded by auth.js on every
 * protected page right after sign-in. Shows releases this person has not
 * dismissed yet (backend/releases.py); dismissing records it server-side so it
 * never shows again for them. Never blocks the page: any failure is silent.
 */
(function () {
  const API = (location.hostname === "localhost" || location.hostname === "127.0.0.1")
    ? "http://localhost:5000" : location.origin;
  const THROTTLE_MS = 2 * 60 * 1000;   // don't re-ask on every click-through between pages

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function fmtDate(d) {
    try {
      const dt = new Date(d + "T00:00:00");
      return dt.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
    } catch (e) { return d || ""; }
  }

  function show(releases, history) {
    if (!releases.length || document.getElementById("whatsnew-overlay")) return;
    const css = document.createElement("style");
    css.textContent = `
      #whatsnew-overlay{position:fixed;inset:0;z-index:30000;display:flex;align-items:center;justify-content:center;
        padding:16px;background:rgba(0,0,0,.55);font-family:inherit}
      #whatsnew-box{width:100%;max-width:460px;max-height:86vh;overflow:auto;border-radius:14px;padding:22px 22px 18px;
        background:var(--s1,#17171c);color:var(--txt,var(--text,#ececf1));border:1px solid var(--bdr,var(--border,#2c2c35));
        box-shadow:0 18px 60px rgba(0,0,0,.5)}
      #whatsnew-box .wn-kicker{font-size:12px;letter-spacing:.08em;text-transform:uppercase;opacity:.6;margin-bottom:4px}
      #whatsnew-box h2{margin:0 0 14px;font-size:20px;font-weight:650}
      #whatsnew-box .wn-rel{padding:12px 0;border-top:1px solid var(--bdr,var(--border,#2c2c35))}
      #whatsnew-box .wn-rel:first-of-type{border-top:0;padding-top:0}
      #whatsnew-box .wn-title{font-weight:600;font-size:15px}
      #whatsnew-box .wn-date{font-size:12px;opacity:.55;margin:2px 0 6px}
      #whatsnew-box ul{margin:6px 0 0;padding-left:18px}
      #whatsnew-box li{margin:5px 0;line-height:1.45;font-size:14px}
      #whatsnew-box .wn-how{margin-top:10px;padding:9px 11px;border-radius:8px;font-size:13px;line-height:1.45;
        background:rgba(249,115,22,.1);border:1px solid rgba(249,115,22,.25)}
      #whatsnew-box .wn-how b{display:block;font-size:12px;letter-spacing:.04em;text-transform:uppercase;opacity:.75;margin-bottom:3px}
      #whatsnew-box button{margin-top:16px;width:100%;padding:10px 14px;border:0;border-radius:9px;cursor:pointer;
        font-size:14px;font-weight:600;background:var(--acc,var(--accent,#f97316));color:#fff}
    `;
    document.head.appendChild(css);

    const ov = document.createElement("div");
    ov.id = "whatsnew-overlay";
    ov.setAttribute("role", "dialog");
    ov.setAttribute("aria-modal", "true");
    ov.innerHTML = `<div id="whatsnew-box">
      <div class="wn-kicker">Lumina</div>
      <h2>${history ? "What's new: all updates" : "What's new"}</h2>
      ${releases.map(r => `<div class="wn-rel">
          <div class="wn-title">${esc(r.title)}${history && r.new ? ' <span style="font-size:11px;color:var(--acc,#f97316)">NEW</span>' : ""}</div>
          <div class="wn-date">${esc(fmtDate(r.date))}</div>
          <ul>${(r.bullets || []).map(b => `<li>${esc(b)}</li>`).join("")}</ul>
          ${r.how ? `<div class="wn-how"><b>How to use it</b>${esc(r.how)}</div>` : ""}
        </div>`).join("")}
      <button type="button" id="whatsnew-ok">${history ? "Close" : "Got it"}</button>
    </div>`;
    document.body.appendChild(ov);

    let closed = false;
    function close() {
      if (closed) return;
      closed = true;
      ov.remove();
      document.removeEventListener("keydown", onKey);
      if (!history) fetch(`${API}/api/whatsnew/seen`, {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: releases.map(r => r.id) }),
      }).catch(() => {});
    }
    function onKey(e) { if (e.key === "Escape") close(); }
    document.addEventListener("keydown", onKey);
    document.getElementById("whatsnew-ok").addEventListener("click", close);
    ov.addEventListener("click", e => { if (e.target === ov) close(); });
  }

  async function check() {
    try {
      try {
        const last = Number(sessionStorage.getItem("whatsnew_checked") || 0);
        if (Date.now() - last < THROTTLE_MS) return;
        sessionStorage.setItem("whatsnew_checked", String(Date.now()));
      } catch (e) { /* storage blocked: just check every time */ }
      const r = await fetch(`${API}/api/whatsnew`, { credentials: "include" });
      if (!r.ok) return;
      const d = await r.json();
      show(d.releases || []);
    } catch (e) {
      console.warn("[whatsnew] check failed:", e.message);
    }
  }

  // dashboard "i" button: every past update, newest first
  window.openWhatsNewHistory = async function () {
    try {
      const r = await fetch(`${API}/api/whatsnew/all`, { credentials: "include" });
      const d = r.ok ? await r.json() : { releases: [] };
      if (!(d.releases || []).length) { alert("No updates yet."); return; }
      show(d.releases, true);
      fetch(`${API}/api/whatsnew/seen`, {
        method: "POST", credentials: "include", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: d.releases.filter(x => x.new).map(x => x.id).slice(0, 20) }),
      }).catch(() => {});
    } catch (e) { console.warn("[whatsnew] history failed:", e.message); }
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", check);
  else check();
})();
