"""
group_digest.py -- rigid, verifiable answers about what was said in the group.

A language model summarising a chat log will, sooner or later, put words in
people's mouths ("Happy said omotec is not working" -- nobody did). So for the
plain "what was discussed in the group" questions the reply is built here, in
code, from the stored messages:

  * every line is a real stored message (time, speaker, text), verbatim;
  * the only thing a model may ever do is GROUP message numbers into topics
    (long days only), and even then the code writes the topic header from the
    data (counts, speakers, times, a quoted first line) and rejects a model
    title that is not made of words found in those messages;
  * it says honestly when recording started later than the window asked for.

`quotes_ok()` is the safety net for the free-form model path: any "quoted
text" in a reply must exist in the log, else the caller swaps the reply for
this module's verbatim digest.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))
MAX_REPLY = 3800          # WhatsApp is happy to ~4000
VERBATIM_MAX_MSGS = 45    # above this (or too long) -> topic view

_ASK = re.compile(
    r"\b(?:exact messages|full chat|discuss\w*|summari[sz]e|summary|recap|what (?:was|were|did|have|has|is) .{0,30}"
    r"(?:said|talk\w*|happen\w*|going on|mention\w*|decid\w*|up)|"
    r"catch me up|i missed|talked about|said)\b", re.I)
_GROUPWORD = re.compile(r"\b(?:group|wizards|team chat)\b", re.I)
_LOOSE = re.compile(r"\b(?:things?|tell me|update|updates|news|pressing|important|urgent|priority|"
                    r"happening|anything|missed|catch|going on|latest|new)\b", re.I)
_PRESSING = re.compile(r"\b(?:pressing|important|urgent|priority|priorities|needs? attention|"
                       r"anything i should know|what did i miss)\b", re.I)
_NOGROUP = re.compile(r"^(?:lumina[,:\s]+)?(?:what did i miss|catch me up|anything (?:important|new|pressing)"
                      r"(?: (?:today|in the chat))?)[\s?.!]*$", re.I)
# a bare "summary" / "give me a summary" / "summarise" means the group chat (the day's own
# overview is "my dashboard")
_BARE = re.compile(r"^(?:hi\s+|hey\s+|bro\s+)?(?:lumina[,:\s]+)?(?:please\s+)?(?:give me |show me |tell me |send me |get me |do )?"
                   r"(?:a |the |me a |me the )?(?:quick |short |full |today'?s |todays )?"
                   r"(?:summary|summari[sz]e|summari[sz]ation|recap|sum ?up)(?: (?:of )?(?:today|the day|everything|it|this|chat|the chat|the messages))?"
                   r"(?: (?:please|pls|now|bro))?[\s?.!]*$", re.I)
_URL = re.compile(r"https?://\S+", re.I)


# ---------- window -------------------------------------------------------
DAY_START_HOUR = 3   # a "day" in the group runs 3 AM to the next 3 AM (IST)

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def _named_day(t: str, now: datetime):
    """The calendar date the question names ('monday', '12 oct', 'oct 12', '12/10'), or None."""
    for i, wd in enumerate(_WEEKDAYS):
        if re.search(rf"\b{wd}\b", t):
            back = (now.weekday() - i) % 7
            return now.date() - timedelta(days=back or 7)
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s*(?:of\s*)?(" + "|".join(_MONTHS) + r")[a-z]*\b", t)
    m2 = re.search(r"\b(" + "|".join(_MONTHS) + r")[a-z]*\s*(\d{1,2})(?:st|nd|rd|th)?\b", t)
    day = mon = None
    if m:
        day, mon = int(m.group(1)), m.group(2)
    elif m2:
        day, mon = int(m2.group(2)), m2.group(1)
    if mon:
        try:
            d = datetime(now.year, _MONTHS.index(mon[:3]) + 1, day).date()
            return d if d <= now.date() else d.replace(year=d.year - 1)
        except ValueError:
            return None
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})\b", t)
    if m:
        try:
            d = datetime(now.year, int(m.group(2)), int(m.group(1))).date()
            return d if d <= now.date() else d.replace(year=d.year - 1)
        except ValueError:
            return None
    return None


def _window(text: str, now: datetime | None = None):
    """(start, end, label) in IST. A day runs 3 AM to the next 3 AM. Default = the current
    day (since the last 3 AM); 'yesterday' or a named day gives that 3 AM-to-3 AM block."""
    now = now or datetime.now(_IST)
    t = text.lower()
    cur0 = now.replace(hour=DAY_START_HOUR, minute=0, second=0, microsecond=0)
    if now < cur0:
        cur0 -= timedelta(days=1)           # before 3 AM still belongs to the previous day
    m = re.search(r"(?:last|past)\s+(\d{1,3})\s*(hour|hr|h|day|d)s?\b", t)
    if m:
        n = int(m.group(1))
        delta = timedelta(hours=n) if m.group(2).startswith("h") else timedelta(days=n)
        return now - delta, now, f"last {n} {'hours' if m.group(2).startswith('h') else 'days'}"
    if "yesterday" in t:
        return cur0 - timedelta(days=1), cur0, "yesterday (3 AM to 3 AM)"
    if re.search(r"this week|week so far", t):
        mon = cur0 - timedelta(days=cur0.weekday())
        return mon, now, "this week"
    d = _named_day(t, now)
    if d and d != cur0.date():
        s0 = datetime(d.year, d.month, d.day, DAY_START_HOUR, tzinfo=now.tzinfo)
        return s0, s0 + timedelta(days=1), f"{d.strftime('%a %d %b')} (3 AM to 3 AM)"
    return cur0, now, "today (since 3 AM)"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


# ---------- filters ------------------------------------------------------
def _people_in(text: str) -> list:
    out = []
    try:
        import utils
        for e in utils._load_employees().get("employees", []):
            nm = str(e.get("name") or "").strip()
            if not nm:
                continue
            first = nm.split()[0]
            if len(first) >= 3 and re.search(rf"\b{re.escape(first)}\b", text, re.I):
                out.append(first.lower())
    except Exception:
        pass
    return out


def _clients_in(text: str) -> list:
    """Client names to filter on when the question is about 'client things'."""
    if not re.search(r"\bclients?\b", text, re.I):
        return []
    try:
        import org_profile
        names = [str(p.get("client") or "") for p in org_profile.projects()]
    except Exception:
        names = []
    return [n for n in names if n]


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


# ---------- rendering ----------------------------------------------------
def _domain(m) -> str:
    u = m.group(0).rstrip(".,)")
    parts = u.split("/")
    host = parts[2] if len(parts) > 2 else "link"
    return "[link: " + re.sub(r"^www\.", "", host) + "]"


_SECRET = re.compile(r"(?i)\b(pass(?:word|wd|wrd|code)?|pws|pw|psw|pswd|pwd|otp|pin|secret|token|api[ _-]?key)\b(\s*(?:is|:|=|-)?\s*)(\S+)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def redact(text: str) -> str:
    """Never repeat a shared password / OTP / key (or the login it belongs to) in a reply."""
    t = text or ""
    if _SECRET.search(t):
        t = _SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}[hidden]", t)
        t = _EMAIL.sub("[email hidden]", t)
    return t


def _urls_of(row) -> list:
    """Real links in one stored message, in order, without trailing punctuation.
    Skipped for a message that also shares a password/OTP (never repeat those)."""
    txt = row["text"] or ""
    if _SECRET.search(txt):
        return []
    out = []
    for u in _URL.findall(txt):
        u = u.rstrip(".,)>]\"'")
        if u and u not in out:
            out.append(u)
    return out


def _clean(text: str, cap: int) -> str:
    t = redact(_URL.sub(_domain, text))
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= cap else t[:cap].rstrip() + "..."


def _hm(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone(_IST).strftime("%H:%M")
    except Exception:
        return "--:--"


def _day(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone(_IST).strftime("%a %d %b")
    except Exception:
        return ""


def _speakers(rows) -> str:
    c = {}
    for r in rows:
        c[r["sender_name"]] = c.get(r["sender_name"], 0) + 1
    return ", ".join(f"{n} {k}" for n, k in sorted(c.items(), key=lambda x: -x[1]))


def _line(r, cap: int, with_day: bool) -> str:
    d = f"{_day(r['created_at'])} " if with_day else ""
    return f"{d}{_hm(r['created_at'])} *{r['sender_name']}*: {_clean(r['text'], cap)}"


def _verbatim(rows, label: str, notes: list) -> str | None:
    multi_day = len({_day(r["created_at"]) for r in rows}) > 1
    head = (f"*Group chat, {label}*: {len(rows)} message(s), "
            f"{_hm(rows[0]['created_at'])} to {_hm(rows[-1]['created_at'])}\n"
            f"Spoke: {_speakers(rows)}\n")
    foot = "\n".join(notes + ["These are the exact messages, nothing added."])
    for cap in (200, 140, 100, 70):
        body = "\n".join(_line(r, cap, multi_day) for r in rows)
        out = f"{head}\n{body}\n\n{foot}"
        if len(out) <= MAX_REPLY:
            return out
    return None


def _title_ok(title: str, rows) -> bool:
    words = [w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 3]
    if not words:
        return False
    hay = " ".join(r["text"].lower() for r in rows)
    hits = sum(1 for w in words if w in hay)
    return hits / len(words) >= 0.6


def _topic_view(rows, label: str, notes: list, llm) -> str | None:
    if llm is None:
        return None
    numbered = "\n".join(f"{i + 1}|{_hm(r['created_at'])}|{r['sender_name']}|{_clean(r['text'], 160)}"
                         for i, r in enumerate(rows))
    system = ("Group these numbered chat messages into topics. Reply with ONLY JSON: "
              '{"topics":[{"title":"max 6 words, use only words that appear in the '
              'messages","ids":[1,2]}]}. Every message number appears in at most one '
              "topic. Do not summarise, do not add facts.")
    try:
        raw = llm(system, numbered[:14000])
        m = re.search(r"\{[\s\S]*\}", raw or "")
        topics = json.loads(m.group(0)).get("topics") if m else None
    except Exception:
        logger.debug("group_digest: topic grouping failed", exc_info=True)
        return None
    if not isinstance(topics, list) or not topics:
        return None
    used, blocks = set(), []
    for tp in topics[:12]:
        try:
            ids = [i for i in tp.get("ids", []) if isinstance(i, int) and 1 <= i <= len(rows) and i not in used]
        except Exception:
            continue
        if not ids:
            continue
        used.update(ids)
        sub = [rows[i - 1] for i in sorted(ids)]
        title = str(tp.get("title") or "").strip()
        if not _title_ok(title, sub):
            title = '"' + " ".join(_clean(sub[0]["text"], 60).split()[:6]) + '"'
        shown = [sub[0]] + ([sub[-1]] if len(sub) > 1 else [])
        blocks.append(f"*{title}* ({len(sub)} msgs, {_hm(sub[0]['created_at'])}-{_hm(sub[-1]['created_at'])}, "
                      f"{_speakers(sub)})\n" + "\n".join(_line(r, 140, False) for r in shown))
    rest = [rows[i] for i in range(len(rows)) if (i + 1) not in used]
    if rest:
        blocks.append(f"*Other* ({len(rest)} msgs, {_speakers(rest)})\n"
                      + "\n".join(_line(r, 100, False) for r in rest[:3]))
    if not blocks:
        return None
    head = (f"*Group chat, {label}*: {len(rows)} messages, "
            f"{_hm(rows[0]['created_at'])} to {_hm(rows[-1]['created_at'])}\n")
    foot = "\n".join(notes + ["Topic names are grouping only; the quoted lines are exact messages. "
                              "Ask for a person or a shorter time range to see more."])
    out = head + "\n" + "\n\n".join(blocks) + "\n\n" + foot
    return out if len(out) <= MAX_REPLY else out[:MAX_REPLY - 3] + "..."


# ---------- checked summary ---------------------------------------------
_EXACT = re.compile(r"\b(?:exact|verbatim|word for word|full chat|every message|all (?:the )?messages|"
                    r"show (?:me )?(?:the )?(?:chat|messages)|raw)\b", re.I)
_HEDGE = re.compile(r"\b(?:probably|likely|seems?|maybe|might|sounds? like|possibly|apparently|"
                    r"presumably|perhaps|i think|appears? to)\b", re.I)
_CAPS_OK = {"the", "a", "an", "in", "on", "at", "and", "he", "she", "they", "it", "i", "monday", "tuesday",
            "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february", "march",
            "april", "may", "june", "july", "august", "september", "october", "november", "december",
            "everyone", "group", "team", "link", "drive", "meet", "google", "whatsapp", "also", "then",
            "later", "after", "before", "one", "two", "three", "his", "her", "their", "tomorrow", "today"}

_SUM_SYSTEM = (
    "You summarise a team WhatsApp group chat for the boss. The messages are numbered "
    "as: number|time|speaker|text. Return ONLY JSON: "
    '{"points":[{"topic":"2-3 word topic name","text":"one short plain sentence","ids":[message numbers that prove it],"important":true or false}]}. '
    "Rules: 4 to 12 points. Give every point a short topic name (2-3 words, e.g. Shoot plan, Stempedia collab, Script, Client asks); points about the same thing MUST use the exact same topic name. Set important=true only for what the boss must act on or know: a request or deadline aimed at a named person, a decision, a client problem or ask, an approval, something blocked or urgent; routine chatter is false. Every fact in a point must be "
    "stated in the messages you cite. Never guess a purpose, mood or meaning (no 'probably', "
    "'likely', 'seems'). A message that is only a link: say who shared a link and its site, "
    "nothing more. Do not put quotes around anything. Name only people who sent the cited "
    "messages or are named inside them. A message that tags or calls out a named person with a "
    "request (shown as @Name) is IMPORTANT: always make it its own point: who asked whom to do what. "
    "Skip noise like '.' or 'ok'. Hinglish: give the meaning "
    "faithfully in English. Cover EVERY substantive message: plans, requests, assignments (which person "
    "got which role), decisions, links shared, collaborations and follow-ups. Skip only pure noise. "
    "If nothing real was discussed, return fewer points.")


_FUNC = set("""shared said says asked announced posted sent wrote mentioned confirmed picked chose chosen
planned told wants wanted need needs needed bring brought requested suggested replied agreed joined about
around again after before during while where which their there these those would could should other others
everyone someone message messages chat group team character characters list lists version versions first
second third final tomorrow today tonight morning evening afternoon event tagging tagged people member
members file files video photo image link links site started asking saying people being have has had will
been were with from into over also then later each both more most some many another among between group's
selected reacted responded liked loved shared""".split())


def _words_ok(text: str, hay: str) -> bool:
    """Every content word of the point must come from the cited messages (5-letter stem
    match) or be an everyday connective/verb -- stops a made-up topic slipping through."""
    for w in re.findall(r"[A-Za-z][A-Za-z']+", text):
        wl = w.lower().strip("'")
        if len(wl) < 5 or wl in _FUNC or wl in _CAPS_OK:
            continue
        stem = wl[:5]
        if stem not in hay and _squash(wl) not in _squash(hay):
            return False
    return True


def _point_ok(text: str, ids: list, rows) -> bool:
    if not text or len(text) > 300 or _HEDGE.search(text):
        return False
    cited = [rows[i - 1] for i in ids]
    hay = " ".join(f"{r['sender_name']} {r['text']}" for r in cited).lower()
    hay_sq = _squash(hay)
    for w in re.findall(r"[A-Za-z][A-Za-z0-9']+", text):
        if w[0].isupper() and w.lower() not in _CAPS_OK and len(w) > 2:
            if w.lower() not in hay and _squash(w) not in hay_sq:
                if not difflib.get_close_matches(w.lower(), re.findall(r"[a-z0-9']+", hay), n=1, cutoff=0.75):
                    return False
    for n in re.findall(r"\d+", text):
        if n not in hay:
            return False
    return quotes_ok(text, hay)


LAST: dict = {}   # last summary attempt (raw model points + verdicts), for the debug route


def _verify(cand, use, llm) -> dict:
    """{n: bool} from an independent fact-check call, or {} if it can't be run."""
    if not cand or llm is None:
        return {}
    blocks = []
    for n, (ids, text, _m) in enumerate(cand, 1):
        msgs = "\n".join(f"  [{_hm(use[i - 1]['created_at'])}] {use[i - 1]['sender_name']}: "
                         f"{_clean(use[i - 1]['text'], 400)}" for i in ids)
        blocks.append(f"CLAIM {n}: {text}\nMESSAGES:\n{msgs}")
    system = ("You are a strict fact checker. For each CLAIM decide if the MESSAGES under it "
              "directly support EVERY detail of the claim (who, what, when, numbers). Ignore spelling differences in names. A claim "
              "that adds a purpose, motive, feeling or detail not in the messages is NOT supported. "
              'Reply ONLY with JSON: {"results":[{"n":1,"supported":true}]}.')
    try:
        raw = llm(system, "\n\n".join(blocks)[:20000])
        m = re.search(r"\{[\s\S]*\}", raw or "")
        res = json.loads(m.group(0)).get("results") if m else None
        out = {int(r["n"]): bool(r.get("supported")) for r in res or []}
        LAST["verifier"] = out
        return out if len(out) == len(cand) else {}
    except Exception:
        logger.debug("group_digest: verifier failed", exc_info=True)
        return {}


def _gen(use, llm):
    """One writer + checker pass over `use`. Returns ([(ids, text)] accepted, bad_count),
    or None if the model call itself failed."""
    numbered = "\n".join(f"{i + 1}|{_hm(r['created_at'])}|{r['sender_name']}|{_clean(r['text'], 220)}"
                         for i, r in enumerate(use))
    try:
        raw = llm(_SUM_SYSTEM, numbered[:24000])
        LAST["raw"] = (raw or "")[:4000]
        m = re.search(r"\{[\s\S]*\}", raw or "")
        points = json.loads(m.group(0)).get("points") if m else None
    except Exception as e:
        import traceback
        LAST["error"] = traceback.format_exc()[-1500:]
        logger.warning("group_digest: summary call failed: %s", e)
        return None
    if not isinstance(points, list):
        LAST["error"] = "no JSON points in model output: " + (raw or "")[:300]
        return None
    cand, bad = [], 0
    for pt in points[:14]:
        try:
            ids = sorted({i for i in pt.get("ids", []) if isinstance(i, int) and 1 <= i <= len(use)})[:12]
            text = redact(re.sub(r"\s+", " ", str(pt.get("text") or "")).strip())
            topic = redact(re.sub(r"[^\w &/+\-]", "", str(pt.get("topic") or "")).strip())[:30]
            imp = pt.get("important") is True
        except Exception:
            bad += 1
            continue
        ok = bool(ids) and _point_ok(text, ids, use)
        LAST.setdefault("verdicts", []).append({"ok": ok, "text": text, "ids": ids})
        if ok:
            cand.append((ids, text, (topic, imp)))
        else:
            bad += 1
    # Second opinion: a strict fact-checker pass over each surviving point and the exact
    # messages it cites; the conservative word-by-word test only if the checker is unavailable.
    verdict = _verify(cand, use, llm)
    good = []
    for n, (ids, text, meta) in enumerate(cand, 1):
        hay = " ".join(f"{use[i - 1]['sender_name']} {use[i - 1]['text']}" for i in ids).lower()
        ok = verdict.get(n) if verdict else _words_ok(text, hay)
        LAST.setdefault("verdicts", []).append({"checked": ok, "text": text})
        if ok:
            # the model only ever saw "[link: site]"; the real links are added here, in code
            links = []
            for i in ids:
                for u in _urls_of(use[i - 1]):
                    if u not in links:
                        links.append(u)
            if links:
                text = text + "\n  " + "\n  ".join(links[:3])
            good.append((ids, text, meta))
        else:
            bad += 1
    return good, bad


def _summary(rows, label: str, notes: list, llm) -> str | None:
    """Short, checked summary: just the points, newest-last. Messages the first pass did not
    cover get a second pass of their own, so nothing real is left out and nothing is dumped."""
    if llm is None or not rows:
        return None
    LAST.clear()
    LAST["verdicts"] = []
    use = rows[-300:]
    first = _gen(use, llm)
    if first is None or not first[0]:
        logger.warning("group_digest: summary rejected")
        return None
    pts = [(use[ids[0] - 1]["created_at"], text, meta) for ids, text, meta in first[0]]
    covered = {i for ids, _, _m in first[0] for i in ids}
    rest = [r for i, r in enumerate(use, 1) if i not in covered and len(_clean(r["text"], 400)) >= 15]
    if len(rest) >= 2:
        second = _gen(rest, llm)
        if second and second[0]:
            pts += [(rest[ids[0] - 1]["created_at"], text, meta) for ids, text, meta in second[0]]
    pts.sort(key=lambda p: p[0])
    head = f"*Group summary, {label}* ({len(rows)} messages)\n"
    imp = [t for _, t, m in pts if m[1]]
    groups: dict = {}
    for _, t, (topic, flag) in pts:
        if flag:
            continue
        groups.setdefault(topic or "Other", []).append(t)
    parts = []
    if imp:
        parts.append("*❗ Important*\n" + "\n".join(f"- {t}" for t in imp))
    for topic, items in groups.items():
        parts.append(f"*{topic}*\n" + "\n".join(f"- {t}" for t in items))
    body = head + "\n" + "\n\n".join(parts)
    # every link shared in the window that is not already shown under a point
    shown = "\n".join(t for _, t, _m in pts)
    extra, seen = [], set()
    for r in use:
        for u in _urls_of(r):
            if u in shown or u in seen:
                continue
            seen.add(u)
            extra.append(f"- {r['sender_name']} ({_hm(r['created_at'])}): {u}")
    links_block = ("\n\n*Links shared*\n" + "\n".join(extra[:25])
                   + (f"\n...and {len(extra) - 25} more" if len(extra) > 25 else "")) if extra else ""
    room = MAX_REPLY - len(links_block)
    if len(body) > room:      # trim the summary, never the links
        body = body[:max(room - 3, 0)] + "..."
    return body + links_block


# ---------- public -------------------------------------------------------
def is_group_summary_question(text: str) -> bool:
    t = (text or "").strip()
    if not t or len(t) >= 220:
        return False
    if _NOGROUP.match(t) or _BARE.match(t):
        return True
    return bool(_GROUPWORD.search(t) and (_ASK.search(t) or _LOOSE.search(t)))


_BOT_ADDRESS = re.compile(r"^\s*(?:@?lumina\b|/ask\b|!ask\b|@assistant\b)", re.I)


def _to_the_bot(text: str) -> bool:
    """A message that is just someone talking to the bot (not group content)."""
    t = (text or "").strip()
    return bool(_BOT_ADDRESS.match(t) or (len(t) < 90 and (_NOGROUP.match(t) or _BARE.match(t))))


def build(text: str, group_ids, llm=None, now: datetime | None = None, llm_smart=None) -> str:
    """The rigid answer for a group-summary question."""
    import group_memory
    gids = [g for g in (group_memory._digits(x) for x in (group_ids or [])) if g]
    if not gids:
        return "I'm not recording any group chat yet."
    start, end, label = _window(text, now)
    people = _people_in(text)
    clients = _clients_in(text)
    where = f"group_id IN ({','.join('?' * len(gids))}) AND created_at>=? AND created_at<=?"
    params = tuple(gids) + (_iso(start), _iso(end))
    rows = group_memory._select(where, params, 900)
    rows = [r for r in rows if not _to_the_bot(r["text"])]
    rows.sort(key=lambda r: r["created_at"])   # imported/late messages can have older times than their id order
    notes = []
    first_at = group_memory.first_seen(gids)
    if first_at and first_at > _iso(start):
        notes.append(f"Note: I only started recording this group on {_day(first_at)} at {_hm(first_at)}; "
                     "anything said before that isn't here.")
    if people:
        rows = [r for r in rows if any(p in r["sender_name"].lower() for p in people)]
        label += f", from {', '.join(p.title() for p in people)}"
    if clients:
        keys = [_squash(c) for c in clients]
        rows = [r for r in rows if any(k and k in _squash(r["text"]) for k in keys)]
        label += ", messages naming a client"
    if not rows:
        return "\n".join([f"No messages found in the group chat for {label}."] + notes)
    # Owner: only ever a summary, never the raw messages (they can read the chat themselves).
    out = _summary(rows, label, notes, llm_smart or llm)
    if out is None and llm is not None and llm is not llm_smart:
        out = _summary(rows, label, notes, llm)      # second try on the other model
    if out is None:
        out = _summary(rows, label, notes, llm_smart or llm)   # one more try
    if out is None:
        out = (f"*Group summary, {label}* ({len(rows)} messages)\n\n"
               "I could not put a checked summary together just now. Ask again in a minute.")
    return out


NIGHTLY_HOUR, NIGHTLY_MIN = 22, 30


def run_nightly(now: datetime | None = None, force_ids: list | None = None) -> dict:
    """Every day from 22:30 IST: DM the day's group summary to Vidit and Kshitij
    (app_settings 'nightly_group_summary_ids', default emp001,emp004). Called every
    10 minutes by the scheduler; each person gets it once a day (claim key
    nightly_group_<date>_<id>, released if the send fails). Nothing is sent on a
    day with no group messages. force_ids sends now, ignoring the time and the claim."""
    now = now or datetime.now(_IST)
    day = now.strftime("%Y-%m-%d")
    if not force_ids and (now.hour, now.minute) < (NIGHTLY_HOUR, NIGHTLY_MIN):
        return {"skipped": "before 22:30"}
    import week_digest
    import wa_outbox
    from db import get_connection
    ids = list(force_ids or [])
    if not ids:
        try:
            conn = get_connection()
            row = conn.execute("SELECT value FROM app_settings WHERE key='nightly_group_summary_ids'").fetchone()
            conn.close()
            ids = [x for x in re.split(r"[\s,]+", str(row[0]) if row and row[0] else "emp001,emp004") if x]
        except Exception:
            ids = ["emp001", "emp004"]
    people = [e for e in week_digest._people(only_ids=ids) if str(e.get("whatsapp") or "").strip()]
    if not people:
        return {"sent": 0}
    claimed = []
    if not force_ids:
        for e in people:
            conn = get_connection()
            try:
                with conn:
                    got = conn.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, '1')",
                                       (f"nightly_group_{day}_{e['id']}",)).rowcount
            finally:
                conn.close()
            if got:
                claimed.append(e)
        people = claimed
        if not people:
            return {"sent": 0, "skipped": "already sent today"}

    def _release(es):
        for e in es:
            try:
                conn = get_connection()
                with conn:
                    conn.execute("DELETE FROM app_settings WHERE key=?", (f"nightly_group_{day}_{e['id']}",))
                conn.close()
            except Exception:
                pass

    try:
        import whatsapp_agent as wa
        text = build("today", wa._group_allowlist(), llm=wa._digest_llm, now=now, llm_smart=wa._digest_llm_smart)
    except Exception:
        logger.exception("group_digest: nightly build failed")
        if not force_ids:
            _release(people)
        return {"sent": 0, "error": "build failed"}
    if text.startswith("No messages found") or text.startswith("I'm not recording") \
            or "could not put a checked summary together" in text:
        failed = "could not put" in text
        if failed and not force_ids:
            _release(people)          # model trouble: try again on the next tick
        return {"sent": 0, "skipped": "summary failed, will retry" if failed else "no messages"}
    sent = 0
    for e in people:
        if wa_outbox.enqueue(wa_outbox.wa_jid(e["whatsapp"]), "🌙 *Daily group summary*\n" + text):
            sent += 1
        elif not force_ids:
            _release([e])
    return {"sent": sent}


def try_answer(text: str, group_ids, llm=None, llm_smart=None) -> str | None:
    if not is_group_summary_question(text):
        return None
    try:
        out = build(text, group_ids, llm, llm_smart=llm_smart)
        if _PRESSING.search(text or ""):
            # "what is pressing / important": the tracked open asks and promises first
            try:
                import group_intel
                rows = group_intel.list_commitments("", "open", 12)
                if rows:
                    out = ("*Open in the group (promises and asks not yet done):*\n"
                           + group_intel.format_commitments(rows) + "\n\n" + out)
            except Exception:
                logger.debug("group_digest: open items failed", exc_info=True)
        return out
    except Exception:
        logger.exception("group_digest: failed, falling back to the model")
        return None


# ---------- safety net for the free-form path ---------------------------
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", str(s).lower())).strip()


def bad_quotes(reply: str, log: str) -> list:
    """Quoted passages (>=12 chars) in the reply that are NOT in the log."""
    hay = _norm(log)
    return [q for q in re.findall(r'["\u201c]([^"\u201d]{12,})["\u201d]', reply or "")
            if _norm(q) not in hay]


def quotes_ok(reply: str, log: str) -> bool:
    """True if every quoted passage (>=12 chars) in the reply exists in the log."""
    return not bad_quotes(reply, log)
