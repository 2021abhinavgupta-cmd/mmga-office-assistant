import sqlite3
import json
import os
import time
import functools
import logging

logger = logging.getLogger(__name__)

# DB_PATH env var lets you point to a Railway volume (e.g. DB_PATH=/logs/app.db)
# Falls back to the local logs/ directory for development.
_default_db = os.path.join(os.path.dirname(__file__), "..", "logs", "app.db")
DB_PATH = os.environ.get("DB_PATH", _default_db)

def get_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    # Enable Write-Ahead Logging for high concurrency
    conn.execute("PRAGMA journal_mode=WAL")
    # NORMAL is the recommended synchronous setting for WAL
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def with_retry(max_retries=5, initial_delay=0.1):
    """Decorator to retry SQLite operations if the database is locked."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            delay = initial_delay
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e) and attempt < max_retries - 1:
                        logger.warning(f"Database locked, retrying {func.__name__} in {delay}s (attempt {attempt + 1}/{max_retries})")
                        time.sleep(delay)
                        delay *= 2  # Exponential backoff
                    else:
                        raise
        return wrapper
    return decorator

def init_db():
    conn = get_connection()
    with conn:
        # Budget tracking
        conn.execute("CREATE TABLE IF NOT EXISTS budget (period TEXT PRIMARY KEY, total_cost REAL DEFAULT 0)")
        
        # Conversations (storing the entire JSON dict to minimize refactoring)
        conn.execute("CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, data TEXT)")
        # Index to prevent full table scans when querying by user_id
        conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations (json_extract(data, '$.user_id'))")
        
        # User Memory
        conn.execute("CREATE TABLE IF NOT EXISTS memory (user_id TEXT PRIMARY KEY, data TEXT)")
        
        # Custom Skills
        conn.execute("""CREATE TABLE IF NOT EXISTS custom_skills (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            model TEXT DEFAULT 'haiku',
            task_type TEXT DEFAULT 'general',
            prompt TEXT NOT NULL,
            is_shared INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")

        
        # Usage Logs
        conn.execute("CREATE TABLE IF NOT EXISTS usage_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT)")
        
        # Attendance tracking
        conn.execute("CREATE TABLE IF NOT EXISTS attendance (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, action TEXT, timestamp TEXT)")
        conn.execute("""CREATE TABLE IF NOT EXISTS daily_attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            date TEXT NOT NULL,
            checkin_time TEXT,
            checkout_time TEXT,
            UNIQUE(user_id, date)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_attendance_date ON daily_attendance (date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_attendance_user_date ON daily_attendance (user_id, date)")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_user_date ON daily_attendance(user_id, date)"
        )
        try:
            conn.execute("ALTER TABLE daily_attendance ADD COLUMN last_seen_at TEXT DEFAULT NULL")
        except Exception:
            pass  # Column already exists
        # Check-in/check-out geolocation (browser navigator.geolocation, best-effort --
        # null whenever the employee denies/doesn't have location permission, which
        # never blocks the check-in/checkout itself). HR-visibility request, 2026-09-28.
        for _col in ("checkin_lat", "checkin_lng", "checkout_lat", "checkout_lng"):
            try:
                conn.execute(f"ALTER TABLE daily_attendance ADD COLUMN {_col} REAL DEFAULT NULL")
            except Exception:
                pass  # Column already exists
        # 1 when checkout_time was filled in by the heartbeat sweep (the tab simply
        # went quiet), 0 for an explicit Check Out / Logout. Only an auto checkout
        # is undone when the person is seen again (/api/auth/verify); a manual one
        # sticks until they check in again.
        try:
            conn.execute("ALTER TABLE daily_attendance ADD COLUMN checkout_auto INTEGER DEFAULT 0")
        except Exception:
            pass  # Column already exists

        # Daily standups
        conn.execute("""CREATE TABLE IF NOT EXISTS standups (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT NOT NULL,
            date        TEXT NOT NULL,
            yesterday   TEXT,
            today       TEXT,
            blockers    TEXT,
            submitted_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, date)
        )""")
        
        # Mohit bet
        conn.execute("""CREATE TABLE IF NOT EXISTS mohit_bets (
            user_id TEXT PRIMARY KEY,
            vote TEXT NOT NULL
        )""")
        
        # App Settings
        conn.execute("""CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )""")
        cur = conn.execute("SELECT key FROM app_settings WHERE key='bet_question'")
        if not cur.fetchone():
            conn.execute("INSERT INTO app_settings (key, value) VALUES ('bet_question', 'Enter bet question here...')")

        # Personal daily task tracker (separate from project tasks)
        conn.execute("""CREATE TABLE IF NOT EXISTS standup_tasks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      TEXT NOT NULL,
            date         TEXT NOT NULL,
            title        TEXT NOT NULL,
            status       TEXT DEFAULT 'pending',
            carried_from TEXT DEFAULT NULL,
            blocker      TEXT DEFAULT NULL,
            created_at   TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_standup_tasks_user_date ON standup_tasks (user_id, date)")
        
        try:
            conn.execute("ALTER TABLE standup_tasks ADD COLUMN blocker TEXT DEFAULT NULL")
        except Exception:
            pass  # Column already exists

        try:
            # Recycle bin: "delete forever" / the nightly clear set purged=1 instead of deleting the
            # row, so the 'person already removed this task' memory (Sync All) is kept.
            conn.execute("ALTER TABLE standup_tasks ADD COLUMN purged INTEGER DEFAULT 0")
        except Exception:
            pass  # Column already exists

        try:
            conn.execute("ALTER TABLE standup_tasks ADD COLUMN notion_id TEXT DEFAULT NULL")
        except Exception:
            pass  # Column already exists



        try:
            conn.execute("ALTER TABLE standup_tasks ADD COLUMN subtasks TEXT DEFAULT '[]'")
        except Exception:
            pass  # Column already exists

        try:
            conn.execute("ALTER TABLE standup_tasks ADD COLUMN delegated_to TEXT DEFAULT NULL")
        except Exception:
            pass  # Column already exists

        try:
            conn.execute("ALTER TABLE standup_tasks ADD COLUMN delegated_from TEXT DEFAULT NULL")
        except Exception:
            pass  # Column already exists

        try:
            conn.execute("ALTER TABLE standup_tasks ADD COLUMN due_date TEXT DEFAULT NULL")
        except Exception:
            pass  # Column already exists

        try:
            # Bumped on every PATCH to a row (status/blocker/title/subtasks) --
            # lets a carried-over row (carried_from set) still count as "worked
            # on today" for the 11:30 WhatsApp standup nudge, e.g. someone who
            # only ticks subtasks under a persisting main task instead of
            # adding a fresh row each day.
            conn.execute("ALTER TABLE standup_tasks ADD COLUMN updated_at TEXT DEFAULT NULL")
        except Exception:
            pass  # Column already exists

        # Task risk escalation log (tracks alert level per task)
        conn.execute("""CREATE TABLE IF NOT EXISTS task_risk (
            task_id     TEXT PRIMARY KEY,
            risk_level  TEXT DEFAULT 'normal',
            alerted_day1 INTEGER DEFAULT 0,
            alerted_day2 INTEGER DEFAULT 0,
            alerted_day3 INTEGER DEFAULT 0,
            alerted_day5 INTEGER DEFAULT 0,
            last_checked TEXT,
            updated_at  TEXT
        )""")

        # Server-side auth sessions (#1)
        conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
            token       TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at  TEXT NOT NULL
        )""")

        # Per-employee PIN-login lockout (security warning #8: PIN login had
        # no rate limit/lockout at all -- 10,000 4-digit combos, brute-
        # forceable). fail_count resets to 0 on a correct login; locked_until
        # is set once fail_count crosses the threshold and cleared on the
        # next successful login.
        conn.execute("""CREATE TABLE IF NOT EXISTS login_lockout (
            user_id      TEXT PRIMARY KEY,
            fail_count   INTEGER DEFAULT 0,
            locked_until TEXT DEFAULT NULL
        )""")

        # Employee leave / holiday -- inclusive [start_date, end_date]
        # windows. A row here exempts the person from the daily-standup
        # lock and from every "you haven't logged in" nudge for the days
        # it covers, but ONLY once status='approved' (CLAUDE.md gotcha
        # #119, extended by the leave management system to add a real
        # approval workflow -- 2026-09-30). status defaults to 'approved'
        # so every row written before this change (all WhatsApp instant
        # grants) stays valid and still correctly counts as leave taken.
        conn.execute("""CREATE TABLE IF NOT EXISTS employee_leave (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT NOT NULL,
            start_date  TEXT NOT NULL,
            end_date    TEXT NOT NULL,
            reason      TEXT DEFAULT '',
            created_by  TEXT DEFAULT '',
            created_at  TEXT DEFAULT (datetime('now')),
            status      TEXT DEFAULT 'approved',
            leave_type  TEXT DEFAULT 'full',
            approved_by TEXT DEFAULT NULL,
            approved_at TEXT DEFAULT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_employee_leave_user "
                     "ON employee_leave(user_id, start_date, end_date)")
        for _col, _ddl in (
            ("status", "TEXT DEFAULT 'approved'"),
            ("leave_type", "TEXT DEFAULT 'full'"),
            ("approved_by", "TEXT DEFAULT NULL"),
            ("approved_at", "TEXT DEFAULT NULL"),
        ):
            try:
                conn.execute(f"ALTER TABLE employee_leave ADD COLUMN {_col} {_ddl}")
            except Exception:
                pass  # Column already exists

        # Company-wide office calendar maintained by HR (emp009): public
        # holidays, work-from-home days, in-person office days. Inclusive
        # [start_date, end_date] ranges; shown on every employee's Leave page.
        conn.execute("""CREATE TABLE IF NOT EXISTS office_calendar (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            kind        TEXT NOT NULL,
            title       TEXT DEFAULT '',
            start_date  TEXT NOT NULL,
            end_date    TEXT NOT NULL,
            created_by  TEXT DEFAULT '',
            created_at  TEXT DEFAULT (datetime('now'))
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_office_calendar_dates "
                     "ON office_calendar(start_date, end_date)")
        # Timed events (kind='event'): time of day, whether to notify
        # everyone, and when the pre-start reminder went out (office_events.py).
        for _col, _ddl in (("start_time", "TEXT DEFAULT NULL"),
                           ("end_time", "TEXT DEFAULT NULL"),
                           ("notify", "INTEGER DEFAULT 1"),
                           ("reminded_at", "TEXT DEFAULT NULL")):
            try:
                conn.execute(f"ALTER TABLE office_calendar ADD COLUMN {_col} {_ddl}")
            except Exception:
                pass  # Column already exists

        # Daily overtime -- one row per employee per day with a computed
        # worked/overtime figure, feeding the leave-conversion sweep below.
        # Reuses the same 9hr/day baseline as the attendance Excel export
        # (routes/attendance.py::_FULL_DAY_HOURS/_overtime_hours).
        conn.execute("""CREATE TABLE IF NOT EXISTS overtime_ledger (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      TEXT NOT NULL,
            date         TEXT NOT NULL,
            worked_hours REAL NOT NULL,
            ot_hours     REAL NOT NULL,
            converted_at TEXT DEFAULT NULL,
            created_at   TEXT DEFAULT (datetime('now')),
            UNIQUE(user_id, date)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_overtime_ledger_user "
                     "ON overtime_ledger(user_id, converted_at)")

        # One row per 24hr-overtime -> +1 leave day conversion event.
        conn.execute("""CREATE TABLE IF NOT EXISTS comp_off_ledger (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         TEXT NOT NULL,
            days            REAL NOT NULL,
            source_ot_hours REAL NOT NULL,
            created_at      TEXT DEFAULT (datetime('now'))
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_comp_off_ledger_user "
                     "ON comp_off_ledger(user_id, created_at)")

        # Standing instructions -- "from now on always..." rules the WhatsApp
        # agent honours indefinitely (backend/standing_rules.py).
        conn.execute("""CREATE TABLE IF NOT EXISTS standing_rules (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT NOT NULL,
            rule        TEXT NOT NULL,
            remind_day  TEXT DEFAULT '',
            remind_time TEXT DEFAULT '',
            active      INTEGER DEFAULT 1,
            created_at  TEXT DEFAULT (datetime('now'))
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_standing_rules_user "
                     "ON standing_rules(user_id, active)")

        # Dedup log for the proactive follow-up sweep (backend/followups.py) --
        # stops one stalled task being mentioned on every sweep.
        conn.execute("""CREATE TABLE IF NOT EXISTS followup_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   TEXT NOT NULL,
            kind      TEXT NOT NULL,
            ref       TEXT NOT NULL,
            sent_at   TEXT DEFAULT (datetime('now'))
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_followup_log_lookup "
                     "ON followup_log(user_id, kind, ref, sent_at)")

        # Client portal users (separate from employees)
        conn.execute("""CREATE TABLE IF NOT EXISTS client_users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            password      TEXT NOT NULL,
            client_name   TEXT NOT NULL,
            client_notion_id TEXT DEFAULT '',
            created_at    TEXT DEFAULT CURRENT_TIMESTAMP
        )""")

        # Client portal dependencies (files, notes, links)
        conn.execute("""CREATE TABLE IF NOT EXISTS client_dependencies (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id     TEXT NOT NULL,
            type          TEXT NOT NULL,
            content       TEXT NOT NULL,
            original_name TEXT,
            created_at    TEXT DEFAULT CURRENT_TIMESTAMP
        )""")

        # Client portal sessions (separate from employee sessions)
        conn.execute("""CREATE TABLE IF NOT EXISTS client_sessions (
            token       TEXT PRIMARY KEY,
            client_id   INTEGER NOT NULL,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at  TEXT NOT NULL
        )""")

        # Client task feedback
        conn.execute("""CREATE TABLE IF NOT EXISTS client_task_feedback (
            task_id     TEXT PRIMARY KEY,
            status      TEXT,
            comments    TEXT,
            updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
        )""")

        try:
            conn.execute("ALTER TABLE client_task_feedback ADD COLUMN audio_url TEXT DEFAULT NULL")
        except Exception:
            pass  # Column already exists

        # Projects migration
        try:
            # Check if old schema exists (has 'data' column)
            cursor = conn.execute("PRAGMA table_info(projects)")
            columns = [row[1] for row in cursor.fetchall()]
            if 'data' in columns and 'name' not in columns:
                conn.execute("DROP TABLE projects")
        except Exception:
            pass

        # Projects
        conn.execute("""CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            instructions TEXT,
            memory TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")

        # Discovery Questionnaire
        conn.execute("""CREATE TABLE IF NOT EXISTS form_templates (
            id TEXT PRIMARY KEY,
            schema_json TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS client_form_answers (
            client_id TEXT PRIMARY KEY,
            answers_json TEXT NOT NULL
        )""")
        
        # Public Discovery Questionnaire Submissions
        conn.execute("""CREATE TABLE IF NOT EXISTS discovery_submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            form_id TEXT DEFAULT 'discovery_global',
            company_name TEXT NOT NULL,
            email TEXT NOT NULL,
            answers_json TEXT NOT NULL,
            submitted_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        
        try:
            conn.execute("ALTER TABLE discovery_submissions ADD COLUMN form_id TEXT DEFAULT 'discovery_global'")
        except Exception:
            pass  # Column already exists
        try:
            conn.execute("ALTER TABLE discovery_submissions ADD COLUMN xlsx_blob BLOB")
        except Exception:
            pass  # Column already exists
        
        # Seed default questionnaire
        cur = conn.execute("SELECT id FROM form_templates WHERE id='discovery_global'")
        if not cur.fetchone():
            default_questions = [
                {"id": "q1", "section": "1. Company & Project Overview", "label": "Company Name", "type": "text"},
                {"id": "q2", "section": "1. Company & Project Overview", "label": "What does your company do and how does this platform support your operations?", "type": "textarea"},
                {"id": "q3", "section": "1. Company & Project Overview", "label": "What are the top three business goals?", "type": "textarea"},
                {"id": "q4", "section": "1. Company & Project Overview", "label": "What would define a successful redesign?", "type": "textarea"},
                {"id": "q5", "section": "2. Users & Personas", "label": "Primary user groups", "type": "textarea"},
                {"id": "q6", "section": "2. Users & Personas", "label": "Most active users", "type": "textarea"},
                {"id": "q7", "section": "2. Users & Personas", "label": "Devices used", "type": "text"},
                {"id": "q8", "section": "2. Users & Personas", "label": "Biggest pain points", "type": "textarea"},
                {"id": "q9", "section": "3. Workflow Analysis", "label": "Describe the most common workflow", "type": "textarea"},
                {"id": "q10", "section": "3. Workflow Analysis", "label": "Which tasks take longest?", "type": "textarea"},
                {"id": "q11", "section": "3. Workflow Analysis", "label": "Where do users get confused?", "type": "textarea"},
                {"id": "q12", "section": "3. Workflow Analysis", "label": "Which steps can be simplified?", "type": "textarea"},
                {"id": "q13", "section": "4. Design Preferences", "label": "Preferred style (Minimal, Corporate, Industrial, Premium, Modern SaaS)", "type": "text"},
                {"id": "q14", "section": "4. Design Preferences", "label": "Preferred theme (Light/Dark/Both)", "type": "text"},
                {"id": "q15", "section": "4. Design Preferences", "label": "Link 3-5 reference products and why", "type": "textarea"},
                {"id": "q16", "section": "4. Design Preferences", "label": "Designs to avoid", "type": "textarea"},
                {"id": "q17", "section": "4. Design Preferences", "label": "Animation or interaction preferences", "type": "textarea"},
                {"id": "q18", "section": "5. Branding", "label": "Are their any Brand guidelines", "type": "textarea"},
                {"id": "q19", "section": "5. Branding", "label": "Preferred colors & why?", "type": "textarea"},
                {"id": "q20", "section": "5. Branding", "label": "Preferred typography & why?", "type": "textarea"},
                {"id": "q21", "section": "6. Dashboard & Navigation", "label": "Information shown after login", "type": "textarea"},
                {"id": "q22", "section": "6. Dashboard & Navigation", "label": "Important KPIs", "type": "textarea"},
                {"id": "q23", "section": "6. Dashboard & Navigation", "label": "Navigation preference", "type": "textarea"},
                {"id": "q24", "section": "6. Dashboard & Navigation", "label": "Simple vs data-rich dashboard", "type": "textarea"},
                {"id": "q25", "section": "6. Dashboard & Navigation", "label": "Preferred charts and visualizations", "type": "textarea"},
                {"id": "q26", "section": "7. Screen Prioritization", "label": "Dashboard, Projects, Tasks", "type": "textarea"},
                {"id": "q27", "section": "8. Mobile Experience", "label": "Desktop", "type": "textarea"},
                {"id": "q28", "section": "8. Mobile Experience", "label": "Tablet", "type": "textarea"},
                {"id": "q29", "section": "8. Mobile Experience", "label": "Mobile usage", "type": "textarea"},
                {"id": "q30", "section": "9. Accessibility & Technical Constraints", "label": "Components that cannot change", "type": "textarea"},
                {"id": "q31", "section": "10. Success & Delivery", "label": "Success metrics", "type": "textarea"},
                {"id": "q32", "section": "10. Success & Delivery", "label": "Additional comments", "type": "textarea"}
            ]
            conn.execute("INSERT INTO form_templates (id, schema_json) VALUES (?, ?)", ("discovery_global", json.dumps(default_questions)))

        conn.execute("""CREATE TABLE IF NOT EXISTS project_files (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            filename TEXT,
            content TEXT,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")

        # Sheets row version history -- one row per real edit, storing a full
        # 12-field snapshot (not a diff) so restore is a single re-apply and
        # the UI can diff consecutive snapshots for display. Lives in our own
        # SQLite regardless of whether the task itself is Notion- or
        # SQLite-backed -- Notion has no changelog concept to build on.
        conn.execute("""CREATE TABLE IF NOT EXISTS sheet_edit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id     TEXT NOT NULL,
            client_id   TEXT NOT NULL,
            editor_name TEXT NOT NULL,
            edited_at   TEXT NOT NULL,
            snapshot    TEXT NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sheet_edit_log_task ON sheet_edit_log (task_id)")
        try:
            conn.execute("ALTER TABLE sheet_edit_log ADD COLUMN changed_fields TEXT DEFAULT NULL")
        except Exception:
            pass  # Column already exists

        # Sheets row hide/show -- shared across every employee (was previously
        # per-browser localStorage, which meant one person hiding a row was
        # invisible to everyone else). One row per hidden task; presence = hidden.
        conn.execute("""CREATE TABLE IF NOT EXISTS sheet_hidden_rows (
            task_id    TEXT PRIMARY KEY,
            client_id  TEXT NOT NULL,
            hidden_by  TEXT,
            hidden_at  TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sheet_hidden_rows_client ON sheet_hidden_rows (client_id)")

        # Google Sheets two-way sync -- one linked spreadsheet per client.
        # link_token authenticates the inbound pull webhook (see routes/sheets_sync.py);
        # is_notion records whether this client's tasks live in Notion or the local
        # SQLite `clients`/`tasks` tables, decided once at link time from the same
        # `notionMode && !!client.notion_id` check the frontend already uses.
        conn.execute("""CREATE TABLE IF NOT EXISTS google_sheet_links (
            client_id      TEXT PRIMARY KEY,
            spreadsheet_id TEXT NOT NULL,
            link_token     TEXT NOT NULL UNIQUE,
            is_notion      INTEGER NOT NULL DEFAULT 0,
            client_name    TEXT,
            linked_at      TEXT,
            linked_by      TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_google_sheet_links_token ON google_sheet_links (link_token)")
        for col, coltype in [("last_push_at", "TEXT"), ("last_push_ok", "INTEGER"),
                              ("last_pull_at", "TEXT"), ("last_pull_summary", "TEXT")]:
            try:
                conn.execute(f"ALTER TABLE google_sheet_links ADD COLUMN {col} {coltype} DEFAULT NULL")
            except Exception:
                pass  # Column already exists

        # multi_tab -- whether this client's sync targets one tab per
        # due-date month (see google_sheets_store.py's *_multi_tab
        # functions / reconcile_sheet_tabs) or the original single-first-tab
        # sync. Defaults 0 so every already-linked client (existing rows
        # predate this column and get SQLite's column default on ALTER
        # TABLE) keeps its current single-tab behavior untouched; new
        # connects explicitly insert 1 (see
        # routes/sheets_sync.py::create_google_sheet_link).
        try:
            conn.execute("ALTER TABLE google_sheet_links ADD COLUMN multi_tab INTEGER DEFAULT 0")
        except Exception:
            pass  # Column already exists

        # Sheets tombstones -- one row per task_id deliberately deleted from
        # Lumina while linked to a Google Sheet. reconcile_sheet_rows() checks
        # this before recreating a row whose task_id it doesn't recognize, so
        # a delete_row() failure or a racing/stale webhook payload can't
        # resurrect a task that was actually deleted on purpose. Short-lived
        # by design (see TOMBSTONE_TTL_MINUTES in google_sheets_store.py) --
        # rows older than the TTL are pruned opportunistically, not kept
        # forever, so this table never grows unbounded and a genuine Ctrl+Z
        # undo of a Sheet-side delete (the original, still-desired use of the
        # recreate path) still works once the TTL has passed.
        conn.execute("""CREATE TABLE IF NOT EXISTS sheet_task_tombstones (
            task_id    TEXT PRIMARY KEY,
            client_id  TEXT NOT NULL,
            deleted_at TEXT NOT NULL
        )""")

        # Rolling per-sender context for the WhatsApp CRM/knowledge agent
        # (backend/whatsapp_agent.py). `messages` is a JSON list of plain
        # {role, content} text turns — tool-use scaffolding is never stored.
        # A thread older than whatsapp_agent._CONTEXT_TTL_HOURS is ignored and
        # started fresh, so this table stays tiny and self-pruning in practice.
        conn.execute("""CREATE TABLE IF NOT EXISTS whatsapp_agent_context (
            sender     TEXT PRIMARY KEY,
            messages   TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL
        )""")

        # Outbound WhatsApp queue. The backend runs on Railway and can't reach
        # the Baileys bridge (loopback on the laptop), so proactive messages
        # (task-assigned nudges, "remind X to do Y") are queued here and the
        # laptop companion (scripts/laptop_agent.py) polls + delivers them.
        conn.execute("""CREATE TABLE IF NOT EXISTS whatsapp_outbox (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            to_number  TEXT NOT NULL,
            body       TEXT NOT NULL,
            status     TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL,
            sent_at    TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wa_outbox_status ON whatsapp_outbox (status, id)")
        for _col in ("attempts INTEGER DEFAULT 0", "wa_message_id TEXT",
                     "send_after TEXT",  # NULL = deliver on next poll; else an IST "YYYY-MM-DD HH:MM[:SS]" not-before time
                     "image_token TEXT"):  # set = send the wa_images PNG with `body` as its caption
            try:
                conn.execute(f"ALTER TABLE whatsapp_outbox ADD COLUMN {_col}")
            except Exception:
                pass

        # Holiday/WFH/office changes held for 60 min before alerting, merged per
        # calendar entry so a fix-up edit sends one message, not two (week_digest.py).
        conn.execute("""CREATE TABLE IF NOT EXISTS cal_change_pending (
            entry_id    INTEGER PRIMARY KEY,
            before_json TEXT,
            after_json  TEXT,
            editor_id   TEXT,
            due_at      TEXT NOT NULL,
            first_at    TEXT
        )""")
        try:
            conn.execute("ALTER TABLE cal_change_pending ADD COLUMN first_at TEXT")
        except Exception:
            pass

        # Generated calendar pictures (week_digest.py). The token is the only
        # secret: the email <img> and the laptop's WhatsApp sender fetch
        # /api/public/cal-image/<token>.png without a login.
        conn.execute("""CREATE TABLE IF NOT EXISTS wa_images (
            token      TEXT PRIMARY KEY,
            png        BLOB NOT NULL,
            created_at TEXT NOT NULL
        )""")

        # One-way WhatsApp announcement calls (wa_outbox.py::enqueue_call,
        # scripts/laptop_agent.py::job_voice_calls, whatsapp-bridge/voice.js).
        # Same queue/poll/ack shape as whatsapp_outbox above -- the laptop
        # companion polls this, places the call via the bridge's /call
        # endpoint, and reports back. No send_after here (unlike the message
        # outbox) -- announcement calls are always "place it now", there's no
        # scheduled-call feature yet.
        conn.execute("""CREATE TABLE IF NOT EXISTS wa_call_outbox (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            to_number  TEXT NOT NULL,
            message    TEXT NOT NULL,
            status     TEXT DEFAULT 'pending',
            attempts   INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            sent_at    TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wa_call_outbox_status ON wa_call_outbox (status, id)")

        # One pending "reply yes to confirm" broadcast per employee (send_group_message).
        conn.execute("""CREATE TABLE IF NOT EXISTS wa_pending_action (
            sender     TEXT PRIMARY KEY,
            kind       TEXT NOT NULL,
            payload    TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")

        # Audit trail of every write the WhatsApp agent makes on someone's behalf.
        conn.execute("""CREATE TABLE IF NOT EXISTS wa_action_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sender      TEXT,
            sender_name TEXT,
            scope       TEXT,
            action      TEXT NOT NULL,
            detail      TEXT,
            created_at  TEXT NOT NULL
        )""")

        # Messages from allow-listed staff WhatsApp groups (group_memory.py).
        conn.execute("""CREATE TABLE IF NOT EXISTS wa_group_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id     TEXT NOT NULL,
            group_name   TEXT,
            sender       TEXT,
            sender_name  TEXT,
            text         TEXT NOT NULL,
            wa_id        TEXT,
            created_at   TEXT NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wa_group_log_g ON wa_group_log (group_id, id)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_wa_group_log_u ON wa_group_log (group_id, wa_id)")
        try:
            conn.execute("ALTER TABLE wa_group_log ADD COLUMN processed INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        # Promises tracked from group chat (group_intel.py)
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

        # Project Knowledge Base search index (FTS5)
        # Stores chunked text for fast, System-Projects-like retrieval.
        try:
            conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS kb_chunks_fts USING fts5(
                project_id UNINDEXED,
                user_id    UNINDEXED,
                doc_id     UNINDEXED,
                filename   UNINDEXED,
                chunk,
                tokenize='porter'
            )""")
        except Exception:
            # FTS5 might be unavailable in some SQLite builds; app will gracefully fall back.
            pass

        # Optional semantic layer over kb_chunks_fts (backend/semantic_kb.py).
        # One row per FTS chunk: its rowid + a unit-norm embedding as a float32
        # BLOB. Stays empty (and unused) until an admin enables the feature and
        # runs a backfill; see semantic_kb.py. `fts_rowid` mirrors
        # kb_chunks_fts.rowid — orphans are swept by semantic_kb.prune().
        conn.execute("""CREATE TABLE IF NOT EXISTS kb_chunk_vectors (
            fts_rowid INTEGER PRIMARY KEY,
            dim       INTEGER NOT NULL,
            vec       BLOB NOT NULL
        )""")

        # Optional semantic layer over per-user memory (backend/smart_memory.py).
        # One row per memory item (memory.data's JSON list is one blob per user,
        # so items need their own rows here to each get a vector). Same
        # off-by-default / guarded-import pattern as kb_chunk_vectors above --
        # reuses semantic_kb's embedder, no second model load. Orphans (a memory
        # deleted via /api/memory/<user>/<id>) are swept by smart_memory.prune().
        conn.execute("""CREATE TABLE IF NOT EXISTS memory_vectors (
            user_id   TEXT NOT NULL,
            memory_id TEXT NOT NULL,
            dim       INTEGER NOT NULL,
            vec       BLOB NOT NULL,
            PRIMARY KEY (user_id, memory_id)
        )""")

        # Add task_type column to tasks if not already present
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN task_type TEXT DEFAULT NULL")
        except Exception:
            pass  # Column already exists

        # Add last-edited tracking columns to tasks (Sheets "who/when/what changed" indicator)
        for col in ("last_edited_by TEXT", "last_edited_at TEXT", "last_edited_summary TEXT"):
            try:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {col}")
            except Exception:
                pass  # Column already exists

        # creation_date -- real column for SQLite-mode (is_notion=0) tasks, mirroring
        # Notion mode's real "Creation Date" property (gotcha #50). Before this, the
        # only place a SQLite task's creation date lived was text embedded in
        # `description` ("Creation Date: X | Content: ..."), written by
        # auto_generate_tasks()'s CSV-import path in app.py -- but
        # google_sheets_store.py's _build_sheet_notes() (Sheets sync) never included
        # it in that text, so the very first Sheets-side edit of such a task silently
        # dropped its creation date, and worse, every reconcile pass afterward saw a
        # spurious creation_date diff (stored "" vs the Sheet's real value) and kept
        # re-triggering an update with nothing actually changed. See CLAUDE.md gotcha #87.
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN creation_date TEXT DEFAULT NULL")
        except Exception:
            pass  # Column already exists

        # Optional WhatsApp number for a client portal account. Lets the
        # WhatsApp agent (backend/whatsapp_agent.py) identify an inbound
        # client sender and scope its replies to that client's own tasks.
        # Populated manually / via client-admin — safe to be empty.
        try:
            conn.execute("ALTER TABLE client_users ADD COLUMN whatsapp TEXT DEFAULT ''")
        except Exception:
            pass  # Column already exists

        # ── Startup cleanup: remove orphaned client_users ──────────────
        # Removes portal credentials for clients that no longer exist in the DB.
        # This fixes stuck "username already taken" errors after a client is deleted.
        try:
            conn.execute("""
                DELETE FROM client_users
                WHERE client_notion_id != ''
                AND client_notion_id NOT IN (
                    SELECT CAST(id AS TEXT) FROM clients
                )
            """)
        except Exception:
            pass  # Silently skip if schema not ready yet

    conn.close()

init_db()

# --- Migration Script from JSON to SQLite (Run Once) ---
def migrate_from_json():
    logs_dir = os.path.dirname(DB_PATH)
    conn = get_connection()
    
    # Migrate Conversations
    conv_file = os.path.join(logs_dir, "conversations.json")
    if os.path.exists(conv_file):
        try:
            with open(conv_file, "r") as f:
                data = json.load(f)
            with conn:
                for cid, cdata in data.items():
                    conn.execute("INSERT OR IGNORE INTO conversations (id, data) VALUES (?, ?)", (cid, json.dumps(cdata)))
            os.rename(conv_file, conv_file + ".bak")
        except Exception as e:
            print("Migration error conversations:", e)
            
    # Migrate Memory mem_file prep
    mem_file = os.path.join(logs_dir, "memory.json")
    if os.path.exists(mem_file):
        try:
            with open(mem_file, "r") as f:
                data = json.load(f)
            with conn:
                for uid, mdata in data.items():
                    conn.execute("INSERT OR IGNORE INTO memory (user_id, data) VALUES (?, ?)", (uid, json.dumps(mdata)))
            os.rename(mem_file, mem_file + ".bak")
        except Exception as e:
            print("Migration error memory:", e)
            
    # Migrate Budget & Usage
    usage_file = os.path.join(logs_dir, "usage.json")
    if os.path.exists(usage_file):
        try:
            with open(usage_file, "r") as f:
                data = json.load(f)
            with conn:
                budget = data.get("budget", {})
                for period, bdata in budget.items():
                    conn.execute("INSERT OR IGNORE INTO budget (period, total_cost) VALUES (?, ?)", (period, bdata.get("total_cost", 0.0)))
                # Dedup by exact content, not just primary key (this table has no
                # natural unique key) — if the process is killed between this
                # commit and the os.rename() below, usage.json is still present
                # on next boot and this whole block re-runs; without the dedup
                # check every log line would be re-inserted and double-count
                # toward budget/usage totals shown on the dashboard.
                logs = data.get("logs", [])
                for log in logs:
                    log_json = json.dumps(log)
                    conn.execute(
                        "INSERT INTO usage_logs (data) SELECT ? WHERE NOT EXISTS "
                        "(SELECT 1 FROM usage_logs WHERE data = ?)",
                        (log_json, log_json)
                    )
            os.rename(usage_file, usage_file + ".bak")
        except Exception as e:
            print("Migration error usage:", e)
            
    conn.close()

migrate_from_json()
