/**
 * auth.js — Shared auth guard. Include in every protected page.
 * Usage: <script src="auth.js"></script>
 * The script auto-runs on load and redirects to login.html if session is invalid.
 */
function getAuthApiBase() {
  return location.hostname === "localhost" || location.hostname === "127.0.0.1"
    ? "http://localhost:5000"
    : location.origin;
}

// Hide immediately to prevent flash of protected content while auth check is in flight
document.documentElement.style.visibility = 'hidden';

(async function authGuard() {
  const authApi = getAuthApiBase();

  try {
    const res = await fetch(`${authApi}/api/auth/verify`, {
      credentials: "include"
    });
    const data = await res.json();

    if (!data.valid) {
      localStorage.removeItem("agency_portal_user");
      window.location.href = "login.html";
      return;
    }

    // Refresh stored user data from server truth
    const user = {
      user_id:  data.user_id,
      name:     data.name,
      role:     data.role,
      is_admin: data.is_admin,
    };
    localStorage.setItem("agency_portal_user", JSON.stringify(user));
    window.__currentUser = user;

    // Daily-standup lock: on a weekday (and unless the person is on
    // recorded leave), every employee page except the Standup screen
    // itself redirects there until today's attendance check-in is done
    // AND today's standup has at least one task (see
    // /api/standup/lock-status, CLAUDE.md gotchas #108/#119). Frontend-only
    // nudge, not a security boundary -- every API still works regardless.
    // If the check itself fails (network hiccup), fail open rather than
    // trap someone out of the whole app.
    const __page = location.pathname.split("/").pop() || "index.html";
    if (__page !== "standup.html" && __page !== "checkin.html") {
      try {
        const lr = await fetch(
          `${authApi}/api/standup/lock-status?user_id=${encodeURIComponent(user.user_id)}`,
          { credentials: "include" }
        );
        const ld = await lr.json();
        if (ld && ld.locked) {
          const need = [];
          if (ld.needs_checkin) need.push("checkin");
          if (ld.needs_standup) need.push("standup");
          window.location.href =
            "standup.html?locked=1" + (need.length ? "&need=" + need.join(",") : "");
          return;
        }
      } catch (e) {
        console.warn("[auth.js] lock-status check failed, not blocking:", e.message);
      }
    }

    // Auth passed — reveal the page
    document.documentElement.style.visibility = 'visible';

    // "What's new" popup: shows once per release per person (backend/releases.py).
    // Loaded as its own script so a problem in it can never affect sign-in.
    if (__page !== "checkin.html") {
      const wn = document.createElement("script");
      wn.src = "whatsnew.js";
      document.head.appendChild(wn);
      // Tasks the bot found in the team group: Add / Reject popup, bottom-right.
      const gp = document.createElement("script");
      gp.src = "groupprops.js";
      document.head.appendChild(gp);
      // Daily mood check-in card (happy / mid / sad), required once a day after check-in.
      const md = document.createElement("script");
      md.src = "mood.js";
      document.head.appendChild(md);
    }

    // Presence heartbeat: immediate + every 60s. Real checkout is inferred
    // server-side from staleness of this timestamp (see task_scheduler.py's
    // attendance_presence_sweep job) -- NOT from a pagehide/beforeunload
    // event. An unload-event approach was tried once (see CLAUDE.md gotcha
    // #70/#71) and reverted: pagehide fires identically on a real tab close
    // and on clicking a link to another Lumina page (separate .html files,
    // not an SPA), so it wrote a checkout on every internal navigation --
    // the resulting write volume was implicated in a production DB hang.
    // Heartbeat-only avoids firing anything on navigation at all.
    sendAttendancePing(authApi, user.user_id);
    setInterval(() => sendAttendancePing(authApi, user.user_id), 60000);

  } catch (e) {
    // Network error — reveal page then redirect to login
    document.documentElement.style.visibility = 'visible';
    console.warn("[auth.js] Network error during verify — redirecting to login:", e.message);
    window.location.href = "login.html?error=network";
  }
})();

/**
 * Best-effort browser geolocation, used for the HR-visible location tag on
 * check-in/checkout (CLAUDE.md gotcha, 2026-09-28). Resolves {lat, lng} or
 * null on any denial/timeout/unsupported browser -- NEVER throws, and the
 * caller must never let this block or fail the actual check-in/checkout.
 */
function getAttendanceLocation(timeoutMs = 4000) {
  return new Promise((resolve) => {
    if (!navigator.geolocation) return resolve(null);
    let settled = false;
    const done = (v) => { if (!settled) { settled = true; resolve(v); } };
    const t = setTimeout(() => done(null), timeoutMs);
    navigator.geolocation.getCurrentPosition(
      (pos) => { clearTimeout(t); done({ lat: pos.coords.latitude, lng: pos.coords.longitude }); },
      () => { clearTimeout(t); done(null); },
      { timeout: timeoutMs, maximumAge: 120000 }
    );
  });
}
window.getAttendanceLocation = getAttendanceLocation;

function sendAttendancePing(authApi, userId) {
  fetch(`${authApi}/api/attendance/ping`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: userId }),
  }).catch(() => {});
}

/**
 * Logout helper — call window.authLogout() from any page.
 */
window.authLogout = async function () {
  const authApi = getAuthApiBase();
  const token = sessionStorage.getItem("_session_token") || localStorage.getItem("_session_token");
  const user = JSON.parse(localStorage.getItem("agency_portal_user") || "{}");
  if (user.user_id) {
    const loc = await getAttendanceLocation().catch(() => null);
    await fetch(`${authApi}/api/attendance/checkout`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: user.user_id, ...(loc || {}) }),
      keepalive: true,
    }).catch(() => {});
  }
    await fetch(`${authApi}/api/auth/logout`, {
      method: "POST",
      headers: { 
        "Content-Type": "application/json"
      },
      credentials: "include",
      body: JSON.stringify({}),
    }).catch(() => {});
  sessionStorage.removeItem("_session_token");
  localStorage.removeItem("agency_portal_user");
  window.location.href = "login.html";
};
