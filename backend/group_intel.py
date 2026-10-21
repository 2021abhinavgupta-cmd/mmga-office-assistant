"""Group intelligence -- the assistant follows what people PROMISE in the team
group and chases it when nothing shows it happened.

Layer on top of group_memory.py (which just stores messages, free):

  1. extract_pending()  every few minutes, one small model call over the NEW
     group messages. It returns commitments ("I'll post the story today"),
     completions ("posted it"), cancellations and date changes. Every item must
     carry the exact quote it came from and that quote is checked against the
     real messages -- an item the messages don't back up is thrown away, so the
     model can't invent a promise. (Extract-then-verify pattern; owner is never
     guessed, an unknown owner becomes "team" and is never nudged.)
  2. commitments live in `group_commitments` with status open/done/dropped.
  3. commitment_items() feeds followups.py: at end of day (or when overdue) it
     first looks for evidence the thing was done (a ticked standup task, a
     Notion task that is Posted/Final/Done) and only if there is none does it
     surface "you said X in the group, is it done?" to the owner. The existing
     followup rails (working hours, leave, 20h cooldown, 2/day cap) still apply.

Nothing here sends a message itself. Cost is one small call per batch of new
messages (none when the group is quiet).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from db import get_connection

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))
MAX_BATCH = 60            # new messages handled per group per pass
CONTEXT_LINES = 12        # older messages shown for context only
MAX_NUDGES = 3            # stop chasing one item after this many nudges
MAX_FAILS = 3
_FAILS: dict = {}         # group_id -> consecutive failed passes (in memory)
STALE_UNPROCESSED_HOURS = 24
END_OF_DAY_HOUR, END_OF_DAY_MIN = 16, 30     # due-today items surface from here
NO_DUE_SAME_DAY_HOUR = 17                    # undated promise made today

_CLOSED_STATUS = {"done", "complete", "completed", "posted", "final", "approved",
                  "need for approval", "need_for_approval", "published"}
_STOP = {"the", "a", "an", "to", "of", "for", "and", "on", "in", "my", "our", "it",
         "this", "that", "i", "will", "ill", "we", "is", "be", "do", "post",
         "today", "tomorrow", "please", "pls", "with", "at", "by", "up"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today_ist() -> str:
    return datetime.now(_IST).strftime("%Y-%m-%d")


def ensure_schema(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS group_commitments (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id      TEXT NOT NULL,
        group_name    TEXT,
        owner_name    TEXT,
        owner_id      TEXT,
        action        TEXT NOT NULL,
        due_date      TEXT,
        quote         TEXT,
        source_ts     TEXT,
        status        TEXT NOT NULL DEFAULT 'open',
        evidence      TEXT,
        nudge_count   INTEGER NOT NULL DEFAULT 0,
        last_nudged_at TEXT,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gc_status ON group_commitments (status, owner_id)")
    for col, typ in (("requester_name", "TEXT"), ("requester_id", "TEXT"),   # who directed the work (an "ask"); empty for self-promises
                     ("auto_task_id", "INTEGER")):                           # standup row group_actions created for an ask
        try:
            conn.execute(f"ALTER TABLE group_commitments ADD COLUMN {col} {typ}")
        except Exception:
            pass
    # decisions and client preferences ("client approved the blue logo", "Mellow
    # wants captions in Hinglish") -- the assistant's memory of what was settled
    conn.execute("""CREATE TABLE IF NOT EXISTS group_facts (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id    TEXT NOT NULL,
        kind        TEXT NOT NULL,
        client      TEXT,
        text        TEXT NOT NULL,
        quote       TEXT,
        source_ts   TEXT,
        created_at  TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gf_client ON group_facts (client)")
    # mistakes the boss corrected ("that wasn't a promise") -- fed back as examples
    conn.execute("""CREATE TABLE IF NOT EXISTS group_feedback (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        example    TEXT NOT NULL,
        note       TEXT,
        created_at TEXT NOT NULL
    )""")
    try:
        conn.execute("ALTER TABLE wa_group_log ADD COLUMN processed INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass   # already there


# ── roster / text helpers ────────────────────────────────────────────────────

def _roster() -> list:
    try:
        import utils
        return [e for e in utils._load_employees().get("employees", [])
                if str(e.get("status", "active")).strip().lower()
                not in {"inactive", "disabled", "left", "removed", "archived", "former"}]
    except Exception:
        return []


def _owner(name: str, roster: list) -> tuple:
    """(display name, employee id or ''). Never guesses: no unambiguous roster
    match -> ('team', '')."""
    n = re.sub(r"[^a-z0-9 ]", "", str(name or "").lower()).strip()
    if not n or n in {"team", "everyone", "all", "someone", "unknown"}:
        return ("team", "")
    hits = []
    for e in roster:
        en = re.sub(r"[^a-z0-9 ]", "", str(e.get("name") or "").lower()).strip()
        if en and (en == n or en.split()[0] == n.split()[0]):
            hits.append(e)
    if len(hits) == 1:
        return (hits[0].get("name") or "", hits[0].get("id") or "")
    return ("team", "")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", str(s or "").lower())).strip()


def _grounded(quote: str, texts: list) -> bool:
    """The quote must really be in the messages (or very nearly): the guard
    that stops the model inventing a promise nobody made."""
    q = _norm(quote)
    if len(q) < 6:
        return False
    for t in texts:
        nt = _norm(t)
        if q in nt or nt in q and len(nt) >= 12:
            return True
        qa, ta = set(q.split()), set(nt.split())
        if qa and len(qa & ta) / len(qa) >= 0.8:
            return True
    return False


def _keywords(s: str) -> set:
    return {w for w in _norm(s).split() if len(w) > 2 and w not in _STOP}


def _overlap(a: str, b: str) -> float:
    ka, kb = _keywords(a), _keywords(b)
    return (len(ka & kb) / len(ka)) if ka else 0.0


def _valid_date(s) -> str:
    try:
        d = datetime.strptime(str(s or "").strip()[:10], "%Y-%m-%d").date()
        today = datetime.now(_IST).date()
        if today - timedelta(days=1) <= d <= today + timedelta(days=60):
            return d.isoformat()
    except Exception:
        pass
    return ""


# ── extraction ───────────────────────────────────────────────────────────────

_SYSTEM = (
    "You read a team's WhatsApp group chat and track commitments. Reply with "
    "ONE JSON object and nothing else.\n"
    "A commitment is a clear promise or assignment about work: 'I'll post the "
    "story today', 'Nupur will send the logo by 5', 'can you share the report "
    "tomorrow?' answered with 'sure'. NOT commitments: jokes, vague wishes, "
    "questions, things already finished, general chat.\n"
    "Schema: {\"new\":[{\"owner\":\"exact name from the team list, or team\","
    "\"action\":\"short imperative phrase\",\"due\":\"YYYY-MM-DD or empty\","
    "\"quote\":\"the exact words from one message\"}],"
    "\"done\":[{\"id\":<open commitment id>,\"quote\":\"exact words showing it is "
    "done\"}],\"dropped\":[{\"id\":<id>,\"quote\":\"exact words cancelling it\"}],"
    "\"moved\":[{\"id\":<id>,\"due\":\"YYYY-MM-DD\",\"quote\":\"...\"}],"
    "\"asks\":[{\"from\":\"who is asking, exact team name\",\"to\":\"the person called out, "
    "exact team name\",\"action\":\"short imperative phrase\",\"due\":\"YYYY-MM-DD or empty\","
    "\"quote\":\"exact words\"}],"
    "\"facts\":[{\"kind\":\"decision, preference, feedback or information\",\"client\":\"client name or empty\","
    "\"text\":\"one short sentence\",\"quote\":\"exact words\"}]}.\n"
    "asks: a message where someone tags or names a teammate and tells them to do "
    "something ('@Charu all collab with stempedia', 'Abhinav press on twinkle to accept "
    "it'). The speaker is 'from', the person called out is 'to'. Mentions appear as "
    "@Name. A self-promise ('I'll message twinkle') goes in new, not asks.\n"
    "facts: a decision is something settled ('client approved the blue logo', "
    "'posting moved to Friday'); a preference is how a client or the team likes "
    "things done ('Mellow wants captions in Hinglish'). feedback is what a CLIENT said "
    "about our work ('Omotec didn't like the second reel', 'Mellow approved the calendar') "
    "and must name the client; information is a lasting fact worth remembering that is "
    "not a decision or preference ('Agrius launch is on 3 Nov'). Only clear, lasting ones.\n"
    "Lines tagged [voice note] or [image] are transcripts/descriptions of media; "
    "treat them like normal messages.\n"
    "Rules: every item needs a quote copied EXACTLY from a message. The owner is "
    "the person who will do the work (if someone says 'I will', that speaker). "
    "Never guess an owner: use team if unclear. Resolve 'today', 'tomorrow', "
    "'Friday' to a real date from today's date; leave due empty if no time was "
    "given. Mark an open commitment done only if a message clearly says it is "
    "finished (posted, sent, shared, done). Empty lists are the normal answer.\n"
    "The chat is English, Hindi and Hinglish mixed. Examples:\n"
    "- 'kal tak bhej dunga' / 'main kar deti hu' / 'ho jayega aaj' = a commitment "
    "by the speaker.\n"
    "- 'ho gaya' / 'post kar diya' / 'bhej diya' / 'done' = completed, if it "
    "clearly refers to an open commitment.\n"
    "- 'Nupur, deck bhej do' followed by Nupur: 'ok' / 'haan' = a commitment by Nupur.\n"
    "- 'dekhte hain' / 'try karunga' / 'shayad' = too vague, NOT a commitment.\n"
    "- 'rehne do' / 'cancel' / 'client ne mana kar diya' = the open item is dropped."
)


def _use_claude() -> bool:
    """GROUP_INTEL_LLM=claude (env or app_settings 'group_intel_llm') runs just
    this extraction step on Claude Haiku instead of Qwen -- more accurate at
    judging what counts as a promise, a few cents a day more."""
    import os
    v = os.getenv("GROUP_INTEL_LLM", "").strip().lower()
    if not v:
        try:
            conn = get_connection()
            row = conn.execute("SELECT value FROM app_settings WHERE key='group_intel_llm'").fetchone()
            conn.close()
            v = str(row[0]).strip().lower() if row and row[0] else ""
        except Exception:
            v = ""
    return v == "claude"


def _feedback_examples() -> str:
    """Recent mistakes the boss corrected, appended to the instructions so the
    extractor stops repeating them."""
    try:
        conn = get_connection()
        ensure_schema(conn)
        rows = conn.execute("SELECT example, note FROM group_feedback ORDER BY id DESC LIMIT 8").fetchall()
        conn.close()
    except Exception:
        return ""
    if not rows:
        return ""
    lines = [f"- \"{r[0][:140]}\"" + (f" ({r[1][:80]})" if r[1] else "") for r in rows]
    return ("\nLEARNED FROM CORRECTIONS -- messages like these were NOT commitments, "
            "do not extract them:\n" + "\n".join(lines))


def _llm_json(prompt: str) -> dict | None:
    import whatsapp_agent as wa
    budget = wa.check_budget_available()
    if not budget.get("allowed"):
        return None
    model = wa.get_model_for_task("whatsapp")
    usage: dict = {}
    system = _SYSTEM + _feedback_examples()
    if _use_claude():
        model = {**wa.MODEL_COSTS_SONNET, "tier": "sonnet", "task": "whatsapp"}
        resp = wa._llm_create(model, [{"type": "text", "text": system}], None,
                              [{"role": "user", "content": prompt}], 1500, usage, smart=True)
    else:
        resp = wa._llm_create(model, [{"type": "text", "text": system}], None,
                              [{"role": "user", "content": prompt}], 900, usage)
    try:
        wa._record_llm_usage(model, usage, "wa_group_intel")
    except Exception:
        pass
    text = "".join(getattr(b, "text", "") for b in resp.content
                   if getattr(b, "type", "") == "text")
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _fmt_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone(_IST).strftime("%a %d %b %H:%M")
    except Exception:
        return ""


def extract_group(conn, group_id: str) -> dict:
    """Process the unprocessed messages of one group. Returns counts."""
    cutoff = (_now() - timedelta(hours=STALE_UNPROCESSED_HOURS)).isoformat()
    # very old backlog is just marked, not analysed (a restart must not replay a day)
    conn.execute("UPDATE wa_group_log SET processed=1 WHERE group_id=? AND processed=0 "
                 "AND created_at<?", (group_id, cutoff))
    rows = conn.execute(
        "SELECT id, group_name, sender_name, text, created_at FROM wa_group_log "
        "WHERE group_id=? AND processed=0 ORDER BY id LIMIT ?", (group_id, MAX_BATCH)).fetchall()
    if not rows:
        return {"new": 0}
    ids = [r[0] for r in rows]
    group_name = rows[-1][1] or ""
    new_lines = [f"{_fmt_time(r[4])} {r[2]}: {r[3]}" for r in rows]
    # nothing worth a model call (a few 'ok's and emojis) -- but a short "done"
    # can close an open promise, so only skip when nothing is open
    has_open = conn.execute("SELECT 1 FROM group_commitments WHERE group_id=? AND status='open' "
                            "LIMIT 1", (group_id,)).fetchone()
    if sum(len(r[3]) for r in rows) < 25 and not has_open:
        conn.execute(f"UPDATE wa_group_log SET processed=1 WHERE id IN ({','.join('?'*len(ids))})", ids)
        return {"new": 0, "skipped": "trivial"}

    ctx = conn.execute(
        "SELECT sender_name, text, created_at FROM wa_group_log WHERE group_id=? AND processed=1 "
        "ORDER BY id DESC LIMIT ?", (group_id, CONTEXT_LINES)).fetchall()
    ctx_lines = [f"{_fmt_time(r[2])} {r[0]}: {r[1]}" for r in reversed(ctx)]
    open_rows = conn.execute(
        "SELECT id, owner_name, action, due_date FROM group_commitments "
        "WHERE group_id=? AND status='open' ORDER BY id DESC LIMIT 40", (group_id,)).fetchall()
    roster = _roster()
    names = ", ".join(e.get("name", "") for e in roster)
    open_txt = "\n".join(f"#{r[0]} {r[1]}: {r[2]}" + (f" (due {r[3]})" if r[3] else "")
                         for r in open_rows) or "(none)"
    now_ist = datetime.now(_IST)
    prompt = (f"Today is {now_ist.strftime('%A %Y-%m-%d')}, time {now_ist.strftime('%H:%M')} IST.\n"
              f"Team members: {names}\n\nOpen commitments:\n{open_txt}\n\n"
              f"Earlier messages (context only, do not extract from these):\n"
              f"{chr(10).join(ctx_lines) or '(none)'}\n\n"
              f"NEW messages to analyse:\n{chr(10).join(new_lines)}")
    try:
        data = _llm_json(prompt)
    except Exception:
        # A batch that keeps failing must not be retried (and billed) forever:
        # after MAX_FAILS passes it is marked processed and skipped.
        _FAILS[group_id] = _FAILS.get(group_id, 0) + 1
        logger.exception("group_intel: model call failed for %s (%s/%s)",
                         group_id, _FAILS[group_id], MAX_FAILS)
        if _FAILS[group_id] >= MAX_FAILS:
            _FAILS[group_id] = 0
            conn.execute(f"UPDATE wa_group_log SET processed=1 WHERE id IN ({','.join('?'*len(ids))})", ids)
            return {"new": 0, "skipped": "gave up after repeated failures"}
        return {"new": 0, "skipped": "model error, will retry"}
    _FAILS.pop(group_id, None)
    if data is None:       # budget exhausted: leave them for the next pass
        return {"new": 0, "skipped": "budget"}

    texts = [r[3] for r in rows]
    open_ids = {r[0] for r in open_rows}
    now_iso = _now().isoformat()
    counts = {"new": 0, "done": 0, "dropped": 0, "moved": 0, "rejected": 0}
    # who said the quoted line -> source time
    def _src_ts(q):
        nq = _norm(q)
        for r in rows:
            if nq and nq in _norm(r[3]):
                return r[4]
        return rows[-1][4]

    for it in (data.get("new") or [])[:10]:
        if not isinstance(it, dict):
            continue
        action = re.sub(r"\s+", " ", str(it.get("action") or "")).strip()[:200]
        quote = str(it.get("quote") or "")
        if not action or not _grounded(quote, texts):
            counts["rejected"] += 1
            continue
        oname, oid = _owner(it.get("owner"), roster)
        # skip a duplicate of something already open for the same owner
        dup = any(_overlap(action, r[2]) >= 0.7 and (r[1] or "") == oname for r in open_rows)
        if dup:
            continue
        conn.execute(
            "INSERT INTO group_commitments (group_id, group_name, owner_name, owner_id, action, "
            "due_date, quote, source_ts, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (group_id, group_name, oname, oid, action, _valid_date(it.get("due")) or None,
             quote[:300], _src_ts(quote), "open", now_iso, now_iso))
        counts["new"] += 1

    counts["asks"] = 0
    actions: list = []     # run by group_actions AFTER this transaction commits
    for it in (data.get("asks") or [])[:10]:
        if not isinstance(it, dict):
            continue
        action = re.sub(r"\s+", " ", str(it.get("action") or "")).strip()[:200]
        quote = str(it.get("quote") or "")
        if not action or not _grounded(quote, texts):
            counts["rejected"] += 1
            continue
        oname, oid = _owner(it.get("to"), roster)
        if not oid:        # an ask is only tracked when the person called out is a real teammate
            continue
        rname, rid = _owner(it.get("from"), roster)
        if any(_overlap(action, r[2]) >= 0.7 and (r[1] or "") == oname for r in open_rows):
            continue
        due = _valid_date(it.get("due"))
        cur = conn.execute(
            "INSERT INTO group_commitments (group_id, group_name, owner_name, owner_id, action, "
            "due_date, quote, source_ts, status, created_at, updated_at, requester_name, requester_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (group_id, group_name, oname, oid, action, due or None,
             quote[:300], _src_ts(quote), "open", now_iso, now_iso, rname if rid else "", rid or ""))
        counts["asks"] += 1
        actions.append({"type": "ask", "commit_id": cur.lastrowid, "owner_id": oid, "owner_name": oname,
                        "requester_id": rid or "", "requester_name": rname if rid else "",
                        "action": action, "due": due, "quote": quote})

    for key, status in (("done", "done"), ("dropped", "dropped")):
        for it in (data.get(key) or [])[:10]:
            if not isinstance(it, dict):
                continue
            try:
                cid = int(it.get("id"))
            except Exception:
                continue
            if cid not in open_ids or not _grounded(str(it.get("quote") or ""), texts):
                counts["rejected"] += 1
                continue
            conn.execute("UPDATE group_commitments SET status=?, evidence=?, updated_at=? "
                         "WHERE id=? AND status='open'",
                         (status, ("said in group: " + str(it.get("quote"))[:200]), now_iso, cid))
            counts[key] += 1

    for it in (data.get("moved") or [])[:10]:
        if not isinstance(it, dict):
            continue
        try:
            cid = int(it.get("id"))
        except Exception:
            continue
        nd = _valid_date(it.get("due"))
        if cid in open_ids and nd and _grounded(str(it.get("quote") or ""), texts):
            conn.execute("UPDATE group_commitments SET due_date=?, nudge_count=0, updated_at=? "
                         "WHERE id=? AND status='open'", (nd, now_iso, cid))
            counts["moved"] += 1

    counts["facts"] = 0
    for it in (data.get("facts") or [])[:8]:
        if not isinstance(it, dict):
            continue
        ftext = re.sub(r"\s+", " ", str(it.get("text") or "")).strip()[:240]
        quote = str(it.get("quote") or "")
        k = str(it.get("kind") or "").lower()
        kind = ("preference" if k.startswith("pref") else "feedback" if k.startswith("feed")
                else "information" if k.startswith("info") else "decision")
        if not ftext or not _grounded(quote, texts):
            continue
        client = re.sub(r"\s+", " ", str(it.get("client") or "")).strip()[:80]
        if conn.execute("SELECT 1 FROM group_facts WHERE group_id=? AND LOWER(text)=LOWER(?)",
                        (group_id, ftext)).fetchone():
            continue
        conn.execute("INSERT INTO group_facts (group_id, kind, client, text, quote, source_ts, created_at) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (group_id, kind, client, ftext, quote[:300], _src_ts(quote), now_iso))
        counts["facts"] += 1
        if kind == "feedback" and client:
            actions.append({"type": "feedback", "client": client, "text": ftext, "quote": quote})

    conn.execute(f"UPDATE wa_group_log SET processed=1 WHERE id IN ({','.join('?'*len(ids))})", ids)
    counts["_actions"] = actions
    return counts


def extract_pending() -> dict:
    """One pass over every allow-listed group with unprocessed messages."""
    out = {}
    conn = get_connection()
    try:
        ensure_schema(conn)
        groups = [r[0] for r in conn.execute(
            "SELECT DISTINCT group_id FROM wa_group_log WHERE processed=0").fetchall()]
        for gid in groups:
            try:
                with conn:
                    out[gid] = extract_group(conn, gid)
            except Exception:
                logger.exception("group_intel: extraction failed for %s", gid)
                out[gid] = {"error": "failed"}
    finally:
        conn.close()
    # act on what was found only after the extraction transaction committed
    # (group_actions opens its own connections; doing it inside would deadlock)
    for gid, res in out.items():
        acts = res.pop("_actions", None) if isinstance(res, dict) else None
        if acts:
            try:
                import group_actions
                res["actions"] = group_actions.apply(acts)
            except Exception:
                logger.exception("group_intel: group_actions failed for %s", gid)
    return out


# ── evidence + follow-up items ───────────────────────────────────────────────

def _standup_evidence(owner_id: str, action: str, since_date: str) -> str:
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT title, date FROM standup_tasks WHERE user_id=? AND date>=? "
            "AND status IN ('done','completed','need_for_approval')", (owner_id, since_date)).fetchall()
        conn.close()
    except Exception:
        return ""
    for title, date in rows:
        if _overlap(action, title) >= 0.5:
            return f"ticked done in the standup ({title}, {date})"
    return ""


def _notion_evidence(action: str, owner_name: str, cache: dict) -> str:
    try:
        import notion_store
        if not notion_store.is_configured():
            return ""
        if "tasks" not in cache:
            cache["tasks"] = notion_store.list_tasks() or []
        for t in cache["tasks"]:
            st = str(t.get("status") or "").strip().lower().replace("_", " ")
            if st not in _CLOSED_STATUS:
                continue
            if _overlap(action, t.get("title") or "") >= 0.6:
                return f"Notion shows '{t.get('title')}' as {t.get('status')}"
    except Exception:
        logger.debug("group_intel: notion evidence failed", exc_info=True)
    return ""


def nudge_ready(c: dict, now: datetime | None = None) -> bool:
    """Is this open commitment due for an 'is it done?' check right now?"""
    now = now or datetime.now(_IST)
    if c["nudge_count"] >= MAX_NUDGES:
        return False
    today = now.strftime("%Y-%m-%d")
    due = c.get("due_date") or ""
    if due:
        if due < today:
            return True
        if due == today:
            return (now.hour, now.minute) >= (END_OF_DAY_HOUR, END_OF_DAY_MIN)
        return False
    try:
        made = datetime.fromisoformat(c["created_at"]).astimezone(_IST).strftime("%Y-%m-%d")
    except Exception:
        made = today
    if made < today:
        return True
    return now.hour >= NO_DUE_SAME_DAY_HOUR


def commitment_items(user_id: str, name: str, today: str, now: datetime | None = None,
                     cache: dict | None = None, check_evidence: bool = True) -> list:
    """followups.py hook: this person's open group promises that are due a
    check. Resolves silently (and marks done) when real evidence exists."""
    cache = cache if cache is not None else {}
    out = []
    try:
        conn = get_connection()
        ensure_schema(conn)
        conn.row_factory = None
        cols = ["id", "group_name", "owner_name", "owner_id", "action", "due_date", "quote",
                "source_ts", "status", "nudge_count", "created_at"]
        rows = [dict(zip(cols, r)) for r in conn.execute(
            f"SELECT {','.join(cols)} FROM group_commitments WHERE status='open' AND owner_id=?",
            (user_id,)).fetchall()]
        conn.close()
    except Exception:
        logger.debug("group_intel: items query failed", exc_info=True)
        return []
    for c in rows:
        if not nudge_ready(c, now):
            continue
        if check_evidence:
            since = (c["created_at"] or "")[:10] or today
            ev = (_standup_evidence(user_id, c["action"], since)
                  or _notion_evidence(c["action"], name, cache))
            if ev:
                mark(c["id"], "done", ev)
                continue
        when = _fmt_time(c["source_ts"] or "")
        due = f", due {c['due_date']}" if c["due_date"] else ""
        out.append({
            "kind": "group_commitment",
            "ref": f"gc:{c['id']}",
            "days": 0,
            "text": (f"in the group chat{(' at ' + when) if when else ''} they said "
                     f"\"{(c['quote'] or c['action'])[:160]}\" ({c['action']}{due}); "
                     "nothing in the chat, the standup or Notion shows it is done. "
                     "Ask whether it got done or what is holding it up."),
        })
    return out


def open_asks(requester_id: str, check_evidence: bool = True) -> list:
    """Open asks this person made in the group (work they directed at someone else).
    Quietly closes the ones that real evidence shows are done."""
    out = []
    try:
        conn = get_connection()
        ensure_schema(conn)
        conn.row_factory = None
        cols = ["id", "owner_name", "owner_id", "action", "due_date", "source_ts", "created_at"]
        rows = [dict(zip(cols, r)) for r in conn.execute(
            f"SELECT {','.join(cols)} FROM group_commitments WHERE status='open' AND requester_id=? "
            "ORDER BY id", (requester_id,)).fetchall()]
        conn.close()
    except Exception:
        return []
    cache: dict = {}
    for c in rows:
        if check_evidence:
            since = (c["created_at"] or "")[:10]
            ev = (_standup_evidence(c["owner_id"], c["action"], since)
                  or _notion_evidence(c["action"], c["owner_name"] or "", cache))
            if ev:
                mark(c["id"], "done", ev)
                continue
        out.append(c)
    return out


def asks_digest_text(requester_id: str) -> str:
    rows = open_asks(requester_id)
    if not rows:
        return ""
    lines = ["Asks you made in the group that are still open:"]
    for c in rows[:12]:
        due = f", due {c['due_date']}" if c["due_date"] else ""
        lines.append(f"- {c['owner_name']}: {c['action']} ({_fmt_time(c['source_ts'] or '')}{due}) [#{c['id']}]")
    if len(rows) > 12:
        lines.append(f"...and {len(rows) - 12} more")
    lines.append("Nothing in the chat, standup or Notion shows these as done. Reply \"close #<number>\" "
                 "to close one, or ask me what is holding any of them up.")
    return "\n".join(lines)


def run_asks_digest(now: datetime | None = None, force_ids: list | None = None) -> dict:
    """Once per weekday 17:00-18:30 IST: tell each person who directed work in the group
    (default Vidit and Kshitij, setting asks_digest_ids) which of those asks look unfinished."""
    now = now or datetime.now(_IST)
    day = now.strftime("%Y-%m-%d")
    if not force_ids and (now.weekday() >= 5 or not ((17, 0) <= (now.hour, now.minute) < (18, 30))):
        return {"skipped": "outside window"}
    import wa_outbox
    import week_digest
    ids = list(force_ids or [])
    if not ids:
        try:
            conn = get_connection()
            row = conn.execute("SELECT value FROM app_settings WHERE key='asks_digest_ids'").fetchone()
            conn.close()
            ids = [x for x in re.split(r"[\s,]+", str(row[0]) if row and row[0] else "emp001,emp004") if x]
        except Exception:
            ids = ["emp001", "emp004"]
    sent = 0
    for emp in week_digest._people(only_ids=ids):
        uid = emp.get("id")
        if not str(emp.get("whatsapp") or "").strip():
            continue
        if not force_ids:
            conn = get_connection()
            try:
                with conn:
                    got = conn.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, '1')",
                                       (f"asks_digest_{day}_{uid}",)).rowcount
            finally:
                conn.close()
            if not got:
                continue
        text = asks_digest_text(uid)
        if text and wa_outbox.enqueue(wa_outbox.wa_jid(emp["whatsapp"]), text):
            sent += 1
    return {"sent": sent}


def mark(cid: int, status: str, evidence: str = "") -> bool:
    conn = get_connection()
    try:
        ensure_schema(conn)
        with conn:
            cur = conn.execute(
                "UPDATE group_commitments SET status=?, evidence=?, updated_at=? "
                "WHERE id=? AND status='open'", (status, evidence[:300], _now().isoformat(), cid))
        return bool(cur.rowcount)
    finally:
        conn.close()


def note_nudged(ref: str) -> None:
    m = re.match(r"gc:(\d+)$", str(ref or ""))
    if not m:
        return
    conn = get_connection()
    try:
        with conn:
            conn.execute("UPDATE group_commitments SET nudge_count=nudge_count+1, last_nudged_at=? "
                         "WHERE id=?", (_now().isoformat(), int(m.group(1))))
    finally:
        conn.close()


# ── reading (agent tool + inspection) ────────────────────────────────────────

def stats() -> dict:
    """Is it actually reading? Counts for the inspection endpoint."""
    conn = get_connection()
    try:
        ensure_schema(conn)
        one = lambda q: (conn.execute(q).fetchone() or [0])[0]
        since = (_now() - timedelta(hours=24)).isoformat()
        by_status = {r[0]: r[1] for r in conn.execute(
            "SELECT status, COUNT(*) FROM group_commitments GROUP BY status").fetchall()}
        last = conn.execute("SELECT created_at FROM wa_group_log ORDER BY id DESC LIMIT 1").fetchone()
        return {
            "messages_total": one("SELECT COUNT(*) FROM wa_group_log"),
            "messages_last_24h": (conn.execute("SELECT COUNT(*) FROM wa_group_log WHERE created_at>=?", (since,)).fetchone() or [0])[0],
            "messages_waiting_to_be_read": one("SELECT COUNT(*) FROM wa_group_log WHERE processed=0"),
            "last_message_at": last[0] if last else None,
            "commitments": by_status,
            "facts": one("SELECT COUNT(*) FROM group_facts"),
            "corrections_learned": one("SELECT COUNT(*) FROM group_feedback"),
            "extraction_model": "claude" if _use_claude() else "qwen/default",
        }
    finally:
        conn.close()


def list_commitments(person: str = "", status: str = "open", limit: int = 30) -> list:
    conn = get_connection()
    try:
        ensure_schema(conn)
        sql = ("SELECT id, owner_name, action, due_date, status, quote, source_ts, evidence "
               "FROM group_commitments WHERE 1=1")
        params: list = []
        if status in ("open", "done", "dropped"):
            sql += " AND status=?"
            params.append(status)
        if person:
            sql += " AND LOWER(owner_name) LIKE ?"
            params.append(f"%{person.lower()}%")
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        cols = ["id", "owner", "action", "due", "status", "quote", "said_at", "evidence"]
        return [dict(zip(cols, r)) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def search_facts(query: str = "", client: str = "", limit: int = 25) -> str:
    """Decisions / client preferences the assistant picked up, newest first."""
    conn = get_connection()
    try:
        ensure_schema(conn)
        sql = "SELECT kind, client, text, source_ts, created_at FROM group_facts WHERE 1=1"
        params: list = []
        if client:
            sql += " AND LOWER(client) LIKE ?"
            params.append(f"%{client.lower()}%")
        for w in [w for w in re.split(r"\s+", (query or "").strip()) if len(w) > 2][:6]:
            sql += " AND (LOWER(text) LIKE ? OR LOWER(COALESCE(client,'')) LIKE ?)"
            params += [f"%{w.lower()}%", f"%{w.lower()}%"]
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    if not rows:
        return "Nothing recorded about that from the group chat yet."
    return "\n".join(
        f"{_fmt_time(r[3] or r[4])} [{r[0]}{(', ' + r[1]) if r[1] else ''}] {r[2]}" for r in rows)


def reject_commitment(cid: int, note: str = "") -> bool:
    """The boss says a tracked item was wrong: close it and remember the
    mistake so the extractor learns."""
    conn = get_connection()
    try:
        ensure_schema(conn)
        row = conn.execute("SELECT quote, action FROM group_commitments WHERE id=? AND status='open'",
                           (cid,)).fetchone()
        if not row:
            return False
        with conn:
            try:
                import group_actions
                group_actions.undo_for_commitment(conn, cid)   # drop the standup task an ask created
            except Exception:
                logger.debug("group_intel: undo of auto task failed", exc_info=True)
            conn.execute("UPDATE group_commitments SET status='dropped', evidence=?, updated_at=? WHERE id=?",
                         ("marked wrong: " + (note or "not a real commitment")[:200], _now().isoformat(), cid))
            conn.execute("INSERT INTO group_feedback (example, note, created_at) VALUES (?,?,?)",
                         ((row[0] or row[1] or "")[:300], note[:200], _now().isoformat()))
        return True
    finally:
        conn.close()


def add_commitment(owner: str, action: str, due: str = "", group_id: str = "") -> int:
    """A promise the extractor missed, added by hand."""
    roster = _roster()
    oname, oid = _owner(owner, roster)
    conn = get_connection()
    try:
        ensure_schema(conn)
        gid = group_id or (conn.execute("SELECT group_id FROM wa_group_log ORDER BY id DESC LIMIT 1").fetchone() or [""])[0]
        now_iso = _now().isoformat()
        with conn:
            cur = conn.execute(
                "INSERT INTO group_commitments (group_id, owner_name, owner_id, action, due_date, quote, "
                "source_ts, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (gid or "", oname, oid, action[:200], _valid_date(due) or None, "(added by hand)",
                 now_iso, "open", now_iso, now_iso))
        return cur.lastrowid
    finally:
        conn.close()


def format_commitments(rows: list) -> str:
    if not rows:
        return "No tracked group commitments match that."
    lines = []
    for r in rows:
        bit = f"#{r['id']} {r['owner']}: {r['action']}"
        if r.get("due"):
            bit += f" (due {r['due']})"
        if r.get("status") != "open":
            bit += f" [{r['status']}" + (f": {r['evidence']}" if r.get("evidence") else "") + "]"
        lines.append(bit)
    return "\n".join(lines)
