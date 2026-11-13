/**
 * mood.js -- once a day, after checking in, every person picks how they feel
 * (happy / mid / sad). A card sits in the bottom-right corner of every Lumina
 * page until they answer; it has no close button. Loaded by auth.js.
 * Never blocks the page: failures are silent.
 */
(function () {
  const API = (location.hostname === "localhost" || location.hostname === "127.0.0.1")
    ? "http://localhost:5000" : location.origin;
  const POLL_MS = 45 * 1000;
  let card = null;
  let timer = null;

  function build() {
    const css = document.createElement("style");
    css.textContent = `
      #mood-card{position:fixed;right:16px;bottom:16px;z-index:29500;width:min(350px,calc(100vw - 32px));
        border-radius:12px;padding:14px;background:var(--s1,#17171c);color:var(--txt,var(--text,#ececf1));
        border:1px solid var(--bdr,var(--border,#2c2c35));border-left:3px solid var(--acc,var(--accent,#f97316));
        box-shadow:0 10px 34px rgba(0,0,0,.45);font-family:inherit}
      #mood-card .m-kick{font-size:11px;letter-spacing:.07em;text-transform:uppercase;opacity:.6;margin-bottom:5px}
      #mood-card .m-title{font-size:14px;font-weight:600}
      #mood-card .m-row{display:flex;gap:6px;margin-top:12px}
      #mood-card button{width:100%;font-size:24px;line-height:1;padding:8px 0;border-radius:10px;cursor:pointer;
        background:transparent;border:1px solid var(--bdr,var(--border,#2c2c35))}
      #mood-card button:hover{border-color:var(--acc,var(--accent,#f97316))}
      #mood-card button:disabled{opacity:.5;cursor:default}
      #mood-card .m-lab{font-size:10px;opacity:.65;margin-top:4px}
      #mood-card .m-why{width:100%;margin-top:12px;padding:9px 11px;border-radius:10px;font:inherit;font-size:13px;
        background:transparent;color:inherit;border:1px solid var(--bdr,var(--border,#2c2c35));outline:none}
      #mood-card .m-why:focus{border-color:var(--acc,var(--accent,#f97316))}
      #mood-card .m-hint{font-size:11px;opacity:.55;margin-top:6px}`;
    document.head.appendChild(css);
    card = document.createElement("div");
    card.id = "mood-card";
    card.innerHTML = `
      <div class="m-kick">Daily check-in</div>
      <div class="m-title">How are you feeling today?</div>
      <div class="m-row">${[["awesome","🤩","Awesome"],["happy","😊","Happy"],["mid","😐","Mid"],["sad","😞","Sad"],["stressed","😫","Stressed"]]
        .map(([k, e, l]) => `<div style="flex:1;text-align:center"><button type="button" data-m="${k}">${e}</button><div class="m-lab">${l}</div></div>`).join("")}
      </div>
      <input class="m-why" id="mood-why" type="text" maxlength="300" autocomplete="off"
        placeholder="Why? (optional, type a short reason)">
      <div class="m-hint">Type a reason first if you like, then tap how you feel.</div>`;
    card.querySelectorAll("button").forEach(b =>
      b.addEventListener("click", () => answer(b.getAttribute("data-m"))));
    document.body.appendChild(card);
  }

  function hide() {
    if (card) { card.remove(); card = null; }
    if (timer) { clearInterval(timer); timer = null; }
  }

  async function answer(mood) {
    if (card) card.querySelectorAll("button").forEach(b => { b.disabled = true; });
    const whyEl = document.getElementById("mood-why");
    const reason = whyEl ? whyEl.value.trim() : "";
    try {
      const r = await fetch(`${API}/api/mood`, {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mood, reason }),
      });
      if (r.ok) {
        hide();
        if (typeof window.loadAttendance === "function") window.loadAttendance();
        return;
      }
    } catch (e) { /* let them try again */ }
    if (card) card.querySelectorAll("button").forEach(b => { b.disabled = false; });
  }

  async function poll() {
    if (document.visibilityState === "hidden") return;
    try {
      const r = await fetch(`${API}/api/mood/today`, { credentials: "include" });
      if (!r.ok) return;
      const d = await r.json();
      if (d.mood) { hide(); return; }          // answered (maybe on another page)
      if (d.needs_mood && !card) build();
    } catch (e) {
      console.warn("[mood] check failed:", e.message);
    }
  }

  function start() { poll(); timer = setInterval(poll, POLL_MS); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
