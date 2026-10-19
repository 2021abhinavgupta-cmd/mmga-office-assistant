#!/usr/bin/env python3
"""
laptop_agent.py — one always-on process that gets the most out of a spare
machine sitting next to Lumina.

Jobs it runs (each skips itself automatically if not configured):

  1. knowledge sync    — mirror your knowledge folder up to Lumina        (30s)
  2. research feed      — refresh research_sources.txt web snapshots       (1h)
  3. DB backup          — pull logs/app.db off Railway to local disk       (24h)
                          + copy it offsite (mirror dir and/or rclone)
                          + weekly: pull the uploads/ archive too
  4. health check       — ping Lumina, time it, log it; alert on down/slow (5m)
  5. WhatsApp bridge    — keep whatsapp-bridge/index.js (Baileys) alive,
                          + a health watchdog (3m) that alerts loudly if the
                          session logs out / crash-loops / is offline >8m
  5b. self-update       — git pull scripts/ + whatsapp-bridge/ from
                          origin/main (ff-only) and restart what changed (10m)
                          -- a scripts-only change restarts this process but
                          deliberately leaves the bridge child running; the
                          new process adopts it via /health instead of
                          spawning a duplicate, so WhatsApp stays connected
  6. daily brief        — 09:00 & 19:00: fetch /api/companion/digest, send it
  7. sheets watchdog    — check Google-Sheet sync health, alert; optional  (20m)
                          auto-pull with --sheets-autopull
  8. attendance roll-call — 12:00: post who hasn't logged in yet to the
                          team WhatsApp group (needs --rollcall-group + bridge)
  9. standup nudge       — 11:30: DM anyone who hasn't added a task to today's
                          standup ("reply here and I'll add it") (needs bridge)
     attendance nag      — 10:30: DM anyone not checked in yet (needs bridge)
 10. WhatsApp outbox     — deliver queued proactive DMs (task-assigned nudges,
                          "remind X to do Y") via the local bridge            (20s)
 11. lunch nudge         — 14:00: post a "go for lunch" message to the team
                          WhatsApp group (needs --rollcall-group + bridge)
 12. eod summary         — 17:00 group "tasks done today" + 19:30 personal
                          "still open" DMs + leads report (needs bridge)
 13. weekly wrap         — Friday 18:00: per-person completed count for the
                          week, to the group (needs --rollcall-group + bridge)
 14. tomorrow-live       — 09:30 & 18:30: DM the content-calendar lead (default
                          Vidit) everything due to go live tomorrow, read
                          straight from Lumina Sheets/Notion (needs bridge)
 15. class-meeting        — Tue/Thu 10:28: post the Meet link to the group,
                          @-tagging everyone in it (needs --rollcall-group + bridge)

Setup:
    Full first-time setup for a new always-on laptop: see
    ../ALWAYS_ON_LAPTOP.md at the repo root -- that's the one doc to hand
    someone (or a Claude session on that machine) for onboarding. This
    docstring stays the up-to-date reference for the job list/flags/env
    vars once it's already running.

    pip install -r scripts/requirements.txt
    set LUMINA_URL=https://lumina.mmga.agency
    set STORAGE_SYNC_TOKEN=<token you set on Railway>       (jobs 1, 6, 7, 8)
    set FLASK_SECRET_KEY=<Railway FLASK_SECRET_KEY>          (job 3)
    set ALERT_TARGETS=tgram://token/chatid,mailto://...      (job 4 alerts; apprise URLs)
    set DIGEST_TARGETS=tgram://token/chatid                  (job 6; falls back to ALERT_TARGETS)
    set ALERT_WEBHOOK=<Slack/Discord webhook>                (fallback if no *_TARGETS)
    set BACKUP_RCLONE_REMOTE=myremote:lumina-backups         (job 3 offsite, optional)
    set WHATSAPP_BRIDGE_TOKEN=<token you set on Railway>      (job 5)
    set ROLLCALL_GROUP_ID=<group id>                         (job 8; or --rollcall-group)

Run:
    python scripts/laptop_agent.py --dir "C:\\Users\\me\\LuminaKnowledge"

Autostart: Task Scheduler → "At log on" →
    pythonw.exe <path>\\scripts\\laptop_agent.py --dir "C:\\Users\\me\\LuminaKnowledge"
"""

from __future__ import annotations

import os
import re
import sys
import json
import time
import shutil
import argparse
import subprocess
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import requests
except ImportError:
    sys.exit("laptop_agent: `pip install requests` first.")

try:
    import apprise  # 100+ notification services in one lib; optional
except ImportError:
    apprise = None

import kb_sync
import research_feed

DEFAULT_URL = "https://lumina.mmga.agency"
BACKUP_KEEP = 14
UPLOADS_EVERY_DAYS = 7


_LOG_FH = None   # set by main() so the log survives when launched with pythonw
                 # (Task Scheduler) where there's no console to print to


def _log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    if _LOG_FH is not None:
        try:
            _LOG_FH.write(line + "\n")
            _LOG_FH.flush()
        except Exception:
            pass


def _pid_alive(pid: int) -> bool:
    """Is this PID still a running process? os.kill(pid, 0) -- the normal
    POSIX existence probe -- has no equivalent signal-0 meaning on Windows,
    so shell out to tasklist there instead."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10,
            )
            return str(pid) in (out.stdout or "")
        except Exception:
            return True  # can't tell -- assume alive; safer to refuse a
                         # start than to risk two instances fighting over
                         # the same WhatsApp session
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return True


_LOCK_PATH = None  # set by _acquire_singleton_lock(), released in main()'s finally:


def _acquire_singleton_lock(cfg: dict) -> None:
    """Refuse to start a second laptop_agent.py against this same knowledge
    dir. The in-process race that could overlap two bridge instances
    during a self-update-triggered restart was already closed (see
    _stop_bridge's force_orphan handling) -- but that only protects a
    single running process against itself. Nothing stopped a genuinely
    SECOND whole instance (an overlapping Task Scheduler Boot + LogOn
    trigger, a manual run left going alongside the scheduled one, a
    crash-restart racing a fresh trigger) from independently spawning or
    adopting its own bridge with zero knowledge of the other's. Two
    processes each holding a live bridge, even briefly, is exactly the
    condition that forks the WhatsApp Signal session and produces
    "Waiting for this message" -- and unlike the single-process case,
    that fork can recur every time the overlap happens again, and (per
    Baileys' own retry-receipt recovery) can appear to self-heal after a
    while, only to break again on the next overlap. Exits immediately
    (loudly, on every channel that doesn't need the bridge) if another
    live instance already holds the lock."""
    global _LOCK_PATH
    lock_path = cfg["log_dir"] / "companion.lock"
    try:
        cfg["log_dir"].mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    for _attempt in range(2):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            _LOCK_PATH = lock_path
            return
        except FileExistsError:
            try:
                existing = int((lock_path.read_text().strip() or "0"))
            except Exception:
                existing = 0
            if existing and existing != os.getpid() and _pid_alive(existing):
                _log(f"another companion instance is already running "
                     f"(pid {existing}) -- refusing to start a second one; "
                     "this exact overlap is what forks the WhatsApp session")
                _loud_alert(
                    cfg, "companion-duplicate-instance",
                    f"A second Lumina companion tried to start while pid "
                    f"{existing} was already running -- refused, to avoid "
                    "corrupting the WhatsApp session. Check Task Scheduler "
                    "for an overlapping trigger (e.g. Boot + LogOn firing "
                    "close together) or a manually-started copy left running.",
                    repeat_after=300.0,
                )
                sys.exit(1)
            # stale lock left by a PID that's no longer running -- safe to
            # reclaim it and retry the atomic create once
            try:
                lock_path.unlink()
            except Exception:
                pass
            continue
    # Couldn't cleanly acquire after one retry (a filesystem hiccup, not a
    # live conflict already ruled out above) -- proceed rather than
    # permanently wedge the companion over a transient issue.
    try:
        lock_path.write_text(str(os.getpid()))
        _LOCK_PATH = lock_path
    except Exception:
        pass


def _release_singleton_lock() -> None:
    if _LOCK_PATH is None:
        return
    try:
        if (_LOCK_PATH.read_text().strip() or "") == str(os.getpid()):
            _LOCK_PATH.unlink()
    except Exception:
        pass


def _api_token(cfg: dict) -> str:
    """Token for the /api/companion/* + /api/storage/* endpoints."""
    return cfg["storage_token"] or cfg["db_secret"]


def _companion_get(cfg: dict, path: str, timeout: int = 45):
    """GET a token-gated /api/companion/* endpoint -> parsed JSON or None."""
    tok = _api_token(cfg)
    if not tok:
        return None
    try:
        r = requests.get(f"{cfg['url']}{path}",
                         headers={"Authorization": f"Bearer {tok}"}, timeout=timeout)
        if not r.ok:
            _log(f"{path}: HTTP {r.status_code}")
            return None
        return r.json() or {}
    except Exception as e:
        _log(f"{path} error: {e}")
        return None


# ── notify ─────────────────────────────────────────────────────────────────

def _send(targets: list, webhook: str, title: str, body: str) -> bool:
    if targets and apprise is not None:
        try:
            ap = apprise.Apprise()
            for t in targets:
                ap.add(t)
            ap.notify(title=title, body=body)
            return True
        except Exception as e:
            _log(f"apprise failed: {e}")
    if webhook:
        try:
            requests.post(webhook, json={"text": f"{title}\n{body}"}, timeout=15)
            return True
        except Exception as e:
            _log(f"webhook failed: {e}")
    return False


def _notify(cfg: dict, msg: str) -> None:
    """An alert (something is wrong)."""
    if not _send(cfg["alert_targets"], cfg["alert_webhook"], "Lumina watchdog", msg):
        _log("ALERT (no channel): " + msg)


def _digest_notify(cfg: dict, title: str, body: str) -> None:
    """A routine report (the daily brief). Prefers DIGEST_TARGETS / an apprise
    channel; otherwise delivers over WhatsApp — to the team group if
    --digest-group is set, else a DM to the alert recipients."""
    targets = cfg["digest_targets"] or cfg["alert_targets"]
    if _send(targets, cfg["alert_webhook"], title, body):
        return
    msg = f"{title}\n\n{body}"
    if cfg.get("digest_group") and cfg.get("rollcall_group"):
        if _bridge_send(cfg, cfg["rollcall_group"], msg):
            return
    if cfg.get("digest_dm", True) and _alert_dm(cfg, msg) > 0:
        return
    _log(f"{title} (no channel):\n{body}")


# ── job 1: knowledge sync ──────────────────────────────────────────────────

def job_sync(cfg: dict) -> None:
    if not cfg["storage_token"]:
        return
    try:
        kb_sync.sync_once(cfg["dir"], cfg["url"], cfg["storage_token"])
    except Exception as e:
        _log(f"sync error: {e}")


# ── job 2: research feed ───────────────────────────────────────────────────

def job_research(cfg: dict) -> None:
    if not (cfg["dir"] / research_feed.SOURCES_FILE).exists():
        return
    try:
        research_feed.run_once(cfg["dir"])
    except Exception as e:
        _log(f"research error: {e}")


# ── job 3: DB backup (+ offsite + uploads) ─────────────────────────────────

def _mirror(cfg: dict, path: Path) -> None:
    """Copy one backup file offsite: to a mirror dir and/or an rclone remote."""
    mdir = cfg["backup_mirror"]
    if mdir:
        try:
            mdir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, mdir / path.name)
            _log(f"  mirrored -> {mdir / path.name}")
        except Exception as e:
            _log(f"  mirror copy failed: {e}")
    remote = cfg["rclone_remote"]
    if remote and shutil.which("rclone"):
        try:
            subprocess.run(["rclone", "copy", str(path), remote],
                           timeout=600, capture_output=True)
            _log(f"  rclone -> {remote}")
        except Exception as e:
            _log(f"  rclone failed: {e}")
    elif remote:
        _log("  rclone remote set but rclone not on PATH")


def job_backup(cfg: dict) -> None:
    secret = cfg["db_secret"]
    if not secret:
        return
    bdir = cfg["backup_dir"]
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    dest = bdir / f"app-{stamp}.db"
    try:
        r = requests.get(f"{cfg['url']}/admin/download-db",
                         params={"secret": secret}, timeout=120, stream=True)
        if not r.ok:
            _log(f"backup failed: HTTP {r.status_code} {r.text[:120]}")
            return
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(65536):
                fh.write(chunk)
        size = dest.stat().st_size
        if size < 1024:
            _log(f"backup suspiciously small ({size} B) — check the secret")
        _log(f"backup saved {dest.name} ({size // 1024} KB)")
        _mirror(cfg, dest)
        for p in sorted(bdir.glob("app-*.db"), key=lambda p: p.stat().st_mtime,
                        reverse=True)[BACKUP_KEEP:]:
            p.unlink(missing_ok=True)
    except Exception as e:
        _log(f"backup error: {e}")
        return

    # weekly: also grab the uploads/ archive (client files, voice notes)
    tok = _api_token(cfg)
    if not tok:
        return
    recent = sorted(bdir.glob("uploads-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    if recent and (time.time() - recent[0].stat().st_mtime) < UPLOADS_EVERY_DAYS * 86400:
        return
    try:
        r = requests.get(f"{cfg['url']}/api/companion/uploads-archive",
                         headers={"Authorization": f"Bearer {tok}"}, timeout=300, stream=True)
        if r.status_code == 404:
            return  # nothing to archive
        if not r.ok:
            _log(f"uploads archive failed: HTTP {r.status_code}")
            return
        udest = bdir / f"uploads-{datetime.now(timezone.utc).strftime('%Y%m%d')}.zip"
        with open(udest, "wb") as fh:
            for chunk in r.iter_content(65536):
                fh.write(chunk)
        _log(f"uploads archive saved {udest.name} ({udest.stat().st_size // 1024} KB)")
        _mirror(cfg, udest)
        for p in sorted(bdir.glob("uploads-*.zip"), key=lambda p: p.stat().st_mtime,
                        reverse=True)[4:]:
            p.unlink(missing_ok=True)
    except Exception as e:
        _log(f"uploads archive error: {e}")


# ── job 4: health check (timed + logged) ───────────────────────────────────
_HEALTH = {"fails": 0}


def job_health(cfg: dict) -> None:
    t0 = time.monotonic()
    ok, code, err = False, 0, ""
    try:
        r = requests.get(f"{cfg['url']}/api/health", timeout=20)
        code = r.status_code
        ok = r.ok
    except Exception as e:
        err = str(e)[:200]
    ms = int((time.monotonic() - t0) * 1000)

    logf = cfg["log_dir"] / "health.jsonl"
    try:
        cfg["log_dir"].mkdir(parents=True, exist_ok=True)
        with open(logf, "a") as fh:
            fh.write(json.dumps({"t": datetime.now(timezone.utc).isoformat(),
                                 "ok": ok, "code": code, "ms": ms, "err": err}) + "\n")
    except Exception:
        pass

    if ok:
        if _HEALTH["fails"]:
            _log(f"health recovered ({ms} ms)")
        _HEALTH["fails"] = 0
        if ms > cfg["slow_ms"]:
            _log(f"ALERT: slow response {ms} ms")
            _notify(cfg, f"Lumina slow: /api/health took {ms} ms (threshold {cfg['slow_ms']})")
        return

    _HEALTH["fails"] += 1
    msg = f"health check failed: HTTP {code}" if code else f"unreachable: {err}"
    _log(f"ALERT: {msg} (fail #{_HEALTH['fails']})")
    if _HEALTH["fails"] == 2:      # alert once, on the 2nd consecutive miss
        _notify(cfg, msg)


# ── job 5: keep the Baileys WhatsApp bridge alive ─────────────────────────
_BRIDGE: dict = {"proc": None, "started": 0.0, "dead_at": 0.0, "backoff": 0.0,
                 "fails": 0, "logf": None, "adopted": False}


def _logged_out_flag(cfg: dict) -> bool:
    try:
        return (cfg["bridge_dir"] / ".logged_out").exists()
    except Exception:
        return False


def _bridge_token(cfg: dict) -> str:
    return (os.getenv("WHATSAPP_BRIDGE_TOKEN") or cfg.get("storage_token")
            or cfg.get("db_secret") or "")


def _bridge_alive(cfg: dict) -> bool:
    """Is a bridge already answering on the local health port -- ours or an
    orphan left running by a prior self-update restart (see job_selfupdate:
    a scripts/*.py-only change deliberately does NOT kill the bridge child
    before this process exits, so the WhatsApp session survives the
    restart). `connected` may briefly be false while it reconnects on its
    own -- only `loggedOut` (unrecoverable without a human) counts as dead."""
    port = cfg.get("bridge_http_port", 8787)
    try:
        r = requests.get(f"http://127.0.0.1:{port}/health", timeout=5)
        if not r.ok:
            return False
        return not (r.json() or {}).get("loggedOut")
    except Exception:
        return False


def job_bridge(cfg: dict) -> None:
    """(Re)spawn `node index.js` in whatsapp-bridge/ and keep it running.
    A run that lasts < 120s is treated as a failed start and backed off
    (5s per consecutive fail, capped 60s). If WhatsApp logged the device
    out, restarting can't help — back off hard (5 min) and alert loudly,
    since that needs a human to re-scan the QR.

    Before spawning, checks whether a bridge is already alive on the local
    health port (an orphan surviving a self-update restart, see above) and
    adopts it instead of starting a duplicate that would fight it for the
    same WhatsApp session — that fight is exactly what job_bridge used to
    force by always killing+respawning on every restart."""
    if not cfg["bridge_ok"]:
        return
    now = time.monotonic()
    proc = _BRIDGE["proc"]

    if proc is not None:
        if proc.poll() is None:
            return
        up = now - _BRIDGE["started"]
        logged_out = _logged_out_flag(cfg)
        if logged_out:
            _BRIDGE["fails"] += 1
            _BRIDGE["backoff"] = 300.0        # 5 min — don't spin on a dead session
        else:
            _BRIDGE["fails"] = _BRIDGE["fails"] + 1 if up < 120 else 0
            # A crash is, by definition, an UNGRACEFUL disconnect -- unlike
            # a deliberate _stop_bridge() (which waits for a confirmed
            # clean exit before this function ever spawns a replacement),
            # nothing here confirms WhatsApp's own server has finished
            # noticing the old connection is gone. Confirmed live
            # 2026-09-15: a crash after 975s uptime reset `fails` to 0,
            # giving a 0s backoff -- the replacement respawned 17s later,
            # right into the window where several contacts' sessions then
            # forked. A crash after a long healthy run always gets at
            # least this floor now, on top of whatever the normal
            # fail-count backoff already computes for a fast-crash-loop.
            _BRIDGE["backoff"] = max(15.0, min(60.0, 5.0 * _BRIDGE["fails"]))
        _BRIDGE["dead_at"] = now
        _BRIDGE["proc"] = None
        try:
            if _BRIDGE.get("logf"):
                _BRIDGE["logf"].close()
                _BRIDGE["logf"] = None
        except Exception:
            pass
        _log(f"bridge exited (code {proc.returncode}, up {up:.0f}s"
             f"{', LOGGED OUT' if logged_out else ''}); "
             f"restart in {_BRIDGE['backoff']:.0f}s")
        if logged_out:
            _loud_alert(cfg, "bridge-logged-out",
                        "WhatsApp bridge was LOGGED OUT (device unlinked on the "
                        "phone). Delete whatsapp-bridge/auth and re-pair by "
                        "scanning the QR — it can't recover on its own.")
        elif _BRIDGE["fails"] >= 5:
            _loud_alert(cfg, "bridge-crashloop",
                        f"WhatsApp bridge keeps crashing ({_BRIDGE['fails']} fast "
                        "restarts). Check whatsapp-bridge/../lumina-logs/bridge.log.")
        return

    if _BRIDGE.get("adopted"):
        if _bridge_alive(cfg):
            return  # still there -- nothing to do
        _log("bridge: adopted orphan is gone -- spawning our own")
        _BRIDGE["adopted"] = False
    elif _bridge_alive(cfg):
        _log("bridge: found an already-running, healthy bridge (survived a "
             "companion restart) -- adopting it instead of spawning a duplicate")
        _BRIDGE["adopted"] = True
        return

    if now < _BRIDGE["dead_at"] + _BRIDGE["backoff"]:
        return

    env = dict(os.environ)
    env.setdefault("LUMINA_URL", cfg["url"])
    try:
        cfg["log_dir"].mkdir(parents=True, exist_ok=True)
        logp = cfg["log_dir"] / "bridge.log"
        # keep the log from growing forever
        try:
            if logp.exists() and logp.stat().st_size > 2_000_000:
                logp.replace(cfg["log_dir"] / "bridge.log.1")
        except Exception:
            pass
        _BRIDGE["logf"] = open(logp, "a", buffering=1, encoding="utf-8", errors="replace")
        _BRIDGE["proc"] = subprocess.Popen(
            [cfg["node"], "index.js"], cwd=str(cfg["bridge_dir"]), env=env,
            stdout=_BRIDGE["logf"], stderr=subprocess.STDOUT,
        )
        _BRIDGE["started"] = now
        _log(f"bridge started (pid {_BRIDGE['proc'].pid}) -> {logp}")
    except Exception as e:
        _BRIDGE["dead_at"] = now
        _BRIDGE["backoff"] = 30.0
        _log(f"bridge failed to start: {e}")


def _stop_bridge(cfg: dict, force_orphan: bool = False) -> None:
    """Blocks until the bridge is confirmed gone (or a hard timeout is hit)
    before returning. Covers a process we spawned ourselves (tracked in
    _BRIDGE["proc"], killable directly) unconditionally.

    An *adopted* orphan (proc is None -- we've only ever confirmed it via
    /health, never held a process handle for it; the normal state right
    after a scripts-only self-update restart, see job_bridge()'s adoption
    logic above) is only stopped when `force_orphan=True` -- pass that ONLY
    when the caller actually needs the bridge to restart (a real
    whatsapp-bridge/ code change). The default (False) leaves an adopted
    orphan running untouched, which matters for the plain process-exit
    cleanup call in main()'s `finally:` -- a companion crash/restart or a
    scripts-only self-update must NOT drop a perfectly live WhatsApp
    session just because this function got called; only a genuine
    bridge-code change should ever force that.

    job_bridge()'s respawn/adopt logic only runs on the *next* tick, so as
    long as a force_orphan=True call actually waits for the old bridge to
    be gone -- by whichever means applies -- there's no window where two
    node processes hold the same WhatsApp session at once. That overlap is
    what forks the Signal session and leaves messages stuck on "Waiting
    for this message" for whoever gets sent one during it (see CLAUDE.md
    gotcha #118, fixed live that time via a manual session-file wipe).

    Before the bridge's own POST /shutdown endpoint existed, this function
    could only ever kill a process it spawned itself -- against an adopted
    orphan it silently did nothing at all, so any bridge-code push made
    after a scripts-only restart never actually took effect: the stale
    process just kept running on old code indefinitely, undetected."""
    proc = _BRIDGE.get("proc")
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        if proc.poll() is None:
            _log("bridge: could not confirm the spawned process exited "
                 "within the timeout -- a respawn may briefly overlap it")
        else:
            _BRIDGE["proc"] = None
        try:
            if _BRIDGE.get("logf"):
                _BRIDGE["logf"].close()
                _BRIDGE["logf"] = None
        except Exception:
            pass
        return

    _BRIDGE["proc"] = None
    if not force_orphan:
        return
    if not _BRIDGE.get("adopted") or not _bridge_alive(cfg):
        return  # nothing running (or already confirmed gone) -- no-op

    tok = _bridge_token(cfg)
    if not tok:
        _log("bridge: an adopted orphan is running but no bridge token is "
             "set -- can't call /shutdown, it'll keep running on old code "
             "until something else stops it")
        return
    try:
        requests.post(
            f"http://127.0.0.1:{cfg.get('bridge_http_port', 8787)}/shutdown",
            headers={"Authorization": f"Bearer {tok}"}, timeout=5)
    except Exception as e:
        _log(f"bridge: /shutdown request failed ({e}) -- polling anyway "
             "in case it exited before the response came back")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not _bridge_alive(cfg):
            _BRIDGE["adopted"] = False
            return
        time.sleep(0.5)
    _log("bridge: adopted orphan did not exit within 10s of /shutdown -- "
         "it may still be running old code; a respawn could briefly "
         "overlap it")


# ── job 6: daily brief ────────────────────────────────────────────────────

def _digest(cfg: dict, window: str) -> None:
    tok = _api_token(cfg)
    if not tok:
        _log("digest: no token (set STORAGE_SYNC_TOKEN)")
        return
    try:
        r = requests.get(f"{cfg['url']}/api/companion/digest",
                         params={"window": window},
                         headers={"Authorization": f"Bearer {tok}"}, timeout=60)
        if not r.ok:
            _log(f"digest {window}: HTTP {r.status_code}")
            return
        text = (r.json() or {}).get("text", "")
    except Exception as e:
        _log(f"digest {window} error: {e}")
        return
    if not text:
        return
    title = "Lumina — morning brief" if window == "morning" else "Lumina — end of day"
    _digest_notify(cfg, title, text)
    _log(f"digest {window} sent ({len(text)} chars)")


def job_digest_morning(cfg: dict) -> None:
    _digest(cfg, "morning")


def job_digest_evening(cfg: dict) -> None:
    _digest(cfg, "evening")


# ── job 7: Google Sheets sync watchdog ───────────────────────────────────
_SHEETS = {"alerted": ""}


def job_sheets_watchdog(cfg: dict) -> None:
    tok = _api_token(cfg)
    if not tok:
        return
    try:
        r = requests.get(f"{cfg['url']}/api/companion/sheets-health",
                         headers={"Authorization": f"Bearer {tok}"}, timeout=45)
        if not r.ok:
            return
        j = r.json() or {}
    except Exception as e:
        _log(f"sheets watchdog error: {e}")
        return

    bad = [l for l in j.get("links", []) if l.get("push_failing") or l.get("pull_stale")]
    if bad:
        names = ", ".join(l.get("client_name") or l.get("client_id") for l in bad)
        if names != _SHEETS["alerted"]:      # only re-alert when the set changes
            _notify(cfg, f"Google Sheet sync issue for: {names}")
            _alert_dm(cfg, f"Google Sheet sync issue for: {names}. Check the "
                           "Connect Google Sheet panel for that client.")
            _SHEETS["alerted"] = names
        if cfg["sheets_autopull"]:
            try:
                pr = requests.post(f"{cfg['url']}/api/companion/sheets-pull-all",
                                   headers={"Authorization": f"Bearer {tok}"}, timeout=180)
                _log(f"sheets auto-pull: {pr.status_code}")
            except Exception as e:
                _log(f"sheets auto-pull error: {e}")
    else:
        _SHEETS["alerted"] = ""


# ── job 10: deliver queued proactive WhatsApp messages ───────────────────
#
# The Railway backend can't reach the Baileys bridge (loopback on this
# laptop), so whatsapp_agent queues "X assigned you a task" / "remind Y"
# messages in the DB. We poll for them and send via the local bridge.

def job_wa_outbox(cfg: dict) -> None:
    if not cfg["bridge_ok"]:
        return
    tok = _api_token(cfg)
    if not tok:
        return
    try:
        r = requests.get(f"{cfg['url']}/api/companion/whatsapp-outbox?images=1",
                         headers={"Authorization": f"Bearer {tok}"}, timeout=30)
        if not r.ok:
            return
        msgs = (r.json() or {}).get("messages") or []
    except Exception as e:
        _log(f"wa-outbox error: {e}")
        return
    if not msgs:
        return
    sent, failed = [], []
    for m in msgs:
        to, text, mid = m.get("to"), m.get("text"), m.get("id")
        if not (to and text and mid):
            continue
        img_b64 = ""
        if m.get("image_url"):
            try:
                ir = requests.get(f"{cfg['url']}{m['image_url']}", timeout=30)
                if ir.ok and ir.content:
                    import base64
                    img_b64 = base64.b64encode(ir.content).decode()
            except Exception as e:
                _log(f"wa-outbox image fetch error: {e}")
            if not img_b64:
                failed.append(mid)  # retry later rather than send the caption alone
                continue
        ok, wid = _bridge_send_msg(cfg, to, text, image_b64=img_b64)
        if ok:
            sent.append({"id": mid, "wa_id": wid})
        else:
            failed.append(mid)
    try:
        requests.post(f"{cfg['url']}/api/companion/whatsapp-outbox/ack",
                      json={"sent": sent, "failed": failed},
                      headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    except Exception as e:
        _log(f"wa-outbox ack error: {e}")
    _log(f"wa-outbox: {len(sent)} sent, {len(failed)} failed/retry")


# ── job 11: place queued one-way WhatsApp announcement calls ─────────────
#
# Same shape as job_wa_outbox above, but hits the bridge's /call endpoint
# (voice.js: Piper TTS + baileys-caller) instead of /send. A call can take
# a while (TTS + ringing + playback), so this uses a longer timeout and a
# lower default poll frequency than the text outbox -- see --voice-calls-every.

def job_voice_calls(cfg: dict) -> None:
    if not cfg["bridge_ok"]:
        return
    tok = _api_token(cfg)
    if not tok:
        return
    try:
        r = requests.get(f"{cfg['url']}/api/companion/wa-call-outbox",
                         headers={"Authorization": f"Bearer {tok}"}, timeout=30)
        if not r.ok:
            return
        calls = (r.json() or {}).get("calls") or []
    except Exception as e:
        _log(f"voice-calls error: {e}")
        return
    if not calls:
        return
    sent, failed = [], []
    bridge_tok = _bridge_token(cfg)
    for c in calls:
        to, message, cid = c.get("to"), c.get("message"), c.get("id")
        if not (to and message and cid):
            continue
        ok = False
        try:
            resp = requests.post(
                f"http://127.0.0.1:{cfg['bridge_http_port']}/call",
                json={"to": to, "message": message},
                headers={"Authorization": f"Bearer {bridge_tok}"}, timeout=90,
            )
            body = resp.json() if resp.ok else {}
            ok = bool(body.get("ok"))
            if ok:
                _log(f"voice call -> {to}: placed")
            else:
                _log(f"voice call -> {to}: failed ({body.get('error', resp.status_code)})")
        except Exception as e:
            _log(f"voice call -> {to}: error ({e})")
        (sent if ok else failed).append(cid)
    try:
        requests.post(f"{cfg['url']}/api/companion/wa-call-outbox/ack",
                      json={"sent": sent, "failed": failed},
                      headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    except Exception as e:
        _log(f"voice-calls ack error: {e}")
    _log(f"voice-calls: {len(sent)} placed, {len(failed)} failed/retry")


def _alert_dm(cfg: dict, text: str) -> int:
    """DM an ops alert to every configured alert recipient via the bridge."""
    if not cfg["bridge_ok"]:
        return 0
    j = _companion_get(cfg, "/api/companion/alert-recipients")
    if not j:
        return 0
    n = 0
    for r in j.get("recipients", []):
        wa = re.sub(r"\D", "", r.get("whatsapp", ""))
        if wa and _bridge_send(cfg, f"{wa}@s.whatsapp.net", text):
            n += 1
    return n


# ── loud, out-of-band alert (the bridge itself may be the thing that's down,
#    so this must not rely on WhatsApp) ─────────────────────────────────────
_LOUD: dict = {}          # key -> last-sent monotonic time


def _loud_alert(cfg: dict, key: str, text: str, repeat_after: float = 1800.0) -> None:
    """Fire an alert through every channel that doesn't need the bridge:
    apprise/webhook if configured, a desktop file always, and a best-effort
    Windows balloon. Rate-limited per `key`."""
    now = time.monotonic()
    if now - _LOUD.get(key, 0.0) < repeat_after:
        return
    _LOUD[key] = now
    _log(f"LOUD ALERT [{key}]: {text}")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    full = f"[{stamp}] {text}"

    # 1. apprise / webhook (only if the user set them up)
    try:
        _send(cfg["alert_targets"], cfg["alert_webhook"], "Lumina bridge", full)
    except Exception:
        pass

    # 2. a file on the Desktop + in the log dir — always works, hard to miss
    for d in (Path.home() / "Desktop", cfg["log_dir"]):
        try:
            d.mkdir(parents=True, exist_ok=True)
            (d / "LUMINA-BRIDGE-ALERT.txt").write_text(
                full + "\n\nRe-pair steps if it says 'logged out':\n"
                "  1. stop laptop_agent.py + node index.js\n"
                "  2. delete whatsapp-bridge\\auth\\\n"
                "  3. cd whatsapp-bridge && node index.js  (scan the QR)\n"
                "  4. restart laptop_agent.py\n",
                encoding="utf-8",
            )
        except Exception:
            pass

    # 3. best-effort Windows toast/balloon (no extra deps; fine on Home)
    if sys.platform == "win32":
        ps = (
            "[void][reflection.assembly]::LoadWithPartialName('System.Windows.Forms');"
            "[void][reflection.assembly]::LoadWithPartialName('System.Drawing');"
            "$n=New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon=[System.Drawing.SystemIcons]::Warning;$n.Visible=$true;"
            "$n.ShowBalloonTip(20000,'Lumina WhatsApp bridge',"
            + json.dumps(text)
            + ",'Warning');Start-Sleep -Seconds 20;$n.Dispose()"
        )
        try:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass


def _clear_bridge_alert(cfg: dict) -> None:
    for d in (Path.home() / "Desktop", cfg["log_dir"]):
        try:
            f = d / "LUMINA-BRIDGE-ALERT.txt"
            if f.exists():
                f.unlink()
        except Exception:
            pass


# ── job: bridge health watchdog ──────────────────────────────────────────
#
# job_bridge keeps the *process* alive; this checks the process is actually
# CONNECTED to WhatsApp. The 2 AM failure mode was a logged-out session that
# the supervisor kept restarting forever, silently.
_BRIDGE_H: dict = {"unreach": 0, "down_since": 0.0, "alerting": False}


def job_bridge_health(cfg: dict) -> None:
    if not cfg["bridge_ok"]:
        return
    port = cfg.get("bridge_http_port", 8787)
    now = time.monotonic()
    state = None
    try:
        r = requests.get(f"http://127.0.0.1:{port}/health", timeout=8)
        if r.ok:
            state = r.json() or {}
    except Exception:
        state = None

    if state is None:
        _BRIDGE_H["unreach"] += 1
        # the supervisor may just be between restarts; only worry after a while
        if _BRIDGE_H["unreach"] >= 4:                       # ~4 checks in a row
            _BRIDGE_H["alerting"] = True
            _loud_alert(cfg, "bridge-unreachable",
                        "WhatsApp bridge is not responding on "
                        f"127.0.0.1:{port}. The bot is offline.")
        return
    _BRIDGE_H["unreach"] = 0

    if state.get("loggedOut"):
        _BRIDGE_H["alerting"] = True
        _loud_alert(cfg, "bridge-logged-out",
                    "WhatsApp bridge got LOGGED OUT (device unlinked on the "
                    "phone). It cannot recover on its own — delete "
                    "whatsapp-bridge/auth and re-pair by scanning the QR.")
        return

    if not state.get("connected"):
        if not _BRIDGE_H["down_since"]:
            _BRIDGE_H["down_since"] = now
        elif now - _BRIDGE_H["down_since"] > 480:           # not connected >8 min
            _BRIDGE_H["alerting"] = True
            _loud_alert(cfg, "bridge-disconnected",
                        "WhatsApp bridge has been disconnected for over 8 "
                        f"minutes (state: {state.get('state')}). Bot is offline.")
        return

    # connected & healthy
    _BRIDGE_H["down_since"] = 0.0
    if _BRIDGE_H["alerting"]:
        _BRIDGE_H["alerting"] = False
        _LOUD.clear()                                       # let the next issue alert immediately
        _clear_bridge_alert(cfg)
        _log("bridge health: recovered, connected")
        try:
            _alert_dm(cfg, "WhatsApp bridge is back online.")
        except Exception:
            pass


# ── job: self-update ─────────────────────────────────────────────────────
#
# The dev machine pushes to GitHub `main`. Railway auto-deploys the backend;
# this pulls the parts that run HERE (scripts/, whatsapp-bridge/) and
# restarts what changed. Fast-forward only — never touches local state.
_SELF: dict = {"fails": 0}


def _git(repo: Path, *args, timeout: int = 120):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, timeout=timeout)


def job_selfupdate(cfg: dict) -> None:
    if not cfg.get("self_update"):
        return
    repo = cfg["repo_dir"]
    branch = cfg.get("self_update_branch", "main")
    if not (repo / ".git").exists() or not shutil.which("git"):
        return
    try:
        before = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _git(repo, "fetch", "--quiet", "origin", branch)
        ff = _git(repo, "merge", "--ff-only", f"origin/{branch}")
        after = _git(repo, "rev-parse", "HEAD").stdout.strip()
    except Exception as e:
        _log(f"self-update error: {e}")
        return

    if not after or after == before:
        if ff.returncode != 0 and "not possible to fast-forward" in (ff.stderr or ""):
            _SELF["fails"] += 1
            if _SELF["fails"] in (1, 10):
                _loud_alert(cfg, "self-update-diverged",
                            "self-update can't fast-forward (local repo has "
                            "diverged from origin/main). Fix the repo on the "
                            "always-on laptop by hand.")
        return
    _SELF["fails"] = 0

    changed = _git(repo, "diff", "--name-only", before, after).stdout.splitlines()
    _log(f"self-update: {before[:7]} -> {after[:7]} ({len(changed)} file(s) changed)")

    bridge_changed = any(c.startswith("whatsapp-bridge/") for c in changed)
    pkg_changed = any(c.startswith("whatsapp-bridge/package") for c in changed)
    agent_changed = any(c.startswith("scripts/") and c.endswith(".py") for c in changed)

    if pkg_changed and cfg["node"]:
        try:
            _log("self-update: bridge deps changed -> npm install")
            subprocess.run("npm install --no-audit --no-fund", cwd=str(cfg["bridge_dir"]),
                           capture_output=True, timeout=600, shell=True)
        except Exception as e:
            _log(f"self-update: npm install failed: {e}")

    if agent_changed:
        _log("self-update: companion code changed -> restarting")
        if bridge_changed:
            _log("self-update: bridge code also changed -> stopping it before restart")
            _stop_bridge(cfg, force_orphan=True)
        else:
            # Only scripts/*.py changed -- this process has to restart to
            # load it, but the bridge child doesn't need to and deliberately
            # isn't touched: os._exit()/os.execv() below never runs Python's
            # normal exit machinery, so an untouched child simply survives as
            # an orphan, still connected to WhatsApp. The next process adopts
            # it via _bridge_alive() in job_bridge() instead of spawning a
            # second one. This is what keeps the WhatsApp session up across
            # a companion-code restart instead of dropping and reconnecting.
            _log("self-update: bridge code unchanged -> leaving the WhatsApp "
                 "bridge running through the restart")
        if sys.platform == "win32":
            # os.execv on Windows detaches from Task Scheduler's job and the
            # new process gets orphaned/killed (learned the hard way — a
            # scripts/*.py push left the box dark for 23 min). Instead: exit
            # non-zero so the task's own RestartCount fires, AND drop a
            # detached helper that re-runs the task in case it doesn't.
            try:
                task = os.getenv("LUMINA_TASK_NAME", "Lumina companion")
                subprocess.Popen(
                    ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command",
                     f"Start-Sleep -Seconds 10; schtasks /run /tn \"{task}\""],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=(getattr(subprocess, "DETACHED_PROCESS", 0)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)),
                )
            except Exception as e:
                _log(f"self-update: restart helper failed to spawn: {e}")
            _log("self-update: exiting (code 3) for Task Scheduler to restart")
            os._exit(3)
        try:
            os.execv(sys.executable,
                     [sys.executable, os.path.abspath(sys.argv[0]), *sys.argv[1:]])
        except Exception as e:
            _log(f"self-update: re-exec failed ({e}); exiting for restart")
            os._exit(3)
    elif bridge_changed:
        _log("self-update: bridge code changed -> restarting bridge child")
        _stop_bridge(cfg, force_orphan=True)   # job_bridge respawns it next tick with the new code


INTERVAL_JOBS = [
    ("sync", job_sync, "sync_every"),
    ("research", job_research, "research_every"),
    ("backup", job_backup, "backup_every"),
    ("health", job_health, "health_every"),
    ("self-update", job_selfupdate, "self_update_every"),
    ("bridge", job_bridge, "bridge_every"),
    ("bridge-health", job_bridge_health, "bridge_health_every"),
    ("sheets", job_sheets_watchdog, "sheets_every"),
    ("wa-outbox", job_wa_outbox, "outbox_every"),
    ("voice-calls", job_voice_calls, "voice_calls_every"),
]


# ── job 8: noon attendance roll-call to the team WhatsApp group ───────────

def _bridge_send_msg(cfg: dict, to: str, text: str, mention_all: bool = False,
                     image_b64: str = ""):
    """POST to the local Baileys bridge /send. Returns (ok, wa_message_id|None).
    mention_all: bridge tags every member of `to` (must be a group jid) with a
    leading line of @-mentions before the message text.
    image_b64: send this PNG with `text` as its caption."""
    tok = _bridge_token(cfg)
    if not tok:
        _log("bridge send: no token")
        return False, None
    try:
        payload = {"to": to, "text": text, "mentionAll": mention_all}
        if image_b64:
            payload["imageB64"] = image_b64
        r = requests.post(
            f"http://127.0.0.1:{cfg['bridge_http_port']}/send",
            json=payload,
            headers={"Authorization": f"Bearer {tok}"}, timeout=30,
        )
        if r.ok:
            _log(f"bridge send -> {to} ({len(text)} chars)")
            wid = None
            try:
                wid = (r.json() or {}).get("id")
            except Exception:
                pass
            return True, wid
        _log(f"bridge send: HTTP {r.status_code} {r.text[:120]}")
    except Exception as e:
        _log(f"bridge send error: {e}")
    return False, None


def _bridge_send(cfg: dict, to: str, text: str, mention_all: bool = False) -> bool:
    return _bridge_send_msg(cfg, to, text, mention_all=mention_all)[0]


def job_standup_nudge(cfg: dict) -> None:
    """11:30 — DM anyone who hasn't put a fresh task on today's standup."""
    tok = _api_token(cfg)
    if not tok:
        _log("standup nudge: no token")
        return
    try:
        r = requests.get(f"{cfg['url']}/api/companion/standup-missing",
                         headers={"Authorization": f"Bearer {tok}"}, timeout=45)
        if not r.ok:
            _log(f"standup nudge: HTTP {r.status_code}")
            return
        j = r.json() or {}
    except Exception as e:
        _log(f"standup nudge error: {e}")
        return
    if j.get("weekend"):
        _log("standup nudge: weekend — skipping")
        return
    missing = j.get("missing") or []
    if not missing:
        _log("standup nudge: everyone's added a task")
        return
    sent = 0
    for p in missing:
        wa = re.sub(r"\D", "", p.get("whatsapp", ""))
        if not wa:
            continue
        name = p.get("name") or "there"
        text = (
            f"Morning *{name}* — it's gone 11:30 and your standup for today is "
            "still a blank canvas. Add a task or two when you get a moment.\n\n"
            "Shortcut: just reply here with what you're working on and I'll drop "
            "it straight into your standup for you."
        )
        if _bridge_send(cfg, f"{wa}@s.whatsapp.net", text):
            sent += 1
    _log(f"standup nudge: {sent}/{len(missing)} DMs sent")


def _in_window(start_hhmm: str, end_hhmm: str) -> bool:
    now = datetime.now().strftime("%H:%M")
    return start_hhmm <= now <= end_hhmm


# Employee ids that opted out of the every-5-min personal attendance DM.
# They still show up in the group ping (job_login_group_ping) -- this only
# silences the private nag. Founders, by request.
_LOGIN_NUDGE_DM_SKIP = {"emp001", "emp004"}  # Vidit, Kshitij


def job_login_nudge(cfg: dict) -> None:
    """Every few minutes during work hours -- DM anyone who still hasn't
    fully "logged in" for the day: no attendance check-in, an empty
    standup, or both. Weekends and anyone on recorded leave are filtered
    out server-side (/api/companion/not-logged-in). Stops for a person the
    moment they've done both. CLAUDE.md gotcha #119.

    Anyone in _LOGIN_NUDGE_DM_SKIP is left out of the personal DM (they
    still appear in the group ping)."""
    if not cfg["bridge_ok"] or not cfg.get("login_nudge"):
        return
    if datetime.now().weekday() >= 5:
        return
    if not _in_window(cfg["login_nudge_start"], cfg["login_nudge_end"]):
        return
    j = _companion_get(cfg, "/api/companion/not-logged-in")
    if not j or j.get("weekend"):
        return
    missing = [p for p in (j.get("missing") or [])
               if p.get("id") not in _LOGIN_NUDGE_DM_SKIP]
    if not missing:
        _log("login-nudge: everyone's logged in")
        return
    sent = 0
    for p in missing:
        wa = re.sub(r"\D", "", p.get("whatsapp", ""))
        if not wa:
            continue
        name = p.get("name") or "there"
        nc, ns = p.get("needs_checkin"), p.get("needs_standup")
        link = cfg["url"].rstrip("/") + "/checkin.html"
        if nc and ns:
            what = ("you're not checked in on Lumina yet and your standup for "
                    "today is still empty")
            fix = (f"Check in with one tap (it takes your location): {link}\n"
                   "Then reply here with what you're working on and I'll add it to your standup.")
        elif nc:
            what = "you're not checked in on Lumina yet"
            fix = f"Check in with one tap (it takes your location): {link}"
        else:
            what = "your standup for today is still empty"
            fix = ("Reply here with what you're working on and I'll drop it "
                   "into your standup.")
        every_min = max(1, cfg["login_nudge_every"] // 60)
        text = (f"*{name}* — {what}. I'll keep pinging every {every_min} min "
                f"until it's sorted.\n\n{fix}\n\n"
                "On leave today? Reply \"on leave\" and I'll stop.")
        if _bridge_send(cfg, f"{wa}@s.whatsapp.net", text):
            sent += 1
    _log(f"login-nudge: {sent}/{len(missing)} DM(s)")


def job_login_group_ping(cfg: dict) -> None:
    """Mid-morning + one follow-up -- post the list of who still hasn't
    logged in (check-in + a standup task) to the team WhatsApp group.
    Weekend / on-leave already filtered server-side."""
    grp = cfg["rollcall_group"]
    if not grp:
        return
    if datetime.now().weekday() >= 5:
        return
    j = _companion_get(cfg, "/api/companion/not-logged-in")
    if not j or j.get("weekend"):
        return
    missing = j.get("missing") or []
    if not missing:
        _log("login-group-ping: everyone's logged in — nothing to post")
        return
    lines = []
    for p in missing:
        nc, ns = p.get("needs_checkin"), p.get("needs_standup")
        tag = ("no check-in + no standup" if nc and ns
               else "no check-in" if nc else "no standup")
        lines.append(f"• *{p.get('name') or p.get('id')}* — {tag}")
    body = ("*Still not logged in for today:*\n" + "\n".join(lines)
            + "\n\nCheck in on Lumina and put at least one task on your "
              "standup — you can reply to me here to do both.")
    if _bridge_send(cfg, grp, body):
        _log(f"login-group-ping: posted ({len(missing)} pending)")


# runs on the 5-min interval loop (defined here, after the fn exists)
INTERVAL_JOBS.append(("login-nudge", job_login_nudge, "login_nudge_every"))


_LUNCH_LINES = [
    "It's 2 o'clock. Lunch. Step away from the screen, go eat something. 🍽",
    "2pm — lunch break, everyone. Food first, deadlines after.",
    "Lunchtime. Close the laptop for a bit and go grab a proper meal. 🍛",
    "It's 2. Go have lunch, the tasks will still be here when you're back.",
    "2pm lunch call — take the break, you've earned it. 🍴",
]


def job_lunch(cfg: dict) -> None:
    """14:00 — nudge the team WhatsApp group to go for lunch."""
    grp = cfg["rollcall_group"]
    if not grp:
        return
    if datetime.now().weekday() >= 5:      # Sat/Sun
        _log("lunch: weekend — skipping")
        return
    idx = datetime.now().timetuple().tm_yday % len(_LUNCH_LINES)
    if _bridge_send(cfg, grp, _LUNCH_LINES[idx]):
        _log("lunch: sent")


def job_team_meeting(cfg: dict) -> None:
    """Mon/Wed/Fri 15:00 -- remind the team WhatsApp group of the 4-5pm meeting."""
    grp = cfg["rollcall_group"]
    if not grp:
        return
    if datetime.now().weekday() not in (0, 2, 4):   # Mon/Wed/Fri only
        return
    if _bridge_send(cfg, grp, "Reminder: team meeting today, *4 to 5 PM*."):
        _log("team-meeting: sent")


_CLASS_MEETING_LINK = "https://meet.google.com/rek-jyvv-jmm"


def job_class_meeting(cfg: dict) -> None:
    """Tue/Thu 10:28 -- tag everyone in the team WhatsApp group with the
    meeting link."""
    grp = cfg["rollcall_group"]
    if not grp:
        return
    if datetime.now().weekday() not in (1, 3):   # Tue/Thu only
        return
    text = f"*Meeting time* -- join here: {_CLASS_MEETING_LINK}"
    if _bridge_send(cfg, grp, text, mention_all=True):
        _log("class-meeting: sent (tagged all)")


def job_weekly_wrap(cfg: dict) -> None:
    """Friday 18:00 — per-person completed count for the week, to the group."""
    grp = cfg["rollcall_group"]
    if not grp:
        return
    if datetime.now().weekday() != 4:      # Friday only
        return
    j = _companion_get(cfg, "/api/companion/weekly-summary")
    if not j:
        return
    text = j.get("text")
    if text and _bridge_send(cfg, grp, text):
        _log("weekly-wrap: sent")


def _task_lines(titles: list, limit: int = 12) -> str:
    """One numbered task per line, so a list of open tasks is easy to scan."""
    rows = [f"{i}. {t}" for i, t in enumerate(titles[:limit], 1)]
    if len(titles) > limit:
        rows.append(f"...and {len(titles) - limit} more")
    return "\n".join(rows)


def _quote_tasks(titles: list) -> str:
    """'a', 'a and b', 'a, b and c' -- quoted, max 3 then 'N more'."""
    q = [f'"{t}"' for t in titles[:3]]
    if len(titles) > 3:
        q.append(f"{len(titles) - 3} more")
    return q[0] if len(q) == 1 else ", ".join(q[:-1]) + " and " + q[-1]


def job_eod_heads_up(cfg: dict) -> None:
    """16:55 -- five minutes before the group summary, DM anyone with an open
    task on today's standup so they can tick what's finished (the 17:00 post
    reads the database at that moment)."""
    if not cfg["bridge_ok"]:
        return
    j = _companion_get(cfg, "/api/companion/eod-summary")
    if not j or j.get("weekend"):
        return
    sent = 0
    for p in j.get("per_person", []):
        pend = p.get("pending") or []
        wa = re.sub(r"\D", "", p.get("whatsapp", ""))
        if not pend or not wa:
            continue
        n = p.get("name", "there")
        text = (
            f"Hey *{n}*, the 5 PM summary goes to the group in 5 minutes.\n\n"
            f"*Still open on your standup ({len(pend)}):*\n{_task_lines(pend)}\n\n"
            "Tick what's done on Lumina, or just tell me here."
        )
        if _bridge_send(cfg, f"{wa}@s.whatsapp.net", text):
            sent += 1
    _log(f"eod-heads-up: {sent} DMs")


def job_eod_group(cfg: dict) -> None:
    """17:00 — post a short per-person 'tasks done today' summary to the group."""
    grp = cfg["rollcall_group"]
    if not grp:
        return
    j = _companion_get(cfg, "/api/companion/eod-summary")
    if not j or j.get("weekend"):
        return
    text = j.get("group_text")
    if text and _bridge_send(cfg, grp, text):
        _log("eod-group: sent")


def job_eod_personal(cfg: dict) -> None:
    """19:30 — DM each person their still-open tasks for today, and DM the
    leads (Vidit/Kshitij) the cross-team 'not done' list."""
    if not cfg["bridge_ok"]:
        return
    j = _companion_get(cfg, "/api/companion/eod-summary")
    if not j or j.get("weekend"):
        return
    sent = 0
    for p in j.get("per_person", []):
        pend = p.get("pending") or []
        wa = re.sub(r"\D", "", p.get("whatsapp", ""))
        if not pend or not wa:
            continue
        n = p.get("name", "there")
        text = (
            f"Hey *{n}*, here's what's still open from today.\n\n"
            f"*Open tasks ({len(pend)}):*\n{_task_lines(pend)}\n\n"
            "Done with any of them? Tick them on Lumina or tell me here, "
            "otherwise they roll over to tomorrow."
        )
        if _bridge_send(cfg, f"{wa}@s.whatsapp.net", text):
            sent += 1
    lt = j.get("leads_text")
    leads = j.get("leads") or []
    for lead in leads:
        wa = re.sub(r"\D", "", lead.get("whatsapp", ""))
        if wa and lt:
            _bridge_send(cfg, f"{wa}@s.whatsapp.net", lt)
    _log(f"eod-personal: {sent} DMs + {len(leads)} lead report(s)")


def job_attendance_nag(cfg: dict) -> None:
    """10:30 -- DM anyone on the active roster who hasn't checked in yet."""
    if not cfg["bridge_ok"]:
        return
    j = _companion_get(cfg, "/api/companion/attendance-missing")
    if not j or j.get("weekend"):
        return
    sent = 0
    for p in j.get("missing_detail", []):
        wa = re.sub(r"\D", "", p.get("whatsapp", ""))
        if not wa:
            continue
        name = p.get("name") or "there"
        text = (f"Morning *{name}*,\n\nit's 10:30 and you're not checked in on Lumina yet.\n\n"
                "Tap check-in when you get a sec, or reply \"checking in\" here "
                "and I'll do it for you.")
        if _bridge_send(cfg, f"{wa}@s.whatsapp.net", text):
            sent += 1
    _log(f"attendance-nag: {sent} DM(s)")


def job_checkout_nudge(cfg: dict, second: bool = False) -> None:
    """22:30 first DM, 23:00 second DM -- only to people who checked in today and
    still haven't checked out (the list is recomputed live each time, so whoever
    checked out after the first nudge is not messaged again). Vidit/Kshitij are
    excluded server-side. At most two messages per person per day (the daily-job
    state is persisted, so a restart can't re-fire either)."""
    if not cfg["bridge_ok"]:
        return
    j = _companion_get(cfg, "/api/companion/checkout-missing")
    if not j:
        return
    link = cfg["url"].rstrip("/") + "/checkin.html?a=out"
    sent = 0
    for p in j.get("missing_checkout", []):
        wa = re.sub(r"\D", "", p.get("whatsapp", ""))
        if not wa:
            continue
        name = p.get("name") or "there"
        when = "11 PM" if second else "10:30 PM"
        head = "Reminder again" if second else "Hey"
        text = (f"{head} *{name}*,\n\nit's {when} and you haven't checked out on Lumina yet.\n\n"
                "Check out in one tap (works from your laptop or phone, it picks up the location "
                f"there):\n{link}\n\nOr just reply \"out\" here and I'll do it for you.")
        if _bridge_send(cfg, f"{wa}@s.whatsapp.net", text):
            sent += 1
    _log(f"checkout-nudge{'-2' if second else ''}: {sent} DM(s)")


def job_checkout_nudge_2(cfg: dict) -> None:
    job_checkout_nudge(cfg, second=True)


def job_tomorrow_live(cfg: dict) -> None:
    """09:30 and 18:30 -- DM Vidit (or whoever's configured) everything due
    to go live tomorrow, read straight from the Lumina Sheets/Notion data,
    so nothing scheduled for the next day gets missed."""
    if not cfg["bridge_ok"]:
        return
    if datetime.now().weekday() >= 5:      # Sat/Sun -- match every other nudge job
        _log("tomorrow-live: weekend — skipping")
        return
    j = _companion_get(cfg, "/api/companion/tomorrow-live")
    if not j:
        return
    text = j.get("text")
    if not text:
        return
    r = _companion_get(cfg, "/api/companion/content-calendar-recipients")
    recipients = (r or {}).get("recipients") or []
    if not recipients:
        _log("tomorrow-live: no recipient configured (content_calendar_recipient_ids)")
        return
    sent = 0
    for p in recipients:
        wa = re.sub(r"\D", "", p.get("whatsapp", ""))
        if wa and _bridge_send(cfg, f"{wa}@s.whatsapp.net", text):
            sent += 1
    _log(f"tomorrow-live: {sent} DM(s), {j.get('count', 0)} post(s) due tomorrow")


def job_rollcall(cfg: dict) -> None:
    grp = cfg["rollcall_group"]
    if not grp:
        return
    tok = _api_token(cfg)
    if not tok:
        _log("rollcall: no token (set STORAGE_SYNC_TOKEN)")
        return
    try:
        r = requests.get(f"{cfg['url']}/api/companion/attendance-missing",
                         headers={"Authorization": f"Bearer {tok}"}, timeout=45)
        if not r.ok:
            _log(f"rollcall: HTTP {r.status_code}")
            return
        j = r.json() or {}
    except Exception as e:
        _log(f"rollcall error: {e}")
        return
    if j.get("weekend"):
        _log("rollcall: weekend — skipping")
        return
    missing = j.get("missing") or []
    if not missing:
        _log("rollcall: everyone logged in — no message sent")
        return
    text = j.get("text") or ("Not logged in yet: " + ", ".join(missing))
    _bridge_send(cfg, grp, text)


# ── wall-clock daily jobs ────────────────────────────────────────────────
# _daily_fired is what stops a daily job (standup-nudge, digest, eod, ...)
# firing more than once per day within its _daily_due() window. It used to
# be in-memory only -- fine as long as the process stays up, but
# job_selfupdate() re-execs the whole process (os.execv, see above) whenever
# a scripts/*.py file changes on origin, and a fresh process starts with an
# empty dict. If that restart happens while a job is still inside its
# window (e.g. a scripts/ change lands on origin at 11:40, ten minutes
# after the 11:30 standup nudge already fired), the reset dict makes it
# look like the job never fired today, so it fires again -- once per
# restart. Confirmed live: the same "your standup is still blank" DM went
# out 3 times in one morning to the same person, each ~6-10 min apart,
# matching self-update's poll interval during a stretch of active
# scripts/ development. Fixed by persisting this to a small JSON file next
# to the knowledge folder (survives the re-exec, since it's the one thing
# on disk rather than in the replaced process's memory) instead of trying
# to preserve state across execv another way.
_daily_fired: dict = {}


def _daily_state_path(cfg: dict) -> Path:
    return cfg["dir"] / "daily_jobs_state.json"


def _load_daily_fired(cfg: dict) -> None:
    global _daily_fired
    try:
        p = _daily_state_path(cfg)
        if p.exists():
            _daily_fired = json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception as e:
        _log(f"daily-state load failed (starting empty): {e}")
        _daily_fired = {}


def _save_daily_fired(cfg: dict) -> None:
    try:
        _daily_state_path(cfg).write_text(json.dumps(_daily_fired), encoding="utf-8")
    except Exception as e:
        _log(f"daily-state save failed: {e}")


def _daily_due(target_hhmm: str, now: datetime, window_min: int = 40) -> bool:
    try:
        h, m = (int(x) for x in target_hhmm.split(":"))
    except Exception:
        return False
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    delta = (now - target).total_seconds()
    return 0 <= delta < window_min * 60


def _run_daily(daily_jobs: list, cfg: dict) -> None:
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    for name, fn, hhmm in daily_jobs:
        if _daily_fired.get(name) == today:
            continue
        if _daily_due(hhmm, now):
            _daily_fired[name] = today
            _save_daily_fired(cfg)
            try:
                fn(cfg)
            except Exception as e:
                _log(f"{name} error: {e}")


# ── main ─────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Always-on companion for Lumina.")
    ap.add_argument("--dir", default="./knowledge", help="knowledge folder")
    ap.add_argument("--url", default=os.getenv("LUMINA_URL", DEFAULT_URL))
    ap.add_argument("--backup-dir", default="")
    ap.add_argument("--backup-mirror", default="",
                    help="second folder to copy each backup into (e.g. a OneDrive/Drive path)")
    ap.add_argument("--sync-every", type=int, default=30)
    ap.add_argument("--research-every", type=int, default=3600)
    ap.add_argument("--backup-every", type=int, default=86400)
    ap.add_argument("--health-every", type=int, default=300)
    ap.add_argument("--sheets-every", type=int, default=1200)
    ap.add_argument("--outbox-every", type=int, default=20,
                    help="how often to check for queued proactive WhatsApp messages")
    ap.add_argument("--voice-calls-every", type=int, default=30,
                    help="how often to check for queued announcement calls")
    ap.add_argument("--sheets-autopull", action="store_true",
                    help="also force a reconcile when a sheet looks out of sync")
    ap.add_argument("--slow-ms", type=int, default=8000, help="alert if /api/health is slower than this")
    ap.add_argument("--digest-morning", default="09:00")
    ap.add_argument("--digest-evening", default="19:00")
    ap.add_argument("--no-digest", action="store_true")
    ap.add_argument("--digest-group", action="store_true",
                    help="post the daily brief to the team WhatsApp group "
                         "(default: DM it to the alert recipients)")
    ap.add_argument("--no-digest-dm", action="store_true",
                    help="don't fall back to a WhatsApp DM for the daily brief")
    ap.add_argument("--rollcall-time", default="12:00",
                    help="daily time to post the attendance roll-call to the group")
    ap.add_argument("--rollcall-group", default=os.getenv("ROLLCALL_GROUP_ID", ""),
                    help="WhatsApp group id (digits or ...@g.us) for the roll-call; "
                         "empty = feature off")
    ap.add_argument("--no-rollcall", action="store_true")
    ap.add_argument("--lunch-time", default="14:00",
                    help="daily time to post the 'go for lunch' nudge to the group")
    ap.add_argument("--no-lunch", action="store_true")
    ap.add_argument("--meeting-time", default="15:00",
                    help="Mon/Wed/Fri time to post the 4-5pm team meeting reminder to the group")
    ap.add_argument("--no-meeting", action="store_true")
    ap.add_argument("--class-meeting-time", default="10:28",
                    help="Tue/Thu time to tag everyone in the group with the class meeting link")
    ap.add_argument("--no-class-meeting", action="store_true")
    ap.add_argument("--eod-group-time", default="17:00",
                    help="daily time to post the 'tasks done today' summary to the group")
    ap.add_argument("--eod-heads-up-time", default="16:55",
                    help="daily time to DM people with open tasks just before the 5 PM group summary")
    ap.add_argument("--eod-personal-time", default="19:30",
                    help="daily time to DM everyone their still-open tasks + the leads report")
    ap.add_argument("--no-eod", action="store_true")
    ap.add_argument("--weekly-wrap-time", default="18:00",
                    help="Friday time to post the weekly completed-tasks wrap to the group")
    ap.add_argument("--no-weekly", action="store_true")
    ap.add_argument("--standup-nudge-time", default="11:30",
                    help="daily time to DM people who haven't added a standup task")
    ap.add_argument("--no-standup-nudge", action="store_true")
    ap.add_argument("--attendance-nag-time", default="10:30",
                    help="daily time to DM people who haven't checked in yet")
    ap.add_argument("--no-attendance-nag", action="store_true")
    # unified login nudge (gotcha #119): the group gets the not-logged-in
    # list first at 11:15, then personal DMs hourly for whoever still
    # hasn't both checked in AND put a task on today's standup.
    # Supersedes the old 10:30 attendance-nag / 11:30 standup-nudge / noon
    # roll-call -- pass --legacy-nudges to keep those too.
    ap.add_argument("--no-login-nudge", action="store_true",
                    help="turn off the recurring 'you haven't logged in' group ping")
    ap.add_argument("--login-nudge-dm", action="store_true",
                    help="also send the personal hourly login-nudge DM "
                         "(OFF by default as of 2026-09-28 -- user asked to stop "
                         "the recurring per-person attendance ping; the twice-daily "
                         "group list at --login-group-times is unaffected)")
    ap.add_argument("--login-nudge-every", type=int, default=3600,
                    help="seconds between personal login-nudge DMs (default 3600 = hourly)")
    ap.add_argument("--login-nudge-start", default="11:20",
                    help="HH:MM -- don't send personal login-nudge DMs before this "
                         "(after the first group ping, so the group heads-up lands first)")
    ap.add_argument("--login-nudge-end", default="19:00",
                    help="HH:MM -- don't send login-nudge DMs after this")
    ap.add_argument("--login-group-times", default="11:15,11:45",
                    help="comma-separated HH:MM times to post the not-logged-in "
                         "list to the team group (first one leads the sequence)")
    ap.add_argument("--legacy-nudges", action="store_true",
                    help="also run the old separate 10:30 attendance-nag, 11:30 "
                         "standup-nudge and noon roll-call (all superseded by "
                         "the unified login nudge)")
    ap.add_argument("--checkout-nudge-time-2", default="23:00",
                    help="second reminder for anyone still not checked out")
    ap.add_argument("--checkout-nudge-time", default="22:30",
                    help="daily time to DM anyone who checked in today but hasn't "
                         "checked out (one message; Vidit/Kshitij excluded)")
    ap.add_argument("--no-checkout-nudge", action="store_true")
    ap.add_argument("--tomorrow-live-morning", default="09:30",
                    help="daily morning time to DM the content-calendar lead (default "
                         "Vidit) everything due to go live tomorrow")
    ap.add_argument("--tomorrow-live-evening", default="18:30",
                    help="daily evening time for the same tomorrow-live reminder")
    ap.add_argument("--no-tomorrow-live", action="store_true")
    ap.add_argument("--bridge-dir", default="",
                    help="path to whatsapp-bridge/ (default: sibling of this repo's scripts/)")
    ap.add_argument("--no-bridge", action="store_true", help="don't supervise the WhatsApp bridge")
    ap.add_argument("--no-self-update", action="store_true",
                    help="don't auto git-pull scripts/ + whatsapp-bridge/ from origin/main")
    ap.add_argument("--self-update-every", type=int, default=600)
    ap.add_argument("--self-update-branch", default="main")
    args = ap.parse_args()

    root = Path(args.dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    # persistent log file — matters when launched with pythonw (no console)
    global _LOG_FH
    try:
        _ld = root.parent / "lumina-logs"
        _ld.mkdir(parents=True, exist_ok=True)
        _lp = _ld / "agent.log"
        if _lp.exists() and _lp.stat().st_size > 3_000_000:
            _lp.replace(_ld / "agent.log.1")
        _LOG_FH = open(_lp, "a", buffering=1, encoding="utf-8", errors="replace")
    except Exception:
        _LOG_FH = None

    bridge_dir = (Path(args.bridge_dir).expanduser().resolve() if args.bridge_dir
                  else Path(__file__).resolve().parent.parent / "whatsapp-bridge")
    node = shutil.which("node") or ""
    bridge_ok = (
        not args.no_bridge
        and bool(node)
        and (bridge_dir / "index.js").exists()
        and (bridge_dir / "node_modules").exists()
        and bool(os.getenv("WHATSAPP_BRIDGE_TOKEN") or os.getenv("STORAGE_SYNC_TOKEN")
                 or os.getenv("FLASK_SECRET_KEY"))
    )

    backup_dir = (Path(args.backup_dir).expanduser().resolve() if args.backup_dir
                  else root.parent / "lumina-backups")
    cfg = {
        "dir": root,
        "url": args.url.rstrip("/"),
        "backup_dir": backup_dir,
        "backup_mirror": Path(args.backup_mirror).expanduser().resolve() if args.backup_mirror else None,
        "rclone_remote": os.getenv("BACKUP_RCLONE_REMOTE", ""),
        "log_dir": root.parent / "lumina-logs",
        "storage_token": os.getenv("STORAGE_SYNC_TOKEN") or os.getenv("FLASK_SECRET_KEY") or "",
        "db_secret": os.getenv("FLASK_SECRET_KEY") or os.getenv("SECRET_KEY") or "",
        "alert_webhook": os.getenv("ALERT_WEBHOOK", ""),
        "alert_targets": [t.strip() for t in os.getenv("ALERT_TARGETS", "").split(",") if t.strip()],
        "digest_targets": [t.strip() for t in os.getenv("DIGEST_TARGETS", "").split(",") if t.strip()],
        "digest_group": args.digest_group,
        "digest_dm": not args.no_digest_dm,
        "bridge_dir": bridge_dir,
        "bridge_ok": bridge_ok,
        "node": node,
        "slow_ms": args.slow_ms,
        "sheets_autopull": args.sheets_autopull,
        "sync_every": args.sync_every,
        "research_every": args.research_every,
        "backup_every": args.backup_every,
        "health_every": args.health_every,
        "sheets_every": args.sheets_every,
        "outbox_every": args.outbox_every,
        "voice_calls_every": args.voice_calls_every,
        "login_nudge": args.login_nudge_dm,
        "login_nudge_every": max(60, args.login_nudge_every),
        "login_nudge_start": args.login_nudge_start,
        "login_nudge_end": args.login_nudge_end,
        "bridge_every": 15,
        "bridge_health_every": 180,
        "repo_dir": Path(__file__).resolve().parent.parent,
        "self_update": not args.no_self_update,
        "self_update_every": args.self_update_every,
        "self_update_branch": args.self_update_branch,
        "bridge_http_port": int(os.getenv("BRIDGE_HTTP_PORT", "8787")),
        "rollcall_group": (lambda d: f"{d}@g.us" if d else "")(
            re.sub(r"\D", "", args.rollcall_group or "")),
    }

    _acquire_singleton_lock(cfg)   # exits immediately if another instance is already up

    _load_daily_fired(cfg)

    daily_jobs = []
    if not args.no_digest:
        daily_jobs += [
            ("digest-morning", job_digest_morning, args.digest_morning),
            ("digest-evening", job_digest_evening, args.digest_evening),
        ]
    if args.legacy_nudges and not args.no_rollcall and cfg["rollcall_group"]:
        daily_jobs.append(("rollcall", job_rollcall, args.rollcall_time))
    if not args.no_login_nudge and cfg["rollcall_group"]:
        _gt = [x.strip() for x in (args.login_group_times or "").split(",") if x.strip()]
        for _i, _t in enumerate(_gt):
            daily_jobs.append((f"login-group-{_i}", job_login_group_ping, _t))
    if not args.no_lunch and cfg["rollcall_group"]:
        daily_jobs.append(("lunch", job_lunch, args.lunch_time))
    if not args.no_meeting and cfg["rollcall_group"]:
        daily_jobs.append(("team-meeting", job_team_meeting, args.meeting_time))
    if not args.no_class_meeting and cfg["rollcall_group"]:
        daily_jobs.append(("class-meeting", job_class_meeting, args.class_meeting_time))
    if not args.no_eod:
        if cfg["rollcall_group"]:
            daily_jobs.append(("eod-heads-up", job_eod_heads_up, args.eod_heads_up_time))
            daily_jobs.append(("eod-group", job_eod_group, args.eod_group_time))
        daily_jobs.append(("eod-personal", job_eod_personal, args.eod_personal_time))
    if not args.no_weekly and cfg["rollcall_group"]:
        daily_jobs.append(("weekly-wrap", job_weekly_wrap, args.weekly_wrap_time))
    if args.legacy_nudges and not args.no_standup_nudge:
        daily_jobs.append(("standup-nudge", job_standup_nudge, args.standup_nudge_time))
    if args.legacy_nudges and not args.no_attendance_nag:
        daily_jobs.append(("attendance-nag", job_attendance_nag, args.attendance_nag_time))
    if not args.no_checkout_nudge:
        daily_jobs.append(("checkout-nudge", job_checkout_nudge, args.checkout_nudge_time))
        daily_jobs.append(("checkout-nudge-2", job_checkout_nudge_2, args.checkout_nudge_time_2))
    if not args.no_tomorrow_live:
        daily_jobs.append(("tomorrow-live-am", job_tomorrow_live, args.tomorrow_live_morning))
        daily_jobs.append(("tomorrow-live-pm", job_tomorrow_live, args.tomorrow_live_evening))

    if not bridge_ok and not args.no_bridge:
        reason = ("node not on PATH" if not node
                  else "whatsapp-bridge/ missing or `npm install` not run"
                  if not (bridge_dir / "node_modules").exists() else "no token")
        bridge_note = f"OFF ({reason})"
    else:
        bridge_note = "OFF (--no-bridge)" if args.no_bridge else "on"

    tok = cfg["storage_token"] or cfg["db_secret"]
    _log(f"laptop_agent up — {root}  <->  {cfg['url']}")
    _log("  sync {} | research {} | backup {}{} | health on | sheets {} | digest {} | rollcall {} | bridge {}".format(
        "on" if cfg["storage_token"] else "OFF (no STORAGE_SYNC_TOKEN)",
        "on" if (root / research_feed.SOURCES_FILE).exists() else "OFF (no research_sources.txt)",
        "on" if cfg["db_secret"] else "OFF (no FLASK_SECRET_KEY)",
        (" +mirror" if cfg["backup_mirror"] else "") + (" +rclone" if cfg["rclone_remote"] else ""),
        "on" if tok else "OFF (no token)",
        "OFF (--no-digest)" if args.no_digest else (
            f"{args.digest_morning}/{args.digest_evening}" if tok else "OFF (no token)"),
        f"{args.rollcall_time}" if (cfg["rollcall_group"] and args.legacy_nudges
                                    and not args.no_rollcall and tok)
        else "OFF (no group)" if not cfg["rollcall_group"] else "OFF (legacy; use --legacy-nudges)",
        bridge_note,
    ))
    _log("  login-nudge personal-DM {}".format(
        f"every {cfg['login_nudge_every']}s {cfg['login_nudge_start']}-{cfg['login_nudge_end']}"
        if (cfg["login_nudge"] and cfg["bridge_ok"] and tok)
        else "OFF (pass --login-nudge-dm to re-enable)" if not cfg["login_nudge"]
        else "OFF (needs bridge + token)"))
    _log("  login-nudge group-ping {}".format(
        f"{args.login_group_times}"
        if (not args.no_login_nudge and cfg["rollcall_group"] and cfg["bridge_ok"] and tok)
        else "OFF (--no-login-nudge)" if args.no_login_nudge
        else "OFF (no group / bridge / token)"))
    _log("  legacy-nudges {}".format(
        "on (standup-nudge + attendance-nag + noon roll-call)" if args.legacy_nudges
        else "off (unified login-nudge replaces them)"))
    _log("  checkout-nudge {}".format(
        args.checkout_nudge_time if (not args.no_checkout_nudge and cfg["bridge_ok"] and tok)
        else "OFF (--no-checkout-nudge)" if args.no_checkout_nudge else "OFF (needs bridge + token)"))
    _log("  tomorrow-live {}".format(
        f"{args.tomorrow_live_morning}/{args.tomorrow_live_evening}"
        if (not args.no_tomorrow_live and cfg["bridge_ok"] and tok)
        else "OFF (--no-tomorrow-live)" if args.no_tomorrow_live else "OFF (needs bridge + token)"))
    _log("  wa-outbox {}".format(
        f"every {args.outbox_every}s" if (cfg["bridge_ok"] and tok)
        else "OFF (needs bridge + token)"))
    _log("  voice-calls {}".format(
        f"every {args.voice_calls_every}s (needs voice.js set up on the bridge -- see CLAUDE.md)"
        if (cfg["bridge_ok"] and tok)
        else "OFF (needs bridge + token)"))
    _log("  bridge-health {}".format(
        f"every {cfg['bridge_health_every']}s (alerts on logout / crashloop / >8m offline)"
        if cfg["bridge_ok"] else "OFF (needs bridge)"))
    _log("  self-update {}".format(
        f"every {cfg['self_update_every']}s from origin/{cfg['self_update_branch']} (ff-only)"
        if (cfg["self_update"] and (cfg["repo_dir"] / ".git").exists() and shutil.which("git"))
        else "OFF (--no-self-update)" if not cfg["self_update"] else "OFF (not a git checkout)"))
    _log("  lunch nudge {}".format(
        args.lunch_time if (cfg["rollcall_group"] and not args.no_lunch)
        else "OFF (--no-lunch)" if args.no_lunch else "OFF (no group)"))
    _log("  team-meeting {}".format(
        f"Mon/Wed/Fri {args.meeting_time}" if (cfg["rollcall_group"] and not args.no_meeting)
        else "OFF (--no-meeting)" if args.no_meeting else "OFF (no group)"))
    _log("  class-meeting {}".format(
        f"Tue/Thu {args.class_meeting_time} (tags all, {_CLASS_MEETING_LINK})"
        if (cfg["rollcall_group"] and not args.no_class_meeting)
        else "OFF (--no-class-meeting)" if args.no_class_meeting else "OFF (no group)"))
    _log("  eod {}".format(
        "OFF (--no-eod)" if args.no_eod else
        f"group {args.eod_group_time} / personal {args.eod_personal_time}" if tok
        else "OFF (no token)"))
    _log("  weekly-wrap {}".format(
        "OFF (--no-weekly)" if args.no_weekly else
        f"Fri {args.weekly_wrap_time}" if (cfg["rollcall_group"] and tok)
        else "OFF (no group)"))
    ch = (f"apprise x{len(cfg['alert_targets'])}" if cfg["alert_targets"] and apprise
          else "webhook" if cfg["alert_webhook"] else "console-only")
    _log(f"  alert channel: {ch}")
    dch = (f"apprise x{len(cfg['digest_targets'] or cfg['alert_targets'])}"
           if (cfg["digest_targets"] or cfg["alert_targets"]) and apprise
           else "webhook" if cfg["alert_webhook"]
           else "WhatsApp group" if (cfg["digest_group"] and cfg["rollcall_group"])
           else "WhatsApp DM to alert recipients" if cfg["digest_dm"] and cfg["bridge_ok"]
           else "console-only")
    _log(f"  digest channel: {dch}")

    next_run = {name: 0.0 for name, _, _ in INTERVAL_JOBS}
    try:
        while True:
            now = time.monotonic()
            for name, fn, every_key in INTERVAL_JOBS:
                if now >= next_run[name]:
                    try:
                        fn(cfg)
                    except Exception as e:
                        _log(f"{name} error: {e}")
                    next_run[name] = now + cfg[every_key]
            if daily_jobs:
                _run_daily(daily_jobs, cfg)
            time.sleep(5)
    except KeyboardInterrupt:
        _log("stopped.")
    finally:
        _stop_bridge(cfg)
        _release_singleton_lock()


if __name__ == "__main__":
    main()
