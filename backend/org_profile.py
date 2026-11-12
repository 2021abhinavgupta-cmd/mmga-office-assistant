"""
org_profile.py -- what the bot (and later the client-status card) knows about
how MMGA is organised: who owns what, who the DM of each client/project is,
and the operating rhythm. Source: "MMGA OS V2.pdf", loaded 2026-10-12; the
long form lives in docs/mmga-os.md and in the bot's knowledge base as
"mmga-os.md" (searchable with search_knowledge_base).

Two small tables, seeded ONCE from the constants below (only when empty, so a
later edit made in the database is never overwritten by a redeploy):

  org_people   (name, emp_id, title, owns, goal)
  org_projects (client, service_type, operating_type, dm, co_dm, performance, notes)

`prompt_block()` renders both plus the short playbook text into the employee
system prompt. Internal only: never shown to a client identity.
Every function is best-effort and returns "" / [] on failure.
"""
from __future__ import annotations

import logging
import re

from db import get_connection

logger = logging.getLogger(__name__)

# (name, employee id in employees.json or "", title, what they own, learning goal)
_PEOPLE = [
    ("Kshitij (KG)", "emp004", "Founder, Business",
     "sales, revenue, expansion, partnerships, commercial decisions; for content: big campaign ideas, "
     "brand positioning, business context", ""),
    ("Vidit (VG)", "emp001", "Founder, Operations + Strategy + Quality",
     "creative direction, operations, quality standards (final internal quality gate), client "
     "satisfaction, major decisions, new processes, AI and automation, training, escalations. "
     "Teaches people to decide; does not decide everything", ""),
    ("Nupur", "emp002", "Design Lead + Web Design",
     "graphic design, UI/UX, social and brand design, website UI. DM of Branding; Co-DM of Website "
     "with Abhinav. Guides and reviews Lakshmi", "design decision-maker"),
    ("Lakshmi", "emp006", "Graphic + Content Designer",
     "social media creatives, design adaptations, supports branding and website design. Works "
     "closely with VG until Nupur is ready", "independent designer"),
    ("Happy", "emp008", "AI Video + 3D",
     "AI videos, 3D with AI, AI motion graphics, client video support. Co-DM of MMGA Production "
     "with Saurav", "MMGA's AI video / 3D specialist"),
    ("Saurav", "emp012", "VFX + Video Production",
     "VFX, heavy editing, shooting, cinematic and complex video, social videos. Co-DM of MMGA "
     "Production with Happy", "go-to for complex video"),
    ("Charu", "emp007", "Social Media + Content",
     "social media management, content planning and calendars, content briefs, SMM coordination. "
     "DM of OMOTEC and Agrius (with VG) and of all SMM-only clients. Takes help from VG/KG on "
     "brand/campaign strategy", "handle strategy herself"),
    ("Meet", "", "Performance Marketing",
     "ads strategy, campaigns, optimisation, reporting; performance layer of OMOTEC, Agrius, Mellow "
     "(with Shums). Talks to VG and clients directly when needed", ""),
    ("Shums", "", "Performance Marketing",
     "same as Meet: performance layer of OMOTEC, Agrius, Mellow", ""),
    ("Abhinav", "emp003", "Full Stack + AI Generalist",
     "websites, full-stack, CRM, AI, automation, internal tools. Co-DM of website projects with Nupur; sole DM of the Mastek CRM project. Works "
     "with KG and VG on the AI CRM, MMGA OS, agents, integrations", ""),
    ("Noorish", "emp009", "HR + Culture",
     "hiring, onboarding, culture, attendance, team activities, internal communication. NOT "
     "responsible for operational performance (that is VG/DM)", ""),
]

# (client/project, service type, operating type, DM, co-DM, performance layer, notes)
_PROJECTS = [
    ("OMOTEC", "360 Marketing", "recurring", "Charu", "Vidit (VG)", "Meet + Shums",
     "high coordination; weekly project planning meeting. VG moves out of the DM role later"),
    ("Agrius", "360 Marketing", "recurring", "Charu", "Vidit (VG)", "Meet + Shums",
     "high coordination; weekly project planning meeting. VG moves out of the DM role later"),
    ("Mellow", "Only SMM", "recurring", "Charu", "", "Meet + Shums",
     "no daily meetings; monthly content planning, production, review, client, publish"),
    ("1Founder", "Only SMM", "recurring", "Charu", "", "",
     "no daily meetings; monthly content planning, production, review, client, publish"),
    ("TopGreen", "Branding", "project", "Nupur", "", "",
     "brief, concept, review, execution, final; daily approval, no daily meeting"),
    ("Bothra", "Branding", "project", "Nupur", "", "",
     "brief, concept, review, execution, final; daily approval, no daily meeting"),
    ("Mastek", "CRM", "project", "Abhinav", "", "", "CRM project; Abhinav is the only DM, no co-DM"),
    ("MMGA Production", "Production (internal)", "project", "Happy", "Saurav", "", ""),
]

_PLAYBOOK = (
    "Decision flow: team member solves it alone; else brings DM two options; DM decides; only strategic "
    "or major decisions go to VG (VG must not become the default answer). DM owns the project, VG owns "
    "the standard. Rule: don't bring a problem without trying to solve it (Problem / Tried / Options / "
    "Recommendation / Decision).\n"
    "SOP: own your work; don't bring a problem without thinking; don't start unclear work; every task "
    "has one owner; never silently get blocked; client feedback becomes a task.\n"
    "Rhythm: Mon 4 PM weekly planning (client by client); daily 6 PM quality + approval; Wed 2 PM team "
    "lunch; Fri weekly closing (done / pending / urgent / blocked) and 6 PM checkpoint; 7:30 PM daily "
    "closing (what's done / pending / blocked). Weekend is only for critical backlog (proposal).\n"
    "SMM 3-week cycle: content planned 2 weeks ahead, produced 1 week ahead, published this week; "
    "every item has owner + format + deadline + status."
)


_SEED_VERSION = 3   # v3: Mastek is a CRM project, Abhinav the only DM (v2 had it as a website with Nupur)


def _ensure() -> None:
    conn = get_connection()
    try:
        with conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS org_people (
                name TEXT PRIMARY KEY, emp_id TEXT, title TEXT, owns TEXT, goal TEXT)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS org_projects (
                client TEXT PRIMARY KEY, service_type TEXT, operating_type TEXT,
                dm TEXT, co_dm TEXT, performance TEXT, notes TEXT)""")
            if conn.execute("SELECT COUNT(*) FROM org_people").fetchone()[0] == 0:
                conn.executemany("INSERT INTO org_people VALUES (?,?,?,?,?)", _PEOPLE)
            if conn.execute("SELECT COUNT(*) FROM org_projects").fetchone()[0] == 0:
                conn.executemany("INSERT INTO org_projects VALUES (?,?,?,?,?,?,?)", _PROJECTS)
            # Newer seed rows are added once per version; rows a human edited are
            # never overwritten (INSERT OR IGNORE) and deleted ones stay deleted
            # until the next version bump.
            row = conn.execute("SELECT value FROM app_settings WHERE key='org_seed_version'").fetchone()
            ver = int(row[0]) if row and str(row[0]).isdigit() else 0
            if ver < _SEED_VERSION:
                conn.executemany("INSERT OR IGNORE INTO org_projects VALUES (?,?,?,?,?,?,?)", _PROJECTS)
                conn.execute("DELETE FROM org_projects WHERE client='Websites' "
                             "AND notes='any client website project'")
                conn.execute("UPDATE org_projects SET service_type='CRM', co_dm='', notes='CRM project; Abhinav is the only DM, no co-DM' "
                             "WHERE client='Mastek' AND notes='website project; Abhinav is DM, Nupur co-DM'")
                # refresh the two people rows whose text changed, only if still the old seed wording
                for nm in ("Nupur", "Abhinav"):
                    new_owns = next(p[3] for p in _PEOPLE if p[0] == nm)
                    conn.execute("UPDATE org_people SET owns=? WHERE name=? AND owns LIKE '%Co-DM of Website%'",
                                 (new_owns, nm))
                conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('org_seed_version', ?)",
                             (str(_SEED_VERSION),))
    finally:
        conn.close()


def add_project(client: str, service_type: str, dm: str, co_dm: str = "") -> bool:
    """Register a newly onboarded client (and who manages it) so the assistant's status card
    and the rhythm messages know it. Replaces an existing row of the same name."""
    client = (client or "").strip()
    if not client:
        return False
    try:
        _ensure()
        recurring = any(k in (service_type or "").lower() for k in ("smm", "social", "360"))
        conn = get_connection()
        try:
            with conn:
                conn.execute("INSERT OR REPLACE INTO org_projects VALUES (?,?,?,?,?,?,?)",
                             (client, service_type or "", "recurring" if recurring else "project",
                              (dm or "").strip(), (co_dm or "").strip(), "", "added at onboarding"))
        finally:
            conn.close()
        return True
    except Exception:
        logger.exception("org_profile: add_project failed")
        return False


def people() -> list:
    try:
        _ensure()
        conn = get_connection()
        try:
            return [dict(zip(("name", "emp_id", "title", "owns", "goal"), r)) for r in
                    conn.execute("SELECT name, emp_id, title, owns, goal FROM org_people").fetchall()]
        finally:
            conn.close()
    except Exception:
        logger.exception("org_profile: people() failed")
        return []


def projects() -> list:
    try:
        _ensure()
        conn = get_connection()
        try:
            return [dict(zip(("client", "service_type", "operating_type", "dm", "co_dm",
                              "performance", "notes"), r)) for r in conn.execute(
                "SELECT client, service_type, operating_type, dm, co_dm, performance, notes "
                "FROM org_projects").fetchall()]
        finally:
            conn.close()
    except Exception:
        logger.exception("org_profile: projects() failed")
        return []


def _squash(x: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (x or "").lower())


def find_project(name: str) -> dict | None:
    """Loose match of a client/project name ('mellow', 'omotec ltd', '1 FOUNDER') -> its row.
    Order: exact (ignoring spaces/punctuation), then a client name contained in the
    query (longest first, 4+ letters), then the query contained in a client name only
    when it is most of that name -- so 'ma' / 'mmga' never match 'Mastek' / 'MMGA Production'."""
    n = _squash(name)
    if len(n) < 3:
        return None
    rows = [(_squash(p["client"]), p) for p in projects()]
    for c, p in rows:
        if c == n:
            return p
    inside = sorted(((c, p) for c, p in rows if len(c) >= 4 and c in n), key=lambda x: -len(x[0]))
    if inside:
        return inside[0][1]
    for c, p in rows:
        if len(n) >= 4 and n in c and len(n) >= 0.6 * len(c):
            return p
    return None


def prompt_block() -> str:
    """The org reference for the employee system prompt (compact on purpose)."""
    try:
        ppl, prj = people(), projects()
        if not ppl and not prj:
            return ""
        lines = ["MMGA ORGANISATION (internal reference, from the company's OS document; use it when "
                 "someone asks who owns something, who a client's DM is, who to escalate to, how the "
                 "week runs. Don't recite it unprompted. If the data in the CRM disagrees, say so.)"]
        lines.append("People:")
        for p in ppl:
            lines.append(f"- {p['name']}, {p['title']}: {p['owns']}"
                         + (f" (growing into: {p['goal']})" if p["goal"] else ""))
        lines.append("Clients/projects (DM = owns delivery, timeline and client communication):")
        for r in prj:
            dm = r["dm"] + (f" + {r['co_dm']}" if r["co_dm"] else "")
            lines.append(f"- {r['client']} [{r['service_type']}, {r['operating_type']}] DM: {dm}"
                         + (f"; performance: {r['performance']}" if r["performance"] else "")
                         + (f"; {r['notes']}" if r["notes"] else ""))
        lines.append(_PLAYBOOK)
        lines.append("'VG' = Vidit, 'KG' = Kshitij, 'Charu' = Charulata.")
        return "\n".join(lines) + "\n"
    except Exception:
        logger.exception("org_profile: prompt_block failed")
        return ""
