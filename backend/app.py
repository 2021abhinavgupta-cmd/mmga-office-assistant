"""
Flask Backend — Agency Portal Assistant API
Routes:
  GET  /api/health                        — Health check
  GET  /api/budget                        — Current month budget
  GET  /api/usage                         — Full usageDashboard
  POST /api/chat                          — Single-turn chat (legacy)
  POST /api/html/generate                 — HTML generator
  POST /api/presentation                  — Slide generator
  GET  /api/conversations                 — List user conversations
  POST /api/conversations                 — Create conversation
  GET  /api/conversations/<id>            — Get conversation + messages
  DEL  /api/conversations/<id>            — Delete conversation
  POST /api/conversations/<id>/chat       — Multi-turn chat in conversation
  POST /api/conversations/<id>/stream     — SSE streaming chat (optional web_search in JSON body)
  GET  /api/web-search?q=                 — DuckDuckGo instant snippets (debug / reuse)
  PATCH /api/conversations/<id>/title     — Rename conversation
  GET  /api/employees                     — List employees
  POST /api/employees/checkin             — Check-in/out
"""

import os
import re
import json
import copy
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from flask import Flask, request, jsonify, render_template_string, Response, stream_with_context
from flask_cors import CORS
from flask_compress import Compress
from dotenv import load_dotenv
import anthropic
import requests

# ── Emergency disk-space guard — MUST run before any local module import ───
# db.py calls init_db() (opens a real sqlite3 connection + runs migrations)
# the instant it's imported, and several modules below (kb_retriever,
# whatsapp_agent, notion_store, ...) import db transitively — so if the
# persistent volume is completely full, the app can die during import,
# before main() ever runs, before any request-time try/except gets a
# chance to help. This runs first, needs ZERO free space to succeed
# (deleting files never needs headroom the way writing/downloading one
# does), and only acts when the disk is actually in trouble — on a healthy
# volume it's a silent no-op. What it clears is semantic_kb.py's downloaded
# embedding-model cache, which is fully disposable and self-heals: the
# code is explicitly designed to lazily re-download it the next time
# semantic search is actually used (see semantic_kb.py's own docstring).
# Real incident this guards against: 2026-09-28, the volume hit 100% and
# the app went down with 502s on every route, including /api/health.
try:
    import shutil as _boot_shutil
    _boot_logs_dir = Path(__file__).parent.parent / "logs"
    os.makedirs(_boot_logs_dir, exist_ok=True)
    _boot_total, _boot_used, _boot_free = _boot_shutil.disk_usage(str(_boot_logs_dir))
    if _boot_total and _boot_free < 50_000_000:   # under 50MB free -- critical
        _boot_hf_cache = _boot_logs_dir / "hf_cache"
        if _boot_hf_cache.exists():
            _boot_shutil.rmtree(_boot_hf_cache, ignore_errors=True)
            print(f"[boot disk-guard] volume was critically full "
                  f"({_boot_free / 1_000_000:.1f}MB free) -- cleared "
                  f"hf_cache to let the app start; it re-downloads lazily "
                  f"on next semantic-search use.", flush=True)
except Exception as _boot_guard_err:
    print(f"[boot disk-guard] skipped (non-fatal): {_boot_guard_err}", flush=True)

# Local modules
from model_router import get_model_for_task, calculate_cost, get_all_routes
from budget_tracker import check_budget_available, record_usage, get_usage_summary, get_all_usage_logs
from system_prompt import MASTER_SYSTEM_PROMPT
import kb_retriever
import conversation_store
import memory_store
import file_processor
import project_store
import task_scheduler
import notion_store
import google_sheets_store
import skills
import custom_skills_store
import whatsapp_agent

from utils import now_ist, today_ist, _load_employees, _save_employees, _is_admin, IST
from extensions import limiter
from routes.auth import auth_bp
from routes.attendance import attendance_bp
from routes.system import system_bp
from routes.ops import ops_bp
from routes.sheets_sync import sheets_sync_bp, get_link_for_client
from routes.companion import companion_bp
from routes.leave import leave_bp
from routes.office_calendar import office_calendar_bp

# ── Config ────────────────────────────────────────────────────────────────────
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', 'config', '.env'))

# Optional Sentry/GlitchTip error monitoring -- no-op until SENTRY_DSN is
# set AND sentry-sdk is actually installed (see error_monitoring.py's own
# header for the two-step "turning it on" process). Called before the
# Flask app is created so Sentry's Flask integration can hook every
# request from the very first one.
import error_monitoring
error_monitoring.init_error_monitoring()

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Add a persistent file handler to log to the Railway volume
from logging.handlers import RotatingFileHandler
LOG_DIR = Path(__file__).parent.parent / "logs"
os.makedirs(LOG_DIR, exist_ok=True)
file_handler = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=5_000_000, backupCount=2)
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logger.addHandler(file_handler)

# ── Flask App ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")
CORS(app, resources={r"/api/*": {"origins": "*"}})
Compress(app)

app.register_blueprint(auth_bp)
app.register_blueprint(attendance_bp)
app.register_blueprint(system_bp)
app.register_blueprint(ops_bp)
app.register_blueprint(sheets_sync_bp)
app.register_blueprint(companion_bp)
app.register_blueprint(leave_bp)
app.register_blueprint(office_calendar_bp)
from routes.alexa import alexa_bp
app.register_blueprint(alexa_bp)
from routes.releases import releases_bp
app.register_blueprint(releases_bp)
from routes.group_proposals import group_proposals_bp
app.register_blueprint(group_proposals_bp)
from routes.mood import mood_bp
app.register_blueprint(mood_bp)


@app.route("/l/<code>")
def short_link_redirect(code):
    import shortlinks
    from flask import redirect
    url = shortlinks.resolve(code)
    if not url:
        return "Link not found", 404
    return redirect(url, code=302)

# ── Run DB Migrations ─────────────────────────────────────────────────────────
import db
try:
    db.init_db()
except Exception as e:
    logger.error(f"Failed to run database migrations: {e}")

# ── Rate Limiting ─────────────────────────────────────────────────────────────
limiter.init_app(app)

# ── Start background scheduler ────────────────────────────────────────────────
_scheduler = task_scheduler.init_scheduler(app)

# ── Frontend Static Files ──────────────────────────────────────────────────────
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

@app.route("/")
def serve_index():
    from flask import send_from_directory
    return send_from_directory(FRONTEND_DIR, "login.html")

@app.route("/<path:filename>")
def serve_frontend(filename):
    from flask import send_from_directory
    return send_from_directory(FRONTEND_DIR, filename)

UPLOAD_DIR = Path(__file__).parent.parent / "logs" / "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

@app.route("/uploads/<path:filename>")
def serve_uploads(filename):
    from flask import send_from_directory
    return send_from_directory(UPLOAD_DIR, filename)

# ── Anthropic Client ──────────────────────────────────────────────────────────
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# ── Paths ─────────────────────────────────────────────────────────────────────
CONFIG_DIR   = Path(__file__).parent.parent / "config"
EMPLOYEES_DB = CONFIG_DIR / "employees.json"

# ── System Prompts per Task ───────────────────────────────────────────────────
SYSTEM_PROMPTS = {
    "coding": (
        "When writing code:\n"
        "- Always explain what the code does before writing it\n"
        "- Add comments for non-obvious logic only\n"
        "- Suggest edge cases the user might not have considered\n"
        "- Point out security issues if you spot any"
    ),
    "html_design": (
        "TASK-SPECIFIC OUTPUT: One complete, self-contained HTML page.\n\n"

        "OUTPUT FORMAT — STRICT:\n"
        "- Reply with ONLY a single ```html code block containing the complete page.\n"
        "- No introduction, no explanation, no summary before or after the code block. Just the code.\n\n"

        "TECHNICAL:\n"
        "- Raw HTML from <!DOCTYPE html> through </html>; all CSS in <style>, all JS in <script>\n"
        "- External resources: Google Fonts only\n"
        "- Semantic HTML5; mobile-first with 2+ breakpoints; never truncate CSS or JS\n"
        "- CSS variables for all colours and spacing\n"
        "- Smooth transitions on hover states; IntersectionObserver scroll reveals\n\n"

        "DESIGN RULES — STRICTLY ENFORCED:\n\n"

        "NEVER USE:\n"
        "- Purple, violet, or indigo as accent colours\n"
        "- Glassmorphism (backdrop-filter blur on cards)\n"
        "- Floating animated orbs or blob backgrounds\n"
        "- Gradient text (-webkit-background-clip: text)\n"
        "- Glowing coloured box-shadows on cards or buttons\n"
        "- Inter, Roboto, Space Grotesk, or Poppins as the primary display font\n"
        "- Generic AI-template layouts\n"
        "- border-radius above 16px on cards\n"
        "- Pill-shaped buttons (border-radius: 50px) unless explicitly requested\n\n"

        "ALWAYS DO:\n"
        "- Pick one of these style directions and commit fully:\n"
        "  a) DARK EDITORIAL: near-black bg (#080808), warm white text (#f0ede8), single bold accent (gold, orange, or red), sharp grid\n"
        "  b) WARM MINIMAL: cream (#faf8f4) or off-white bg, dark text, serif display font, generous whitespace\n"
        "  c) BOLD MODERN: white bg, black text, one electric accent colour, strong typography contrast\n"
        "  d) DARK LUXURY: deep navy or dark green bg, gold or copper accent, premium serif headings\n"
        "- Choose a distinctive Google Font pairing from this list ONLY:\n"
        "  * Bebas Neue (display) + DM Sans (body)\n"
        "  * Playfair Display (display) + Source Sans Pro (body)\n"
        "  * Syne (display) + Manrope (body)\n"
        "  * Instrument Serif (display) + Geist (body)\n"
        "  * Clash Display (display) + Satoshi (body)\n"
        "  * Cormorant Garamond (display) + Inter (body — only acceptable use of Inter)\n"
        "- Use CSS custom properties: --bg, --s1, --s2, --bdr, --txt, --txt-2, --acc\n"
        "- Section labels: 10-11px, uppercase, letter-spacing 0.12em\n"
        "- Headlines: font-weight 800-900, letter-spacing -0.02em to -0.04em\n"
        "- Every section must have a clear visual hierarchy\n"
        "- Nav must be sticky with scroll state change\n"
        "- At least one layout that uses CSS Grid creatively (not just equal columns)\n"
        "- Footer with minimal but complete information\n\n"

        "STRUCTURE (unless user specifies otherwise):\n"
        "- Sticky nav with logo + links + CTA button\n"
        "- Hero: full viewport height, strong headline, subtext, 2 CTAs, scroll indicator\n"
        "- 2-3 substantive content sections appropriate to the brief\n"
        "- One section with a dark/light background contrast flip\n"
        "- CTA section before footer\n"
        "- Footer\n\n"

        "QUALITY BAR:\n"
        "The output must look like it was designed by a senior designer at a top agency. "
        "If the design could have been generated by any generic AI, reject it and redesign. "
        "Every generation must look visually distinct from the last."
    ),
    "presentations": (
        "TASK-SPECIFIC OUTPUT: Slide decks as markdown matching the global presentation rules.\n\n"
        "Every slide, exactly:\n"
        "## SLIDE N: [Statement headline — not a vague topic label]\n"
        "- Bullet or key point\n"
        "- Bullet or key point\n"
        "- Bullet or key point\n"
        "[NOTES: Short speaker notes]\n\n"
        "- Slide 1 = hook or tension (not a generic title slide unless the user asks)\n"
        "- One idea per slide; rule of three where it helps\n"
        "- Last slide = clear next step / CTA — not “Thank you” alone\n"
    ),
    "captions": (
        "You are a social media content expert. Write punchy, engaging captions "
        "optimized for each platform. Include relevant emojis and hashtags. "
        "Provide 3 variations: one for Instagram, one for LinkedIn, one for Twitter/X."
    ),
    "scripts": (
        "You are a professional scriptwriter. Write natural, conversational scripts "
        "with clear speaker cues and stage directions. Adapt tone to context. "
        "Format: [INTRO] ... [MAIN] ... [OUTRO] with timing marks."
    ),
    "general": (
        "You are an intelligent office assistant for a creative agency.\n\n"
        "RESPONSE STYLE:\n"
        "- Match response length to question complexity\n"
        "- Short questions get short answers\n"
        "- Never use filler phrases like \"Certainly!\" or \"Great question!\"\n"
        "- Get to the answer immediately\n"
        "- Use bullet points only when listing 3+ distinct items\n"
        "- Otherwise write in natural prose\n\n"
        "TONE:\n"
        "- Professional but warm\n"
        "- Direct and confident\n"
        "- Never robotic or overly formal\n\n"
        "THINKING:\n"
        "- If a request is ambiguous, make a reasonable assumption \n"
        "  and state it, rather than asking clarifying questions\n"
        "- If you spot a problem the user hasn't noticed, mention it\n"
        "- Always think about what the user actually needs, \n"
        "  not just what they literally asked\n\n"
        "FORMATTING:\n"
        "- Use markdown only when it genuinely helps readability\n"
        "- Never bold random words mid-sentence\n"
        "- Code always goes in code blocks\n"
        "- Keep responses scannable for busy professionals"
    ),
    "meetings": (
        "You are a professional Executive Assistant. Parse this meeting transcript or notes and output exactly four sections:\n"
        "1. Executive Summary (2-3 sentences)\n"
        "2. Key Decisions Made (Bullet points)\n"
        "3. Action Items (Use checkboxes [ ] and bold the owner's name)\n"
        "4. Suggested Agenda for Next Meeting\n"
        "Do not include any other commentary."
    ),
    "announcements": (
        "You are a professional Internal Communications Manager. "
        "Take the rough notes provided and format them into a polite, clear, and professional office announcement. "
        "Include a clear Subject line, proper greetings/sign-offs, and organized formatting. "
        "Make it ready to copy-paste into an email or WhatsApp group."
    ),
    "email": (
        "Write professional emails that are:\n"
        "- Clear and scannable with one main ask\n"
        "- Appropriate length (never longer than needed)\n"
        "- Culturally appropriate for Indian business context\n"
        "- Never starting with \"I hope this email finds you well\""
    ),
    "content": (
        "When writing content:\n"
        "- Match the brand voice of the client\n"
        "- SEO-aware without being keyword-stuffed\n"
        "- Always ask about target audience if not specified\n"
        "- Provide 2-3 headline options, not just one"
    ),
    "analysis": (
        "When analysing anything:\n"
        "- Lead with the conclusion, not the methodology\n"
        "- Use specific numbers and examples\n"
        "- Flag assumptions you're making\n"
        "- End with a clear recommended action"
    ),
}

DEFAULT_SYSTEM = SYSTEM_PROMPTS["general"]
MAX_TOKENS     = 4096

# Multi-turn chat: higher ceilings reduce mid-answer cutoffs (cost/latency tradeoff).
_HAIKU_CONV_MAX_OUT   = 8192
_SONNET_LONG_ARTIFACT = 16384
LONG_OUTPUT_TASK_TYPES = frozenset({
    "coding", "html_design", "presentations", "scripts", "content",
    "meetings", "announcements", "email", "data_analysis",
})


def _anthropic_extra_headers(model_name: str, max_tokens: int) -> dict:
    """Prompt cache + extended output beta when max_tokens exceeds the default 8k cap."""
    headers = {"anthropic-beta": "prompt-caching-2024-07-31"}
    if max_tokens > 8192:
        if "claude-3-5" in model_name:
            headers["anthropic-beta"] += ",max-tokens-3-5-sonnet-2024-07-15"
        else:
            headers["anthropic-beta"] += ",output-128k-2025-02-19"
    return headers


def _conversation_max_tokens(task_type: str, model_tier: str) -> int:
    """Per-task output budget for conversation API + stream (Haiku capped for API limits)."""
    key = (task_type or "general").lower().replace(" ", "_")
    if key == "captions":
        want = MAX_TOKENS
    elif key in LONG_OUTPUT_TASK_TYPES:
        want = _SONNET_LONG_ARTIFACT
    else:
        want = _HAIKU_CONV_MAX_OUT
    if (model_tier or "haiku").lower() == "haiku":
        return min(want, _HAIKU_CONV_MAX_OUT)
    return want


# ── Task Auto-Detection ───────────────────────────────────────────────────────
_TASK_KEYWORDS = {
    "coding":        ["code", "python", "javascript", "typescript", "function", "class",
                      "debug", "bug", "fix", "implement", "algorithm", "sql", "api",
                      "backend", "frontend", "database", "error", "exception"],
    "html_design":   ["html", "css", "webpage", "website", "design", "ui", "ux",
                      "layout", "component", "landing page", "interface", "tailwind"],
    "presentations": ["slide", "presentation", "deck", "powerpoint", "pitch", "keynote"],
    "captions":      ["caption", "instagram", "twitter", "linkedin", "social media",
                      "post", "hashtag", "tweet"],
    "scripts":       ["script", "dialogue", "scene", "screenplay", "video script",
                      "podcast", "voiceover"],
    "meetings":      ["meeting", "transcript", "notes", "agenda", "call recording", "discussion"],
    "announcements": ["announcement", "notice", "office update", "regarding:", "memo", "bulletin"],
    "email":         ["email", "draft email", "write email", "reply to", "compose email",
                      "subject line", "email template", "follow up email"],
    "content":       ["blog", "article", "content", "copywriting", "write about", "seo",
                      "product description", "tagline", "brand voice", "headline"],
    "data_analysis": ["analyse", "analyze", "analysis", "breakdown", "compare", "review",
                      "evaluate", "pros and cons", "research", "insights", "report"],
}

from functools import lru_cache

@lru_cache(maxsize=1000)
def _detect_task(message: str) -> str:
    """Detect task type from message keywords. Cached to save processing time."""
    lower = message.lower()
    for task, keywords in _TASK_KEYWORDS.items():
        if any(k in lower for k in keywords):
            return task
    return "general"

# ── Complexity Detection ──────────────────────────────────────────────────────
_COMPLEX_INDICATORS = [
    "analyse", "analyze", "compare", "evaluate", "strategy", "plan", "build",
    "design", "research", "write a full", "write a complete", "create a",
    "step by step", "in detail", "comprehensive", "explain how", "why does",
    "what are all", "pros and cons",
]

def _is_complex(message: str) -> bool:
    """Returns True if the message is likely a complex, multi-step request."""
    lower = message.lower()
    if len(lower.split()) < 8:
        return False
    return any(kw in lower for kw in _COMPLEX_INDICATORS)

# ── Uncertainty Calibration ────────────────────────────────────────────────
_UNCERTAINTY_TRIGGERS = [
    "recent events", "current prices", "latest version", "today", "right now",
    "live data", "stock price", "weather", "news", "this week", "this month",
    "right now", "currently",
]

def _needs_uncertainty_disclaimer(question: str) -> bool:
    lower = question.lower()
    return any(t in lower for t in _UNCERTAINTY_TRIGGERS)

# ── Quality Check ──────────────────────────────────────────────────────────────
_FILLER_PHRASES = [
    "Certainly!", "Great question!", "Of course!", "Absolutely!",
    "I'd be happy to", "I would be happy to", "Sure thing!",
    "Excellent question!", "That's a great",
]

def _quality_check(response: str, task_type: str, model_name: str) -> str:
    """Check response quality and fix issues using Haiku (cheap model)."""
    issues = []
    for filler in _FILLER_PHRASES:
        if filler in response:
            issues.append(f'Remove filler phrase: "{filler}"')
    if response.rstrip().endswith("...") or len(response.strip()) < 30:
        issues.append("Response appears incomplete — do not end with ellipsis, provide a full answer")
    if not issues:
        return response

    preview = (response[:240].replace("\n", " ") + ("..." if len(response) > 240 else ""))
    logger.info(
        "QUALITY_CHECK triggered | task=%s upstream_model=%s issues=%s out_chars=%d preview=%r",
        task_type, model_name, issues, len(response), preview,
    )

    issue_list = "\n".join(f"- {i}" for i in issues)
    try:
        _haiku = os.getenv("HAIKU_MODEL", "claude-haiku-4-5-20251001")
        fix_resp = client.messages.create(
            model=_haiku,  # Always use cheapest model for cleanup — no reason to use Sonnet here
            max_tokens=2000,
            system="You are an editor. Fix the issues listed and return only the corrected response — nothing else.",
            messages=[{
                "role": "user",
                "content": f"Fix these issues in the response:\n{issue_list}\n\nOriginal response:\n{response}"
            }],
        )
        fixed = (_anthropic_response_text(fix_resp) or response).strip()
        usage = getattr(fix_resp, "usage", None)
        in_tok = getattr(usage, "input_tokens", None) if usage else None
        out_tok = getattr(usage, "output_tokens", None) if usage else None
        logger.info(
            "QUALITY_CHECK applied | task=%s changed=%s fix_in=%s fix_out=%s new_chars=%d",
            task_type, fixed != response.strip(), in_tok, out_tok, len(fixed),
        )
        return fixed or response
    except Exception as e:
        logger.warning(f"Quality check fix failed (non-fatal): {e}")
        return response

# ── Email Notification Helper (#4) ───────────────────────────────────────────
def _notify_email(subject: str, body: str):
    """
    Sends an email notification to the FOUNDER_EMAIL address via SMTP.
    Set SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, FOUNDER_EMAIL in .env.
    Non-fatal — silently logs if not configured.
    """
    import smtplib
    from email.mime.text import MIMEText

    smtp_host  = os.getenv("SMTP_HOST", "")
    smtp_port  = int(os.getenv("SMTP_PORT", 587))
    smtp_user  = os.getenv("SMTP_USER", "")
    smtp_pass  = os.getenv("SMTP_PASS", "")
    founder_email = os.getenv("FOUNDER_EMAIL", "")

    if not all([smtp_host, smtp_user, smtp_pass, founder_email]):
        logger.debug("Email not configured — skipping notification")
        return

    try:
        msg = MIMEText(body, "plain")
        msg["Subject"] = f"[Office Assistant] {subject}"
        msg["From"]    = smtp_user
        msg["To"]      = founder_email
        with smtplib.SMTP(smtp_host, smtp_port, timeout=8) as srv:
            srv.starttls()
            srv.login(smtp_user, smtp_pass)
            srv.send_message(msg)
        logger.info(f"Email sent: {subject}")
    except Exception as e:
        logger.warning(f"Email notification failed (non-fatal): {e}")

# ── Smart File Context ───────────────────────────────────────────────────────────
def _smart_file_context(file_content: str, user_question: str, haiku_model: str) -> str:
    """For large files, extract only sections relevant to the user's question."""
    if len(file_content) < 3000:
        return file_content
    try:
        extract_resp = client.messages.create(
            model=haiku_model,
            max_tokens=1000,
            system="You are a document analyst. Extract only relevant sections from a document, verbatim.",
            messages=[{
                "role": "user",
                "content": (
                    f'From this document, extract only the sections relevant to: "{user_question}"\n\n'
                    f"Document:\n{file_content[:8000]}\n\n"
                    f"Return only the relevant excerpts, nothing else."
                )
            }],
        )
        logger.info(f"Smart file context: reduced {len(file_content)} chars for question")
        return _anthropic_response_text(extract_resp) or file_content[:4000]
    except Exception as e:
        logger.warning(f"Smart file context failed (non-fatal): {e}")
        return file_content[:4000]

# ── Conversation State Awareness ──────────────────────────────────────────────────
def _analyze_conversation_state(history: list, haiku_model: str) -> Optional[str]:
    """Detect if the user is frustrated or conversation is going in circles."""
    if len(history) < 6:
        return None
    try:
        recent = history[-6:]
        history_text = "\n".join(
            f"{m['role'].upper()}: {str(m.get('content',''))[:200]}" for m in recent
        )
        state_resp = client.messages.create(
            model=haiku_model,
            max_tokens=150,
            system="You are a conversation analyst. Be very terse.",
            messages=[{
                "role": "user",
                "content": (
                    f"Analyze this conversation in one line each:\n"
                    f"- Is the user frustrated? (yes/no)\n"
                    f"- Are we going in circles? (yes/no)\n"
                    f"- Core unresolved need (5 words max)\n\n"
                    f"History:\n{history_text}"
                )
            }],
        )
        return _anthropic_response_text(state_resp)
    except Exception as e:
        logger.warning(f"Conversation state analysis failed (non-fatal): {e}")
        return None

# ── Task Timeouts ─────────────────────────────────────────────────────────────────
TASK_TIMEOUTS = {
    "html_design":    3600,  # 60 minutes
    "presentations":  3600,  # 60 minutes
    "coding":         3600,  # 60 minutes
    "analysis":       3600,  # 60 minutes
    "general":        3600,  # 60 minutes
    "email":          3600,
    "content":        3600,
    "captions":       3600,
    "scripts":        3600,
    "meetings":       3600,
    "announcements":  3600,
}



def _get_all_users_str() -> str:
    try:
        with open(EMPLOYEES_DB, "r") as f:
            data = json.load(f)
            return ", ".join([e.get("id") for e in data.get("employees", []) if e.get("id")])
    except Exception:
        return "api"


def _message_content_as_text(content) -> str:
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
    return str(content or "").strip()


HISTORY_SUMMARY_KEEP_LAST = 10
HISTORY_SUMMARY_MIN_MESSAGES = 14
HISTORY_SUMMARY_MIN_TOTAL_CHARS = 12_000


def _maybe_summarize_history(messages: list, haiku_model: str) -> tuple[list, Optional[str]]:
    """
    When the thread is long, summarize older turns with Haiku and keep only the last
    HISTORY_SUMMARY_KEEP_LAST messages in the API payload. Full history remains in SQLite.
    """
    if not messages:
        return messages, None
    if len(messages) <= HISTORY_SUMMARY_KEEP_LAST:
        return messages, None
    total_chars = sum(len(_message_content_as_text(m.get("content"))) for m in messages)
    if len(messages) < HISTORY_SUMMARY_MIN_MESSAGES and total_chars < HISTORY_SUMMARY_MIN_TOTAL_CHARS:
        return messages, None

    old = messages[:-HISTORY_SUMMARY_KEEP_LAST]
    recent = messages[-HISTORY_SUMMARY_KEEP_LAST:]
    parts = []
    for m in old:
        t = _message_content_as_text(m.get("content"))
        t = " ".join(t.split())
        if not t:
            continue
        if len(t) > 4000:
            t = t[:4000] + "…"
        parts.append(f"{m.get('role', '?').upper()}: {t}")
    transcript = "\n\n".join(parts)
    if len(transcript) > 120_000:
        transcript = transcript[:120_000] + "\n…(truncated for summarizer)"

    try:
        summ_resp = client.messages.create(
            model=haiku_model,
            max_tokens=2048,
            system=(
                "Summarize the conversation transcript for another AI that will continue helping the user. "
                "Output dense bullet points: key facts, names, decisions, constraints, open questions, "
                "and anything the assistant promised. No preamble. Max ~900 words."
            ),
            messages=[{"role": "user", "content": transcript}],
        )
        summary = (_anthropic_response_text(summ_resp) or "").strip()
        if not summary or len(summary) < 40:
            return messages, None
        logger.info("History compressed for API | old_turns=%d kept=%d", len(old), len(recent))
        return recent, summary
    except Exception as e:
        logger.warning("History summarization failed (non-fatal): %s", e)
        return messages, None


def _format_output_contract(oc: dict) -> str:
    """Turn UI output-contract chips into a short instruction block."""
    if not oc or not isinstance(oc, dict):
        return ""
    lines = []
    fmt = (oc.get("format") or "").strip()
    ln = (oc.get("length") or "").strip()
    tone = (oc.get("tone") or "").strip()
    aud = (oc.get("audience") or "").strip()
    if fmt:
        lines.append(f"- **Format:** {fmt}")
    if ln:
        lines.append(f"- **Length:** {ln}")
    if tone:
        lines.append(f"- **Tone:** {tone}")
    if aud:
        lines.append(f"- **Audience:** {aud}")
    if not lines:
        return ""
    return "## Output the user requested\n" + "\n".join(lines)


def _attachment_grounding_instruction() -> str:
    return (
        "\n\n## Source discipline (attachments)\n"
        "The user's message includes attached files. When you rely on them, cite which file "
        "as **[Attached: *filename*]** and quote only short spans (under ~25 words) when needed. "
        "If the attachment does not contain the answer, say so clearly."
    )


def _build_system_prompt(
    task_type: str,
    user_id: str,
    project_id: Optional[str] = None,
    message: str = "",
    *,
    history_summary: Optional[str] = None,
    output_contract_block: Optional[str] = None,
    attachment_grounding: bool = False,
) -> tuple[list, list]:
    """
    Returns (system_prompt_blocks, kb_sources) where kb_sources is a list of
    {filename, doc_id} for UI attribution when project KB is used.
    """
    kb_sources: list = []
    base_prompt = SYSTEM_PROMPTS.get(task_type.lower().replace(" ", "_"), DEFAULT_SYSTEM)
    base_prompt += "\n\nBe concise and direct. No unnecessary preamble. No phrases like 'Certainly!' or 'Great question!'. Get to the answer immediately."

    # Auto-memory extraction instruction
    users_str = _get_all_users_str()
    base_prompt += f"""

## AUTO-MEMORY EXTRACTION
If the user shares new preferences, project details, or makes decisions you should remember, output a structured JSON profile update at the very end of your response inside EXACTLY this XML block:
<SAVE_MEMORY_PROFILE user="USER_ID">
{{
  "preferences": {{"communication_style": "...", "response_length": "..."}},
  "active_projects": ["...", "..."],
  "frequently_used": ["...", "..."],
  "past_decisions": [{{"date": "...", "decision": "...", "reason": "..."}}]
}}
</SAVE_MEMORY_PROFILE>

Valid USER_IDs you can assign memories to:
{users_str}
(If saving for the current user, use {user_id})"""

    # Uncertainty disclaimer for time-sensitive questions
    if message and _needs_uncertainty_disclaimer(message):
        base_prompt += (
            "\n\nIMPORTANT: This question may involve real-time or recent information. "
            "If you are not certain about any specific facts, explicitly say so. "
            "Do not present uncertain information as fact."
        )

    try:
        import smart_memory
        mem_ctx = smart_memory.format_for_prompt(user_id, query=message)
    except Exception:
        mem_ctx = memory_store.format_for_prompt(user_id)
    team_mem_ctx = memory_store.format_team_memories()

    sections = [base_prompt]
    if mem_ctx:
        sections.append(mem_ctx)

    if team_mem_ctx:
        sections.append("## Shared team memories\n" + team_mem_ctx)

    if project_id:
        project = project_store.get_project(project_id)
        if project:
            if project.get("instructions"):
                sections.append(f"## Custom Instructions\n{project['instructions']}")
            if project.get("memory"):
                sections.append(f"## Project Memory\n{project['memory']}")
            if message:
                matches = kb_retriever.search_hybrid(project_id, user_id, message, limit=8)
                kb_sources = kb_retriever.unique_doc_labels(matches)
                kb_ctx = kb_retriever.format_for_prompt(matches)
                if kb_ctx:
                    sections.append(kb_ctx)

    if history_summary:
        sections.append("## Earlier in this conversation (summarized)\n" + history_summary)

    if output_contract_block:
        sections.append(output_contract_block)

    if attachment_grounding:
        sections.append(_attachment_grounding_instruction().strip())

    final_text = "\n\n".join(sections)
    combined = MASTER_SYSTEM_PROMPT.rstrip() + "\n\n" + final_text
    return [
        {
            "type": "text",
            "text": combined,
            "cache_control": {"type": "ephemeral"},
        }
    ], kb_sources

# ── Anthropic response parsing ─────────────────────────────────────────────────
def _anthropic_response_text(response, *, include_thinking: bool = False) -> str:
    """
    Concatenate text from a Messages API response. Skips tool_use blocks.
    When include_thinking is False (default), skips thinking blocks — use that for
    user-facing answers. Set True only for internal reasoning snippets.
    Models that emit extended thinking often put a non-text block first; using only
    content[0].text raises or returns empty.
    """
    skip_tool = frozenset({"tool_use", "server_tool_use"})
    parts: list = []
    for block in getattr(response, "content", None) or []:
        typ = getattr(block, "type", None)
        if typ in skip_tool:
            continue
        if typ == "thinking":
            if include_thinking:
                parts.append(getattr(block, "thinking", "") or "")
            continue
        if typ == "redacted_thinking":
            if include_thinking:
                parts.append("")
            continue
        if typ == "text":
            t = getattr(block, "text", None)
            if t:
                parts.append(t)
        elif hasattr(block, "text") and getattr(block, "text", None):
            parts.append(block.text)
    out = "".join(parts).strip()
    if out:
        return out
    blocks = getattr(response, "content", None) or []
    if len(blocks) == 1 and hasattr(blocks[0], "text") and getattr(blocks[0], "text", None):
        return (blocks[0].text or "").strip()
    return ""


# ── Helper: call System ───────────────────────────────────────────────────────
def call_claude(task_type: str, message: str, user_id: str = "api",
                max_tokens: int = MAX_TOKENS, force_tier: Optional[str] = None,
                project_id: Optional[str] = None) -> dict:
    """
    Shared helper to call System with budget check + usage logging.
    Returns dict with: success, response, model_used, model_tier, tokens, cost_usd, budget
    """
    budget = check_budget_available()
    if not budget["allowed"]:
        return {"success": False, "error": "Monthly budget limit reached", "budget": budget}

    model_config  = get_model_for_task(task_type) if not force_tier else _build_config(force_tier)
    model_name    = model_config["name"]
    model_tier    = model_config["tier"]
    system_prompt, _ = _build_system_prompt(task_type, user_id, project_id, message=message)

    logger.info(f"System call | task={task_type} | model={model_tier} | user={user_id}")

    # ── Reasoning step for complex requests ─────────────────────────────────
    reasoning_context = ""
    if _is_complex(message) and task_type not in ("html_design", "presentations", "coding"):
        try:
            think_kwargs = {
                "model": model_name,
                "max_tokens": 600,
                "system": "You are a thinking assistant. Your job is to reason through requests briefly before they are answered.",
                "messages": [{
                    "role": "user",
                    "content": (
                        f"Before answering this request, think through:\n"
                        f"1. What is the user actually trying to achieve?\n"
                        f"2. What information do I need?\n"
                        f"3. What is the best format for the answer?\n"
                        f"4. Are there any risks or issues to flag?\n\n"
                        f"Request: {message}\n\n"
                        f"Respond with your thinking ONLY — no final answer."
                    )
                }],
            }
            think_resp = client.messages.create(**think_kwargs)
            think_txt = _anthropic_response_text(think_resp, include_thinking=True)
            reasoning_context = f"\n\n[Thinking]\n{think_txt}\n\n[Now answer the original request]\n"
            logger.info(f"Reasoning step added for complex task | user={user_id}")
        except Exception as e:
            logger.warning(f"Reasoning step failed (non-fatal): {e}")
    # ── End reasoning step ───────────────────────────────────────────────────

    try:
        kwargs = {
            "model": model_name,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": reasoning_context + message}],
        }
        kwargs["extra_headers"] = _anthropic_extra_headers(model_name, max_tokens)

        response = client.messages.create(**kwargs)
        output_text = _anthropic_response_text(response)
        if not output_text:
            logger.error("System returned no text blocks (present?). content=%r", getattr(response, "content", None))
            return {"success": False, "error": "System returned an empty response. Try again or shorten the deck."}
        # ── Quality check (auto-fix filler phrases & incomplete responses) ──
        output_text = _quality_check(output_text, task_type, model_name)
        # ── End quality check ──
        input_tokens  = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        cost          = calculate_cost(model_tier, input_tokens, output_tokens)

        record_usage(
            task_type=task_type, model_tier=model_tier, model_name=model_name,
            input_tokens=input_tokens, output_tokens=output_tokens, cost=cost, user_id=user_id,
        )
        updated_budget = check_budget_available()
        return {
            "success":    True,
            "response":   output_text,
            "model_used": model_name,
            "model_tier": model_tier,
            "task_type":  task_type,
            "tokens": {"input": input_tokens, "output": output_tokens, "total": input_tokens + output_tokens},
            "cost_usd":   cost,
            "budget": {
                "spent":     updated_budget["spent"],
                "remaining": updated_budget["remaining"],
                "limit":     updated_budget["limit"],
            },
        }
    except anthropic.AuthenticationError:
        logger.error("Invalid Anthropic API key")
        return {"success": False, "error": "Invalid API key. Check ANTHROPIC_API_KEY in config/.env"}
    except anthropic.RateLimitError:
        logger.warning("Anthropic rate limit hit")
        return {"success": False, "error": "Rate limit reached. Please wait and retry."}
    except anthropic.APIError as e:
        logger.error(f"Anthropic API error: {e}")
        return {"success": False, "error": f"System API error: {str(e)}"}
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        return {"success": False, "error": "Internal server error"}


def _build_config(tier: str) -> dict:
    from model_router import MODEL_COSTS
    c = MODEL_COSTS.get(tier, MODEL_COSTS["haiku"]).copy()
    c["tier"] = tier
    return c





def _duckduckgo_instant(query: str) -> dict:
    """Public instant-answer JSON (no API key). May return sparse results."""
    q = (query or "").strip()[:240]
    if len(q) < 2:
        return {}
    try:
        r = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": q, "format": "json", "no_html": 1, "skip_disambig": 1},
            timeout=10,
            headers={"User-Agent": "SystemOfficeAssistant/1.0 (+https://localhost)"},
        )
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {}
    except Exception as e:
        logger.warning("DuckDuckGo instant search failed: %s", e)
        return {}


def _web_search_snippets(query: str, max_chars: int = 4000) -> str:
    """Format DDG instant results for injection into the user turn (not stored verbatim in UI)."""
    data = _duckduckgo_instant(query)
    if not data:
        return ""
    lines = []
    abst = (data.get("AbstractText") or "").strip()
    if abst:
        lines.append(f"Instant summary: {abst}")
        u = (data.get("AbstractURL") or "").strip()
        if u:
            lines.append(f"Primary URL: {u}")
    heading = (data.get("Heading") or "").strip()
    if heading and heading not in (abst or ""):
        lines.append(f"Topic: {heading}")

    def _walk_related(items, depth=0):
        if depth > 4 or not items:
            return
        for item in items[:12]:
            if isinstance(item, dict):
                if item.get("Text"):
                    lines.append(f"- {str(item['Text'])[:420]}")
                if item.get("FirstURL"):
                    lines.append(f"  URL: {item['FirstURL']}")
                if "Topics" in item:
                    _walk_related(item.get("Topics") or [], depth + 1)

    _walk_related(data.get("RelatedTopics") or [])

    if not lines:
        return ""

    body = "\n".join(lines[:24])
    if len(body) > max_chars:
        body = body[:max_chars] + "\n…(truncated)"
    return (
        "\n\n---\n[Web search context — public snippets only; may be incomplete, wrong, or outdated. "
        "Verify anything time-sensitive, legal, medical, or financial.]\n"
        f"Query: {query.strip()[:240]}\n\n{body}\n---\n"
    )


def _inject_web_context(msgs: list, snippet: str) -> list:
    """Append web snippet to the last user message content (string or first text block in list)."""
    if not snippet or not msgs or msgs[-1].get("role") != "user":
        return msgs
    out = list(msgs[:-1])
    last = dict(msgs[-1])
    c = last.get("content")
    if isinstance(c, str):
        last["content"] = c + snippet
    elif isinstance(c, list):
        new_blocks = copy.deepcopy(c)
        if new_blocks and isinstance(new_blocks[0], dict) and new_blocks[0].get("type") == "text":
            new_blocks[0]["text"] = (new_blocks[0].get("text") or "") + snippet
        else:
            new_blocks.insert(0, {"type": "text", "text": snippet.strip()})
        last["content"] = new_blocks
    else:
        return msgs
    out.append(last)
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES

# ── Main Chat ─────────────────────────────────────────────────────────────────
@app.route("/api/chat", methods=["POST"])
@limiter.limit("30 per minute; 500 per day")
def chat():
    """
    Main chat endpoint.
    Body: { task_type, message, user_id? }
    """
    data      = request.get_json(silent=True) or {}
    task_type = data.get("task_type", "").strip()
    message   = " ".join(data.get("message", "").split())
    user_id   = data.get("user_id",   "anonymous")

    if not task_type:
        return jsonify({"error": "task_type is required"}), 400
    if not message:
        return jsonify({"error": "message is required"}), 400

    result = call_claude(task_type, message, user_id)

    if not result["success"]:
        err = result.get("error", "")
        if "Invalid API" in err:
            status = 401
        elif "Rate limit" in err or "budget" in err.lower():
            status = 429
        else:
            status = 500
        return jsonify({"error": err, **{k: v for k, v in result.items() if k != "error"}}), status

    return jsonify(result)


# ── HTML Generator (Advanced Feature — Days 10-11) ────────────────────────────
@app.route("/api/html/generate", methods=["POST"])
def html_generate():
    """
    Generates a complete, self-contained HTML page from a description.
    Body: { description, style_hints?, user_id? }
    Returns: { html_code, preview_url, ... }
    """
    data        = request.get_json(silent=True) or {}
    description = data.get("description", "").strip()
    style_hints = data.get(
        "style_hints",
        "modern, distinctive typography, CSS variables, strong colour direction — avoid glassmorphism",
    ).strip()
    user_id     = data.get("user_id", "anonymous")

    if not description:
        return jsonify({"error": "description is required"}), 400

    import random
    
    style_directions = [
        "Bold editorial — black background, strong typography, minimal color",
        "Warm luxury — cream backgrounds, serif fonts, gold accents",
        "Clean minimal — white space, thin lines, muted palette",
        "Vibrant modern — bright accent colors, geometric shapes, sans-serif",
        "Dark industrial — charcoal tones, monospace elements, sharp edges"
    ]
    
    font_pairs = [
        ("Bebas Neue", "DM Sans", "Bold display + clean body"),
        ("Playfair Display", "Source Sans Pro", "Elegant serif + modern body"),
        ("Syne", "Manrope", "Geometric display + readable UI body (avoid Inter as primary display)"),
        ("Cabinet Grotesk", "Lora", "Modern grotesk + literary body"),
        ("Clash Display", "Satoshi", "Contemporary display pair"),
    ]
    
    chosen_style = random.choice(style_directions)
    chosen_fonts = random.choice(font_pairs)

    prompt = (
        f"Build a complete, production-quality, self-contained HTML page for: {description}\n\n"
        f"Design aesthetic: {chosen_style}\n"
        f"Fonts: {chosen_fonts[0]} for all headings, {chosen_fonts[1]} for body text.\n"
        f"Additional style hints: {style_hints}\n\n"
        "OUTPUT RULES (CRITICAL — do not skip any):\n"
        "1. Output ONLY raw HTML starting with <!DOCTYPE html> and ending with </html>\n"
        "2. NO markdown fences, NO explanations, NO comments like '// rest of code'\n"
        "3. Write EVERY section in FULL — no abbreviations, no '...' placeholders\n\n"
        "REQUIRED SECTIONS (all must be fully implemented):\n"
        "- Navigation bar with logo and links, smooth scroll on click\n"
        "- Hero section with headline, subheadline, CTA button, and a visual element\n"
        "- Features / Services section (at least 3 items with icons or illustrations)\n"
        "- About or social proof section (testimonials, stats, or team)\n"
        "- Call to action section\n"
        "- Footer with links and copyright\n\n"
        "REQUIRED CSS:\n"
        "- CSS custom properties (variables) for all colors, fonts, and spacing\n"
        "- Hover effects and transitions on all interactive elements\n"
        "- Smooth scroll behavior on <html>\n"
        "- At least 2 responsive breakpoints (tablet + mobile)\n"
        "- Keyframe animations for entrance effects\n\n"
        "REQUIRED JAVASCRIPT:\n"
        "- IntersectionObserver for scroll-triggered animations on sections\n"
        "- Mobile hamburger menu toggle\n"
        "- Active nav link highlighting on scroll\n\n"
        "Target output: 500-900 lines of complete, dense, production-quality HTML/CSS/JS."
    )

    def validate_html_output(html):
        issues = []
        if "</html>" not in html.lower():
            issues.append("incomplete")
        if "<style>" in html.lower() and "</style>" not in html.lower():
            issues.append("css_truncated")
        if html.count("{") != html.count("}"):
            issues.append("css_syntax_error")
        return issues

    max_retries = 2
    for attempt in range(max_retries):
        result = call_claude("html_design", prompt, user_id, max_tokens=16000)
        if not result["success"]:
            return jsonify({"error": result.get("error", "Generation failed")}), 500

        html_code = result["response"]
        
        # Strip markdown code fences if System wrapped the output
        if html_code.strip().startswith("```"):
            lines = html_code.strip().split("\n")
            inner = lines[1:] if len(lines) > 1 else lines
            if inner and inner[-1].strip() == "```":
                inner = inner[:-1]
            html_code = "\n".join(inner).strip()
            
        issues = validate_html_output(html_code)
        if not issues or attempt == max_retries - 1:
            break

    return jsonify({
        **result,
        "html_code": html_code,
        "preview_endpoint": "/api/html/preview",
    })

@app.route("/api/html/generate/stream", methods=["POST"])
def html_generate_stream():
    """
    Streams the generation of a complete HTML page using Server-Sent Events.
    Body: { description, style_hints?, user_id? }
    """
    data        = request.get_json(silent=True) or {}
    description = data.get("description", "").strip()
    style_hints = data.get(
        "style_hints",
        "modern, distinctive typography, CSS variables, strong colour direction — avoid glassmorphism",
    ).strip()
    user_id     = data.get("user_id", "anonymous")

    if not description:
        return jsonify({"error": "description is required"}), 400

    import random
    
    style_directions = [
        "a) DARK EDITORIAL: near-black bg (#080808), warm white text (#f0ede8), single bold accent (gold, orange, or red), sharp grid",
        "b) WARM MINIMAL: cream (#faf8f4) or off-white bg, dark text, serif display font, generous whitespace",
        "c) BOLD MODERN: white bg, black text, one electric accent colour, strong typography contrast",
        "d) DARK LUXURY: deep navy or dark green bg, gold or copper accent, premium serif headings",
    ]

    font_pairs = [
        ("Bebas Neue", "DM Sans"),
        ("Playfair Display", "Source Sans Pro"),
        ("Syne", "Manrope"),
        ("Instrument Serif", "Geist"),
        ("Clash Display", "Satoshi"),
        ("Cormorant Garamond", "Inter"),
    ]

    chosen_style = random.choice(style_directions)
    chosen_fonts = random.choice(font_pairs)

    prompt = (
        f"Build a complete, production-quality, self-contained HTML page for: {description}\n\n"
        f"Style direction (commit fully — do not blend styles): {chosen_style}\n"
        f"Font pairing: {chosen_fonts[0]} (all display/headings) + {chosen_fonts[1]} (body text). Use Google Fonts.\n"
        f"Additional brief: {style_hints}\n\n"
        "OUTPUT RULES (CRITICAL — do not skip any):\n"
        "1. Output ONLY raw HTML starting with <!DOCTYPE html> and ending with </html>\n"
        "2. NO markdown fences, NO explanations, NO placeholder comments like '// rest of code'\n"
        "3. Write EVERY section in FULL — no '...' abbreviations\n\n"
        "DESIGN CONSTRAINTS (NEVER violate):\n"
        "- Do NOT use purple, violet, or indigo as accent colours\n"
        "- Do NOT use glassmorphism (backdrop-filter blur on cards)\n"
        "- Do NOT use floating orbs or blob backgrounds\n"
        "- Do NOT use gradient text (-webkit-background-clip: text)\n"
        "- Do NOT use glowing box-shadows on cards or buttons\n"
        "- border-radius on cards must be ≤16px\n"
        "- No pill-shaped buttons unless explicitly requested\n\n"
        "REQUIRED STRUCTURE:\n"
        "- Sticky nav: logo + links + CTA; changes appearance on scroll\n"
        "- Hero: full viewport height, strong headline (weight 800-900), subtext, 2 CTAs, scroll indicator\n"
        "- 2-3 substantive content sections with clear visual hierarchy; at least one dark/light contrast flip\n"
        "- At least one layout using CSS Grid creatively (not just equal columns)\n"
        "- CTA section before footer\n"
        "- Footer with minimal but complete information\n\n"
        "REQUIRED CSS:\n"
        "- CSS custom properties: --bg, --s1, --s2, --bdr, --txt, --txt-2, --acc\n"
        "- Section labels: 10-11px, uppercase, letter-spacing 0.12em\n"
        "- Headlines: font-weight 800-900, letter-spacing -0.02em to -0.04em\n"
        "- Smooth hover transitions on all interactive elements\n"
        "- At least 2 responsive breakpoints (tablet + mobile)\n\n"
        "REQUIRED JAVASCRIPT:\n"
        "- IntersectionObserver for scroll-triggered reveal animations\n"
        "- Mobile hamburger menu toggle\n"
        "- Nav scroll state change\n\n"
        "QUALITY BAR: Must look like it was designed by a senior designer at a top agency. "
        "If the result could be any generic AI output, it fails. Make it visually distinct."
    )

    budget = check_budget_available()
    if not budget["allowed"]:
        def _budget_err():
            yield f"data: {json.dumps({'type':'error','error':'Monthly budget limit reached'})}\n\n"
        return Response(stream_with_context(_budget_err()), mimetype="text/event-stream")

    model_config  = get_model_for_task("html_design")
    model_name    = model_config["name"]
    model_tier    = model_config["tier"]
    system_prompt, _ = _build_system_prompt("html_design", user_id, None)

    def generate():
        full_response  = ""
        input_tokens   = 0
        output_tokens  = 0

        kwargs = {
            "model": model_name,
            "max_tokens": 16000,
            "system": system_prompt,
            "messages": [{"role": "user", "content": prompt}],
        }
        # Use correct extended-output and prompt caching beta per model family
        kwargs["extra_headers"] = {"anthropic-beta": "prompt-caching-2024-07-31"}
        if "claude-3-5" in model_name:
            kwargs["extra_headers"]["anthropic-beta"] += ",max-tokens-3-5-sonnet-2024-07-15"
        else:
            kwargs["extra_headers"]["anthropic-beta"] += ",output-128k-2025-02-19"

        logger.info(f"HTML Stream | model={model_tier} | user={user_id}")

        try:
            with client.messages.stream(**kwargs) as stream:
                for text_chunk in stream.text_stream:
                    full_response += text_chunk
                    yield f"data: {json.dumps({'type':'text','text':text_chunk})}\n\n"

                final_msg     = stream.get_final_message()
                input_tokens  = final_msg.usage.input_tokens
                output_tokens = final_msg.usage.output_tokens
                cost          = calculate_cost(model_tier, input_tokens, output_tokens)

                record_usage(
                    task_type="html_design",
                    model_tier=model_tier,
                    model_name=model_name,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost=cost,
                    user_id=user_id,
                )

                yield f"data: {json.dumps({'type':'done','cost_usd':cost,'tokens':{'input':input_tokens,'output':output_tokens},'budget':check_budget_available()})}\n\n"
        except Exception as e:
            logger.error(f"HTML stream error: {e}")
            yield f"data: {json.dumps({'type':'error','error':str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@app.route("/api/html/preview", methods=["POST"])
def html_preview():
    """Renders raw HTML for live preview in an iframe."""
    data = request.get_json(silent=True) or {}
    html = data.get("html", "")
    return html, 200, {"Content-Type": "text/html"}


# ── Presentation Creator (Advanced Feature) ───────────────────────────────────
@app.route("/api/presentation", methods=["POST"])
def create_presentation():
    """
    Generates structured slide content from a topic/outline.
    Body: { topic, slide_count?, audience?, tone?, user_id? }
    Returns: { slides: [{title, bullets, notes}], raw_markdown, ... }
    """
    data        = request.get_json(silent=True) or {}
    topic       = data.get("topic", "").strip()
    slide_count = data.get("slide_count", 10)
    audience    = data.get("audience", "general business audience")
    tone        = data.get("tone", "professional")
    user_id     = data.get("user_id", "anonymous")

    if not topic:
        return jsonify({"error": "topic is required"}), 400

    prompt = (
        f"Create a {slide_count}-slide presentation on: {topic}\n\n"
        f"Audience: {audience}\n"
        f"Tone: {tone}\n\n"
        "Follow your system instructions for slide structure (hook first slide, statement headlines, CTA last).\n"
        "Format each slide EXACTLY as:\n"
        "## SLIDE N: [Statement headline]\n"
        "- [Point]\n"
        "- [Point]\n"
        "- [Point]\n"
        "[NOTES: Speaker notes]\n\n"
        "If the user asked for images or visuals, add one optional line per slide with a real https URL, e.g. "
        "IMAGE: https://images.unsplash.com/... (pick relevant stock imagery).\n\n"
        f"Number slides 1–{slide_count}. Slide 1 must grab attention; slide {slide_count} must drive a specific action."
    )

    result = call_claude("presentations", prompt, user_id, max_tokens=25000)
    if not result["success"]:
        return jsonify({"success": False, "error": result.get("error", "Generation failed")}), 500

    # Parse slides from markdown
    try:
        slides = _parse_slides(result["response"])
    except Exception as e:
        logger.exception("Slide parse failed: %s", e)
        return jsonify({"success": False, "error": "Could not parse slide format from System response."}), 500

    return jsonify({
        **result,
        "slides": slides,
        "slide_count": len(slides),
        "raw_markdown": result["response"],
    })


def _parse_slides(markdown: str) -> list:
    """Parse ## SLIDE N: Title format into structured slide objects."""
    import re
    slides = []
    blocks = re.split(r"(?=##\s*SLIDE\s+\d+\s*:)", markdown, flags=re.IGNORECASE)
    hdr_re = re.compile(r"^##\s*SLIDE\s+\d+:\s*(.+)$", re.IGNORECASE)
    for block in blocks:
        if not block.strip():
            continue
        lines   = block.strip().split("\n")
        header  = lines[0]
        title_m = hdr_re.match(header.strip())
        title   = title_m.group(1).strip() if title_m else header.strip().lstrip("#").strip()

        bullets = []
        notes   = ""
        for line in lines[1:]:
            ls = line.strip()
            low = ls.lower()
            if low.startswith("[notes:") or low.startswith("[notes :"):
                inner = ls.split(":", 1)[-1].rstrip("]").strip()
                notes = inner
            elif ls.startswith("- "):
                bullets.append(ls[2:].strip())
            elif re.match(r"^\s*(?:IMAGE|IMG|img)\s*:", ls, re.I):
                continue

        slides.append({"title": title, "bullets": bullets, "notes": notes})

    return slides


# ══════════════════════════════════════════════════════════════════════════════
# MULTI-TURN CONVERSATION ROUTES
# ══════════════════════════════════════════════════════════════════════════════

def call_claude_with_context(task_type: str, messages: list,
                             user_id: str = "api",
                             attachments: list = None,
                             project_id: str = None,
                             model_override: str = None,
                             output_contract: dict = None) -> dict:
    """
    Call System with full conversation history + optional file attachments.
    Memories are automatically injected into the system prompt.
    attachments: [{type:'image'|'document', ...}] from file_processor
    """
    budget = check_budget_available()
    if not budget["allowed"]:
        return {"success": False, "error": "Monthly budget limit reached", "budget": budget}

    if model_override and model_override != "auto":
        model_name = model_override
        model_tier = "sonnet" if "sonnet" in model_name.lower() else "haiku" if "haiku" in model_name.lower() else "opus"
    else:
        model_config  = get_model_for_task(task_type)
        model_name    = model_config["name"]
        model_tier    = model_config["tier"]

    haiku_model = get_model_for_task("general")["name"]
    messages_for_api, hist_summary = _maybe_summarize_history(list(messages), haiku_model)
    last_msg_text = messages[-1].get("content", "") if messages else ""
    oc_block = _format_output_contract(output_contract or {})

    system_prompt, _kb = _build_system_prompt(
        task_type,
        user_id,
        project_id,
        message=last_msg_text,
        history_summary=hist_summary,
        output_contract_block=oc_block or None,
        attachment_grounding=bool(attachments),
    )

    # ── Conversation state awareness ──
    conv_state = _analyze_conversation_state(messages_for_api, haiku_model)
    if conv_state and "frustrated: yes" in conv_state.lower():
        system_prompt = list(system_prompt)  # copy
        system_prompt[0] = {
            **system_prompt[0],
            "text": system_prompt[0]["text"] + "\n\nNOTE: The user seems frustrated. Be extra clear, concise, and directly address their core need. Acknowledge if previous responses may not have been helpful."
        }

    logger.info(f"Multi-turn | task={task_type} | model={model_tier} | turns={len(messages_for_api)} | files={len(attachments or [])} | user={user_id}")

    # Build message list — attach files to the last user message if any
    api_messages = list(messages_for_api)
    if attachments and api_messages and api_messages[-1]["role"] == "user":
        last_text = api_messages[-1]["content"]
        content_blocks = [{"type": "text", "text": last_text}]
        for att in attachments:
            if att.get("type") == "image":
                content_blocks.append({
                    "type": "image",
                    "source": {"type": "base64",
                               "media_type": att["media_type"],
                               "data": att["data"]},
                })
            elif att.get("type") == "document":
                # Smart file context — only extract relevant sections for large files
                user_question = last_text
                file_content  = att.get('content', '')
                smart_content = _smart_file_context(file_content, user_question, haiku_model)
                content_blocks[0]["text"] += (
                    f"\n\n---\nAttached file: {att['filename']}\n"
                    f"{smart_content}\n---"
                )
        api_messages = api_messages[:-1] + [{"role": "user", "content": content_blocks}]

    try:
        effective_max = _conversation_max_tokens(task_type, model_tier)
        headers = _anthropic_extra_headers(model_name, effective_max)

        response      = client.messages.create(
            model=model_name, max_tokens=effective_max,
            system=system_prompt, messages=api_messages,
            extra_headers=headers
        )
        output_text = _anthropic_response_text(response)
        if not output_text:
            logger.error("System returned no text (conversation path). content=%r", getattr(response, "content", None))
            return {"success": False, "error": "System returned an empty response. Try again."}
        input_tokens  = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        cost          = calculate_cost(model_tier, input_tokens, output_tokens)

        record_usage(task_type=task_type, model_tier=model_tier, model_name=model_name,
                     input_tokens=input_tokens, output_tokens=output_tokens,
                     cost=cost, user_id=user_id)
        updated_budget = check_budget_available()
        return {
            "success":    True,
            "response":   output_text,
            "model_used": model_name,
            "model_tier": model_tier,
            "task_type":  task_type,
            "tokens":     {"input": input_tokens, "output": output_tokens,
                           "total": input_tokens + output_tokens},
            "cost_usd":   cost,
            "budget":     {"spent":     updated_budget["spent"],
                           "remaining": updated_budget["remaining"],
                           "limit":     updated_budget["limit"]},
        }
    except anthropic.AuthenticationError:
        return {"success": False, "error": "Invalid API key. Check ANTHROPIC_API_KEY in config/.env"}
    except anthropic.RateLimitError:
        return {"success": False, "error": "Rate limit reached. Please wait and retry."}
    except anthropic.APIError as e:
        return {"success": False, "error": f"System API error: {str(e)}"}
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        return {"success": False, "error": "Internal server error"}
# ──Projects ──────────────────────────────────────────────────────────────────
@app.route("/api/projects", methods=["GET"])
def api_get_projects():
    user_id = request.args.get("user_id")
    if not user_id: return jsonify({"error": "user_id required"}), 400
    return jsonify({"projects": project_store.get_projects(user_id)})

@app.route("/api/projects", methods=["POST"])
def api_create_project():
    data = request.json or {}
    user_id = data.get("user_id")
    name = data.get("name")
    description = data.get("description", "")
    if not user_id or not name: return jsonify({"error": "user_id and name required"}), 400
    p = project_store.create_project(user_id, name, description)
    return jsonify(p)

@app.route("/api/projects/<project_id>", methods=["GET"])
def api_get_project(project_id):
    p = project_store.get_project(project_id)
    if not p: return jsonify({"error": "Project not found"}), 404
    return jsonify(p)

@app.route("/api/projects/<project_id>/instructions", methods=["PUT"])
def api_update_project_instructions(project_id):
    data = request.json or {}
    inst = data.get("instructions", "")
    if project_store.update_project_instructions(project_id, inst):
        return jsonify({"success": True})
    return jsonify({"error": "Failed to update instructions"}), 400

@app.route("/api/projects/<project_id>/files", methods=["POST"])
def api_add_project_file(project_id):
    data = request.json or {}
    filename = data.get("filename")
    content = data.get("content")
    if not filename or not content: return jsonify({"error": "filename and content required"}), 400
    doc = project_store.add_knowledge_base_doc(project_id, filename, content)
    return jsonify(doc)

@app.route("/api/projects/<project_id>/files/<file_id>", methods=["DELETE"])
def api_delete_project_file(project_id, file_id):
    if project_store.delete_knowledge_base_doc(project_id, file_id):
        return jsonify({"success": True})
    return jsonify({"error": "Failed to delete"}), 400

@app.route("/api/projects/<project_id>/conversations", methods=["GET"])
def api_project_conversations(project_id):
    convs = conversation_store.list_conversations_for_project(project_id)
    return jsonify({"conversations": convs})

def _maybe_generate_project_memory(project_id):
    if not project_id: return
    convs = conversation_store.list_conversations_for_project(project_id)
    if len(convs) >= 3:
        titles = [c.get("title", "Untitled") for c in convs[:5]]
        titles_str = "\n".join(f"- {t}" for t in titles)
        prompt = f"Based on the following recent conversation titles in this project, write a 2-3 line summary of what System has learned about this project and its ongoing context. Be concise and write in the third person.\n\nTitles:\n{titles_str}"
        try:
            haiku = get_model_for_task("general")["name"]
            resp = client.messages.create(
                model=haiku,
                max_tokens=150,
                system="You are a project memory assistant. Write a 2-3 line summary.",
                messages=[{"role": "user", "content": prompt}]
            )
            memory = resp.content[0].text.strip()
            project_store.update_project_memory(project_id, memory)
        except Exception as e:
            logger.error(f"Failed to generate project memory: {e}")


# ── List conversations ────────────────────────────────────────────────────────
@app.route("/api/conversations", methods=["GET"])
def list_conversations():
    """GET /api/conversations?user_id=xxx"""
    user_id = request.args.get("user_id", "").strip()
    if not user_id:
        return jsonify({"error": "user_id query param is required"}), 400
    convs = conversation_store.list_conversations(user_id)
    return jsonify({"conversations": convs, "total": len(convs)})


# ── Create conversation ───────────────────────────────────────────────────────
@app.route("/api/conversations", methods=["POST"])
def create_conversation():
    """POST /api/conversations  body: {user_id, user_name, task_type?, project_id?}"""
    data      = request.get_json(silent=True) or {}
    user_id   = data.get("user_id",   "").strip()
    user_name = data.get("user_name", "Anonymous").strip()
    task_type = data.get("task_type")
    project_id = data.get("project_id")

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    conv = conversation_store.create_conversation(user_id, user_name, task_type, project_id)
    if project_id:
        _maybe_generate_project_memory(project_id)
    return jsonify(conv), 201


# ── Get conversation ──────────────────────────────────────────────────────────
@app.route("/api/conversations/<conv_id>", methods=["GET"])
def get_conversation(conv_id):
    """GET /api/conversations/<id>  — returns full conversation with messages"""
    conv = conversation_store.get_conversation(conv_id)
    if not conv:
        return jsonify({"error": "Conversation not found"}), 404
    return jsonify(conv)


# ── Delete conversation ───────────────────────────────────────────────────────
@app.route("/api/conversations/<conv_id>", methods=["DELETE"])
def delete_conversation(conv_id):
    deleted = conversation_store.delete_conversation(conv_id)
    if not deleted:
        return jsonify({"error": "Conversation not found"}), 404
    return jsonify({"success": True})


# ── Rename conversation ───────────────────────────────────────────────────────
@app.route("/api/conversations/<conv_id>/title", methods=["PATCH"])
def rename_conversation(conv_id):
    data  = request.get_json(silent=True) or {}
    title = data.get("title", "").strip()
    if not title:
        return jsonify({"error": "title is required"}), 400
    conv = conversation_store.get_conversation(conv_id)
    if not conv:
        return jsonify({"error": "Conversation not found"}), 404
    conversation_store.update_title(conv_id, title)
    return jsonify({"success": True, "title": title})


import threading
import queue

# ── Huddle: live SSE state ────────────────────────────────────────────────────
_huddle_subscribers: dict = {}   # conv_id -> list[queue.Queue]
_huddle_lock = threading.Lock()

def _huddle_broadcast(conv_id: str, event: dict):
    """Push an event dict to all SSE subscribers of a conversation."""
    with _huddle_lock:
        listeners = _huddle_subscribers.get(conv_id, [])
        dead = []
        for q in listeners:
            try:
                q.put_nowait(event)
            except queue.Full:
                dead.append(q)
        for dq in dead:
            listeners.remove(dq)

# ── Huddle: invite a participant ──────────────────────────────────────────────
@app.route("/api/conversations/<conv_id>/invite", methods=["POST"])
def invite_to_huddle(conv_id):
    """POST /api/conversations/<id>/invite  body: {user_id, user_name}"""
    data      = request.get_json(silent=True) or {}
    user_id   = data.get("user_id", "").strip()
    user_name = data.get("user_name", "").strip()
    if not user_id or not user_name:
        return jsonify({"error": "user_id and user_name required"}), 400
    ok = conversation_store.add_participant(conv_id, user_id, user_name)
    if not ok:
        return jsonify({"error": "Conversation not found"}), 404
    _huddle_broadcast(conv_id, {"type": "joined", "user_id": user_id, "user_name": user_name})
    return jsonify({"success": True})


@app.route("/api/conversations/<conv_id>/huddle-events", methods=["GET"])
def huddle_events(conv_id):
    """
    GET /api/conversations/<id>/huddle-events
    Server-Sent Events stream — delivers real-time message events to all huddle participants.
    """
    conv = conversation_store.get_conversation(conv_id)
    if not conv:
        return jsonify({"error": "Conversation not found"}), 404

    q = queue.Queue(maxsize=100)
    with _huddle_lock:
        _huddle_subscribers.setdefault(conv_id, []).append(q)

    def stream():
        try:
            yield "data: {\"type\":\"connected\"}\n\n"
            while True:
                try:
                    event = q.get(timeout=25)
                    yield f"data: {json.dumps(event)}\n\n"
                except queue.Empty:
                    yield ": ping\n\n"   # keep-alive
        finally:
            with _huddle_lock:
                listeners = _huddle_subscribers.get(conv_id, [])
                if q in listeners:
                    listeners.remove(q)

    return Response(
        stream_with_context(stream()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


def _auto_tag_bg(conv_id, message):
    try:
        from db import get_connection
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT id, name FROM projects")
        projects = cur.fetchall()
        cur.execute("SELECT id, client_name FROM client_users")
        clients = cur.fetchall()
        
        cur.execute("SELECT data FROM conversations WHERE id=?", (conv_id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return
        conv = json.loads(row[0])
        if conv.get("project_id") or conv.get("client_id"):
            return

        proj_str = ", ".join([f"'{p[1]}' (ID: {p[0]})" for p in projects])
        client_str = ", ".join([f"'{c[1]}' (ID: {c[0]})" for c in clients])

        prompt = f"""
        The user sent this first message in a new chat: "{message}"
        Match it to ONE of our projects or clients based on the text. 
        Only return a match if you are reasonably confident.
        
       Projects: {proj_str}
        Clients: {client_str}

        Return JSON strictly in this format: {{"project_id": "ID_HERE", "client_id": "ID_HERE"}}
        Use null if there is no match.
        """
        
        res = client.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=256,
            system="Return ONLY strict JSON with keys 'project_id' and 'client_id'. Use null if no match.",
            messages=[{"role": "user", "content": prompt}]
        )
        
        response_text = ""
        for block in res.content:
            if hasattr(block, "text") and getattr(block, "text", None):
                response_text += block.text
                
        if response_text:
            import re
            m = re.search(r'\{.*\}', response_text, re.DOTALL)
            if m:
                d = json.loads(m.group(0))
                p_id = d.get("project_id")
                c_id = d.get("client_id")
                
                if p_id or c_id:
                    conn = get_connection()
                    cur = conn.cursor()
                    cur.execute("SELECT data FROM conversations WHERE id=?", (conv_id,))
                    row = cur.fetchone()
                    if row:
                        conv = json.loads(row[0])
                        if p_id: 
                            conv["project_id"] = p_id
                            p_name = next((p[1] for p in projects if str(p[0]) == str(p_id)), p_id)
                            conv["project_name"] = p_name
                        if c_id: 
                            conv["client_id"] = c_id
                            c_name = next((c[1] for c in clients if str(c[0]) == str(c_id)), c_id)
                            conv["client_name"] = c_name
                        with conn:
                            conn.execute("UPDATE conversations SET data=? WHERE id=?", (json.dumps(conv), conv_id))
                    conn.close()
    except Exception as e:
        logger.error(f"Auto-tagging failed: {e}")

# ── Multi-turn chat in a conversation ─────────────────────────────────────────
@app.route("/api/conversations/<conv_id>/chat", methods=["POST"])
def conversation_chat(conv_id):
    """
    Send a message in a conversation. Maintains full multi-turn context.
    Body: { message, task_type? }
    Returns: { response, model_used, model_tier, cost_usd, tokens, budget,
               conv_id, task_type, title }
    """
    conv = conversation_store.get_conversation(conv_id)
    if not conv:
        return jsonify({"error": "Conversation not found"}), 404

    data        = request.get_json(silent=True) or {}
    message     = " ".join(data.get("message", "").split())
    attachments = data.get("attachments", [])  # [{type,filename,content|data,media_type}]
    if not message:
        return jsonify({"error": "message is required"}), 400

    # Determine task type: request override > conv stored > auto-detect
    task_type = (
        data.get("task_type")
        or conv.get("task_type")
        or _detect_task(message)
    )

    # Determine sender name for multi-participant huddles
    sender_id   = data.get("sender_id") or conv.get("user_id", "")
    sender_name = data.get("sender_name") or (conv.get("participant_names") or {}).get(sender_id, "")
    participants = conv.get("participant_ids", [conv.get("user_id")])
    is_huddle = len(participants) > 1
    # Prefix sender name when multiple people are in the chat
    prefixed_message = f"[{sender_name}]: {message}" if is_huddle and sender_name else message

    # Save user message (or amend last user when regenerating)
    if data.get("amend_last_user"):
        if not conversation_store.amend_last_user_content(conv_id, prefixed_message):
            return jsonify({"error": "Could not update last user message"}), 400
    else:
        conversation_store.add_message(conv_id, "user", prefixed_message, metadata={"sender_id": sender_id, "sender_name": sender_name})

    # Broadcast user message to all huddle listeners
    if is_huddle:
        _huddle_broadcast(conv_id, {"type": "message", "role": "user", "sender": sender_name, "sender_id": sender_id, "content": message})

    # Lock in task_type if not already set
    if not conv.get("task_type"):
        conversation_store.update_task_type(conv_id, task_type)

    # Build context for System (full history including message just saved)
    context = conversation_store.get_context_messages(conv_id)
    if len(context) == 1:
        threading.Thread(target=_auto_tag_bg, args=(conv_id, message)).start()
    if data.get("web_search"):
        snip = _web_search_snippets(message)
        if snip:
            context = _inject_web_context(list(context), snip)
            logger.info("Chat | web_search context injected | len=%d", len(snip))

    if message.strip().lower().startswith("/standup"):
        try:
            from db import get_connection
            conn = get_connection()
            cur = conn.cursor()
            date_str = datetime.utcnow().strftime("%Y-%m-%d")
            cur.execute(
                "SELECT id, title, due_date, status FROM standup_tasks WHERE user_id=? AND date=? AND status NOT IN ('done', 'need_for_approval')",
                (sender_id, date_str),
            )
            rows = cur.fetchall()
            conn.close()
            
            task_list_str = "\n".join([f"- [ID: {r[0]}] {r[1]} (Due: {r[2]}, Status: {r[3]})" for r in rows]) if rows else "No active tasks found."
            standup_note = f"""

SYSTEM INSTRUCTION FOR /standup COMMAND:
The user wants to do their daily standup. Here are their active tasks for today:
{task_list_str}

Please act as an AI Standup Coach.
1. Recommend what they should say in their standup today, prioritizing tasks due soon.
2. IMPORTANT: If the user asks you to mark a task as done, delayed, or add a new task (or if they paste meeting notes to extract tasks), you MUST output a JSON block at the very end of your response wrapped exactly in <standup_actions> tags, like this:
<standup_actions>
[
  {{"action": "update_task", "task_id": 12, "status": "done"}},
  {{"action": "add_task", "title": "Review docs", "due_date": "2026-07-01"}}
]
</standup_actions>
- EXCEPTION FOR SOCIAL MEDIA: If the task being marked as done is a Social Media task, you MUST set its status to "need_for_approval" instead of "done" so it can be approved before posting.
3. If the user mentions they are blocked on something, proactively provide advice or search for solutions to unblock them.

"""
            
            if context and context[-1]["role"] == "user":
                if isinstance(context[-1]["content"], str):
                    context[-1]["content"] += standup_note
                elif isinstance(context[-1]["content"], list):
                    context[-1]["content"].append({"type": "text", "text": standup_note})
        except Exception as e:
            logger.error(f"Error fetching tasks for /standup in chat: {e}")

    # Call System with full conversation context + any file attachments
    result = call_claude_with_context(
        task_type,
        context,
        conv.get("user_id", "api"),
        attachments=attachments,
        project_id=conv.get("project_id"),
        model_override=data.get("model_override"),
        output_contract=data.get("output_contract") or {},
    )

    if not result["success"]:
        err = result.get("error", "")
        if "Invalid API" in err:  status = 401
        elif "Rate limit" in err or "budget" in err.lower(): status = 429
        else: status = 500
        return jsonify({"error": err, **{k: v for k, v in result.items() if k != "error"}}), status

    # Save assistant reply with metadata
    conversation_store.add_message(conv_id, "assistant", result["response"], {
        "model_tier": result["model_tier"],
        "model_used": result["model_used"],
        "cost_usd":   result["cost_usd"],
        "task_type":  task_type,
    })

    # Broadcast assistant reply to huddle listeners
    if is_huddle:
        _huddle_broadcast(conv_id, {"type": "message", "role": "assistant", "sender": "System", "sender_id": "system", "content": result["response"]})

    updated_conv = conversation_store.get_conversation(conv_id)
    return jsonify({
        **result,
        "conv_id":   conv_id,
        "task_type": task_type,
        "title":     updated_conv["title"] if updated_conv else "",
    })


# ── Skills API ────────────────────────────────────────────────────────────────
@app.route("/api/skills", methods=["GET"])
def get_skills():
    user_id = request.args.get("user_id", "anonymous")
    builtin = skills.get_all_skills()
    custom = custom_skills_store.get_skills_for_user(user_id)
    return jsonify({"builtin": builtin, "custom": custom})

@app.route("/api/skills/custom", methods=["POST"])
def create_custom_skill():
    data = request.get_json() or {}
    user_id = data.get("user_id")
    name = data.get("name")
    model = data.get("model", "haiku")
    prompt = data.get("prompt")
    is_shared = data.get("is_shared", False)
    
    if not all([user_id, name, prompt]):
        return jsonify({"error": "Missing required fields"}), 400
        
    skill_id = custom_skills_store.create_skill(user_id, name, model, prompt, is_shared)
    if skill_id:
        return jsonify({"success": True, "skill_id": skill_id})
    return jsonify({"error": "Failed to create skill"}), 500

@app.route("/api/skills/custom/<skill_id>", methods=["DELETE"])
def delete_custom_skill(skill_id):
    data = request.get_json() or {}
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
        
    success = custom_skills_store.delete_skill(skill_id, user_id)
    if success:
        return jsonify({"success": True})
    return jsonify({"error": "Skill not found or unauthorized"}), 403

import web_fetcher

@app.route("/api/fetch-url", methods=["POST"])
def fetch_url_route():
    data    = request.json or {}
    url     = data.get("url", "").strip()
    user_id = data.get("user_id", "anonymous")
    
    if not url.startswith("http"):
        return jsonify({"error": "Invalid URL"}), 400
        
    budget = check_budget_available()
    if budget["remaining"] <= 0:
        return jsonify({"error": "Budget limit reached. Cannot fetch URL."}), 403
    
    result = web_fetcher.fetch_url_content(url)
    
    if result.get("success"):
        # Record usage for fetch
        record_usage(task_type="url_fetch", model_tier="haiku", model_name="web_fetcher",
                     input_tokens=0, output_tokens=0, cost=0.0001, user_id=user_id)
                     
    return jsonify(result)


# ── Streaming chat ──────────────────────────────────────────────────────────
@app.route("/api/conversations/<conv_id>/stream", methods=["POST"])
def conversation_stream(conv_id):
    """
    POST /api/conversations/<id>/stream
    Server-Sent Events: yields text chunks as System generates them.
    Events: {type:"text",text:"..."} | {type:"done",...stats} | {type:"error",error:"..."}
    """
    conv = conversation_store.get_conversation(conv_id)
    if not conv:
        return jsonify({"error": "Conversation not found"}), 404

    data        = request.get_json(silent=True) or {}
    message     = " ".join(data.get("message", "").split())
    attachments = data.get("attachments", [])
    if not message:
        return jsonify({"error": "message is required"}), 400

    task_type = (data.get("task_type") or conv.get("task_type") or _detect_task(message))
    
    sender_id = data.get("sender_id") or conv.get("user_id", "")
    sender_name = data.get("sender_name") or (conv.get("participant_names") or {}).get(sender_id, "")
    participants = conv.get("participant_ids", [conv.get("user_id")])
    is_huddle = len(participants) > 1
    prefixed_message = f"[{sender_name}]: {message}" if is_huddle and sender_name else message

    user_id   = conv.get("user_id", "api")

    truncate_idx = data.get("truncate_from_index")
    if truncate_idx is not None:
        conversation_store.truncate_messages(conv_id, int(truncate_idx))

    amend_last = bool(data.get("amend_last_user"))
    if amend_last:
        if not conversation_store.amend_last_user_content(conv_id, prefixed_message):
            return jsonify({"error": "Could not update last user message"}), 400
    else:
        conversation_store.add_message(conv_id, "user", prefixed_message, metadata={"sender_id": sender_id, "sender_name": sender_name})

    if is_huddle:
        _huddle_broadcast(conv_id, {"type": "message", "role": "user", "sender": sender_name, "sender_id": sender_id, "content": message})

    if not conv.get("task_type"):
        conversation_store.update_task_type(conv_id, task_type)

    # Build context + attachments (Haiku summary of older turns when very long)
    context = conversation_store.get_context_messages(conv_id)
    if len(context) == 1:
        threading.Thread(target=_auto_tag_bg, args=(conv_id, message)).start()
    haiku_m = get_model_for_task("general")["name"]
    compressed_ctx, hist_summary = _maybe_summarize_history(list(context), haiku_m)
    output_contract = data.get("output_contract") or {}
    oc_block = _format_output_contract(output_contract) or None

    model_override = data.get("model_override")

    if model_override and model_override != "auto":
        model_name = model_override
        model_tier = "sonnet" if "sonnet" in model_name.lower() or "pro" in model_name.lower() else "haiku"
    else:
        model_config = get_model_for_task(task_type)
        model_name   = model_config["name"]
        model_tier   = model_config["tier"]

    # --- Skill handling ---
    skill_id = data.get("skill_id")
    web_search_enabled = data.get("webSearchEnabled", False)
    skill_prompt = ""
    
    if skill_id:
        if skill_id.startswith("sk_"):
            # Load custom skill
            custom_skills = custom_skills_store.get_skills_for_user(user_id)
            for sk in custom_skills:
                if sk["id"] == skill_id:
                    skill_prompt = "SKILL INSTRUCTION:\n" + sk["prompt"] + "\n\n"
                    # Auto upgrade model if skill requires sonnet, unless overridden
                    if not model_override and sk.get("model") == "sonnet":
                        model_name = get_model_for_task("coding")["name"] # using coding task as a proxy for sonnet
                        model_tier = "sonnet"
                    break
        else:
            # Load builtin skill
            builtin_skill = skills.get_skill(skill_id)
            if builtin_skill:
                skill_prompt = "SKILL INSTRUCTION:\n" + builtin_skill["prompt"] + "\n\n"
                if not model_override and builtin_skill.get("model") == "sonnet":
                    model_name = get_model_for_task("coding")["name"]
                    model_tier = "sonnet"
                    
        if skill_id == "web_search":
            web_search_enabled = True

    if message.strip().lower().startswith("/standup"):
        try:
            from db import get_connection
            conn = get_connection()
            cur = conn.cursor()
            date_str = datetime.utcnow().strftime("%Y-%m-%d")
            cur.execute(
                "SELECT id, title, due_date, status FROM standup_tasks WHERE user_id=? AND date=? AND status NOT IN ('done', 'need_for_approval')",
                (sender_id, date_str),
            )
            rows = cur.fetchall()
            conn.close()
            
            task_list_str = "\n".join([f"- [ID: {r[0]}] {r[1]} (Due: {r[2]}, Status: {r[3]})" for r in rows]) if rows else "No active tasks found."
            
            skill_prompt += f"""

SYSTEM INSTRUCTION FOR /standup COMMAND:
The user wants to do their daily standup. Here are their active tasks for today:
{task_list_str}

Please act as an AI Standup Coach.
1. Recommend what they should say in their standup today, prioritizing tasks due soon.
2. IMPORTANT: If the user asks you to mark a task as done, delayed, or add a new task (or if they paste meeting notes to extract tasks), you MUST output a JSON block at the very end of your response wrapped exactly in <standup_actions> tags, like this:
<standup_actions>
[
  {{"action": "update_task", "task_id": 12, "status": "done"}},
  {{"action": "add_task", "title": "Review docs", "due_date": "2026-07-01"}}
]
</standup_actions>
- EXCEPTION FOR SOCIAL MEDIA: If the task being marked as done is a Social Media task, you MUST set its status to "need_for_approval" instead of "done" so it can be approved before posting.
3. If the user mentions they are blocked on something, proactively provide advice or search for solutions to unblock them.

"""
        except Exception as e:
            logger.error(f"Error fetching tasks for /standup: {e}")

    # --- Style handling ---
    style = data.get("style")
    style_prompt = ""
    if style:
        style_prompts = {
            "concise": "Keep all responses under 3 sentences unless the task genuinely requires more.",
            "detailed": "Be thorough and comprehensive. Explain your reasoning step by step.",
            "formal": "Use formal professional language throughout. No casual phrasing or contractions."
        }
        if style in style_prompts:
            style_prompt = "STYLE INSTRUCTION:\n" + style_prompts[style] + "\n\n"

    mem_blocks, kb_sources = _build_system_prompt(
        task_type,
        user_id,
        conv.get("project_id"),
        message=message,
        history_summary=hist_summary,
        output_contract_block=oc_block,
        attachment_grounding=bool(attachments),
    )
    final_system = mem_blocks[0]["text"] if mem_blocks else MASTER_SYSTEM_PROMPT
    
    if skill_prompt or style_prompt:
        final_system = skill_prompt + style_prompt + final_system

    # --- URL Fetching ---
    import re
    url_pattern = r'https?://[^\s]+'
    urls_in_msg = re.findall(url_pattern, message)
    if urls_in_msg:
        url_context = ""
        for u in urls_in_msg:
            fetch_res = web_fetcher.fetch_url_content(u)
            if fetch_res.get("success"):
                url_context += f"\nThe user shared this URL: {u}\nPage title: {fetch_res['title']}\nPage content:\n{fetch_res['content']}\n---\n"
            else:
                url_context += f"\nCould not load {u}: {fetch_res.get('error', 'Unknown error')}\n"
        if url_context:
            final_system = final_system + "\n\n" + url_context

    project_id = conv.get("project_id")
    if project_id:
        proj = project_store.get_project(project_id)
        if proj:
            proj_ctx = ""
            if proj.get("instructions"):
                proj_ctx += f"PROJECT INSTRUCTIONS:\n{proj['instructions']}\n\n"
            if proj.get("memory"):
                proj_ctx += f"PROJECT MEMORY:\n{proj['memory']}\n\n"
            for f in proj.get("knowledge_base", []):
                proj_ctx += f"<project_file filename=\"{f['filename']}\">\n{f['content']}\n</project_file>\n\n"
            if proj_ctx:
                final_system = f"{proj_ctx}\n\n---\n{final_system}"

    system_prompt = [{
        "type": "text",
        "text": final_system,
        "cache_control": {"type": "ephemeral"}
    }]

    api_messages = list(compressed_ctx)
    if attachments and api_messages and api_messages[-1]["role"] == "user":
        last_text      = api_messages[-1]["content"]
        content_blocks = [{"type": "text", "text": last_text}]
        for att in attachments:
            if att.get("type") == "image":
                content_blocks.append({"type": "image",
                    "source": {"type": "base64", "media_type": att["media_type"], "data": att["data"]}})
            elif att.get("type") == "document":
                file_content = att.get("content", "") or ""
                smart_content = _smart_file_context(file_content, last_text, haiku_m)
                content_blocks[0]["text"] += (
                    f"\n\n---\nAttached: {att['filename']}\n{smart_content}\n---"
                )
        api_messages = api_messages[:-1] + [{"role": "user", "content": content_blocks}]

    if data.get("web_search"):
        snip = _web_search_snippets(message)
        if snip:
            api_messages = _inject_web_context(api_messages, snip)
            logger.info("Stream | web_search context injected | len=%d", len(snip))

    # Check budget before opening stream
    budget = check_budget_available()
    if not budget["allowed"]:
        def _budget_err():
            yield f"data: {json.dumps({'type':'error','error':'Monthly budget limit reached'})}\n\n"
        return Response(stream_with_context(_budget_err()), mimetype="text/event-stream")

    def should_think(override_val) -> bool:
        # Thinking is currently only available on Sonnet models.
        # If user explicitly selected Haiku, disable thinking.
        if override_val == "claude-haiku-4-5-20251001":
            return False
        # For 'auto' or 'sonnet', always turn thinking on.
        return True

    def generate():
        full_response  = ""
        input_tokens   = 0
        output_tokens  = 0
        stream_error   = None
        model_used_for_call = model_name
        sent_thinking_start = False
        sent_thinking_end   = False

        logger.info(f"Stream | task={task_type} | model={model_tier} | user={user_id}")

        try:
            use_thinking = should_think(model_override)
            stream_max = _conversation_max_tokens(task_type, model_tier)
            stream_kwargs = {
                "system": system_prompt,
                "messages": api_messages,
            }
            if use_thinking:
                # Thinking is only available on Sonnet-class models. We pin to the
                # same Sonnet model shown in the UI for clarity/consistency.
                stream_kwargs["model"] = "claude-sonnet-4-6"
                # budget_tokens counts toward max_tokens; leave headroom for long answers.
                stream_kwargs["max_tokens"] = 36000
                stream_kwargs["thinking"] = {"type": "enabled", "budget_tokens": 14000}
                model_used_for_call = stream_kwargs["model"]
            else:
                stream_kwargs["model"] = model_name
                stream_kwargs["max_tokens"] = stream_max
                model_used_for_call = stream_kwargs["model"]

            stream_kwargs["extra_headers"] = _anthropic_extra_headers(
                stream_kwargs["model"], stream_kwargs["max_tokens"]
            )

            if web_search_enabled:
                stream_kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]

            with client.messages.stream(**stream_kwargs) as stream:
                for event in stream:
                    if hasattr(event, "type"):
                        if event.type == "content_block_start":
                            if hasattr(event, "content_block"):
                                if event.content_block.type == "thinking":
                                    sent_thinking_start = True
                                    yield "data: {\"type\":\"thinking_start\"}\n\n"
                                elif event.content_block.type == "text":
                                    sent_thinking_end = True
                                    yield "data: {\"type\":\"thinking_end\"}\n\n"
                        elif event.type == "content_block_delta":
                            if hasattr(event, "delta") and event.delta.type == "text_delta":
                                full_response += event.delta.text
                                yield f"data: {json.dumps({'type':'text','text':event.delta.text})}\n\n"

                final_msg     = stream.get_final_message()
                input_tokens  = final_msg.usage.input_tokens
                output_tokens = final_msg.usage.output_tokens

        except anthropic.AuthenticationError:
            stream_error = "Invalid API key — check ANTHROPIC_API_KEY"
        except anthropic.RateLimitError:
            stream_error = "Rate limit reached — please retry shortly"
        except anthropic.APIError as e:
            stream_error = f"System API error: {e}"
        except Exception as e:
            logger.exception(f"Streaming error: {e}")
            stream_error = "Internal server error during stream"

        if stream_error:
            # Ensure the UI doesn't get stuck in a "thinking" state.
            if sent_thinking_start and not sent_thinking_end:
                yield "data: {\"type\":\"thinking_end\"}\n\n"
            yield f"data: {json.dumps({'type':'error','error':stream_error})}\n\n"
            return

        # Parse auto-memory extraction
        import re
        memory_matches = re.finditer(r'<SAVE_MEMORY_PROFILE\s+user="([^"]+)">([\s\S]*?)</SAVE_MEMORY_PROFILE>', full_response)
        for match in memory_matches:
            target_user = match.group(1)
            mem_content = match.group(2).strip()
            new_entries = memory_store.update_profile(target_user, mem_content)
            if new_entries:
                try:
                    import smart_memory
                    smart_memory.index_memories(target_user, new_entries)
                except Exception:
                    pass
            logger.info(f"Auto-saved memory profile for {target_user}")

        # Strip the memory tags from the final saved message so they don't pollute the chat history
        clean_response = re.sub(r'<SAVE_MEMORY_PROFILE\s+user="[^"]+">[\s\S]*?</SAVE_MEMORY_PROFILE>', '', full_response).strip()

        # Persist assistant reply + record usage
        cost = calculate_cost(model_tier, input_tokens, output_tokens)
        record_usage(task_type=task_type, model_tier=model_tier, model_name=model_used_for_call,
                     input_tokens=input_tokens, output_tokens=output_tokens,
                     cost=cost, user_id=user_id)
        conversation_store.add_message(conv_id, "assistant", clean_response, {
            "model_tier": model_tier, "model_used": model_used_for_call,
            "cost_usd": cost, "task_type": task_type,
        })

        if is_huddle:
            _huddle_broadcast(conv_id, {"type": "message", "role": "assistant", "sender": "System", "sender_id": "system", "trigger_user_id": sender_id, "content": clean_response})

        updated_conv   = conversation_store.get_conversation(conv_id)
        updated_budget = check_budget_available()
        yield f"data: {json.dumps({'type':'done','model_tier':model_tier,'model_used':model_used_for_call,'cost_usd':cost,'task_type':task_type,'title':updated_conv['title'] if updated_conv else '','budget':{'spent':updated_budget['spent'],'remaining':updated_budget['remaining'],'limit':updated_budget['limit']},'kb_sources': kb_sources or []})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

# ── Prompt Optimizer ───────────────────────────────────────────────────────────
OPTIMIZER_SYSTEM = """You are an expert prompt engineer. Rewrite the user's prompt to make it clearer, more specific, and more effective for an AI assistant.

Rules:
- Keep the exact same intent and topic
- Add specificity: format, length, tone, structure, context where helpful
- Fix grammar and spelling
- For code requests: mention language, what it should do, edge cases
- For writing requests: mention audience, length, style
- Return ONLY the improved prompt — no explanation, no preamble
- Keep it concise (under 200 words)"""

@app.route("/api/optimize-prompt", methods=["POST"])
def optimize_prompt():
    """
    POST /api/optimize-prompt  body: {prompt, task_type?}
    Uses System Haiku to rewrite a rough prompt into a precise one.
    Cost:  per call (Haiku, minimal tokens).
    """
    data      = request.get_json(silent=True) or {}
    prompt    = data.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "prompt is required"}), 400
    if len(prompt) > 2000:
        return jsonify({"error": "Prompt too long (max 2000 chars)"}), 400

    try:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=400,
            system=OPTIMIZER_SYSTEM,
            messages=[{"role": "user", "content": f"Optimize this prompt:\n\n{prompt}"}],
        )
        optimized = (_anthropic_response_text(response) or "").strip()
        input_tokens  = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        cost          = calculate_cost("haiku", input_tokens, output_tokens)
        record_usage(
            task_type="general", model_tier="haiku", model_name="claude-haiku-4-5",
            input_tokens=input_tokens, output_tokens=output_tokens,
            cost=cost, user_id=data.get("user_id", "anonymous"),
        )
        logger.info(f"Prompt optimized | {input_tokens}→{output_tokens} tokens | ${cost:.6f}")
        return jsonify({
            "success":   True,
            "original":  prompt,
            "optimized": optimized,
            "cost_usd":  cost,
            "tokens":    {"input": input_tokens, "output": output_tokens},
        })
    except anthropic.AuthenticationError:
        return jsonify({"error": "Invalid API key — check ANTHROPIC_API_KEY"}), 401
    except anthropic.RateLimitError:
        return jsonify({"error": "Rate limit — please retry in a moment"}), 429
    except anthropic.APIError as e:
        return jsonify({"error": f"System API error: {e}"}), 502
    except Exception as e:
        logger.exception(f"Optimize prompt error: {e}")
        return jsonify({"error": "Server error"}), 500


@app.route("/api/upload", methods=["POST"])
@limiter.limit("20 per minute")
def upload_file():
    """
    POST /api/upload   multipart: file=<file>
    Returns processed content ready to attach to a message.
    Images:    {type, filename, media_type, data (base64), size_bytes}
    Documents: {type, filename, content (str), pages?}
    """
    if 'file' not in request.files:
        return jsonify({"error": "No file field in request"}), 400
    f = request.files['file']
    if not f or f.filename == '':
        return jsonify({"error": "No file selected"}), 400

    file_bytes = f.read()

    result = file_processor.process_file(file_bytes, f.filename, f.content_type or '')
    if result['type'] == 'error':
        return jsonify({"error": result['error'], "filename": result['filename']}), 422

    logger.info(f"File uploaded: {f.filename} ({result['type']}, {len(file_bytes)} bytes)")
    return jsonify({"success": True, **result})


# ── Knowledge storage sync ───────────────────────────────────────────────────
# A folder on a laptop is kept in sync with the agency knowledge base by
# scripts/kb_sync.py, which calls these endpoints. Everything lands under a
# single sentinel project so the WhatsApp agent's unscoped kb_chunks_fts
# search (backend/whatsapp_agent.py::_kb_search) picks it up automatically.
_STORAGE_PID = "_storage"
_STORAGE_UID = "_storage"


def _storage_auth_ok() -> bool:
    import hmac
    expected = os.getenv("STORAGE_SYNC_TOKEN") or os.getenv("FLASK_SECRET_KEY") or ""
    if not expected:
        return False
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else request.args.get("token", "")
    return bool(token) and hmac.compare_digest(token, expected)


@app.route("/api/storage/doc", methods=["POST"])
@limiter.limit("120 per minute")
def storage_add_doc():
    """Add/replace one document in the synced knowledge store.
    multipart: file=<file>   or   json: {filename, content}
    Auth: Authorization: Bearer <STORAGE_SYNC_TOKEN or FLASK_SECRET_KEY>"""
    if not _storage_auth_ok():
        return jsonify({"error": "unauthorized"}), 401

    filename = ""
    content = ""
    if "file" in request.files:
        f = request.files["file"]
        filename = request.form.get("filename") or f.filename or ""
        raw = f.read()
        result = file_processor.process_file(raw, f.filename or filename, f.content_type or "")
        if result.get("type") == "error":
            return jsonify({"error": result.get("error", "could not parse"), "filename": filename}), 422
        if result.get("type") != "document":
            return jsonify({"error": "only text documents are indexable", "filename": filename}), 422
        content = result.get("content", "")
    else:
        data = request.get_json(silent=True) or {}
        filename = (data.get("filename") or "").strip()
        content = data.get("content") or ""

    filename = filename.strip().lstrip("/\\")
    if not filename or not content.strip():
        return jsonify({"error": "filename and non-empty content required"}), 400

    # Idempotent replace: drop any existing copy of this filename first.
    old_ids = project_store.delete_knowledge_base_docs_by_filename(_STORAGE_PID, filename)
    for oid in old_ids:
        try:
            kb_retriever.delete_doc_index(_STORAGE_PID, _STORAGE_UID, oid)
        except Exception:
            pass

    doc = project_store.add_knowledge_base_doc(_STORAGE_PID, filename, content)
    try:
        kb_retriever.index_doc(_STORAGE_PID, _STORAGE_UID, doc["id"], filename, content)
    except Exception:
        logger.exception("storage: FTS index failed for %s", filename)

    return jsonify({"success": True, "filename": filename, "chars": len(content), "doc_id": doc["id"]})


@app.route("/api/storage/docs", methods=["GET"])
@limiter.limit("60 per minute")
def storage_list_docs():
    if not _storage_auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    docs = project_store.list_knowledge_base_docs(_STORAGE_PID)
    return jsonify({"docs": docs, "count": len(docs)})


@app.route("/api/storage/doc", methods=["DELETE"])
@limiter.limit("120 per minute")
def storage_delete_doc():
    if not _storage_auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    filename = (request.args.get("name") or (request.get_json(silent=True) or {}).get("filename") or "").strip()
    if not filename:
        return jsonify({"error": "name required"}), 400
    ids = project_store.delete_knowledge_base_docs_by_filename(_STORAGE_PID, filename)
    for oid in ids:
        try:
            kb_retriever.delete_doc_index(_STORAGE_PID, _STORAGE_UID, oid)
        except Exception:
            pass
    return jsonify({"success": True, "deleted": len(ids), "filename": filename})


# ──Saved Notes Routes ───────────────────────────────────────────────────────────
@app.route("/api/memory/<user_id>", methods=["GET"])
def get_memory(user_id):
    """GET /api/memory/<user_id>  — list all memories for a user"""
    mems = memory_store.get_memories(user_id)
    return jsonify({"memories": mems, "total": len(mems)})


@app.route("/api/memory/<user_id>", methods=["POST"])
def add_memory(user_id):
    """POST /api/memory/<user_id>  body: {content, source?}"""
    data    = request.get_json(silent=True) or {}
    content = data.get("content", "").strip()
    source  = data.get("source", "manual")
    if not content:
        return jsonify({"error": "content is required"}), 400
    try:
        import smart_memory
        mem = smart_memory.dedupe_or_save(user_id, content, source)
    except Exception:
        mem = memory_store.add_memory(user_id, content, source)
    return jsonify({"success": True, "memory": mem}), 201


@app.route("/api/memory/<user_id>/<memory_id>", methods=["DELETE"])
def delete_memory(user_id, memory_id):
    """DELETE /api/memory/<user_id>/<memory_id>"""
    deleted = memory_store.delete_memory(user_id, memory_id)
    if not deleted:
        return jsonify({"error": "Memory not found"}), 404
    try:
        import smart_memory
        smart_memory.remove_memory(user_id, memory_id)
    except Exception:
        pass
    return jsonify({"success": True})


# ──Projects ──────────────────────────────────────────────────────────────────
# NOTE: GET/POST /api/projects and GET /api/projects/<id> are already defined
# above (~line 1450, api_get_projects/api_create_project/api_get_project) —
# those are the live routes since Flask keeps whichever rule was registered
# first. A second, unreachable copy used to live here (list_projects/
# create_project/get_project) and was deleted since it could never execute
# and only risked someone editing the dead copy by mistake (see CLAUDE.md
# gotcha on duplicate route definitions in this file). PATCH/DELETE below
# are NOT duplicated anywhere else and are the only place they're defined.
@app.route("/api/projects/<project_id>", methods=["PATCH"])
def update_project(project_id):
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    name = data.get("name")
    custom_instructions = data.get("custom_instructions")
    p = project_store.update_project(project_id, user_id, name, custom_instructions)
    if not p: return jsonify({"error": "Not found"}), 404
    return jsonify(p)

@app.route("/api/projects/<project_id>", methods=["DELETE"])
def delete_project(project_id):
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id") or request.args.get("user_id")
    if project_store.delete_project(project_id, user_id):
        return jsonify({"success": True})
    return jsonify({"error": "Not found"}), 404

@app.route("/api/projects/<project_id>/knowledge", methods=["POST"])
def add_project_knowledge(project_id):
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    filename = data.get("filename", "untitled.txt")
    content = data.get("content", "")
    doc = project_store.add_knowledge_base_doc(project_id, filename, content)
    if not doc: return jsonify({"error": "Project not found"}), 404
    # Index for retrieval (non-fatal if FTS is unavailable)
    try:
        kb_retriever.index_doc(project_id, user_id, doc["id"], filename, content)
    except Exception:
        pass
    return jsonify(doc), 201

@app.route("/api/projects/<project_id>/knowledge/<doc_id>", methods=["DELETE"])
def delete_project_knowledge(project_id, doc_id):
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id") or request.args.get("user_id")
    if project_store.delete_knowledge_base_doc(project_id, doc_id):
        try:
            kb_retriever.delete_doc_index(project_id, user_id, doc_id)
        except Exception:
            pass
        return jsonify({"success": True})
    return jsonify({"error": "Not found"}), 404


# ══════════════════════════════════════════════════════════════════════════════
# PROJECT TRACKER ROUTES
# ══════════════════════════════════════════════════════════════════════════════

def _pt_conn():
    from db import get_connection
    return get_connection()

def _calc_progress(task: dict) -> int:
    """Return progress 0-100 based on task state events."""
    if task["status"] == "approved":
        return 100
    if task.get("submission_file"):
        return 80
    count = task.get("submission_count", 0)
    if count >= 2:
        return 60
    if count == 1:
        return 30
    if task.get("opened_at"):
        return 10
    return 0

def _is_admin(user_id: str) -> bool:
    return bool(user_id)

def _task_row_to_dict(row) -> dict:
    keys = ["id","client_id","title","description","assigned_to","status",
            "progress","due_date","submission_note","submission_file",
            "rejection_note","submission_count","opened_at","created_at",
            "last_edited_by","last_edited_at","last_edited_summary"]
    return dict(zip(keys, row))

def _client_row_to_dict(row) -> dict:
    keys = ["id","name","contact","requirements","deadline","status","created_at"]
    return dict(zip(keys, row))

# ── GET /api/projects ─────────────────────────────────────────────────────────
@app.route("/api/projects", methods=["GET"])
def get_projects():
    """All clients with their tasks. Admins see everything; others see only their tasks."""
    user_id = request.args.get("user_id", "")
    conn = _pt_conn()
    cur = conn.cursor()
    cur.execute("SELECT id,name,contact,requirements,deadline,status,created_at FROM clients ORDER BY created_at DESC")
    clients = [_client_row_to_dict(r) for r in cur.fetchall()]
    
    for c in clients:
        cur.execute("""SELECT id,client_id,title,description,assigned_to,status,progress,
                       due_date,submission_note,submission_file,rejection_note,
                       submission_count,opened_at,created_at
                       FROM tasks WHERE client_id=? ORDER BY created_at""", (c["id"],))
        tasks = [_task_row_to_dict(r) for r in cur.fetchall()]
        # Recalculate progress
        for t in tasks:
            t["progress"] = _calc_progress(t)
        # Non-admins only see their own tasks
        if not _is_admin(user_id):
            tasks = [t for t in tasks if t["assigned_to"] == user_id]
        c["tasks"] = tasks
    conn.close()
    return jsonify({"clients": clients})

# ── GET /api/clients ──────────────────────────────────────────────────────────
@app.route("/api/clients", methods=["GET"])
def get_clients():
    """List all clients (alias for /api/projects GET). Used by client-onboard.html."""
    user_id = request.args.get("user_id", "")
    conn = _pt_conn()
    cur = conn.cursor()
    cur.execute("SELECT id,name,contact,requirements,deadline,status,created_at FROM clients ORDER BY created_at DESC")
    clients = [_client_row_to_dict(r) for r in cur.fetchall()]
    
    if _is_admin(user_id):
        cur.execute("SELECT client_name, username FROM client_users")
        users = {r[0]: {"username": r[1]} for r in cur.fetchall()}
        for c in clients:
            if c.get("name") in users:
                c["client_username"] = users[c["name"]]["username"]
            
    conn.close()
    return jsonify({"clients": clients})


# ── POST /api/clients ─────────────────────────────────────────────────────────
@app.route("/api/clients", methods=["POST"])
def create_client():
    """Create a new client. Admin only."""
    from routes.auth import _verify_session
    token = request.cookies.get("session_token", "")
    user_id = _verify_session(token)
    if not user_id or not _is_admin(user_id):
        return jsonify({"error": "Unauthorized"}), 403
    body = request.get_json(silent=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
        
    c_user = body.get("client_username", "").strip()
    c_pass = body.get("client_password", "").strip()
    
    conn = _pt_conn()
    
    if c_user:
        try:
            # Admin is forcing creation: wipe any stuck or old credential with this username
            # so the new client can claim it seamlessly.
            cur = conn.cursor()
            cur.execute("DELETE FROM client_users WHERE username=?", (c_user,))
            conn.commit()
        except Exception as e:
            logger.error(f"Error wiping old username: {e}")
    with conn:
        cur = conn.execute(
            "INSERT INTO clients (name,contact,requirements,deadline,status) VALUES (?,?,?,?,?)",
            (name, body.get("contact",""), body.get("requirements",""),
             body.get("deadline",""), body.get("status","active"))
        )
        client_id = cur.lastrowid
        
        c_user = body.get("client_username", "").strip()
        c_pass = body.get("client_password", "").strip()
        if c_user and c_pass:
            try:
                from werkzeug.security import generate_password_hash
                conn.execute(
                    "INSERT INTO client_users (username, password, client_name, client_notion_id) VALUES (?,?,?,?)",
                    (c_user, generate_password_hash(c_pass), name, str(client_id))
                )
            except Exception as e:
                logger.error(f"Failed to create client user: {e}")
    conn.close()
    return jsonify({"success": True, "client_id": client_id}), 201

# ── DELETE /api/clients/<id> ──────────────────────────────────────────────────
@app.route("/api/clients/<int:client_id>", methods=["DELETE"])
def delete_client(client_id):
    """Delete a client, all their tasks, and their login credentials."""
    from routes.auth import _verify_session
    token = request.cookies.get("session_token", "")
    user_id = _verify_session(token)
    if not user_id or not _is_admin(user_id):
        return jsonify({"error": "Unauthorized"}), 403
    conn = _pt_conn()
    try:
        with conn:
            # Delete their login credentials
            conn.execute("DELETE FROM client_users WHERE client_notion_id=?", (str(client_id),))
            # Delete their tasks
            conn.execute("DELETE FROM tasks WHERE client_id=?", (client_id,))
            # Delete the client
            conn.execute("DELETE FROM clients WHERE id=?", (client_id,))
            # Unlink any Google Sheet -- without this, the link's still-valid
            # link_token survives the client's deletion, and the next edit to
            # that now-orphaned physical Sheet resurrects every row as a
            # brand-new task under a client_id that no longer exists anywhere
            # in Lumina (see the matching cleanup in notion_delete_client,
            # routes/ops.py, and CLAUDE.md gotcha #87).
            conn.execute("DELETE FROM google_sheet_links WHERE client_id=?", (str(client_id),))
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"delete_client error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

# ── DELETE /api/client-users/<username> (remove orphaned credential) ───────────
@app.route("/api/client-users/<username>", methods=["DELETE"])
def delete_client_user(username):
    """Remove a client portal credential by username. Admin only."""
    user_id = request.args.get("user_id", "") or (request.get_json(silent=True) or {}).get("user_id", "")
    if not _is_admin(user_id):
        return jsonify({"error": "Unauthorized"}), 403
    conn = _pt_conn()
    try:
        with conn:
            conn.execute("DELETE FROM client_users WHERE username=?", (username,))
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

# ── GET /api/clients/<id>/tasks ───────────────────────────────────────────────
@app.route("/api/clients/<int:client_id>/tasks", methods=["GET"])
def get_client_tasks(client_id):
    user_id = request.args.get("user_id", "")
    conn = _pt_conn()
    cur = conn.cursor()
    cur.execute("""SELECT id,client_id,title,description,assigned_to,status,progress,
                   due_date,submission_note,submission_file,rejection_note,
                   submission_count,opened_at,created_at
                   FROM tasks WHERE client_id=? ORDER BY created_at""", (client_id,))
    tasks = [_task_row_to_dict(r) for r in cur.fetchall()]
    for t in tasks:
        t["progress"] = _calc_progress(t)
        # Fetch dependencies
        cur.execute("SELECT depends_on_task_id FROM dependencies WHERE task_id=?", (t["id"],))
        t["depends_on"] = [r[0] for r in cur.fetchall()]
    conn.close()
    if not _is_admin(user_id):
        tasks = [t for t in tasks if t["assigned_to"] == user_id]
    return jsonify({"tasks": tasks})

# ── POST /api/tasks (create task) ─────────────────────────────────────────────
@app.route("/api/tasks", methods=["POST"])
def create_task():
    """Create a task for a client. Admin only."""
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "")
    if not _is_admin(user_id):
        return jsonify({"error": "Unauthorized"}), 403
    title = body.get("title", "").strip()
    client_id = body.get("client_id")
    if not title or not client_id:
        return jsonify({"error": "title and client_id are required"}), 400
    conn = _pt_conn()
    with conn:
        cur = conn.execute(
            """INSERT INTO tasks (client_id,title,description,assigned_to,due_date,status,progress)
               VALUES (?,?,?,?,?,'not_started',0)""",
            (client_id, title, body.get("description",""),
             body.get("assigned_to",""), body.get("due_date",""))
        )
        task_id = cur.lastrowid
        # Dependencies
        for dep_id in body.get("depends_on", []):
            conn.execute("INSERT OR IGNORE INTO dependencies VALUES (?,?)", (task_id, dep_id))
    conn.close()
    return jsonify({"success": True, "task_id": task_id}), 201

# ── POST /api/tasks/<id>/open ─────────────────────────────────────────────────
@app.route("/api/tasks/<int:task_id>/open", methods=["POST"])
def open_task(task_id):
    """Log when a team member first opens a task → 10% progress."""
    body = request.get_json(silent=True) or {}
    conn = _pt_conn()
    cur = conn.cursor()
    cur.execute("SELECT opened_at, assigned_to FROM tasks WHERE id=?", (task_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Task not found"}), 404
    if not row[0]:  # Only set once
        with conn:
            conn.execute("UPDATE tasks SET opened_at=?, status='in_progress', progress=10 WHERE id=?",
                         (datetime.utcnow().isoformat()+"Z", task_id))
    conn.close()
    return jsonify({"success": True, "progress": 10})

# ── POST /api/tasks/<id>/submit ───────────────────────────────────────────────
@app.route("/api/tasks/<int:task_id>/submit", methods=["POST"])
def submit_task(task_id):
    """Submit work for a task. Progress jumps to 30/60/80 based on real actions."""
    body = request.get_json(silent=True) or {}
    note     = body.get("note", "").strip()
    file_url = body.get("file_url", "").strip()

    conn = _pt_conn()
    cur = conn.cursor()
    cur.execute("SELECT submission_count, assigned_to FROM tasks WHERE id=?", (task_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Task not found"}), 404

    # Validate file URL is actually reachable (#3)
    url_valid = False
    if file_url:
        try:
            import urllib.request
            req = urllib.request.Request(file_url, method="HEAD")
            req.add_header("User-Agent", "Mozilla/5.0")
            with urllib.request.urlopen(req, timeout=5) as resp:
                url_valid = resp.status < 400
        except Exception:
            url_valid = False  # Unreachable URL — don't credit the +20%

    new_count    = (row[0] or 0) + 1
    new_progress = 80 if url_valid else (60 if new_count >= 2 else 30)
    new_status   = "pending_review"

    with conn:
        conn.execute("""UPDATE tasks SET submission_note=?, submission_file=?,
                        submission_count=?, status=?, progress=?, rejection_note=NULL
                        WHERE id=?""",
                     (note, file_url or None, new_count, new_status, new_progress, task_id))
    conn.close()

    # Notify founder via email that a submission needs review (#4)
    _notify_email(
        subject=f"[Review Needed] Task submission #{new_count}",
        body=f"Task ID {task_id} was submitted.\n\nNote: {note}\n\nFile: {file_url or 'None'}"
    )
    return jsonify({"success": True, "progress": new_progress, "status": new_status, "url_verified": url_valid})

# ── PATCH /api/tasks/<id> ─────────────────────────────────────────────────────
@app.route("/api/tasks/<int:task_id>", methods=["PATCH"])
def update_task_endpoint(task_id):
    body = request.get_json(silent=True) or {}
    conn = _pt_conn()
    cur = conn.cursor()
    
    # We may need to get Notion ID if we track it in db. Currently, Notion ID is not consistently stored 
    # for all tasks, but `notion_store` might be able to update if we have it. 
    # Actually, projects.html passes the local task_id.
    updates = []
    params = []
    
    if "description" in body:
        updates.append("description=?")
        params.append(body["description"])
    if "title" in body:
        updates.append("title=?")
        params.append(body["title"])
    if "due_date" in body:
        updates.append("due_date=?")
        params.append(body["due_date"])
        
    if updates:
        params.append(task_id)
        with conn:
            conn.execute(f"UPDATE tasks SET {','.join(updates)} WHERE id=?", params)
            
    conn.close()
    return jsonify({"success": True})

# ── POST /api/tasks/<id>/approve ──────────────────────────────────────────────
@app.route("/api/tasks/<int:task_id>/approve", methods=["POST"])
def approve_task(task_id):
    """Approve a task submission. Founder only. Unlocks dependent tasks."""
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "")
    if not _is_admin(user_id):
        return jsonify({"error": "Unauthorized"}), 403

    conn = _pt_conn()
    with conn:
        conn.execute("UPDATE tasks SET status='approved', progress=100 WHERE id=?", (task_id,))
        # Unlock dependent tasks: remove the blocker if all dependencies approved
        cur = conn.cursor()
        cur.execute("SELECT task_id FROM dependencies WHERE depends_on_task_id=?", (task_id,))
        unblocked = []
        for (dependent_id,) in cur.fetchall():
            cur2 = conn.cursor()
            cur2.execute("""SELECT COUNT(*) FROM dependencies d
                            JOIN tasks t ON d.depends_on_task_id = t.id
                            WHERE d.task_id=? AND t.status != 'approved'""", (dependent_id,))
            remaining_blockers = cur2.fetchone()[0]
            if remaining_blockers == 0:
                conn.execute("UPDATE tasks SET status='unlocked' WHERE id=? AND status='not_started'",
                             (dependent_id,))
                unblocked.append(dependent_id)
    conn.close()
    return jsonify({"success": True, "unblocked_tasks": unblocked})

# ── POST /api/tasks/<id>/reject ───────────────────────────────────────────────
@app.route("/api/tasks/<int:task_id>/reject", methods=["POST"])
def reject_task(task_id):
    """Reject a submission and send it back with notes. Founder only."""
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "")
    if not _is_admin(user_id):
        return jsonify({"error": "Unauthorized"}), 403
    rejection_note = body.get("note", "Needs revision.").strip()

    conn = _pt_conn()
    with conn:
        conn.execute("""UPDATE tasks SET status='rejected', rejection_note=?,
                        submission_file=NULL WHERE id=?""",
                     (rejection_note, task_id))
    conn.close()
    return jsonify({"success": True})

# ── GET /api/dashboard/founder ────────────────────────────────────────────────
@app.route("/api/dashboard/founder", methods=["GET"])
def founder_dashboard():
    """Full founder view — all clients, tasks, and blockers."""
    user_id = request.args.get("user_id", "")
    if not _is_admin(user_id):
        return jsonify({"error": "Unauthorized"}), 403

    conn = _pt_conn()
    cur = conn.cursor()
    cur.execute("SELECT id,name,contact,requirements,deadline,status,created_at FROM clients ORDER BY created_at DESC")
    clients = [_client_row_to_dict(r) for r in cur.fetchall()]

    pending_review = []
    blocked_tasks  = []
    overdue_tasks  = []
    today_str = datetime.utcnow().strftime("%Y-%m-%d")

    for c in clients:
        cur.execute("""SELECT id,client_id,title,description,assigned_to,status,progress,
                       due_date,submission_note,submission_file,rejection_note,
                       submission_count,opened_at,created_at,
                       last_edited_by,last_edited_at,last_edited_summary
                       FROM tasks WHERE client_id=? ORDER BY id ASC""", (c["id"],))
        tasks = [_task_row_to_dict(r) for r in cur.fetchall()]
        for t in tasks:
            t["progress"] = _calc_progress(t)
            t["client_name"] = c["name"]
            cur2 = conn.cursor()
            cur2.execute("SELECT depends_on_task_id FROM dependencies WHERE task_id=?", (t["id"],))
            t["depends_on"] = [r[0] for r in cur2.fetchall()]
            if t["status"] == "pending_review":
                pending_review.append(t)
            if t["depends_on"] and t["status"] == "not_started":
                blocked_tasks.append(t)
            if t.get("due_date") and t["due_date"] < today_str and t["status"] not in ("approved",):
                overdue_tasks.append(t)
        c["tasks"] = tasks
    conn.close()

    return jsonify({
        "clients": clients,
        "summary": {
            "pending_review": pending_review,
            "blocked_tasks": blocked_tasks,
            "overdue_tasks": overdue_tasks,
        }
    })

# ── Service → Default Task Templates ─────────────────────────────────────────
# Maps service type checkboxes → auto-generated tasks with assignees + dependencies
SERVICE_TASK_TEMPLATES = {
    "social": [
        {"title": "Content Ideation",              "assigned_to": "emp006", "order": 1},
        {"title": "Content Approval (Cal Sheet)",  "assigned_to": "emp006", "order": 2},
        {"title": "Discussion with Creative",      "assigned_to": "emp002", "order": 3},
        {"title": "Creative Assigning",            "assigned_to": "emp006", "order": 4},
        {"title": "Content Creation (Kanban)",     "assigned_to": "emp002", "order": 5},
        {"title": "Final Drive Link",              "assigned_to": "emp006", "order": 6},
    ],
    "branding": [
        {"title": "Brand Essence",                 "assigned_to": "emp002", "order": 7},
        {"title": "Stylescape",                    "assigned_to": "emp002", "order": 8},
        {"title": "Logo Design Presentation",      "assigned_to": "emp002", "order": 9},
        {"title": "Logo Iterations",               "assigned_to": "emp002", "order": 10},
        {"title": "Visual Style",                  "assigned_to": "emp002", "order": 11},
        {"title": "Collateral",                    "assigned_to": "emp002", "order": 12},
        {"title": "Brand guidelines Content",      "assigned_to": "emp006", "order": 13},
        {"title": "Brand guidelines",              "assigned_to": "emp002", "order": 14},
    ],
    "website": [
        {"title": "Concept & Flow",                "assigned_to": "emp001", "order": 15},
        {"title": "UI 1st Draft Review",           "assigned_to": "emp002", "order": 16},
        {"title": "Content",                       "assigned_to": "emp006", "order": 17},
        {"title": "Final Build",                   "assigned_to": "emp003", "order": 18},
    ],
    "shoot": [
        {"title": "Video Script & Concept",        "assigned_to": "emp006", "order": 19},
        {"title": "Video Shoot / Production",      "assigned_to": "emp005", "order": 20},
        {"title": "Video Editing & Post",          "assigned_to": "emp005", "order": 21},
    ],
    "miscellaneous": [
        {"title": "Custom Deliverable Setup",      "assigned_to": "emp001", "order": 22},
    ],
}

# ── POST /api/social-media/auto-fill ──────────────────────────────────────────
@app.route("/api/social-media/auto-fill", methods=["POST"])
def auto_fill_social_media():
    """Takes a list of social media posts. If idea, content, scripts, or caption are empty, use Claude to fill them based on title and type."""
    body = request.get_json(silent=True) or {}
    posts = body.get("posts", [])
    if not posts:
        return jsonify({"error": "No posts provided"}), 400

    if not client:
        return jsonify({"error": "Claude client not configured"}), 500

    try:
        # Prompt to process multiple posts in bulk to save time
        prompt = (
            "You are a social media manager. "
            "For each of the following posts, fill in ANY empty fields "
            "(content, scripts, caption) based on the post title and type. "
            "IMPORTANT: Do NOT fill in the 'idea' field. Leave 'idea' exactly as it is (even if empty). "
            "Keep scripts/copy VERY brief (max 3 lines). "
            "Leave other fields that are already filled untouched. "
            "Return ONLY a raw JSON array — no markdown, no explanation, "
            "no code fences, no extra text at all.\n\n"
        )
        prompt += json.dumps(posts, ensure_ascii=False)

        from model_router import get_model_for_task
        sonnet_model = get_model_for_task("coding")["name"]

        response = client.messages.create(
            model=sonnet_model,
            max_tokens=8192,   # large enough for 17 rows
            system="Output ONLY a raw JSON array. No markdown. No explanation. No code fences.",
            messages=[{"role": "user", "content": prompt}]
        )

        in_toks = getattr(getattr(response, "usage", None), "input_tokens", 0)
        out_toks = getattr(getattr(response, "usage", None), "output_tokens", 0)
        
        # Sonnet 3.5 Pricing approx: $3 / 1M input, $15 / 1M output
        cost = (in_toks * 3.0 / 1_000_000) + (out_toks * 15.0 / 1_000_000)

        # ── Extract JSON robustly ──────────────────────────────────────────────
        import re
        raw = response.content[0].text

        # Strip any code fences
        raw = re.sub(r'^```(?:json)?', '', raw.strip())
        raw = re.sub(r'```$', '', raw.strip()).strip()

        # Strip XML-style wrapper tags if present  (<json>...</json>)
        xml_m = re.search(r'<json>(.*?)</json>', raw, re.DOTALL)
        if xml_m:
            raw = xml_m.group(1).strip()

        # Find the outermost [ ... ] array
        arr_m = re.search(r'\[.*\]', raw, re.DOTALL)
        if arr_m:
            raw = arr_m.group(0)

        # Sanitize: escape literal newlines/tabs inside JSON string values
        def _sanitize(s):
            result, in_str = [], False
            i = 0
            while i < len(s):
                c = s[i]
                if c == '\\' and in_str:
                    result.append(c)
                    i += 1
                    if i < len(s):
                        result.append(s[i])
                    i += 1
                    continue
                if c == '"':
                    in_str = not in_str
                    result.append(c)
                elif in_str and c in '\n\r':
                    result.append('\\n')
                elif in_str and c == '\t':
                    result.append('\\t')
                else:
                    result.append(c)
                i += 1
            return ''.join(result)

        raw = _sanitize(raw)
        # Strip trailing commas before ] or }
        raw = re.sub(r',\s*([\]}])', r'\1', raw)

        try:
            filled_posts = json.loads(raw)
        except Exception as e:
            logger.error(f"auto-fill JSON parse failed.\nError: {e}\nRaw:\n{raw}")
            return jsonify({"error": f"JSON parse error: {e}. Raw output: {raw[:600]}"}), 500

        return jsonify({"posts": filled_posts, "cost": cost})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

_EMP_ID_RE = re.compile(r"^emp\d+$", re.I)


def _resolve_assignee_names(raw: str) -> str:
    """An 'Assigned To' value handed to auto_generate_tasks() can be a raw
    employee id instead of a display name -- confirmed live in production:
    23 real tasks (Omotec/Mellow/1 FOUNDER) had assigned_to values like
    "emp008"/"emp012"/"emp002" sitting straight in Notion, because
    importClientSocialCSV()'s Excel/CSV import (frontend/projects.html) just
    passes through whatever text is in the sheet's Assigned To column --
    unlike the manual Add Tasks calendar UI, which always sends the
    checkbox's displayed name text (add-tasks.html ~line 1452), a CSV built
    from an old export or typed by hand can easily contain the id instead.
    This normalizes any comma-separated token that looks like an employee id
    into its current real name (live-loaded from employees.json, same
    convention as ops.py::_load_emp_names() -- CLAUDE.md gotcha #63 -- never
    a hardcoded id->name map); anything else (an already-real name, a
    freelancer name not in employees.json) passes through unchanged."""
    if not raw:
        return raw
    try:
        emp_by_id = {e.get("id", ""): e.get("name") or e.get("id", "")
                     for e in _load_employees().get("employees", [])}
    except Exception:
        return raw
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    resolved = [emp_by_id.get(p.lower(), p) if _EMP_ID_RE.match(p) else p
                for p in parts]
    return ", ".join(resolved)


def _push_new_social_task_to_sheet(client_id, task_id, *, creation_date, due_date, title,
                                    post_type, content, idea, scripts, caption, link_url, assignee):
    """Best-effort push of a freshly-created social task into its client's
    linked Google Sheet, if any -- mirrors the push applySheetFields() does
    for a normal Sheets edit (frontend/projects.html). Without this, tasks
    created here (auto_generate_tasks -- reached both by the Add Tasks
    social calendar and by importClientSocialCSV's bulk CSV/Excel import)
    never reach an already-linked Sheet, and are then at risk of being
    deleted on the very next Sheet-side edit's reconcile pass, which treats
    "a task Lumina has that the Sheet's own snapshot doesn't recognize" as a
    Sheet-initiated delete to mirror. Must never raise -- a sync failure
    here must not undo or fail the task creation that already succeeded by
    the time this runs."""
    if not task_id:
        return
    try:
        sheet_link = get_link_for_client(str(client_id))
        if not sheet_link:
            return
        fields = {
            "creation_date": creation_date or "", "due_date": due_date or "",
            "title": title or "", "type": post_type or "Post",
            "content": content or "", "idea": idea or "", "scripts": scripts or "",
            "caption": caption or "", "link": link_url or "", "myNotes": "",
            "assigned_to": assignee or "", "status": "not_started",
        }
        push_fn = (google_sheets_store.push_task_to_sheet_multi_tab if sheet_link.get("multi_tab")
                   else google_sheets_store.push_task_to_sheet)
        push_fn(sheet_link, str(task_id), fields)
    except Exception:
        logger.exception(f"Sheets: failed to push newly-created task {task_id} for client {client_id}")


# ── POST /api/clients/<id>/auto-tasks ─────────────────────────────────────────
# NOTE: this decorator must stay directly above auto_generate_tasks. It was
# previously separated from it by the _push_new_social_task_to_sheet helper
# above, which silently bound this route to that helper instead -- every POST
# then called it with only client_id, raising TypeError on its required
# keyword-only args (500 HTML, so the frontend's `ad.error` was undefined and
# it showed a generic "Failed to add tasks"), while auto_generate_tasks was
# never registered at all. That broke both the Add Tasks calendar submit and
# projects.html's bulk CSV/Excel import, which post to this same endpoint.
@app.route("/api/clients/<client_id>/auto-tasks", methods=["POST"])
def auto_generate_tasks(client_id):
    """Auto-generate tasks from selected service types. Admin only."""
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "")
    if not _is_admin(user_id):
        return jsonify({"error": "Unauthorized"}), 403

    services = body.get("services", [])
    due_date = body.get("due_date", body.get("deadline", ""))
    extra_notes = body.get("extra_notes", "")
    social_posts = body.get("social_posts", [])  # structured post rows from the calendar
    client_name = body.get("client_name", "Unknown Client")
    if not services:
        return jsonify({"error": "No services selected"}), 400

    custom_tasks = body.get("custom_tasks", {})
    created_ids = []
    ordered_tasks = []
    failed_posts = []  # social rows whose Notion create failed even after retry -- surfaced to the caller instead of silently vanishing (see comment below)

    # For social: if custom posts were provided, use them directly as tasks
    # instead of the generic templates. For other services, use custom_tasks if present, else templates.
    for svc in services:
        if svc == "social" and social_posts:
            continue  # handled separately below
            
        if svc in custom_tasks and custom_tasks[svc]:
            # Use custom tasks from the UI
            for idx, ct in enumerate(custom_tasks[svc]):
                ordered_tasks.append({
                    "title": ct.get("title"),
                    "assigned_to": ct.get("assignee"),
                    "due_date": ct.get("due_date"),
                    "order": idx,
                    "service": svc
                })
        else:
            # Fall back to templates
            templates = SERVICE_TASK_TEMPLATES.get(svc, [])
            for tmpl in templates:
                new_tmpl = dict(tmpl)
                new_tmpl["service"] = svc
                ordered_tasks.append(new_tmpl)

    # Sort by order field so dependencies chain correctly
    ordered_tasks.sort(key=lambda x: x.get("order", 0))

    # Notion Mode
    if not str(client_id).isdigit():
        # Create one task per social post row
        for idx, post in enumerate(social_posts):
            title = post.get("title") or f"Post {idx + 1}"
            post_day = post.get("post_day") or due_date
            post_type = post.get("type", "")
            content = post.get("content", "")
            idea = post.get("idea", "")
            scripts = post.get("scripts", "")
            caption = post.get("caption", "")
            link = post.get("link", "")
            assignee = _resolve_assignee_names(post.get("assignee", ""))
            # Build a rich title: "[Type] Title" -- normalized through the
            # same _normalize_type() google_sheets_store.py's own
            # _create_task/_update_task use, since post_type here is raw,
            # unvalidated text straight from an imported Excel/CSV cell
            # (see importClientSocialCSV() in projects.html: `getCell(row,
            # 'type') || "Story"`, no constraint on what that cell contains).
            # An unrecognized bracket type (e.g. a source file using "IG
            # Reel" instead of "Reel") builds a title _is_social_task()
            # can't recognize -- invisible to _current_tasks_by_id() on the
            # very next Sheets reconcile pass for a linked client, which
            # (task_id present in the Sheet from this row's own push below,
            # not tombstoned) recreates it again and again: the exact same
            # infinite-duplicate-creation hazard fixed in google_sheets_store
            # for direct Sheet edits (CLAUDE.md gotcha #87), just reachable
            # through CSV import instead.
            normalized_type = google_sheets_store._normalize_type(post_type)
            task_title = f"[{normalized_type}] {title}"
            # Pack extra detail into a description-like note via the brief
            creation_date = post.get("creation_date", "")
            detail_parts = []
            if creation_date: detail_parts.append(f"Creation Date: {creation_date}")
            if content: detail_parts.append(f"Content: {content}")
            if idea:    detail_parts.append(f"Idea: {idea}")
            if scripts: detail_parts.append(f"Scripts: {scripts}")
            if caption: detail_parts.append(f"Caption: {caption}")
            if link:    detail_parts.append(f"Link: {link}")
            notes = " | ".join(detail_parts)
            # notion_store.create_task() already retries transient/429 failures
            # 3x internally (~7s total) before giving up and returning None --
            # but a bulk import can land right after a burst of other Notion
            # traffic against this workspace (e.g. a bulk delete/cleanup) that
            # keeps rate-limiting for longer than that. One extra retry here,
            # after a longer pause, catches that case; a row that still fails
            # is recorded in failed_posts instead of silently vanishing (the
            # previous behavior: `if res` skipped it with no trace anywhere
            # the caller could see -- a full-looking "success" response that
            # had quietly dropped rows).
            res = notion_store.create_task(
                title=task_title,
                client_name=client_name,
                client_notion_id=client_id,
                assigned_to=assignee,
                due_date=post_day,
                status="not_started",
                progress=0,
                service="Social Media",
                notes=notes
            )
            if not res:
                time.sleep(5)
                res = notion_store.create_task(
                    title=task_title,
                    client_name=client_name,
                    client_notion_id=client_id,
                    assigned_to=assignee,
                    due_date=post_day,
                    status="not_started",
                    progress=0,
                    service="Social Media",
                    notes=notes
                )
            if res:
                created_ids.append(res["notion_id"])
                _push_new_social_task_to_sheet(
                    client_id, res["notion_id"], creation_date=creation_date, due_date=post_day,
                    title=title, post_type=post_type, content=content, idea=idea, scripts=scripts,
                    caption=caption, link_url=link, assignee=assignee,
                )
            else:
                failed_posts.append({"title": title, "post_day": post_day})

        # Generic template tasks for non-social services
        for tmpl in ordered_tasks:
            res = notion_store.create_task(
                title=tmpl["title"],
                client_name=client_name,
                client_notion_id=client_id,
                assigned_to=_resolve_assignee_names(tmpl.get("assigned_to", "") or ""),
                due_date=tmpl.get("due_date") or due_date,
                status="not_started",
                progress=0
            )
            if res:
                created_ids.append(res["notion_id"])

        if extra_notes:
            notion_store.append_client_requirements(client_id, extra_notes)

        return jsonify({"success": True, "tasks_created": len(created_ids), "task_ids": created_ids, "social_posts_failed": failed_posts})

    # SQLite Mode
    conn = _pt_conn()
    with conn:
        # One task per social post row
        for idx, post in enumerate(social_posts):
            title = post.get("title") or f"Post {idx + 1}"
            post_day = post.get("post_day") or due_date
            post_type = post.get("type", "")
            content = post.get("content", "")
            idea = post.get("idea", "")
            scripts = post.get("scripts", "")
            caption = post.get("caption", "")
            link = post.get("link", "")
            assignee = _resolve_assignee_names(post.get("assignee", ""))
            # Same normalization as the Notion-mode branch above, and for
            # the same reason: post_type is raw, unvalidated text from an
            # imported Excel/CSV cell. See the comment on that branch.
            normalized_type = google_sheets_store._normalize_type(post_type)
            task_title = f"[{normalized_type}] {title}"
            creation_date = post.get("creation_date", "")
            detail_parts = []
            if creation_date: detail_parts.append(f"Creation Date: {creation_date}")
            if content: detail_parts.append(f"Content: {content}")
            if idea:    detail_parts.append(f"Idea: {idea}")
            if scripts: detail_parts.append(f"Scripts: {scripts}")
            if caption: detail_parts.append(f"Caption: {caption}")
            if link:    detail_parts.append(f"Link: {link}")
            notes = " | ".join(detail_parts)
            cur = conn.execute(
                """INSERT INTO tasks (client_id,title,description,assigned_to,due_date,status,progress)
                   VALUES (?,?,?,?,?,'not_started',0)""",
                (client_id, task_title, notes, assignee, post_day)
            )
            created_ids.append(cur.lastrowid)
            _push_new_social_task_to_sheet(
                client_id, cur.lastrowid, creation_date=creation_date, due_date=post_day,
                title=title, post_type=post_type, content=content, idea=idea, scripts=scripts,
                caption=caption, link_url=link, assignee=assignee,
            )

        # Template tasks for non-social services
        for tmpl in ordered_tasks:
            cur = conn.execute(
                """INSERT INTO tasks (client_id,title,assigned_to,due_date,status,progress)
                   VALUES (?,?,?,?,'not_started',0)""",
                (client_id, tmpl["title"], _resolve_assignee_names(tmpl.get("assigned_to", "") or ""),
                 tmpl.get("due_date") or due_date)
            )
            created_ids.append(cur.lastrowid)

        # Wire sequential dependencies (each task depends on the one before it within service).
        # Pair by position (ordered_tasks[i] was inserted as created_ids[i] in the
        # loop above) rather than ordered_tasks.index(tmpl) — .index() always
        # resolves to the FIRST matching dict, so two tasks with identical
        # title/assignee/due_date/order/service (e.g. duplicated custom tasks
        # left with default titles) would both wire to the same task's ID.
        task_id_by_position = list(zip(ordered_tasks, created_ids[len(social_posts):]))
        for svc in services:
            svc_ids = [tid for tmpl, tid in task_id_by_position if tmpl.get("service") == svc]
            for i in range(1, len(svc_ids)):
                conn.execute("INSERT OR IGNORE INTO dependencies VALUES (?,?)",
                             (svc_ids[i], svc_ids[i-1]))
                             
        if extra_notes:
            conn.execute("UPDATE clients SET requirements = requirements || ? WHERE id = ?", ("\n" + extra_notes, client_id))

    conn.close()
    return jsonify({"success": True, "tasks_created": len(created_ids), "task_ids": created_ids})

# ── GET /api/blockers ─────────────────────────────────────────────────────────
@app.route("/api/blockers", methods=["GET"])
def get_blockers():
    """Get the full dependency blocker chain across all active tasks."""
    user_id = request.args.get("user_id", "")
    if not _is_admin(user_id):
        return jsonify({"error": "Unauthorized"}), 403

    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    conn = _pt_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT t.id, t.title, t.assigned_to, t.status, t.due_date,
               c.name as client_name
        FROM tasks t
        JOIN clients c ON t.client_id = c.id
        WHERE t.status NOT IN ('approved')
    """)
    all_tasks = {r[0]: {"id":r[0],"title":r[1],"assigned_to":r[2],
                         "status":r[3],"due_date":r[4],"client_name":r[5]} for r in cur.fetchall()}

    cur.execute("SELECT task_id, depends_on_task_id FROM dependencies")
    deps = cur.fetchall()
    conn.close()

    _emp_data = _load_employees()
    EMP_NAMES = {e["id"]: e["name"] for e in _emp_data.get("employees", [])}

    blockers = []
    for (task_id, blocker_id) in deps:
        if blocker_id not in all_tasks or task_id not in all_tasks:
            continue
        blocker = all_tasks[blocker_id]
        blocked  = all_tasks[task_id]
        if blocker["status"] not in ("approved",):
            days_overdue = 0
            if blocker.get("due_date") and blocker["due_date"] < today_str:
                from datetime import date
                d1 = date.fromisoformat(blocker["due_date"])
                days_overdue = (date.today() - d1).days
            blockers.append({
                "blocker_task": {**blocker, "name": EMP_NAMES.get(blocker["assigned_to"], blocker["assigned_to"])},
                "blocked_task": {**blocked, "name": EMP_NAMES.get(blocked["assigned_to"], blocked["assigned_to"])},
                "days_overdue": days_overdue,
            })

    blockers.sort(key=lambda x: x["days_overdue"], reverse=True)
    return jsonify({"blockers": blockers})

# ── POST /api/tasks/<id>/done ─────────────────────────────────────────────────
@app.route("/api/tasks/<int:task_id>/done", methods=["POST"])
def quick_done(task_id):
    """Quick 'Mark Done' — sets task to pending_review at 80%."""
    conn = _pt_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM tasks WHERE id=?", (task_id,))
    if not cur.fetchone():
        conn.close()
        return jsonify({"error": "Task not found"}), 404
    with conn:
        conn.execute("UPDATE tasks SET status='pending_review', progress=80 WHERE id=?", (task_id,))
    conn.close()
    return jsonify({"success": True})

# ══════════════════════════════════════════════════════════════════════════════
# ── Entry Point ───────────────────────────────────────────────────────────────

@app.route("/api/admin/backup-db", methods=["GET"])
def backup_db():
    from routes.auth import _verify_session
    from datetime import date
    from pathlib import Path
    
    token = request.cookies.get("session_token", "")
    user_id = _verify_session(token)
    
    if not user_id or not _is_admin(user_id):
        return jsonify({"error": "Unauthorised"}), 403
        
    db_path = Path(__file__).parent.parent / "logs" / "app.db"
    if not db_path.exists():
        return jsonify({"error": "Database file not found"}), 404
        
    # Checkpoint WAL to ensure the downloaded app.db has all latest data
    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
    except Exception as e:
        logger.error(f"Failed to checkpoint WAL before backup: {e}")

    from flask import send_file
    return send_file(
        db_path,
        as_attachment=True,
        download_name=f"backup_{date.today()}.db"
    )

@app.route('/api/admin/restore-db', methods=['POST'])
def restore_db():
    from pathlib import Path
    from db import DB_PATH as _db_path
    from routes.auth import _verify_session
    token = request.cookies.get("session_token", "")
    user_id = _verify_session(token)
    if not user_id or not _is_admin(user_id):
        return jsonify({"error": "Unauthorized"}), 403

    file = request.files.get('db')
    if not file:
        return 'No file', 400
    
    db_path = Path(_db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    file.save(str(db_path))
    
    # Safely remove WAL files to prevent corruption after overwrite
    wal = Path(str(db_path) + "-wal")
    shm = Path(str(db_path) + "-shm")
    if wal.exists():
        wal.unlink()
    if shm.exists():
        shm.unlink()
        
    return 'Database restored successfully!', 200



# ── WhatsApp Bot (Meta Cloud API) ─────────────────────────────────────────────

def send_whatsapp_message(to: str, text: str):
    """Send a WhatsApp text message via Meta Cloud API."""
    import requests as req
    phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    token    = os.getenv("WHATSAPP_ACCESS_TOKEN")
    if not phone_id or not token:
        logger.warning("WhatsApp env vars not set — message not sent.")
        return
    url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text[:4096]},  # WhatsApp max message length
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    try:
        resp = req.post(url, json=payload, headers=headers, timeout=10)
        if not resp.ok:
            logger.error(f"WhatsApp send failed: HTTP {resp.status_code} — {resp.text[:500]}")
    except Exception as exc:
        logger.error(f"WhatsApp send failed: {exc}")


@app.route("/whatsapp/webhook", methods=["GET"])
def whatsapp_verify():
    """Meta webhook verification handshake."""
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == os.getenv("META_VERIFY_TOKEN"):
        logger.info("WhatsApp webhook verified successfully.")
        return challenge, 200
    logger.warning("WhatsApp webhook verification failed — token mismatch.")
    return "Forbidden", 403


@app.route("/whatsapp/webhook", methods=["POST"])
def whatsapp_webhook():
    """Receive an inbound WhatsApp message and reply via the CRM/knowledge agent."""
    data = request.json or {}
    try:
        entry   = data["entry"][0]["changes"][0]["value"]
        message = entry["messages"][0]
        sender  = message["from"]

        # Only handle text messages — silently ignore media, audio, etc.
        if message.get("type") != "text":
            return "OK", 200
        text = message["text"]["body"]

        reply = whatsapp_agent.handle_message(sender, text)
        if isinstance(reply, dict):
            reply = reply.get("reply")   # Meta path can't send our local stickers
        if reply:
            send_whatsapp_message(sender, reply)
            logger.info(f"WhatsApp reply sent to {sender}")

    except (KeyError, IndexError):
        # Status updates, read-receipts, and non-message webhooks — ignore silently
        pass
    except Exception as exc:
        logger.error(f"WhatsApp webhook error: {exc}")

    # Always return 200 — Meta retries if it receives anything else
    return "OK", 200


def _bridge_auth_ok() -> bool:
    import hmac
    expected = (os.getenv("WHATSAPP_BRIDGE_TOKEN")
                or os.getenv("STORAGE_SYNC_TOKEN")
                or os.getenv("FLASK_SECRET_KEY") or "")
    if not expected:
        return False
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else request.args.get("token", "")
    return bool(token) and hmac.compare_digest(token, expected)


@app.route("/whatsapp/bridge", methods=["POST"])
@limiter.limit("60 per minute")
def whatsapp_bridge():
    """Transport-agnostic entry for the self-hosted Baileys bridge
    (whatsapp-bridge/). Body: {"from": "<digits>", "text": "<message>"}.
    Reuses the same agent as the Meta webhook — identity, tools, web
    search, per-sender context, budget guard all apply. The bridge only
    ever calls this in response to an inbound message (reply-only)."""
    if not _bridge_auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    sender = str(data.get("from") or data.get("sender") or "").strip()
    text = str(data.get("text") or "").strip()
    group_id = str(data.get("group_id") or "").strip() or None
    group_name = str(data.get("group_name") or "").strip() or None

    # Group memory: the bridge batches what it saw in a group. Stored only for
    # allow-listed (staff-only) groups, no AI call, no reply.
    if data.get("observe"):
        stored = 0
        try:
            if group_id and whatsapp_agent._group_observable(group_id):
                import group_memory
                stored = group_memory.record(group_id, group_name or "",
                                             data.get("messages") or [])
        except Exception:
            logger.exception("WhatsApp bridge: group observe failed")
        return jsonify({"stored": stored})

    # Group media: a voice note or image from an allow-listed group, turned into
    # a text line and stored like a message (so promises in voice notes count).
    if data.get("observe_media"):
        stored = 0
        try:
            if group_id and whatsapp_agent._group_observable(group_id):
                import base64, group_memory, media_ai
                raw = base64.b64decode(data.get("data_b64") or "")
                kind = str(data.get("kind") or "")
                if kind == "audio":
                    t = media_ai.transcribe_audio(raw, str(data.get("mime") or ""))
                    line = f"[voice note] {t}" if t else ""
                elif kind == "image":
                    t = media_ai.describe_image(raw, str(data.get("mime") or ""),
                                                str(data.get("caption") or ""))
                    line = f"[image] {t}" if t else ""
                else:
                    line = ""
                if line:
                    stored = group_memory.record(group_id, group_name or "", [{
                        "from": data.get("from"), "name": data.get("name"),
                        "text": line, "ts": data.get("ts"), "id": data.get("id")}])
        except Exception:
            logger.exception("WhatsApp bridge: group media failed")
        return jsonify({"stored": stored})

    # Burst reaction: the bridge saw a lively spell in an allow-listed group and
    # wants a mood word for a reaction sticker (or nothing). No agent, no reply,
    # no text/from field on this call.
    if data.get("burst"):
        try:
            mood = whatsapp_agent.group_vibe_sticker(
                group_id or "", data.get("messages") or [], data.get("tags") or None)
        except Exception:
            logger.exception("WhatsApp bridge: burst vibe failed")
            mood = None
        return jsonify({"sticker": mood})

    if not sender or not text:
        return jsonify({"error": "from and text required"}), 400

    # '!stickers ...' management commands are handled by the bridge, not the
    # agent — return a directive it can act on (DM + employee only).
    if not group_id:
        sc = whatsapp_agent.sticker_command(text, whatsapp_agent.identify_sender(sender))
        if sc:
            return jsonify({"reply": sc[0], "sticker_cmd": sc[1]})

    try:
        result = whatsapp_agent.handle_message(
            sender, text, group_id=group_id, group_name=group_name
        )
    except Exception:
        logger.exception("WhatsApp bridge: agent failed")
        # stay quiet in a group on error rather than posting noise for everyone
        return jsonify({"reply": None if group_id else
                        "Sorry — I hit an error. Try again in a moment."})
    # Stickers are for the team only: a client (in a DM or their group) or a stranger
    # never gets one, not even the bridge's random DM flair.
    try:
        no_sticker = whatsapp_agent.identify_sender(sender).get("kind") != "employee"
        if group_id:
            import client_groups
            no_sticker = no_sticker or client_groups.get(group_id) is not None
    except Exception:
        no_sticker = True
    if isinstance(result, dict):
        return jsonify({"reply": result.get("reply"),
                        "sticker": None if no_sticker else result.get("sticker"),
                        "no_sticker": no_sticker})
    return jsonify({"reply": result, "no_sticker": no_sticker})


@app.route("/api/whatsapp/groups", methods=["GET", "POST"])
@limiter.limit("30 per minute")
def whatsapp_groups():
    """Manage the WhatsApp group allow-list (which groups the assistant will
    answer in). Token-gated, same scheme as /whatsapp/bridge.

      GET                       -> {stored, env, effective}
      POST {"add": "<id>"}      -> add a group (numeric id or full ...@g.us)
      POST {"remove": "<id>"}   -> remove one

    `stored` lives in app_settings and is editable here at runtime; `env` is
    the read-only WHATSAPP_GROUP_ALLOWLIST fallback; `effective` is the
    digits-only union actually enforced."""
    if not _bridge_auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    from db import get_connection

    def _digits(s):
        return re.sub(r"\D", "", str(s or ""))

    def _load():
        try:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute("SELECT value FROM app_settings WHERE key='whatsapp_group_allowlist'")
            row = cur.fetchone()
            conn.close()
            return json.loads(row[0]) if row and row[0] else []
        except Exception:
            return []

    def _store(lst):
        conn = get_connection()
        with conn:
            conn.execute(
                "INSERT INTO app_settings (key, value) VALUES ('whatsapp_group_allowlist', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(lst),),
            )
        conn.close()

    stored = _load()
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        add = _digits(body.get("add"))
        rem = _digits(body.get("remove"))
        if add and add not in [_digits(x) for x in stored]:
            stored.append(add)
        if rem:
            stored = [x for x in stored if _digits(x) != rem]
        _store(stored)
        # client groups (formal, client-scoped): {"client_group": "<id>", "client": "<name>"}
        import client_groups
        cg_add = _digits(body.get("client_group"))
        if cg_add:
            c = whatsapp_agent._find_client(str(body.get("client") or ""))
            cid = (c or {}).get("notion_id") or (c or {}).get("id") or ""
            if not c or not cid:
                return jsonify({"error": "client not found"}), 404
            client_groups.link(cg_add, c.get("name") or str(body.get("client")), str(cid),
                               str(body.get("group_name") or ""), "api")
        cg_rem = _digits(body.get("remove_client_group"))
        if cg_rem:
            client_groups.unlink(cg_rem)

    import client_groups
    env_toks = [t for t in re.split(r"[,\s]+", os.getenv("WHATSAPP_GROUP_ALLOWLIST", "")) if t]
    effective = sorted({_digits(x) for x in stored} | {_digits(x) for x in env_toks} - {""})
    # `observe` = every group whose messages the bridge should store (staff + client groups);
    # `client_groups` = the ones where the bridge must never send stickers or greetings.
    cgl = client_groups.listing()
    cg_ids = [g["group_id"] for g in cgl]
    return jsonify({"stored": stored, "env": env_toks, "effective": effective,
                    "client_groups": cgl, "client_group_ids": cg_ids,
                    "observe": sorted(set(effective) | set(cg_ids))})


if __name__ == "__main__":

    port  = int(os.getenv("FLASK_PORT", 5000))
    debug = os.getenv("FLASK_ENV", "development") == "development"
    logger.info(f"Starting Agency Portal Assistant API on port {port}")
    app.run(host="0.0.0.0", port=port, debug=debug)

