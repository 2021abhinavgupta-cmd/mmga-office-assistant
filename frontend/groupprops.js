/**
 * groupprops.js -- tasks the bot found in the team WhatsApp group are proposed
 * here instead of being added by themselves. A persistent card per proposal sits
 * in the bottom-right corner of every Lumina page until the person taps Add (it
 * goes on today's Daily Standup) or Reject; the card then disappears.
 * Loaded by auth.js after sign-in. Never blocks the page: failures are silent.
 */
(function () {
  const API = (location.hostname === "localhost" || location.hostname === "127.0.0.1")
    ? "http://localhost:5000" : location.origin;
  const POLL_MS = 60 * 1000;
  const cards = new Map();   // proposal id -> element
  let box = null;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function ensureBox() {
    if (box) return box;
    const css = document.createElement("style");
    css.textContent = `
      #gp-box{position:fixed;right:16px;bottom:16px;z-index:29000;display:flex;flex-direction:column;gap:10px;
        width:min(340px,calc(100vw - 32px));max-height:80vh;overflow:auto;font-family:inherit}
      .gp-card{border-radius:12px;padding:14px;background:var(--s1,#17171c);color:var(--txt,var(--text,#ececf1));
        border:1px solid var(--bdr,var(--border,#2c2c35));border-left:3px solid var(--acc,var(--accent,#f97316));
        box-shadow:0 10px 34px rgba(0,0,0,.45)}
      .gp-kick{font-size:11px;letter-spacing:.07em;text-transform:uppercase;opacity:.6;margin-bottom:5px}
      .gp-title{font-size:14px;font-weight:600;line-height:1.4;word-break:break-word}
      .gp-meta{font-size:12px;opacity:.7;margin-top:5px;line-height:1.4}
      .gp-quote{font-size:12px;opacity:.6;margin-top:5px;font-style:italic;line-height:1.4;word-break:break-word}
      .gp-row{display:flex;gap:8px;margin-top:12px}
      .gp-row button{flex:1;padding:8px 10px;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;border:1px solid transparent}
      .gp-add{background:var(--acc,var(--accent,#f97316));color:#fff}
      .gp-no{background:transparent;color:inherit;border-color:var(--bdr,var(--border,#2c2c35))!important}
      .gp-row button:disabled{opacity:.55;cursor:default}`;
    document.head.appendChild(css);
    box = document.createElement("div");
    box.id = "gp-box";
    document.body.appendChild(box);
    return box;
  }

  function drop(id) {
    const el = cards.get(id);
    if (el) el.remove();
    cards.delete(id);
    if (box && !cards.size) { box.remove(); box = null; }
  }

  async function answer(id, action, el) {
    el.querySelectorAll("button").forEach(b => { b.disabled = true; });
    try {
      const r = await fetch(`${API}/api/group-proposals/${id}/${action}`, { method: "POST", credentials: "include" });
      // 409 = already answered somewhere else: the card should go either way
      if (r.ok || r.status === 409 || r.status === 404) {
        drop(id);
        if (action === "accept" && r.ok && typeof window.loadMyTasks === "function") window.loadMyTasks();
        return;
      }
    } catch (e) { /* fall through: let them try again */ }
    el.querySelectorAll("button").forEach(b => { b.disabled = false; });
  }

  function addCard(p) {
    if (cards.has(p.id)) return;
    const el = document.createElement("div");
    el.className = "gp-card";
    el.innerHTML = `
      <div class="gp-kick">Task from the group</div>
      <div class="gp-title">${esc(p.title)}</div>
      <div class="gp-meta">${p.requester ? "Asked by " + esc(p.requester) : "Asked in the team group"}${p.due ? " &middot; due " + esc(p.due) : ""}</div>
      ${p.quote ? `<div class="gp-quote">&ldquo;${esc(p.quote)}&rdquo;</div>` : ""}
      <div class="gp-row">
        <button type="button" class="gp-add">Add to my standup</button>
        <button type="button" class="gp-no">Reject</button>
      </div>`;
    el.querySelector(".gp-add").addEventListener("click", () => answer(p.id, "accept", el));
    el.querySelector(".gp-no").addEventListener("click", () => answer(p.id, "reject", el));
    ensureBox().appendChild(el);
    cards.set(p.id, el);
  }

  async function poll() {
    if (document.visibilityState === "hidden") return;
    try {
      const r = await fetch(`${API}/api/group-proposals`, { credentials: "include" });
      if (!r.ok) return;
      const list = (await r.json()).proposals || [];
      const live = new Set(list.map(p => p.id));
      for (const id of Array.from(cards.keys())) if (!live.has(id)) drop(id);   // answered elsewhere
      list.forEach(addCard);
    } catch (e) {
      console.warn("[groupprops] check failed:", e.message);
    }
  }

  function start() { poll(); setInterval(poll, POLL_MS); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
