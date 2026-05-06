from __future__ import annotations

"""
Goalbazi backend
================

This single Flask file powers the public pages, athlete dashboard, admin panel,
Arena Partner portal, PWA helpers, phone notifications, Google login, ratings,
friends, private messages, leagues, games, bookings, and AI assistant memory.

The project is intentionally kept simple for early-stage development: routes,
database setup, and feature logic live together so the app can be copied to
GitHub/Railway without a complicated framework structure.
"""

import hashlib
import json
import os
import re
import secrets
import smtplib
import uuid
import base64
from datetime import datetime, timedelta
from email.message import EmailMessage
from functools import wraps
from urllib import error as urllib_error
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

import psycopg2
import psycopg2.extras
from flask import Flask, g, jsonify, redirect, render_template, request, send_from_directory, session

try:
    from pywebpush import WebPushException, webpush
except Exception:
    WebPushException = Exception
    webpush = None

app = Flask(__name__, static_folder="static", template_folder=".")
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
# Keep users signed in after closing the browser/PWA. If SECRET_KEY changes on deploy,
# old cookies still become invalid, so Railway should use a fixed SECRET_KEY variable.
app.permanent_session_lifetime = timedelta(days=int(os.environ.get("SESSION_DAYS", "30")))
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("PUBLIC_BASE_URL", "").startswith("https://"),
)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "").strip()
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "").strip()
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
VAPID_CLAIMS_EMAIL = os.environ.get("VAPID_CLAIMS_EMAIL", "admin@goalbazi.app").strip()
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "").strip()
SMTP_FROM_EMAIL = os.environ.get("SMTP_FROM_EMAIL", SMTP_USER or "support@goalbazi.com").strip()
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "support@goalbazi.com").strip()
SHOW_PASSWORD_RESET_LINK = os.environ.get("SHOW_PASSWORD_RESET_LINK", "").strip().lower() in {"1", "true", "yes"}
# Production should not recreate sample arenas/leagues/teams after admin deletes
# them. Set SEED_STARTER_DATA=true only when you intentionally want demo data.
SEED_STARTER_DATA = os.environ.get("SEED_STARTER_DATA", "").strip().lower() in {"1", "true", "yes"}


def app_port() -> int:
    """Return a safe app port for local/Railway startup."""
    raw_port = os.environ.get("PORT", "8000")
    try:
        return int(raw_port)
    except (TypeError, ValueError):
        return 8000


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    """Create one Postgres connection per request context."""
    if "db" not in g:
        url = DATABASE_URL
        # Railway/Heroku give postgres:// but psycopg2 needs postgresql://
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        g.db = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query(sql, params=(), one=False, commit=False):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(sql, params)
    if commit:
        conn.commit()
        return cur.lastrowid if cur.rowcount else None
    if one:
        return cur.fetchone()
    return cur.fetchall()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    hashed = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}:{hashed}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, hashed = stored.split(":", 1)
        return hashlib.sha256((salt + password).encode()).hexdigest() == hashed
    except Exception:
        return False


def activity_status(last_seen_at: str | None) -> dict:
    """Convert last_seen_at into a friendly activity label for athlete cards."""
    if not last_seen_at:
        return {"label": "Offline", "kind": "offline"}
    try:
        seen = datetime.fromisoformat(str(last_seen_at))
    except Exception:
        return {"label": "Offline", "kind": "offline"}
    minutes = (datetime.now() - seen).total_seconds() / 60
    if minutes <= 5:
        return {"label": "Online now", "kind": "online"}
    if minutes <= 60:
        return {"label": f"Active {int(minutes)}m ago", "kind": "recent"}
    if minutes <= 1440:
        return {"label": f"Active {int(minutes // 60)}h ago", "kind": "recent"}
    return {"label": "Offline", "kind": "offline"}


def touch_user_activity() -> None:
    """Throttle last-seen writes so activity status is useful without hammering Postgres."""
    if "user_id" not in session:
        return
    now = datetime.now()
    try:
        last_touch = datetime.fromisoformat(session.get("last_seen_touch", ""))
        if (now - last_touch).total_seconds() < 180:
            return
    except Exception:
        pass
    query("UPDATE users SET last_seen_at = %s WHERE id = %s", (now.isoformat(), session["user_id"]), commit=True)
    session["last_seen_touch"] = now.isoformat()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect("/login")
        touch_user_activity()
        return f(*args, **kwargs)
    return decorated


def current_user_id():
    return session.get("user_id")


def sanitize_handle(handle: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (handle or "").strip().lower())


def display_handle(handle: str) -> str:
    clean = sanitize_handle(handle)
    return f"@{clean}" if clean else "@player"


def public_url(path: str) -> str:
    """Build a user-facing URL for emails and reset links."""
    base = PUBLIC_BASE_URL or request.url_root.rstrip("/")
    return f"{base}{path}"


def email_is_configured() -> bool:
    """SMTP is optional during pre-launch; UI falls back gracefully when missing."""
    return bool(SMTP_HOST and SMTP_FROM_EMAIL)


def send_email(to_email: str, subject: str, body: str) -> bool:
    """Send plain-text transactional email when SMTP variables are configured."""
    if not email_is_configured():
        return False
    message = EmailMessage()
    message["From"] = SMTP_FROM_EMAIL
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=12) as smtp:
            smtp.starttls()
            if SMTP_USER and SMTP_PASSWORD:
                smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.send_message(message)
        return True
    except Exception:
        return False


def unique_handle_from_email_name(email: str, name: str) -> str:
    base = sanitize_handle((email or "").split("@")[0]) or sanitize_handle(name) or "athlete"
    base = base[:20] or "athlete"
    candidate = base
    suffix = 1
    while query("SELECT id FROM users WHERE handle = %s", (candidate,), one=True):
        suffix += 1
        candidate = f"{base[:16]}{suffix}"
    return candidate


def google_redirect_uri() -> str:
    """Build the exact public callback URL registered in Google Cloud.

    Railway sits behind a proxy, so request.url_root can sometimes look like
    http://... inside Flask even when the real user-facing site is https://...
    Google requires an exact redirect_uri match, so prefer PUBLIC_BASE_URL and
    otherwise force forwarded Railway hosts to HTTPS.
    """
    if PUBLIC_BASE_URL:
        return f"{PUBLIC_BASE_URL}/auth/google/callback"
    forwarded_host = request.headers.get("X-Forwarded-Host", "").split(",")[0].strip()
    host = forwarded_host or request.host
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
    scheme = forwarded_proto or request.scheme
    if host.endswith(".up.railway.app") or host.endswith(".railway.app"):
        scheme = "https"
    return f"{scheme}://{host}/auth/google/callback"


def exchange_google_code(code: str) -> dict:
    token_body = urlencode({
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": google_redirect_uri(),
        "grant_type": "authorization_code",
    }).encode("utf-8")
    token_req = Request(
        "https://oauth2.googleapis.com/token",
        data=token_body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(token_req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_google_profile(access_token: str) -> dict:
    profile_req = Request(
        "https://www.googleapis.com/oauth2/v3/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    with urlopen(profile_req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def push_is_configured() -> bool:
    return bool(webpush and VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY)


def send_push_to_user(user_id: int, title: str, body: str, url: str = "/dashboard") -> None:
    """Send one high-priority phone/browser notification to a subscribed user."""
    if not push_is_configured():
        return
    subscriptions = [dict(r) for r in query(
        "SELECT id, endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = %s",
        (user_id,),
    )]
    payload = json.dumps({
        "title": title,
        "body": body,
        "url": url,
        "icon": "/assets/goalbazi-logo.svg",
        "badge": "/assets/goalbazi-logo.svg",
    })
    for subscription in subscriptions:
        try:
            webpush(
                subscription_info={
                    "endpoint": subscription["endpoint"],
                    "keys": {
                        "p256dh": subscription["p256dh"],
                        "auth": subscription["auth"],
                    },
                },
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": f"mailto:{VAPID_CLAIMS_EMAIL}"},
            )
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                query("DELETE FROM push_subscriptions WHERE id = %s", (subscription["id"],), commit=True)
        except Exception:
            pass


def haversine_km(lat1, lon1, lat2, lon2):
    from math import asin, cos, radians, sin, sqrt

    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    lat1 = radians(lat1)
    lat2 = radians(lat2)
    a = sin(d_lat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(d_lon / 2) ** 2
    return 6371 * 2 * asin(sqrt(a))


def log_event(event_type: str, path_value: str, meta: dict | None = None) -> None:
    """Store product analytics and admin audit events in one lightweight timeline."""
    try:
        query(
            """INSERT INTO analytics_events (event_type, path, user_id, owner_id, meta, created_at)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (
                event_type,
                path_value,
                current_user_id(),
                current_owner_id() if "owner_id" in session else None,
                json.dumps(meta or {}),
                datetime.now().isoformat(),
            ),
            commit=True,
        )
    except Exception:
        pass


def parse_event_meta(row: dict) -> dict:
    """Decode analytics meta safely so admin activity never breaks if old rows are malformed."""
    try:
        return json.loads(row.get("meta") or "{}")
    except Exception:
        return {}


def current_user_is_admin() -> bool:
    if "user_id" not in session:
        return False
    user = query("SELECT is_admin FROM users WHERE id = %s", (current_user_id(),), one=True)
    return bool(user and user.get("is_admin"))


# ---------------------------------------------------------------------------
# Database seeding
# ---------------------------------------------------------------------------

def seed_db():
    """Create/upgrade database tables and seed starter data if tables are empty."""
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS turf_owners (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            phone TEXT NOT NULL DEFAULT '',
            password_hash TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            handle TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            location TEXT NOT NULL DEFAULT 'Delhi NCR',
            position TEXT NOT NULL DEFAULT 'Midfielder',
            preferred_format TEXT NOT NULL DEFAULT '5v5',
            skill TEXT NOT NULL DEFAULT 'Intermediate',
            bio TEXT DEFAULT ''
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS turfs (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            area TEXT NOT NULL,
            distance_km REAL NOT NULL,
            surface TEXT NOT NULL,
            rating REAL NOT NULL,
            price_per_hour INTEGER NOT NULL
        )
    """)

    # Add new columns to turfs if they don't exist
    cur.execute("ALTER TABLE turfs ADD COLUMN IF NOT EXISTS owner_id INTEGER REFERENCES turf_owners(id)")
    cur.execute("ALTER TABLE turfs ADD COLUMN IF NOT EXISTS upi_id TEXT DEFAULT ''")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS turf_slots (
            id SERIAL PRIMARY KEY,
            turf_id INTEGER NOT NULL REFERENCES turfs(id),
            slot_date TEXT NOT NULL,
            slot_time TEXT NOT NULL,
            is_booked INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'available',
            booked_by INTEGER REFERENCES users(id)
        )
    """)

    cur.execute("ALTER TABLE turf_slots ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'available'")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id SERIAL PRIMARY KEY,
            slot_id INTEGER NOT NULL REFERENCES turf_slots(id),
            user_id INTEGER NOT NULL REFERENCES users(id),
            player_name TEXT NOT NULL,
            player_email TEXT NOT NULL,
            utr_number TEXT DEFAULT '',
            amount INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT '' 
        )
    """)

    # New columns for existing tables
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_base64 TEXT DEFAULT ''")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS google_id TEXT")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_provider TEXT NOT NULL DEFAULT 'password'")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS secondary_position TEXT NOT NULL DEFAULT ''")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS strong_foot TEXT NOT NULL DEFAULT ''")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS availability TEXT NOT NULL DEFAULT ''")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS pace INTEGER NOT NULL DEFAULT 50")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS shooting INTEGER NOT NULL DEFAULT 50")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS passing INTEGER NOT NULL DEFAULT 50")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS dribbling INTEGER NOT NULL DEFAULT 50")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS defending INTEGER NOT NULL DEFAULT 50")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS physical INTEGER NOT NULL DEFAULT 50")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen_at TEXT DEFAULT ''")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS admin_note TEXT NOT NULL DEFAULT ''")
    cur.execute("ALTER TABLE turfs ADD COLUMN IF NOT EXISTS qr_base64 TEXT DEFAULT ''")
    cur.execute("ALTER TABLE turfs ADD COLUMN IF NOT EXISTS map_link TEXT DEFAULT ''")
    cur.execute("ALTER TABLE turfs ADD COLUMN IF NOT EXISTS latitude DOUBLE PRECISION")
    cur.execute("ALTER TABLE turfs ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION")
    cur.execute("ALTER TABLE turfs ADD COLUMN IF NOT EXISTS description TEXT DEFAULT ''")
    cur.execute("ALTER TABLE turfs ADD COLUMN IF NOT EXISTS image_urls TEXT NOT NULL DEFAULT '[]'")
    cur.execute("ALTER TABLE turfs ADD COLUMN IF NOT EXISTS archived_at TEXT")
    cur.execute("ALTER TABLE turfs ADD COLUMN IF NOT EXISTS admin_note TEXT NOT NULL DEFAULT ''")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS analytics_events (
            id SERIAL PRIMARY KEY,
            event_type TEXT NOT NULL,
            path TEXT NOT NULL,
            user_id INTEGER REFERENCES users(id),
            owner_id INTEGER REFERENCES turf_owners(id),
            meta TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS games (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            format TEXT NOT NULL,
            skill_level TEXT NOT NULL,
            visibility TEXT NOT NULL,
            game_date TEXT NOT NULL,
            game_time TEXT NOT NULL,
            kickoff_at TEXT NOT NULL,
            turf_id INTEGER NOT NULL REFERENCES turfs(id),
            created_by INTEGER NOT NULL REFERENCES users(id),
            status TEXT NOT NULL DEFAULT 'Open'
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS player_ratings (
            id SERIAL PRIMARY KEY,
            game_id INTEGER NOT NULL REFERENCES games(id),
            rater_id INTEGER NOT NULL REFERENCES users(id),
            rated_id INTEGER NOT NULL REFERENCES users(id),
            rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 10),
            created_at TEXT NOT NULL,
            UNIQUE(game_id, rater_id, rated_id)
        )
    """)
    cur.execute("ALTER TABLE player_ratings DROP CONSTRAINT IF EXISTS player_ratings_rating_check")
    cur.execute("ALTER TABLE player_ratings ADD CONSTRAINT player_ratings_rating_check CHECK (rating >= 1 AND rating <= 10)")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS game_players (
            id SERIAL PRIMARY KEY,
            game_id INTEGER NOT NULL REFERENCES games(id),
            user_id INTEGER REFERENCES users(id),
            player_name TEXT NOT NULL,
            player_role TEXT NOT NULL,
            team_name TEXT NOT NULL,
            is_captain INTEGER NOT NULL DEFAULT 0,
            confirmed INTEGER NOT NULL DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS game_messages (
            id SERIAL PRIMARY KEY,
            game_id INTEGER NOT NULL REFERENCES games(id),
            sender_name TEXT NOT NULL,
            message TEXT NOT NULL,
            is_system INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS leagues (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            format TEXT NOT NULL,
            stage TEXT NOT NULL,
            status TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS standings (
            id SERIAL PRIMARY KEY,
            league_id INTEGER NOT NULL REFERENCES leagues(id),
            rank INTEGER NOT NULL,
            team_name TEXT NOT NULL,
            played INTEGER NOT NULL,
            won INTEGER NOT NULL,
            points INTEGER NOT NULL,
            form TEXT NOT NULL
        )
    """)

    cur.execute("ALTER TABLE leagues ADD COLUMN IF NOT EXISTS city TEXT NOT NULL DEFAULT 'Delhi NCR'")
    cur.execute("ALTER TABLE leagues ADD COLUMN IF NOT EXISTS season TEXT NOT NULL DEFAULT '2026'")
    cur.execute("ALTER TABLE leagues ADD COLUMN IF NOT EXISTS banner_url TEXT DEFAULT ''")
    cur.execute("ALTER TABLE leagues ADD COLUMN IF NOT EXISTS archived_at TEXT")
    cur.execute("ALTER TABLE leagues ADD COLUMN IF NOT EXISTS admin_note TEXT NOT NULL DEFAULT ''")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS teams (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            city TEXT NOT NULL DEFAULT 'Delhi NCR',
            short_name TEXT NOT NULL DEFAULT '',
            logo_url TEXT DEFAULT '',
            skill_level TEXT NOT NULL DEFAULT 'Intermediate',
            description TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("ALTER TABLE teams ADD COLUMN IF NOT EXISTS archived_at TEXT")
    cur.execute("ALTER TABLE teams ADD COLUMN IF NOT EXISTS admin_note TEXT NOT NULL DEFAULT ''")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS team_memberships (
            id SERIAL PRIMARY KEY,
            team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role TEXT NOT NULL DEFAULT 'Player',
            jersey_number TEXT DEFAULT '',
            joined_at TEXT NOT NULL,
            UNIQUE(user_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS team_challenges (
            id SERIAL PRIMARY KEY,
            challenger_team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            opponent_team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            format TEXT NOT NULL DEFAULT '5v5',
            proposed_date TEXT NOT NULL DEFAULT '',
            proposed_time TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS league_teams (
            id SERIAL PRIMARY KEY,
            league_id INTEGER NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            played INTEGER NOT NULL DEFAULT 0,
            won INTEGER NOT NULL DEFAULT 0,
            drawn INTEGER NOT NULL DEFAULT 0,
            lost INTEGER NOT NULL DEFAULT 0,
            goals_for INTEGER NOT NULL DEFAULT 0,
            goals_against INTEGER NOT NULL DEFAULT 0,
            points INTEGER NOT NULL DEFAULT 0,
            rank INTEGER NOT NULL DEFAULT 0,
            form TEXT NOT NULL DEFAULT '',
            notes TEXT DEFAULT '',
            UNIQUE(league_id, team_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS friendships (
            id SERIAL PRIMARY KEY,
            user_one_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            user_two_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            requested_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            UNIQUE(user_one_id, user_two_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS direct_messages (
            id SERIAL PRIMARY KEY,
            sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            receiver_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS support_requests (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT 'support',
            message TEXT NOT NULL,
            user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            endpoint TEXT NOT NULL UNIQUE,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            user_agent TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_assistant_messages (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS player_open_ratings (
            id SERIAL PRIMARY KEY,
            rater_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            rated_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 10),
            created_at TEXT NOT NULL,
            UNIQUE(rater_id, rated_id)
        )
    """)
    cur.execute("ALTER TABLE player_open_ratings DROP CONSTRAINT IF EXISTS player_open_ratings_rating_check")
    cur.execute("ALTER TABLE player_open_ratings ADD CONSTRAINT player_open_ratings_rating_check CHECK (rating >= 1 AND rating <= 10)")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS profile_assessments (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            requested_payload TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',
            admin_note TEXT NOT NULL DEFAULT '',
            reviewed_by INTEGER REFERENCES users(id),
            created_at TEXT NOT NULL,
            reviewed_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    def mark_seed_done(key):
        cur.execute(
            """INSERT INTO app_settings (key, value, updated_at)
               VALUES (%s, 'true', %s)
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at""",
            (key, datetime.now().isoformat()),
        )

    def seed_done(key):
        cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
        row = cur.fetchone()
        return bool(row and row["value"] == "true")

    def should_seed_once(key, existing_count):
        """Seed starter data only once, so admin deletions do not come back after restart."""
        if not SEED_STARTER_DATA:
            mark_seed_done(key)
            return False
        if existing_count > 0:
            mark_seed_done(key)
            return False
        return not seed_done(key)

    if ADMIN_EMAIL:
        cur.execute("UPDATE users SET is_admin = TRUE WHERE LOWER(email) = %s", (ADMIN_EMAIL.lower(),))

    # Seed turfs
    turf_count = query("SELECT COUNT(*) FROM turfs", one=True)["count"]
    if should_seed_once("seeded_default_turfs", turf_count):
        turfs = [
            ("Siri Fort Sports Complex", "South Delhi", 1.2, "Astroturf", 4.8, 600),
            ("Vasant Kunj Football Ground", "South West Delhi", 3.4, "Natural grass", 4.5, 400),
            ("JLN Arena Turf", "Central Delhi", 5.1, "Astroturf", 4.7, 750),
        ]
        cur.executemany(
            "INSERT INTO turfs (name, area, distance_km, surface, rating, price_per_hour) VALUES (%s,%s,%s,%s,%s,%s)",
            turfs,
        )

        # Seed slots for each turf for next 5 days
        cur.execute("SELECT id FROM turfs ORDER BY id")
        turf_ids = [row["id"] for row in cur.fetchall()]
        time_sets = [
            ["06:00", "07:00", "08:00", "18:00", "19:00"],
            ["06:00", "07:00", "09:00", "17:00", "18:00"],
            ["07:00", "08:00", "18:00", "20:00"],
        ]
        for day_offset in range(5):
            slot_date = (datetime.now() + timedelta(days=day_offset)).date().isoformat()
            for index, turf_id in enumerate(turf_ids):
                for slot_time in time_sets[index]:
                    cur.execute(
                        "INSERT INTO turf_slots (turf_id, slot_date, slot_time, is_booked) VALUES (%s,%s,%s,0)",
                        (turf_id, slot_date, slot_time),
                    )
        mark_seed_done("seeded_default_turfs")

    # Seed leagues
    league_count = query("SELECT COUNT(*) FROM leagues", one=True)["count"]
    if should_seed_once("seeded_default_leagues", league_count):
        leagues = [
            ("Delhi Premier 5v5", "Round robin with eight neighborhood teams.", "5v5", "Week 4 of 7", "Live"),
            ("South Delhi 7s Cup", "Knockout fixtures featuring amateur weekend squads.", "7v7", "Quarter-finals", "Live"),
            ("Monsoon Masters", "Registration-driven small-sided competition.", "5v5", "Registration", "Open"),
        ]
        cur.executemany(
            "INSERT INTO leagues (name, description, format, stage, status) VALUES (%s,%s,%s,%s,%s)",
            leagues,
        )
        cur.execute("SELECT id FROM leagues ORDER BY id LIMIT 1")
        league_id = cur.fetchone()["id"]
        standings_rows = [
            (league_id, 1, "FC Malviya", 3, 3, 9, "W,W,W"),
            (league_id, 2, "Yodha FC", 3, 2, 6, "W,L,W"),
            (league_id, 3, "Saket Rovers", 3, 1, 4, "W,D,L"),
            (league_id, 4, "DK Strikers", 3, 1, 3, "L,W,L"),
        ]
        cur.executemany(
            "INSERT INTO standings (league_id, rank, team_name, played, won, points, form) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            standings_rows,
        )
        mark_seed_done("seeded_default_leagues")

    team_count = query("SELECT COUNT(*) FROM teams", one=True)["count"]
    if should_seed_once("seeded_default_teams", team_count):
        teams = [
            ("FC Malviya", "Delhi NCR", "FCM", "", "Competitive", "Fast-transition neighborhood side."),
            ("Yodha FC", "Delhi NCR", "YOD", "", "Competitive", "Press-heavy squad with strong wing play."),
            ("Saket Rovers", "Delhi NCR", "SAK", "", "Intermediate", "Balanced possession-focused club."),
            ("DK Strikers", "Delhi NCR", "DKS", "", "Intermediate", "Counter-attacking city squad."),
        ]
        cur.executemany(
            """INSERT INTO teams
               (name, city, short_name, logo_url, skill_level, description, created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            [(name, city, short_name, logo, skill_level, description, datetime.now().isoformat()) for (name, city, short_name, logo, skill_level, description) in teams],
        )
        mark_seed_done("seeded_default_teams")

    league_team_count = query("SELECT COUNT(*) FROM league_teams", one=True)["count"]
    if should_seed_once("seeded_default_league_teams", league_team_count):
        primary_league = query("SELECT id FROM leagues ORDER BY id ASC LIMIT 1", one=True)
        if primary_league:
            team_lookup = {row["name"]: row["id"] for row in query("SELECT id, name FROM teams ORDER BY id ASC")}
            league_rows = [
                (primary_league["id"], team_lookup.get("FC Malviya"), 3, 3, 0, 0, 14, 6, 9, 1, "W,W,W", ""),
                (primary_league["id"], team_lookup.get("Yodha FC"), 3, 2, 0, 1, 10, 7, 6, 2, "W,L,W", ""),
                (primary_league["id"], team_lookup.get("Saket Rovers"), 3, 1, 1, 1, 9, 8, 4, 3, "W,D,L", ""),
                (primary_league["id"], team_lookup.get("DK Strikers"), 3, 1, 0, 2, 7, 10, 3, 4, "L,W,L", ""),
            ]
            cur.executemany(
                """INSERT INTO league_teams
                   (league_id, team_id, played, won, drawn, lost, goals_for, goals_against, points, rank, form, notes)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                [row for row in league_rows if row[1]],
            )
        mark_seed_done("seeded_default_league_teams")

    conn.commit()


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def get_profile(user_id):
    row = query("SELECT * FROM users WHERE id = %s", (user_id,), one=True)
    if not row:
        return None
    profile = dict(row)
    profile["is_admin"] = bool(profile.get("is_admin"))
    profile["handle"] = sanitize_handle(profile.get("handle", ""))
    profile["handle_display"] = display_handle(profile["handle"])
    team = query(
        """SELECT t.id, t.name, t.logo_url, tm.role, tm.jersey_number
           FROM team_memberships tm
           JOIN teams t ON t.id = tm.team_id
           WHERE tm.user_id = %s AND t.archived_at IS NULL""",
        (user_id,),
        one=True,
    )
    profile["team"] = dict(team) if team else None
    profile["pending_assessment"] = get_pending_profile_assessment(user_id)
    return profile


PROFILE_EDIT_FIELDS = [
    "name",
    "handle",
    "location",
    "position",
    "secondary_position",
    "preferred_format",
    "skill",
    "strong_foot",
    "availability",
    "bio",
    "avatar_base64",
    "pace",
    "shooting",
    "passing",
    "dribbling",
    "defending",
    "physical",
]

PROFILE_TEXT_LIMITS = {
    "name": 80,
    "handle": 30,
    "location": 80,
    "position": 50,
    "secondary_position": 50,
    "preferred_format": 30,
    "skill": 40,
    "strong_foot": 20,
    "availability": 80,
    "bio": 500,
}

PROFILE_SCORE_FIELDS = {"pace", "shooting", "passing", "dribbling", "defending", "physical"}


def clamp_profile_score(value) -> int:
    try:
        return max(1, min(100, int(value)))
    except (TypeError, ValueError):
        return 50


def normalize_profile_request(data: dict) -> dict:
    payload = {}
    for field in PROFILE_EDIT_FIELDS:
        if field in PROFILE_SCORE_FIELDS:
            payload[field] = clamp_profile_score(data.get(field, 50))
        elif field == "handle":
            payload[field] = sanitize_handle(data.get(field, ""))
        elif field == "avatar_base64":
            payload[field] = str(data.get(field, "") or "")[:2_000_000]
        else:
            limit = PROFILE_TEXT_LIMITS.get(field, 120)
            payload[field] = str(data.get(field, "") or "").strip()[:limit]
    return payload


def get_pending_profile_assessment(user_id):
    row = query(
        """SELECT id, requested_payload, status, admin_note, created_at, reviewed_at
           FROM profile_assessments
           WHERE user_id = %s AND status = 'pending'
           ORDER BY id DESC
           LIMIT 1""",
        (user_id,),
        one=True,
    )
    if not row:
        return None
    item = dict(row)
    try:
        item["requested_payload"] = json.loads(item.get("requested_payload") or "{}")
    except Exception:
        item["requested_payload"] = {}
    return item


def get_profile_assessments(status="pending", limit=40):
    rows = query(
        """SELECT pa.id, pa.user_id, pa.requested_payload, pa.status, pa.admin_note,
                  pa.created_at, pa.reviewed_at, u.name, u.handle, u.email
           FROM profile_assessments pa
           JOIN users u ON u.id = pa.user_id
           WHERE pa.status = %s
           ORDER BY pa.id DESC
           LIMIT %s""",
        (status, limit),
    )
    items = []
    for row in rows:
        item = dict(row)
        try:
            item["requested_payload"] = json.loads(item.get("requested_payload") or "{}")
        except Exception:
            item["requested_payload"] = {}
        item["handle_display"] = display_handle(item.get("handle", ""))
        items.append(item)
    return items


def get_stats():
    """Dashboard statistic cards for athletes."""
    return [
        {"value": query("SELECT COUNT(*) FROM users", one=True)["count"], "label": "Players"},
        {"value": query("SELECT COUNT(*) FROM games", one=True)["count"], "label": "Games"},
        {"value": query("SELECT COUNT(*) FROM turfs WHERE archived_at IS NULL", one=True)["count"], "label": "Arenas"},
        {"value": query("SELECT COUNT(*) FROM leagues WHERE archived_at IS NULL", one=True)["count"], "label": "Leagues"},
    ]


def get_user_team(user_id: int):
    """Return the active team connected to a user, including their team role."""
    team = query(
        """SELECT t.id, t.name, t.city, t.short_name, t.logo_url, t.skill_level, tm.role
           FROM team_memberships tm
           JOIN teams t ON t.id = tm.team_id
           WHERE tm.user_id = %s AND t.archived_at IS NULL""",
        (user_id,),
        one=True,
    )
    return dict(team) if team else None


def user_can_manage_team(user_id: int, team_id: int) -> bool:
    """Allow captains/admin-style team roles to create or answer challenges."""
    membership = query(
        """SELECT role FROM team_memberships
           WHERE user_id = %s AND team_id = %s""",
        (user_id, team_id),
        one=True,
    )
    if not membership:
        return False
    return (membership["role"] or "").strip().lower() in {"captain", "manager", "owner", "admin"}


def get_founder_badge(user_id: int) -> dict:
    """Early users get a First XI badge; no public 'founder' wording is shown."""
    row = query("SELECT id FROM users WHERE id = %s", (user_id,), one=True)
    is_founder = bool(row and row["id"] <= 50)
    return {
        "enabled": is_founder,
        "label": "First XI" if is_founder else "",
        "description": "Early Goalbazi member" if is_founder else "",
    }


def get_team_challenges_for_user(user_id: int) -> list[dict]:
    """Return challenge activity relevant to the user's current team."""
    team = get_user_team(user_id)
    if not team:
        return []
    rows = query(
        """SELECT tc.*,
                  ct.name AS challenger_name,
                  ot.name AS opponent_name,
                  u.name AS creator_name
           FROM team_challenges tc
           JOIN teams ct ON ct.id = tc.challenger_team_id
           JOIN teams ot ON ot.id = tc.opponent_team_id
           JOIN users u ON u.id = tc.created_by
           WHERE (tc.challenger_team_id = %s OR tc.opponent_team_id = %s)
             AND ct.archived_at IS NULL
             AND ot.archived_at IS NULL
           ORDER BY tc.id DESC
           LIMIT 12""",
        (team["id"], team["id"]),
    )
    return [dict(row) for row in rows]


def notify_team_members(team_id: int, title: str, body: str, url: str = "/dashboard", exclude_user_id: int | None = None) -> None:
    """Send push notifications to all active users on a team."""
    rows = query(
        """SELECT user_id FROM team_memberships
           WHERE team_id = %s
             AND (%s IS NULL OR user_id != %s)""",
        (team_id, exclude_user_id, exclude_user_id),
    )
    for row in rows:
        send_push_to_user(row["user_id"], title, body, url)


def normalize_friend_pair(user_a, user_b):
    return (min(int(user_a), int(user_b)), max(int(user_a), int(user_b)))


def have_shared_game(user_a, user_b):
    row = query(
        """SELECT COUNT(*) AS count
           FROM game_players gp1
           JOIN game_players gp2 ON gp2.game_id = gp1.game_id
           WHERE gp1.user_id = %s AND gp2.user_id = %s
             AND gp1.confirmed = 1 AND gp2.confirmed = 1""",
        (user_a, user_b),
        one=True,
    )
    return bool(row and row["count"] > 0)


def are_friends(user_a, user_b):
    one_id, two_id = normalize_friend_pair(user_a, user_b)
    row = query(
        """SELECT id
           FROM friendships
           WHERE user_one_id = %s AND user_two_id = %s AND status = 'accepted'""",
        (one_id, two_id),
        one=True,
    )
    return bool(row)


def can_rate_athlete(rater_id, rated_id):
    if rater_id == rated_id:
        return False, "You cannot rate yourself."
    if are_friends(rater_id, rated_id):
        return True, "You are connected as friends."
    if have_shared_game(rater_id, rated_id):
        return True, "You have played together."
    return False, "Ratings are only available for friends or athletes you have played with."


def get_rating_summary(user_id):
    row = query(
        """SELECT
             ROUND(AVG(rating_value)::numeric, 1) AS avg_rating,
             COUNT(*) AS total_ratings,
             COUNT(*) FILTER (WHERE rating_type = 'game') AS game_rating_count,
             COUNT(*) FILTER (WHERE rating_type = 'open') AS open_rating_count
           FROM (
             SELECT rating AS rating_value, 'game' AS rating_type FROM player_ratings WHERE rated_id = %s
             UNION ALL
             SELECT rating AS rating_value, 'open' AS rating_type FROM player_open_ratings WHERE rated_id = %s
           ) ratings""",
        (user_id, user_id),
        one=True,
    )
    return {
        "avg_rating": float(row["avg_rating"]) if row and row["avg_rating"] is not None else None,
        "total_ratings": row["total_ratings"] if row else 0,
        "game_rating_count": row["game_rating_count"] if row else 0,
        "open_rating_count": row["open_rating_count"] if row else 0,
    }


def get_leagues_with_teams(include_empty=False):
    """Return active leagues and their non-archived teams for dashboard/admin views."""
    leagues = [dict(r) for r in query(
        """SELECT * FROM leagues
           WHERE archived_at IS NULL
             AND LOWER(status) IN ('open', 'live')
           ORDER BY id ASC"""
    )]
    league_ids = [league["id"] for league in leagues]
    league_rows = [dict(r) for r in query(
        """SELECT lt.id, lt.league_id, lt.team_id, lt.played, lt.won, lt.drawn, lt.lost,
                  lt.goals_for, lt.goals_against, lt.points, lt.rank, lt.form, lt.notes,
                  t.name AS team_name, t.logo_url, t.city, t.skill_level
           FROM league_teams lt
           JOIN teams t ON t.id = lt.team_id
           WHERE t.archived_at IS NULL
           ORDER BY lt.league_id ASC, lt.rank ASC, lt.points DESC, t.name ASC"""
    )]
    for league in leagues:
        league["teams"] = [row for row in league_rows if row["league_id"] == league["id"]]
        league["team_count"] = len(league["teams"])
    if not include_empty:
        leagues = [league for league in leagues if league["team_count"] > 0]
    primary_standings = leagues[0]["teams"] if leagues else []
    return leagues, primary_standings


def get_challenge_opponents(user_id: int) -> list[dict]:
    """Return active teams the user's team can challenge."""
    my_team = get_user_team(user_id)
    if not my_team:
        return []
    rows = query(
        """SELECT t.id, t.name, t.city, t.skill_level, COUNT(tm.id) AS member_count
           FROM teams t
           LEFT JOIN team_memberships tm ON tm.team_id = t.id
           WHERE t.archived_at IS NULL AND t.id != %s
           GROUP BY t.id
           ORDER BY t.name ASC
           LIMIT 40""",
        (my_team["id"],),
    )
    return [dict(row) for row in rows]


def parse_image_urls(raw_value):
    if not raw_value:
        return []
    if isinstance(raw_value, list):
        return [str(item).strip() for item in raw_value if str(item).strip()]
    text = str(raw_value).strip()
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [str(item).strip() for item in data if str(item).strip()]
    except Exception:
        pass
    return [line.strip() for line in text.replace(",", "\n").splitlines() if line.strip()]


def serialize_image_urls(raw_value):
    return json.dumps(parse_image_urls(raw_value))


def get_notifications():
    """Build lightweight in-app notifications for athletes, admins, and arena partners."""
    items = []
    if "user_id" in session:
        uid = current_user_id()
        pending_friends = [dict(r) for r in query(
            """SELECT f.id, u.name
               FROM friendships f
               JOIN users u ON u.id = f.requested_by
               WHERE (f.user_one_id = %s OR f.user_two_id = %s)
                 AND f.status = 'pending'
                 AND f.requested_by != %s
               ORDER BY f.id DESC
               LIMIT 5""",
            (uid, uid, uid),
        )]
        for row in pending_friends:
            items.append({
                "type": "friend_request",
                "title": "Friend request",
                "message": f"{row['name']} sent you a friend request.",
            })
        accepted_friends = [dict(r) for r in query(
            """SELECT u.name
               FROM friendships f
               JOIN users u ON u.id = CASE WHEN f.user_one_id = %s THEN f.user_two_id ELSE f.user_one_id END
               WHERE (f.user_one_id = %s OR f.user_two_id = %s)
                 AND f.status = 'accepted'
               ORDER BY f.id DESC
               LIMIT 3""",
            (uid, uid, uid),
        )]
        for row in accepted_friends:
            items.append({
                "type": "friend_accepted",
                "title": "New connection",
                "message": f"You are now connected with {row['name']}.",
            })
        recent_messages = [dict(r) for r in query(
            """SELECT u.name, dm.message
               FROM direct_messages dm
               JOIN users u ON u.id = dm.sender_id
               WHERE dm.receiver_id = %s
               ORDER BY dm.id DESC
               LIMIT 5""",
            (uid,),
        )]
        for row in recent_messages:
            items.append({
                "type": "direct_message",
                "title": "New message",
                "message": f"{row['name']}: {row['message'][:60]}",
            })
        reminder_rows = [dict(r) for r in query(
            """SELECT g.title, g.game_date, g.game_time, t.name AS arena_name
               FROM game_players gp
               JOIN games g ON g.id = gp.game_id
               JOIN turfs t ON t.id = g.turf_id
               WHERE gp.user_id = %s
                 AND gp.confirmed = 1
                 AND t.archived_at IS NULL
                 AND g.game_date BETWEEN %s AND %s
               ORDER BY g.game_date ASC, g.game_time ASC
               LIMIT 3""",
            (
                uid,
                datetime.now().date().isoformat(),
                (datetime.now() + timedelta(days=1)).date().isoformat(),
            ),
        )]
        for row in reminder_rows:
            items.append({
                "type": "match_reminder",
                "title": "Match reminder",
                "message": f"{row['title']} at {row['arena_name']} on {row['game_date']} {row['game_time']}.",
            })
        if get_pending_profile_assessment(uid):
            items.append({
                "type": "profile_assessment",
                "title": "Profile under assessment",
                "message": "Your requested profile changes are waiting for admin approval.",
            })
        my_team = get_user_team(uid)
        for challenge in get_team_challenges_for_user(uid)[:3]:
            incoming = bool(my_team and challenge["opponent_team_id"] == my_team["id"])
            if challenge["status"] != "pending":
                items.append({
                    "type": "team_challenge_status",
                    "title": f"Challenge {challenge['status']}",
                    "message": f"{challenge['challenger_name']} vs {challenge['opponent_name']} was {challenge['status']}.",
                })
                continue
            items.append({
                "type": "team_challenge",
                "title": "New team challenge" if incoming else "Challenge pending",
                "message": (
                    f"{challenge['challenger_name']} challenged {challenge['opponent_name']}."
                    if incoming else
                    f"Waiting for {challenge['opponent_name']} to respond."
                ),
            })
    elif "owner_id" in session:
        pending_bookings = [dict(r) for r in query(
            """SELECT b.player_name, ts.slot_date, ts.slot_time
               FROM bookings b
               JOIN turf_slots ts ON ts.id = b.slot_id
               JOIN turfs t ON t.id = ts.turf_id
               WHERE t.owner_id = %s AND t.archived_at IS NULL AND b.status = 'pending'
               ORDER BY b.id DESC
               LIMIT 5""",
            (current_owner_id(),),
        )]
        for row in pending_bookings:
            items.append({
                "type": "booking_pending",
                "title": "Booking approval needed",
                "message": f"{row['player_name']} booked {row['slot_date']} at {row['slot_time']}.",
            })
    return items[:10]


def get_assistant_prompt_suggestions(profile=None):
    location = (profile or {}).get("location") or "my area"
    first_name = ((profile or {}).get("name") or "me").split(" ")[0]
    return [
        {"label": "Find arenas near me", "message": f"Find the best arenas near {location} and tell me which one to try first.", "action": "arena_nearby"},
        {"label": "Show open matches", "message": f"Show me upcoming matches that suit {first_name}.", "action": "games_overview"},
        {"label": "Explain leagues", "message": "Explain the leagues and the current standings in a simple way.", "action": "league_info"},
        {"label": "Review my profile", "message": "Review my athlete profile and tell me what I should improve.", "action": "profile_review"},
        {"label": "Find athletes", "message": f"Suggest athletes in {location} I should connect with.", "action": "community_search"},
    ]


def get_assistant_messages(user_id, limit=16):
    rows = [dict(r) for r in query(
        """SELECT role, message, created_at
           FROM ai_assistant_messages
           WHERE user_id = %s
           ORDER BY id DESC
           LIMIT %s""",
        (user_id, limit),
    )]
    return list(reversed(rows))


def build_assistant_context(user_id):
    """Collect a small user-aware context package for Goalbazi AI."""
    profile = get_profile(user_id) or {}
    user_location = profile.get("location", "")
    profile_team = profile.get("team", {})

    game_rows = [dict(r) for r in query(
        """SELECT g.id, g.title, g.game_date, g.game_time, g.format, g.skill_level, g.status,
                  t.name AS arena_name, t.area
           FROM games g
           JOIN turfs t ON t.id = g.turf_id
           WHERE t.archived_at IS NULL
           ORDER BY g.game_date ASC, g.game_time ASC
           LIMIT 4"""
    )]
    arena_rows = [dict(r) for r in query(
        """SELECT id, name, area, rating, price_per_hour, surface
           FROM turfs
           WHERE archived_at IS NULL
             AND (LOWER(name) LIKE %s OR LOWER(area) LIKE %s)
           ORDER BY rating DESC, price_per_hour ASC
           LIMIT 4""",
        (f"%{user_location.lower()}%", f"%{user_location.lower()}%"),
    )]
    if not arena_rows:
        arena_rows = [dict(r) for r in query(
            """SELECT id, name, area, rating, price_per_hour, surface
               FROM turfs
               WHERE archived_at IS NULL
               ORDER BY rating DESC, price_per_hour ASC
               LIMIT 4"""
        )]

    friend_count = query(
        """SELECT COUNT(*) AS count FROM friendships
           WHERE status = 'accepted' AND (user_one_id = %s OR user_two_id = %s)""",
        (user_id, user_id),
        one=True,
    )["count"]
    community_rows = [dict(r) for r in query(
        """SELECT u.id, u.name, u.location, u.position, u.skill,
                  t.name AS team_name
           FROM users u
           LEFT JOIN team_memberships tm ON tm.user_id = u.id
           LEFT JOIN teams t ON t.id = tm.team_id AND t.archived_at IS NULL
           WHERE u.id != %s
           ORDER BY u.id DESC
           LIMIT 5""",
        (user_id,),
    )]
    league_rows = [dict(r) for r in query(
        """SELECT l.id, l.name, l.season, COUNT(lt.id) AS team_count
           FROM leagues l
           LEFT JOIN league_teams lt ON lt.league_id = l.id
           WHERE l.archived_at IS NULL
           GROUP BY l.id
           ORDER BY l.id DESC
           LIMIT 3"""
    )]

    return {
        "profile": {
            "name": profile.get("name", ""),
            "location": profile.get("location", ""),
            "position": profile.get("position", ""),
            "preferred_format": profile.get("preferred_format", ""),
            "skill": profile.get("skill", ""),
            "team_name": profile_team.get("name", "") if profile_team else "",
        },
        "community": {
            "friend_count": friend_count,
            "athletes": community_rows,
        },
        "games": game_rows,
        "arenas": arena_rows,
        "leagues": league_rows,
    }


def build_local_assistant_reply(user_id, message, context):
    """Fallback assistant brain used when no OpenAI key is configured."""
    text = (message or "").strip()
    lowered = text.lower()
    profile = context["profile"]

    if any(word in lowered for word in ("arena", "venue", "book", "near", "turf")):
        arenas = context["arenas"][:3]
        if not arenas:
            return "I could not find arenas right now. Try again after more arenas are added."
        lines = ["Here are the best arena options I found for you:"]
        for arena in arenas:
            lines.append(
                f"- {arena['name']} in {arena['area']} · rating {arena['rating']} · Rs {arena['price_per_hour']}/hour · {arena['surface']}"
            )
        lines.append("If you want, tap the arena finder section and I can help narrow this down by area, budget, or surface.")
        return "\n".join(lines)

    if any(word in lowered for word in ("match", "game", "kickoff", "play today", "open match")):
        games = context["games"][:4]
        if not games:
            return "There are no upcoming matches listed yet. You can create one from the dashboard and invite athletes into it."
        lines = ["These are the upcoming matches I would look at first:"]
        for game in games:
            lines.append(
                f"- {game['title']} · {game['game_date']} at {game['game_time']} · {game['arena_name']} · {game['format']} · {game['skill_level']}"
            )
        lines.append("If you want, I can next help you choose the best one for your skill level and preferred format.")
        return "\n".join(lines)

    if any(word in lowered for word in ("league", "standing", "table", "team")):
        leagues = context["leagues"]
        team_name = profile.get("team_name")
        lines = []
        if team_name:
            lines.append(f"Your athlete profile is currently linked to {team_name}.")
        if leagues:
            lines.append("These are the main leagues currently visible in Goalbazi:")
            for league in leagues:
                season = f" ({league['season']})" if league.get("season") else ""
                lines.append(f"- {league['name']}{season} · {league['team_count']} teams")
            lines.append("Open the leagues section if you want the full table, team details, or edits.")
        else:
            lines.append("There are no leagues available yet.")
        return "\n".join(lines)

    if any(word in lowered for word in ("friend", "community", "athlete", "connect", "message")):
        athletes = context["community"]["athletes"][:3]
        lines = [f"You currently have {context['community']['friend_count']} confirmed connections."]
        if athletes:
            lines.append("Athletes worth checking next:")
            for athlete in athletes:
                parts = [athlete["location"], athlete["position"], athlete["skill"], athlete.get("team_name", "")]
                lines.append(f"- {athlete['name']} · " + " · ".join([p for p in parts if p]))
        lines.append("Use the Community section to send requests, chat privately, and rate athletes.")
        return "\n".join(lines)

    if any(word in lowered for word in ("profile", "improve", "bio", "rating", "skill")):
        lines = [
            f"Your profile currently shows {profile.get('position') or 'your position'} in {profile.get('location') or 'your city'}.",
            f"Preferred format: {profile.get('preferred_format') or 'not set'} · Skill level: {profile.get('skill') or 'not set'}."
        ]
        if profile.get("team_name"):
            lines.append(f"You are also linked to {profile['team_name']}.")
        lines.append("To make your athlete card stronger, keep your bio specific, update your avatar, and make sure your skill level is accurate.")
        return "\n".join(lines)

    return (
        "I can help with arenas, matches, leagues, athlete profiles, and community connections. "
        "Try one of the suggestion chips below, or ask me something specific like finding arenas near you, reviewing your profile, or recommending a match."
    )


def extract_openai_text(payload):
    if isinstance(payload, dict):
        if payload.get("output_text"):
            return str(payload["output_text"]).strip()
        for item in payload.get("output", []):
            for content in item.get("content", []):
                if isinstance(content, dict) and content.get("type") == "output_text" and content.get("text"):
                    return str(content["text"]).strip()
    return ""


def generate_assistant_reply(user_id, message):
    context = build_assistant_context(user_id)
    history = get_assistant_messages(user_id, limit=12)
    fallback_reply = build_local_assistant_reply(user_id, message, context)

    if not OPENAI_API_KEY:
        return fallback_reply

    model = OPENAI_MODEL or "gpt-4.1-mini"
    system_prompt = (
        "You are Goalbazi AI, a concise and practical football app assistant. "
        "Use the supplied Goalbazi app context to answer. Be warm, clear, and mobile-friendly. "
        "Prefer short paragraphs or flat bullets. Do not invent data beyond the provided context."
    )
    history_text = "\n".join([f"{item['role'].upper()}: {item['message']}" for item in history[-10:]])
    input_text = (
        f"App context:\n{json.dumps(context, ensure_ascii=True)}\n\n"
        f"Recent chat:\n{history_text or 'No prior messages.'}\n\n"
        f"User message:\n{message}"
    )
    body = {
        "model": model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
            {"role": "user", "content": [{"type": "input_text", "text": input_text}]},
        ],
    }
    req = Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        reply = extract_openai_text(payload)
        return reply or fallback_reply
    except (urllib_error.URLError, urllib_error.HTTPError, TimeoutError, ValueError, json.JSONDecodeError):
        return fallback_reply


def get_turfs(date_value, search="", user_lat=None, user_lng=None):
    """Return active arenas for dashboard search, including daily slot availability."""
    search = (search or "").strip().lower()
    like = f"%{search}%"
    turfs = [dict(r) for r in query(
        """SELECT * FROM turfs
           WHERE archived_at IS NULL
             AND (%s = '' OR LOWER(name) LIKE %s OR LOWER(area) LIKE %s OR LOWER(surface) LIKE %s)
           ORDER BY id DESC""",
        (search, like, like, like),
    )]
    if user_lat is not None and user_lng is not None:
        for turf in turfs:
            if turf.get("latitude") is not None and turf.get("longitude") is not None:
                turf["nearby_distance_km"] = round(
                    haversine_km(float(user_lat), float(user_lng), float(turf["latitude"]), float(turf["longitude"])),
                    1,
                )
            else:
                turf["nearby_distance_km"] = None
        turfs.sort(key=lambda turf: turf["nearby_distance_km"] if turf["nearby_distance_km"] is not None else turf["distance_km"])
    for turf in turfs:
        slots = query(
            "SELECT id, slot_time, is_booked, status FROM turf_slots WHERE turf_id = %s AND slot_date = %s ORDER BY slot_time",
            (turf["id"], date_value),
        )
        turf["slots"] = [dict(s) for s in slots]
    return turfs


def get_game_detail(game_id):
    game = query(
        """
        SELECT g.*, t.name AS location,
               CASE g.format WHEN '11v11' THEN 11 WHEN '7v7' THEN 7 ELSE 5 END AS players_per_team
        FROM games g JOIN turfs t ON t.id = g.turf_id
        WHERE g.id = %s AND t.archived_at IS NULL
        """,
        (game_id,), one=True,
    )
    if not game:
        raise KeyError("Game not found")
    game_dict = dict(game)
    players = [dict(r) for r in query(
        "SELECT player_name, player_role, team_name, is_captain, confirmed, user_id FROM game_players WHERE game_id = %s ORDER BY team_name, is_captain DESC, id ASC",
        (game_id,),
    )]
    messages = [dict(r) for r in query(
        "SELECT sender_name, message, is_system, created_at FROM game_messages WHERE game_id = %s ORDER BY id ASC",
        (game_id,),
    )]
    game_dict["players"] = players
    game_dict["messages"] = messages
    game_dict["confirmed_players"] = sum(1 for p in players if p["confirmed"])
    return game_dict


def get_games():
    """Return public games linked to active arenas only."""
    rows = query(
        """SELECT g.id
           FROM games g
           JOIN turfs t ON t.id = g.turf_id
           WHERE t.archived_at IS NULL
           ORDER BY g.game_date ASC, g.game_time ASC"""
    )
    return [get_game_detail(row["id"]) for row in rows]


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/login")
def login_page():
    return send_from_directory(".", "login.html")


@app.route("/register")
def register_page():
    return send_from_directory(".", "register.html")


@app.route("/forgot-password")
def forgot_password_page():
    return send_from_directory(".", "forgot_password.html")


@app.route("/reset-password/<token>")
def reset_password_page(token):
    return send_from_directory(".", "reset_password.html")


@app.route("/support")
def support_page():
    return send_from_directory(".", "support.html")


@app.route("/api/auth/register", methods=["POST"])
def api_register():
    data = request.get_json()
    name = data.get("name", "").strip()
    handle = sanitize_handle(data.get("handle", ""))
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    location = data.get("location", "Delhi NCR").strip()

    if not all([name, handle, email, password]):
        return jsonify({"error": "All fields are required"}), 400
    if len(handle) < 3:
        return jsonify({"error": "Username must have at least 3 lowercase letters or numbers"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400

    existing = query("SELECT id FROM users WHERE email = %s OR handle = %s", (email, handle), one=True)
    if existing:
        return jsonify({"error": "Email or handle already taken"}), 409

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (name, handle, email, password_hash, location) VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (name, handle, email, hash_password(password), location),
    )
    user_id = cur.fetchone()["id"]
    conn.commit()
    session.permanent = True
    session["user_id"] = user_id
    log_event("auth_register", "/api/auth/register", {"user_id": user_id})
    return jsonify({"ok": True}), 201


@app.route("/api/auth/login", methods=["POST"])
def api_login():
    data = request.get_json()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    user = query("SELECT id, password_hash FROM users WHERE email = %s", (email,), one=True)
    if not user or not verify_password(password, user["password_hash"]):
        return jsonify({"error": "Invalid email or password"}), 401
    session.permanent = True
    session["user_id"] = user["id"]
    log_event("auth_login", "/api/auth/login", {"user_id": user["id"]})
    return jsonify({"ok": True})


@app.route("/api/auth/forgot-password", methods=["POST"])
def api_forgot_password():
    """Create a one-hour reset token without revealing whether the email exists."""
    data = request.get_json() or {}
    email = data.get("email", "").strip().lower()
    response = {
        "ok": True,
        "message": "If that email exists on Goalbazi, password reset instructions are ready.",
        "email_sent": False,
        "support_email": SUPPORT_EMAIL,
    }
    if not email:
        return jsonify(response)

    user = query("SELECT id, name FROM users WHERE email = %s", (email,), one=True)
    if not user:
        return jsonify(response)

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    expires_at = (datetime.now() + timedelta(hours=1)).isoformat()
    query(
        """INSERT INTO password_reset_tokens (user_id, token_hash, expires_at, created_at)
           VALUES (%s, %s, %s, %s)""",
        (user["id"], token_hash, expires_at, datetime.now().isoformat()),
        commit=True,
    )
    reset_url = public_url(f"/reset-password/{raw_token}")
    email_sent = send_email(
        email,
        "Reset your Goalbazi password",
        f"Hi {user['name']},\n\nUse this secure link to reset your Goalbazi password. It expires in 1 hour:\n{reset_url}\n\nIf you did not request this, you can ignore this email.\n\nGoalbazi",
    )
    response["email_sent"] = email_sent
    if SHOW_PASSWORD_RESET_LINK and not email_sent:
        # Dev/pre-launch escape hatch only; keep disabled in production unless testing.
        response["reset_url"] = reset_url
    log_event("forgot_password_request", "/api/auth/forgot-password", {"user_id": user["id"], "email_sent": email_sent})
    return jsonify(response)


@app.route("/api/auth/reset-password", methods=["POST"])
def api_reset_password():
    """Validate a reset token, set the new password, and mark the token as used."""
    data = request.get_json() or {}
    token = data.get("token", "").strip()
    password = data.get("password", "")
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    reset = query(
        """SELECT * FROM password_reset_tokens
           WHERE token_hash = %s AND used_at IS NULL
           ORDER BY id DESC""",
        (token_hash,),
        one=True,
    )
    if not reset:
        return jsonify({"error": "Reset link is invalid or already used"}), 400
    try:
        expires_at = datetime.fromisoformat(reset["expires_at"])
    except Exception:
        expires_at = datetime.now() - timedelta(seconds=1)
    if expires_at < datetime.now():
        return jsonify({"error": "Reset link has expired"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET password_hash = %s WHERE id = %s", (hash_password(password), reset["user_id"]))
    cur.execute("UPDATE password_reset_tokens SET used_at = %s WHERE id = %s", (datetime.now().isoformat(), reset["id"]))
    conn.commit()
    session.permanent = True
    session["user_id"] = reset["user_id"]
    log_event("password_reset_complete", "/api/auth/reset-password", {"user_id": reset["user_id"]})
    return jsonify({"ok": True})


@app.route("/auth/google")
def google_login():
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return redirect("/login?error=google_not_configured")
    state = secrets.token_urlsafe(24)
    session["google_oauth_state"] = state
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": google_redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    }
    return redirect("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params))


@app.route("/auth/google/callback")
def google_callback():
    if request.args.get("error"):
        return redirect("/login?error=google_cancelled")
    if request.args.get("state") != session.pop("google_oauth_state", None):
        return redirect("/login?error=google_state")
    code = request.args.get("code", "")
    if not code:
        return redirect("/login?error=google_missing_code")
    try:
        token_data = exchange_google_code(code)
        profile = fetch_google_profile(token_data["access_token"])
    except Exception:
        return redirect("/login?error=google_failed")

    email = (profile.get("email") or "").strip().lower()
    google_id = profile.get("sub", "").strip()
    name = (profile.get("name") or email.split("@")[0] or "Goalbazi Athlete").strip()
    avatar_url = profile.get("picture", "").strip()
    if not email or not google_id:
        return redirect("/login?error=google_profile")

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE google_id = %s OR email = %s", (google_id, email))
    user = cur.fetchone()
    if user:
        user_id = user["id"]
        cur.execute(
            """UPDATE users
               SET google_id = %s, auth_provider = 'google',
                   avatar_base64 = CASE WHEN COALESCE(avatar_base64, '') = '' THEN %s ELSE avatar_base64 END
               WHERE id = %s""",
            (google_id, avatar_url, user_id),
        )
    else:
        handle = unique_handle_from_email_name(email, name)
        cur.execute(
            """INSERT INTO users
               (name, handle, email, password_hash, location, avatar_base64, google_id, auth_provider)
               VALUES (%s,%s,%s,%s,%s,%s,%s,'google') RETURNING id""",
            (name, handle, email, hash_password(secrets.token_urlsafe(32)), "Delhi NCR", avatar_url, google_id),
        )
        user_id = cur.fetchone()["id"]
    conn.commit()
    session.clear()
    session.permanent = True
    session["user_id"] = user_id
    log_event("google_login", "/auth/google/callback", {"user_id": user_id})
    return redirect("/dashboard")


@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/support", methods=["POST"])
def api_support_request():
    """Collect support and bug reports even from users who cannot log in."""
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    email = data.get("email", "").strip().lower()
    category = data.get("category", "support").strip() or "support"
    message = data.get("message", "").strip()
    if len(message) < 8:
        return jsonify({"error": "Please describe the issue in a little more detail."}), 400
    user_id = current_user_id()
    query(
        """INSERT INTO support_requests (name, email, category, message, user_id, created_at)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (name, email, category, message, user_id, datetime.now().isoformat()),
        commit=True,
    )
    send_email(
        SUPPORT_EMAIL,
        f"Goalbazi {category.title()} request",
        f"Name: {name or 'Not provided'}\nEmail: {email or 'Not provided'}\nUser ID: {user_id or 'guest'}\n\n{message}",
    )
    log_event("support_request", "/api/support", {"category": category, "user_id": user_id})
    return jsonify({"ok": True, "message": "Thanks. Goalbazi support has received your request."})


@app.route("/api/auth/me")
@login_required
def api_me():
    return jsonify(get_profile(current_user_id()))


# ---------------------------------------------------------------------------
# App routes
# ---------------------------------------------------------------------------

# / route now handled by multi-page routes below


@app.route("/app.js")
def serve_js():
    return send_from_directory(".", "app.js")


@app.route("/styles.css")
def serve_css():
    return send_from_directory(".", "styles.css")


@app.route("/manifest.webmanifest")
def serve_manifest():
    return send_from_directory(".", "manifest.webmanifest", mimetype="application/manifest+json")


@app.route("/service-worker.js")
def serve_service_worker():
    return send_from_directory(".", "service-worker.js", mimetype="application/javascript")


@app.route("/assets/<path:filename>")
def serve_assets(filename):
    return send_from_directory("assets", filename)


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.route("/api/dashboard")
@login_required
def api_dashboard():
    """Main athlete dashboard payload: profile, games, arenas, leagues, community, notifications."""
    date_value = request.args.get("date", datetime.now().date().isoformat())
    search = request.args.get("search", "")
    user_lat = request.args.get("user_lat", type=float)
    user_lng = request.args.get("user_lng", type=float)
    leagues, standings = get_leagues_with_teams()
    friend_count = query(
        """SELECT COUNT(*) AS count FROM friendships
           WHERE status = 'accepted' AND (user_one_id = %s OR user_two_id = %s)""",
        (current_user_id(), current_user_id()),
        one=True,
    )["count"]
    unread_messages = query(
        "SELECT COUNT(*) AS count FROM direct_messages WHERE receiver_id = %s",
        (current_user_id(),),
        one=True,
    )["count"]
    # Team hub needs the logged-in athlete's team before building the dashboard JSON.
    my_team = get_user_team(current_user_id())
    return jsonify({
        "profile": get_profile(current_user_id()),
        "founder_badge": get_founder_badge(current_user_id()),
        "stats": get_stats(),
        "games": get_games(),
        "turfs": get_turfs(date_value, search, user_lat, user_lng),
        "leagues": leagues,
        "standings": standings,
        "team_hub": {
            "my_team": my_team,
            "can_manage_team": bool(my_team and user_can_manage_team(current_user_id(), my_team["id"])),
            "opponents": get_challenge_opponents(current_user_id()),
            "challenges": get_team_challenges_for_user(current_user_id()),
        },
        "community": {
            "friend_count": friend_count,
            "message_count": unread_messages,
        },
        "notifications": get_notifications(),
    })


@app.route("/api/profile", methods=["PUT"])
@login_required
def api_profile_update():
    data = request.get_json() or {}
    current_profile = get_profile(current_user_id()) or {}
    if "avatar_base64" not in data:
        data["avatar_base64"] = current_profile.get("avatar_base64", "")
    payload = normalize_profile_request(data)
    handle = payload["handle"]
    if not handle:
        return jsonify({"error": "Username can only contain lowercase letters and numbers"}), 400
    existing = query(
        "SELECT id FROM users WHERE handle = %s AND id != %s",
        (handle, current_user_id()),
        one=True,
    )
    if existing:
        return jsonify({"error": "Username already taken"}), 409
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """UPDATE profile_assessments
           SET status = 'superseded', reviewed_at = %s
           WHERE user_id = %s AND status = 'pending'""",
        (datetime.now().isoformat(), current_user_id()),
    )
    cur.execute(
        """INSERT INTO profile_assessments
           (user_id, requested_payload, status, created_at)
           VALUES (%s,%s,'pending',%s) RETURNING id""",
        (current_user_id(), json.dumps(payload), datetime.now().isoformat()),
    )
    assessment_id = cur.fetchone()["id"]
    conn.commit()
    log_event("profile_assessment_request", "/api/profile", {"assessment_id": assessment_id})
    return jsonify({
        "ok": True,
        "message": "Your profile is under assessment.",
        "profile": get_profile(current_user_id()),
    })


@app.route("/api/games", methods=["POST"])
@login_required
def api_create_game():
    """Create a match and auto-add the creator as confirmed organizer."""
    data = request.get_json()
    turf_id = int(data["turf_id"])
    if not query("SELECT id FROM turfs WHERE id = %s AND archived_at IS NULL", (turf_id,), one=True):
        return jsonify({"error": "Arena not found"}), 404
    kickoff = datetime.fromisoformat(f"{data['date']}T{data['time']}:00").isoformat()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO games (title, format, skill_level, visibility, game_date, game_time, kickoff_at, turf_id, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (data["title"], data["format"], data["skill"], data["visibility"], data["date"], data["time"], kickoff, turf_id, current_user_id()),
    )
    game_id = cur.fetchone()["id"]
    user = get_profile(current_user_id())
    cur.execute(
        "INSERT INTO game_players (game_id, user_id, player_name, player_role, team_name, is_captain, confirmed) VALUES (%s,%s,%s,'Organizer','A',1,1)",
        (game_id, current_user_id(), user["name"]),
    )
    cur.execute(
        "INSERT INTO game_messages (game_id, sender_name, message, is_system, created_at) VALUES (%s,'System',%s,1,%s)",
        (game_id, f"{user['name']} created the game", datetime.now().isoformat()),
    )
    conn.commit()
    log_event("game_create", "/api/games", {"game_id": game_id})
    return jsonify({"id": game_id}), 201


@app.route("/api/games/<int:game_id>")
@login_required
def api_game_detail(game_id):
    try:
        return jsonify(get_game_detail(game_id))
    except KeyError:
        return jsonify({"error": "Not found"}), 404


@app.route("/api/games/<int:game_id>/messages", methods=["POST"])
@login_required
def api_post_message(game_id):
    data = request.get_json()
    user = get_profile(current_user_id())
    query(
        "INSERT INTO game_messages (game_id, sender_name, message, is_system, created_at) VALUES (%s,%s,%s,0,%s)",
        (game_id, user["name"], data["message"], datetime.now().isoformat()),
        commit=True,
    )
    log_event("game_message", f"/api/games/{game_id}/messages", {"game_id": game_id})
    return jsonify({"ok": True}), 201


@app.route("/api/games/<int:game_id>/attendance", methods=["POST"])
@login_required
def api_confirm_attendance(game_id):
    user = get_profile(current_user_id())
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM game_players WHERE game_id = %s AND user_id = %s", (game_id, current_user_id()))
    existing = cur.fetchone()
    if existing:
        cur.execute("UPDATE game_players SET confirmed = 1 WHERE id = %s", (existing["id"],))
    else:
        cur.execute("SELECT COUNT(*) FROM game_players WHERE game_id = %s AND team_name = 'A'", (game_id,))
        a_count = cur.fetchone()["count"]
        cur.execute("SELECT COUNT(*) FROM game_players WHERE game_id = %s AND team_name = 'B'", (game_id,))
        b_count = cur.fetchone()["count"]
        team_name = "A" if a_count <= b_count else "B"
        cur.execute(
            "INSERT INTO game_players (game_id, user_id, player_name, player_role, team_name, is_captain, confirmed) VALUES (%s,%s,%s,%s,%s,0,1)",
            (game_id, current_user_id(), user["name"], user["position"], team_name),
        )
    cur.execute(
        "INSERT INTO game_messages (game_id, sender_name, message, is_system, created_at) VALUES (%s,'System',%s,1,%s)",
        (game_id, f"{user['name']} confirmed attendance", datetime.now().isoformat()),
    )
    conn.commit()
    log_event("game_attendance_confirm", f"/api/games/{game_id}/attendance", {"game_id": game_id})
    return jsonify({"ok": True}), 201


@app.route("/api/games/<int:game_id>/attendance", methods=["DELETE"])
@login_required
def api_leave_game(game_id):
    user = get_profile(current_user_id())
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM game_players WHERE game_id = %s AND user_id = %s", (game_id, current_user_id()))
    cur.execute(
        "INSERT INTO game_messages (game_id, sender_name, message, is_system, created_at) VALUES (%s,'System',%s,1,%s)",
        (game_id, f"{user['name']} left the game", datetime.now().isoformat()),
    )
    conn.commit()
    log_event("game_attendance_leave", f"/api/games/{game_id}/attendance", {"game_id": game_id})
    return jsonify({"ok": True})


@app.route("/api/bookings/<int:slot_id>", methods=["POST"])
@login_required
def api_book_slot(slot_id):
    data = request.get_json() or {}
    utr_number = data.get("utr_number", "").strip()
    amount = data.get("amount", 0)
    user = get_profile(current_user_id())
    conn = get_db()
    cur = conn.cursor()
    slot = query("SELECT * FROM turf_slots WHERE id = %s", (slot_id,), one=True)
    if not slot:
        return jsonify({"error": "Slot not found"}), 404
    if slot["is_booked"] or slot["status"] != "available":
        return jsonify({"error": "This slot is no longer available"}), 409
    cur.execute(
        "UPDATE turf_slots SET is_booked = 1, status = 'pending', booked_by = %s WHERE id = %s",
        (current_user_id(), slot_id),
    )
    cur.execute(
        """INSERT INTO bookings (slot_id, user_id, player_name, player_email, utr_number, amount, status)
           VALUES (%s, %s, %s, %s, %s, %s, 'pending') RETURNING id""",
        (slot_id, current_user_id(), user["name"], user["email"], utr_number, amount),
    )
    conn.commit()
    log_event("turf_booking", f"/api/bookings/{slot_id}", {"slot_id": slot_id, "amount": amount})
    return jsonify({"ok": True}), 201






# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Unauthorized"}), 401
        if not current_user_is_admin():
            return jsonify({"error": "Forbidden"}), 403
        return f(*args, **kwargs)
    return decorated


@app.route("/admin")
@login_required
def admin_page():
    if not current_user_is_admin():
        return redirect("/dashboard")
    return send_from_directory(".", "admin.html")


@app.route("/api/admin/overview")
@admin_required
def api_admin_overview():
    active_since = (datetime.now() - timedelta(minutes=15)).isoformat()
    stats = [
        {"value": query("SELECT COUNT(*) FROM users", one=True)["count"], "label": "Total users"},
        {"value": query("SELECT COUNT(*) FROM users WHERE last_seen_at >= %s", (active_since,), one=True)["count"], "label": "Active now"},
        {"value": query("SELECT COUNT(*) FROM profile_assessments WHERE status = 'pending'", one=True)["count"], "label": "Pending approvals"},
        {"value": query("SELECT COUNT(*) FROM games g JOIN turfs t ON t.id = g.turf_id WHERE t.archived_at IS NULL", one=True)["count"], "label": "Total games"},
        {"value": query("SELECT COUNT(*) FROM turf_slots ts JOIN turfs t ON t.id = ts.turf_id WHERE ts.is_booked = 1 AND t.archived_at IS NULL", one=True)["count"], "label": "Bookings"},
        {"value": query("SELECT COUNT(*) FROM game_messages WHERE is_system = 0", one=True)["count"], "label": "Chat messages"},
        {"value": query("SELECT COUNT(*) FROM analytics_events WHERE event_type = 'page_view'", one=True)["count"], "label": "Page views"},
        {"value": query("SELECT COUNT(*) FROM analytics_events WHERE event_type != 'page_view'", one=True)["count"], "label": "Tracked actions"},
    ]
    recent_users = [dict(r) for r in query(
        "SELECT id, name, handle, email, location FROM users ORDER BY id DESC LIMIT 5"
    )]
    recent_games = [dict(r) for r in query(
        """SELECT g.id, g.title, g.format, g.game_date, g.status,
           COUNT(gp.id) as player_count
           FROM games g
           JOIN turfs t ON t.id = g.turf_id
           LEFT JOIN game_players gp ON gp.game_id = g.id
           WHERE t.archived_at IS NULL
           GROUP BY g.id ORDER BY g.id DESC LIMIT 5"""
    )]
    analytics = {
        "top_pages": [dict(r) for r in query(
            """SELECT path, COUNT(*) AS visits
               FROM analytics_events
               WHERE event_type = 'page_view'
               GROUP BY path
               ORDER BY visits DESC
               LIMIT 8"""
        )],
        "top_actions": [dict(r) for r in query(
            """SELECT event_type, COUNT(*) AS total
               FROM analytics_events
               WHERE event_type != 'page_view'
               GROUP BY event_type
               ORDER BY total DESC
               LIMIT 8"""
        )],
    }
    recent_activity = admin_activity_rows(limit=10)
    return jsonify({"stats": stats, "recent_users": recent_users, "recent_games": recent_games, "analytics": analytics, "recent_activity": recent_activity})


def admin_activity_rows(limit: int = 80) -> list[dict]:
    """Return recent admin/system activity with actor names for the admin command center."""
    rows = [dict(r) for r in query(
        """SELECT ae.id, ae.event_type, ae.path, ae.user_id, ae.owner_id, ae.meta, ae.created_at,
                  u.name AS user_name, u.handle AS user_handle, o.name AS owner_name
           FROM analytics_events ae
           LEFT JOIN users u ON u.id = ae.user_id
           LEFT JOIN turf_owners o ON o.id = ae.owner_id
           WHERE ae.event_type != 'page_view'
           ORDER BY ae.id DESC
           LIMIT %s""",
        (limit,),
    )]
    for row in rows:
        row["meta"] = parse_event_meta(row)
        row["actor"] = row.get("user_name") or row.get("owner_name") or "System"
    return rows


@app.route("/api/admin/activity")
@admin_required
def api_admin_activity():
    return jsonify({"activity": admin_activity_rows(limit=120)})


@app.route("/api/admin/search")
@admin_required
def api_admin_search():
    """One admin search across athletes, teams, leagues, and arenas."""
    term = (request.args.get("q") or "").strip()
    if len(term) < 2:
        return jsonify({"results": []})
    like = f"%{term}%"
    results = []
    for row in query(
        """SELECT id, name, handle AS meta, location AS subtext, 'athlete' AS type
           FROM users
           WHERE name ILIKE %s OR handle ILIKE %s OR email ILIKE %s OR location ILIKE %s
           ORDER BY name ASC LIMIT 10""",
        (like, like, like, like),
    ):
        results.append(dict(row))
    for row in query(
        """SELECT id, name, city AS meta, skill_level AS subtext, 'team' AS type
           FROM teams
           WHERE archived_at IS NULL AND (name ILIKE %s OR city ILIKE %s OR short_name ILIKE %s)
           ORDER BY name ASC LIMIT 10""",
        (like, like, like),
    ):
        results.append(dict(row))
    for row in query(
        """SELECT id, name, city AS meta, status AS subtext, 'league' AS type
           FROM leagues
           WHERE archived_at IS NULL AND (name ILIKE %s OR city ILIKE %s OR status ILIKE %s)
           ORDER BY name ASC LIMIT 10""",
        (like, like, like),
    ):
        results.append(dict(row))
    for row in query(
        """SELECT id, name, area AS meta, surface AS subtext, 'arena' AS type
           FROM turfs
           WHERE archived_at IS NULL AND (name ILIKE %s OR area ILIKE %s OR surface ILIKE %s)
           ORDER BY name ASC LIMIT 10""",
        (like, like, like),
    ):
        results.append(dict(row))
    return jsonify({"results": results[:30]})


@app.route("/api/admin/users")
@admin_required
def api_admin_users():
    users = [dict(r) for r in query(
        """SELECT u.id, u.name, u.handle, u.email, u.location, u.position, u.secondary_position,
                  u.skill, u.preferred_format, u.strong_foot, u.availability,
                  u.pace, u.shooting, u.passing, u.dribbling, u.defending, u.physical,
                  u.bio, u.avatar_base64, u.is_admin, u.last_seen_at, u.admin_note, t.name AS team_name
           FROM users u
           LEFT JOIN team_memberships tm ON tm.user_id = u.id
           LEFT JOIN teams t ON t.id = tm.team_id AND t.archived_at IS NULL
           ORDER BY u.id DESC"""
    )]
    for user in users:
        # Admin can quickly see which athletes are active without opening each profile.
        user["activity_status"] = activity_status(user.get("last_seen_at"))
    return jsonify({"users": users})


@app.route("/api/admin/profile-assessments")
@admin_required
def api_admin_profile_assessments():
    return jsonify({
        "pending": get_profile_assessments("pending", 80),
        "reviewed": get_profile_assessments("approved", 10) + get_profile_assessments("rejected", 10),
    })


@app.route("/api/admin/users", methods=["POST"])
@admin_required
def api_admin_create_user():
    data = request.get_json()
    name = data.get("name", "").strip()
    handle = sanitize_handle(data.get("handle", ""))
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    location = data.get("location", "Delhi NCR").strip()
    if not all([name, handle, email, password]):
        return jsonify({"error": "All player fields are required"}), 400
    existing = query("SELECT id FROM users WHERE email = %s OR handle = %s", (email, handle), one=True)
    if existing:
        return jsonify({"error": "Email or username already taken"}), 409
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO users
           (name, handle, email, password_hash, location, position, preferred_format, skill, bio)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (name, handle, email, hash_password(password), location, "Midfielder", "5v5", "Intermediate", ""),
    )
    user_id = cur.fetchone()["id"]
    conn.commit()
    log_event("admin_create_user", "/api/admin/users", {"user_id": user_id})
    return jsonify({"ok": True, "id": user_id}), 201


@app.route("/api/admin/users/<int:user_id>", methods=["PUT"])
@admin_required
def api_admin_update_user(user_id):
    data = request.get_json() or {}
    handle = sanitize_handle(data.get("handle", ""))
    existing = query(
        "SELECT id FROM users WHERE handle = %s AND id != %s",
        (handle, user_id),
        one=True,
    )
    if existing:
        return jsonify({"error": "Username already taken"}), 409
    query(
        """UPDATE users
           SET name = %s, handle = %s, email = %s, location = %s, position = %s,
               secondary_position = %s, preferred_format = %s, skill = %s, strong_foot = %s,
               availability = %s, pace = %s, shooting = %s, passing = %s, dribbling = %s,
               defending = %s, physical = %s, bio = %s, avatar_base64 = %s, is_admin = %s,
               admin_note = %s
           WHERE id = %s""",
        (
            data.get("name", "").strip(),
            handle,
            data.get("email", "").strip().lower(),
            data.get("location", "").strip(),
            data.get("position", "Midfielder").strip(),
            data.get("secondary_position", "").strip(),
            data.get("preferred_format", "5v5").strip(),
            data.get("skill", "Intermediate").strip(),
            data.get("strong_foot", "").strip(),
            data.get("availability", "").strip(),
            clamp_profile_score(data.get("pace", 50)),
            clamp_profile_score(data.get("shooting", 50)),
            clamp_profile_score(data.get("passing", 50)),
            clamp_profile_score(data.get("dribbling", 50)),
            clamp_profile_score(data.get("defending", 50)),
            clamp_profile_score(data.get("physical", 50)),
            data.get("bio", ""),
            data.get("avatar_base64", ""),
            bool(data.get("is_admin")),
            str(data.get("admin_note", "") or "").strip()[:1000],
            user_id,
        ),
        commit=True,
    )
    log_event("admin_update_user", f"/api/admin/users/{user_id}", {"user_id": user_id})
    return jsonify({"ok": True})


@app.route("/api/admin/profile-assessments/<int:assessment_id>/approve", methods=["POST"])
@admin_required
def api_admin_approve_profile_assessment(assessment_id):
    assessment = query(
        "SELECT * FROM profile_assessments WHERE id = %s AND status = 'pending'",
        (assessment_id,),
        one=True,
    )
    if not assessment:
        return jsonify({"error": "Assessment request not found"}), 404
    try:
        payload = json.loads(assessment.get("requested_payload") or "{}")
    except Exception:
        payload = {}
    payload = normalize_profile_request(payload)
    handle = payload["handle"]
    existing = query(
        "SELECT id FROM users WHERE handle = %s AND id != %s",
        (handle, assessment["user_id"]),
        one=True,
    )
    if existing:
        return jsonify({"error": "Username already taken by another athlete"}), 409
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """UPDATE users
           SET name = %s, handle = %s, location = %s, position = %s,
               secondary_position = %s, preferred_format = %s, skill = %s,
               strong_foot = %s, availability = %s, bio = %s, avatar_base64 = %s,
               pace = %s, shooting = %s, passing = %s, dribbling = %s,
               defending = %s, physical = %s
           WHERE id = %s""",
        (
            payload["name"],
            handle,
            payload["location"],
            payload["position"],
            payload["secondary_position"],
            payload["preferred_format"],
            payload["skill"],
            payload["strong_foot"],
            payload["availability"],
            payload["bio"],
            payload["avatar_base64"],
            payload["pace"],
            payload["shooting"],
            payload["passing"],
            payload["dribbling"],
            payload["defending"],
            payload["physical"],
            assessment["user_id"],
        ),
    )
    cur.execute(
        """UPDATE profile_assessments
           SET status = 'approved', reviewed_by = %s, reviewed_at = %s
           WHERE id = %s""",
        (current_user_id(), datetime.now().isoformat(), assessment_id),
    )
    conn.commit()
    log_event("admin_profile_assessment_approve", f"/api/admin/profile-assessments/{assessment_id}/approve", {"assessment_id": assessment_id})
    return jsonify({"ok": True})


@app.route("/api/admin/profile-assessments/<int:assessment_id>/reject", methods=["POST"])
@admin_required
def api_admin_reject_profile_assessment(assessment_id):
    data = request.get_json() or {}
    assessment = query(
        "SELECT id FROM profile_assessments WHERE id = %s AND status = 'pending'",
        (assessment_id,),
        one=True,
    )
    if not assessment:
        return jsonify({"error": "Assessment request not found"}), 404
    query(
        """UPDATE profile_assessments
           SET status = 'rejected', admin_note = %s, reviewed_by = %s, reviewed_at = %s
           WHERE id = %s AND status = 'pending'""",
        (
            str(data.get("admin_note", "") or "").strip()[:300],
            current_user_id(),
            datetime.now().isoformat(),
            assessment_id,
        ),
        commit=True,
    )
    log_event("admin_profile_assessment_reject", f"/api/admin/profile-assessments/{assessment_id}/reject", {"assessment_id": assessment_id})
    return jsonify({"ok": True})


@app.route("/api/admin/users/<int:user_id>", methods=["DELETE"])
@admin_required
def api_admin_delete_user(user_id):
    if user_id == current_user_id():
        return jsonify({"error": "Cannot delete yourself"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM player_ratings WHERE rater_id = %s OR rated_id = %s", (user_id, user_id))
    cur.execute("DELETE FROM player_open_ratings WHERE rater_id = %s OR rated_id = %s", (user_id, user_id))
    cur.execute("DELETE FROM bookings WHERE user_id = %s", (user_id,))
    cur.execute("DELETE FROM friendships WHERE user_one_id = %s OR user_two_id = %s", (user_id, user_id))
    cur.execute("DELETE FROM direct_messages WHERE sender_id = %s OR receiver_id = %s", (user_id, user_id))
    cur.execute("DELETE FROM ai_assistant_messages WHERE user_id = %s", (user_id,))
    cur.execute("DELETE FROM team_memberships WHERE user_id = %s", (user_id,))
    cur.execute("DELETE FROM game_players WHERE user_id = %s", (user_id,))
    cur.execute("UPDATE turf_slots SET is_booked = 0, booked_by = NULL WHERE booked_by = %s", (user_id,))
    cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
    conn.commit()
    log_event("admin_delete_user", f"/api/admin/users/{user_id}", {"user_id": user_id})
    return jsonify({"ok": True})


@app.route("/api/admin/games")
@admin_required
def api_admin_games():
    games = [dict(r) for r in query(
        """SELECT g.id, g.title, g.format, g.skill_level, g.game_date, g.game_time, g.status, g.turf_id, g.created_by,
           COUNT(gp.id) as player_count
           FROM games g
           JOIN turfs t ON t.id = g.turf_id
           LEFT JOIN game_players gp ON gp.game_id = g.id
           WHERE t.archived_at IS NULL
           GROUP BY g.id ORDER BY g.id DESC"""
    )]
    return jsonify({"games": games})


@app.route("/api/admin/games", methods=["POST"])
@admin_required
def api_admin_create_game():
    data = request.get_json()
    title = data.get("title", "").strip()
    turf_id = int(data.get("turf_id", 0))
    created_by = int(data.get("created_by") or current_user_id())
    if not title or not turf_id:
        return jsonify({"error": "Title and turf are required"}), 400
    if not query("SELECT id FROM turfs WHERE id = %s AND archived_at IS NULL", (turf_id,), one=True):
        return jsonify({"error": "Arena not found"}), 404
    kickoff = datetime.fromisoformat(f"{data['date']}T{data['time']}:00").isoformat()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO games (title, format, skill_level, visibility, game_date, game_time, kickoff_at, turf_id, created_by)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (
            title,
            data.get("format", "5v5"),
            data.get("skill", "Intermediate"),
            data.get("visibility", "Public"),
            data["date"],
            data["time"],
            kickoff,
            turf_id,
            created_by,
        ),
    )
    game_id = cur.fetchone()["id"]
    creator = get_profile(created_by)
    cur.execute(
        """INSERT INTO game_players (game_id, user_id, player_name, player_role, team_name, is_captain, confirmed)
           VALUES (%s,%s,%s,'Organizer','A',1,1)""",
        (game_id, created_by, creator["name"]),
    )
    cur.execute(
        "INSERT INTO game_messages (game_id, sender_name, message, is_system, created_at) VALUES (%s,'System',%s,1,%s)",
        (game_id, f"{creator['name']} created the game", datetime.now().isoformat()),
    )
    conn.commit()
    log_event("admin_create_game", "/api/admin/games", {"game_id": game_id})
    return jsonify({"ok": True, "id": game_id}), 201


@app.route("/api/admin/games/<int:game_id>", methods=["DELETE"])
@admin_required
def api_admin_delete_game(game_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM player_ratings WHERE game_id = %s", (game_id,))
    cur.execute("DELETE FROM game_players WHERE game_id = %s", (game_id,))
    cur.execute("DELETE FROM game_messages WHERE game_id = %s", (game_id,))
    cur.execute("DELETE FROM games WHERE id = %s", (game_id,))
    conn.commit()
    log_event("admin_delete_game", f"/api/admin/games/{game_id}", {"game_id": game_id})
    return jsonify({"ok": True})


@app.route("/api/admin/turfs")
@admin_required
def api_admin_turfs():
    """Admin arena manager data, split into active and archived arenas."""
    turfs = [dict(r) for r in query("SELECT * FROM turfs WHERE archived_at IS NULL ORDER BY id ASC")]
    for turf in turfs:
        turf["image_urls"] = parse_image_urls(turf.get("image_urls"))
    archived_turfs = [dict(r) for r in query("SELECT * FROM turfs WHERE archived_at IS NOT NULL ORDER BY archived_at DESC, id DESC")]
    for turf in archived_turfs:
        turf["image_urls"] = parse_image_urls(turf.get("image_urls"))
    owners = [dict(r) for r in query("SELECT id, name FROM turf_owners ORDER BY name ASC")]
    players = [dict(r) for r in query("SELECT id, name FROM users ORDER BY name ASC LIMIT 200")]
    return jsonify({"turfs": turfs, "archived_turfs": archived_turfs, "owners": owners, "players": players})


@app.route("/api/admin/bookings")
@admin_required
def api_admin_bookings():
    bookings = [dict(r) for r in query(
        """SELECT ts.id, t.name as turf_name, ts.slot_date, ts.slot_time,
           u.name as booked_by_name
           FROM turf_slots ts
           JOIN turfs t ON t.id = ts.turf_id
           LEFT JOIN users u ON u.id = ts.booked_by
           WHERE ts.is_booked = 1 AND t.archived_at IS NULL
           ORDER BY ts.slot_date DESC, ts.slot_time DESC"""
    )]
    return jsonify({"bookings": bookings})


@app.route("/api/admin/messages")
@admin_required
def api_admin_messages():
    messages = [dict(r) for r in query(
        """SELECT gm.id, g.title as game_title, gm.sender_name, gm.message, gm.is_system, gm.created_at
           FROM game_messages gm
           JOIN games g ON g.id = gm.game_id
           ORDER BY gm.id DESC LIMIT 100"""
    )]
    return jsonify({"messages": messages})


@app.route("/api/admin/ratings")
@admin_required
def api_admin_ratings():
    ratings = [dict(r) for r in query(
        """SELECT pr.id, 'game' AS rating_type, pr.rating, pr.created_at,
                  rr.name AS rater_name, ru.name AS rated_name, g.title AS context_label
           FROM player_ratings pr
           JOIN users rr ON rr.id = pr.rater_id
           JOIN users ru ON ru.id = pr.rated_id
           LEFT JOIN games g ON g.id = pr.game_id
           UNION ALL
           SELECT por.id, 'open' AS rating_type, por.rating, por.created_at,
                  rr.name AS rater_name, ru.name AS rated_name, 'Open rating' AS context_label
           FROM player_open_ratings por
           JOIN users rr ON rr.id = por.rater_id
           JOIN users ru ON ru.id = por.rated_id
           ORDER BY created_at DESC"""
    )]
    return jsonify({"ratings": ratings})


@app.route("/api/admin/teams")
@admin_required
def api_admin_teams():
    """Admin team manager data, split into active and archived teams."""
    teams = [dict(r) for r in query(
        """SELECT t.*,
                  COUNT(tm.id) AS member_count
           FROM teams t
           LEFT JOIN team_memberships tm ON tm.team_id = t.id
           WHERE t.archived_at IS NULL
           GROUP BY t.id
           ORDER BY t.id DESC"""
    )]
    archived_teams = [dict(r) for r in query(
        """SELECT t.*,
                  COUNT(tm.id) AS member_count
           FROM teams t
           LEFT JOIN team_memberships tm ON tm.team_id = t.id
           WHERE t.archived_at IS NOT NULL
           GROUP BY t.id
           ORDER BY t.archived_at DESC, t.id DESC"""
    )]
    members = [dict(r) for r in query(
        """SELECT tm.id, tm.team_id, tm.user_id, tm.role, tm.jersey_number, tm.joined_at,
                  u.name AS user_name, u.handle
           FROM team_memberships tm
           JOIN users u ON u.id = tm.user_id
           JOIN teams t ON t.id = tm.team_id
           WHERE t.archived_at IS NULL
           ORDER BY tm.team_id ASC, u.name ASC"""
    )]
    players = [dict(r) for r in query(
        """SELECT u.id, u.name, u.handle, t.name AS team_name
           FROM users u
           LEFT JOIN team_memberships tm ON tm.user_id = u.id
           LEFT JOIN teams t ON t.id = tm.team_id AND t.archived_at IS NULL
           ORDER BY u.name ASC"""
    )]
    return jsonify({"teams": teams, "archived_teams": archived_teams, "memberships": members, "players": players})


@app.route("/api/admin/teams", methods=["POST"])
@admin_required
def api_admin_create_team():
    data = request.get_json()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO teams (name, city, short_name, logo_url, skill_level, description, created_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (
            data.get("name", "").strip(),
            data.get("city", "").strip(),
            data.get("short_name", "").strip(),
            data.get("logo_url", "").strip(),
            data.get("skill_level", "Intermediate").strip(),
            data.get("description", "").strip(),
            datetime.now().isoformat(),
        ),
    )
    team_id = cur.fetchone()["id"]
    conn.commit()
    log_event("admin_create_team", "/api/admin/teams", {"team_id": team_id})
    return jsonify({"ok": True, "id": team_id}), 201


@app.route("/api/admin/teams/<int:team_id>", methods=["PUT"])
@admin_required
def api_admin_update_team(team_id):
    data = request.get_json()
    query(
        """UPDATE teams
           SET name = %s, city = %s, short_name = %s, logo_url = %s, skill_level = %s,
               description = %s, admin_note = %s
           WHERE id = %s""",
        (
            data.get("name", "").strip(),
            data.get("city", "").strip(),
            data.get("short_name", "").strip(),
            data.get("logo_url", "").strip(),
            data.get("skill_level", "Intermediate").strip(),
            data.get("description", "").strip(),
            str(data.get("admin_note", "") or "").strip()[:1000],
            team_id,
        ),
        commit=True,
    )
    log_event("admin_update_team", f"/api/admin/teams/{team_id}", {"team_id": team_id})
    return jsonify({"ok": True})


@app.route("/api/admin/teams/<int:team_id>", methods=["DELETE"])
@admin_required
def api_admin_delete_team(team_id):
    query("UPDATE teams SET archived_at = %s WHERE id = %s", (datetime.now().isoformat(), team_id), commit=True)
    log_event("admin_delete_team", f"/api/admin/teams/{team_id}", {"team_id": team_id})
    return jsonify({"ok": True})


@app.route("/api/admin/teams/<int:team_id>/restore", methods=["POST"])
@admin_required
def api_admin_restore_team(team_id):
    query("UPDATE teams SET archived_at = NULL WHERE id = %s", (team_id,), commit=True)
    log_event("admin_restore_team", f"/api/admin/teams/{team_id}/restore", {"team_id": team_id})
    return jsonify({"ok": True})


@app.route("/api/admin/teams/<int:team_id>/permanent-delete", methods=["DELETE"])
@admin_required
def api_admin_permanent_delete_team(team_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM league_teams WHERE team_id = %s", (team_id,))
    cur.execute("DELETE FROM team_memberships WHERE team_id = %s", (team_id,))
    cur.execute("DELETE FROM teams WHERE id = %s", (team_id,))
    conn.commit()
    log_event("admin_permanent_delete_team", f"/api/admin/teams/{team_id}/permanent-delete", {"team_id": team_id})
    return jsonify({"ok": True})


@app.route("/api/admin/teams/<int:team_id>/members", methods=["POST"])
@admin_required
def api_admin_add_team_member(team_id):
    data = request.get_json()
    user_id = int(data.get("user_id"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM team_memberships WHERE user_id = %s", (user_id,))
    cur.execute(
        """INSERT INTO team_memberships (team_id, user_id, role, jersey_number, joined_at)
           VALUES (%s,%s,%s,%s,%s)""",
        (
            team_id,
            user_id,
            data.get("role", "Player").strip(),
            data.get("jersey_number", "").strip(),
            datetime.now().isoformat(),
        ),
    )
    conn.commit()
    log_event("admin_add_team_member", f"/api/admin/teams/{team_id}/members", {"team_id": team_id, "user_id": user_id})
    return jsonify({"ok": True}), 201


@app.route("/api/admin/team-memberships/<int:membership_id>", methods=["PUT"])
@admin_required
def api_admin_update_team_member(membership_id):
    data = request.get_json()
    query(
        "UPDATE team_memberships SET role = %s, jersey_number = %s WHERE id = %s",
        (data.get("role", "Player").strip(), data.get("jersey_number", "").strip(), membership_id),
        commit=True,
    )
    return jsonify({"ok": True})


@app.route("/api/admin/team-memberships/<int:membership_id>", methods=["DELETE"])
@admin_required
def api_admin_delete_team_member(membership_id):
    query("DELETE FROM team_memberships WHERE id = %s", (membership_id,), commit=True)
    return jsonify({"ok": True})


@app.route("/api/admin/leagues")
@admin_required
def api_admin_leagues():
    """Admin league manager keeps empty leagues visible so admins can add teams."""
    leagues, _ = get_leagues_with_teams(include_empty=True)
    teams = [dict(r) for r in query("SELECT id, name, city, logo_url, skill_level FROM teams WHERE archived_at IS NULL ORDER BY name ASC")]
    return jsonify({"leagues": leagues, "teams": teams})


@app.route("/api/admin/leagues", methods=["POST"])
@admin_required
def api_admin_create_league():
    data = request.get_json()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO leagues (name, description, format, stage, status, city, season, banner_url)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (
            data.get("name", "").strip(),
            data.get("description", "").strip(),
            data.get("format", "5v5").strip(),
            data.get("stage", "Registration").strip(),
            data.get("status", "Open").strip(),
            data.get("city", "Delhi NCR").strip(),
            data.get("season", "2026").strip(),
            data.get("banner_url", "").strip(),
        ),
    )
    league_id = cur.fetchone()["id"]
    conn.commit()
    log_event("admin_create_league", "/api/admin/leagues", {"league_id": league_id})
    return jsonify({"ok": True, "id": league_id}), 201


@app.route("/api/admin/leagues/<int:league_id>", methods=["PUT"])
@admin_required
def api_admin_update_league(league_id):
    data = request.get_json()
    query(
        """UPDATE leagues
           SET name = %s, description = %s, format = %s, stage = %s, status = %s,
               city = %s, season = %s, banner_url = %s, admin_note = %s
           WHERE id = %s""",
        (
            data.get("name", "").strip(),
            data.get("description", "").strip(),
            data.get("format", "5v5").strip(),
            data.get("stage", "Registration").strip(),
            data.get("status", "Open").strip(),
            data.get("city", "Delhi NCR").strip(),
            data.get("season", "2026").strip(),
            data.get("banner_url", "").strip(),
            str(data.get("admin_note", "") or "").strip()[:1000],
            league_id,
        ),
        commit=True,
    )
    log_event("admin_update_league", f"/api/admin/leagues/{league_id}", {"league_id": league_id})
    return jsonify({"ok": True})


@app.route("/api/admin/leagues/<int:league_id>", methods=["DELETE"])
@admin_required
def api_admin_delete_league(league_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM leagues WHERE id = %s", (league_id,))
    if not cur.fetchone():
        return jsonify({"error": "League not found"}), 404
    cur.execute("UPDATE leagues SET archived_at = %s WHERE id = %s", (datetime.now().isoformat(), league_id))
    conn.commit()
    log_event("admin_archive_league", f"/api/admin/leagues/{league_id}", {"league_id": league_id})
    return jsonify({"ok": True})


@app.route("/api/admin/leagues/<int:league_id>/teams", methods=["POST"])
@admin_required
def api_admin_add_league_team(league_id):
    data = request.get_json()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO league_teams
           (league_id, team_id, played, won, drawn, lost, goals_for, goals_against, points, rank, form, notes)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (league_id, team_id) DO NOTHING
           RETURNING id""",
        (
            league_id,
            int(data.get("team_id")),
            int(data.get("played", 0)),
            int(data.get("won", 0)),
            int(data.get("drawn", 0)),
            int(data.get("lost", 0)),
            int(data.get("goals_for", 0)),
            int(data.get("goals_against", 0)),
            int(data.get("points", 0)),
            int(data.get("rank", 0)),
            data.get("form", "").strip(),
            data.get("notes", "").strip(),
        ),
    )
    row = cur.fetchone()
    conn.commit()
    return jsonify({"ok": True, "id": row["id"] if row else None}), 201


@app.route("/api/admin/league-teams/<int:league_team_id>", methods=["PUT"])
@admin_required
def api_admin_update_league_team(league_team_id):
    data = request.get_json()
    query(
        """UPDATE league_teams
           SET played = %s, won = %s, drawn = %s, lost = %s, goals_for = %s, goals_against = %s,
               points = %s, rank = %s, form = %s, notes = %s
           WHERE id = %s""",
        (
            int(data.get("played", 0)),
            int(data.get("won", 0)),
            int(data.get("drawn", 0)),
            int(data.get("lost", 0)),
            int(data.get("goals_for", 0)),
            int(data.get("goals_against", 0)),
            int(data.get("points", 0)),
            int(data.get("rank", 0)),
            data.get("form", "").strip(),
            data.get("notes", "").strip(),
            league_team_id,
        ),
        commit=True,
    )
    return jsonify({"ok": True})


@app.route("/api/admin/league-teams/<int:league_team_id>", methods=["DELETE"])
@admin_required
def api_admin_delete_league_team(league_team_id):
    query("DELETE FROM league_teams WHERE id = %s", (league_team_id,), commit=True)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Auto-initialize DB on first request
# ---------------------------------------------------------------------------

_db_ready = False

@app.route("/health")
def healthcheck():
    return jsonify({"ok": True, "status": "healthy"}), 200


@app.before_request
def initialize():
    global _db_ready
    if request.path == "/health":
        return
    if not _db_ready:
        seed_db()
        _db_ready = True
    if request.method == "GET" and not request.path.startswith("/api/") and "." not in request.path.rsplit("/", 1)[-1]:
        log_event("page_view", request.path or "/")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=app_port(), debug=False)


# ---------------------------------------------------------------------------
# Turf owner auth + routes
# ---------------------------------------------------------------------------

def owner_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "owner_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect("/owner/login")
        return f(*args, **kwargs)
    return decorated


def current_owner_id():
    return session.get("owner_id")


@app.route("/owner/register")
def owner_register_page():
    return send_from_directory(".", "owner_register.html")


@app.route("/owner/login")
def owner_login_page():
    return send_from_directory(".", "owner_login.html")


@app.route("/owner/dashboard")
@owner_required
def owner_dashboard_page():
    return send_from_directory(".", "owner_dashboard.html")


@app.route("/arena/login")
@app.route("/area/login")
def owner_login_alias_page():
    return redirect("/owner/login")


@app.route("/arena/register")
@app.route("/area/register")
def owner_register_alias_page():
    return redirect("/owner/register")


@app.route("/arena/dashboard")
@app.route("/area/dashboard")
def owner_dashboard_alias_page():
    return redirect("/owner/dashboard")


@app.route("/api/owner/register", methods=["POST"])
def api_owner_register():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    confirm_password = data.get("confirm_password", "")
    phone = data.get("phone", "").strip()
    turf_name = data.get("turf_name", "").strip()
    area = data.get("area", "").strip()
    surface = data.get("surface", "Astroturf")
    upi_id = data.get("upi_id", "").strip()
    map_link = data.get("map_link", "").strip()
    description = data.get("description", "").strip()
    image_urls = serialize_image_urls(data.get("image_urls", ""))
    if not all([name, email, password, turf_name, area, upi_id, data.get("price_per_hour")]):
        return jsonify({"error": "All fields are required"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    if confirm_password and confirm_password != password:
        return jsonify({"error": "Passwords do not match"}), 400
    try:
        price_per_hour = int(data.get("price_per_hour", 0))
        distance_km = float(data.get("distance_km", 0) or 0)
        latitude = float(data["latitude"]) if data.get("latitude") not in (None, "") else None
        longitude = float(data["longitude"]) if data.get("longitude") not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"error": "Price, distance, and GPS values must be valid numbers"}), 400
    if price_per_hour <= 0:
        return jsonify({"error": "Price per hour must be greater than zero"}), 400
    existing = query("SELECT id FROM turf_owners WHERE email = %s", (email,), one=True)
    if existing:
        return jsonify({"error": "Email already registered"}), 409
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO turf_owners (name, email, phone, password_hash) VALUES (%s,%s,%s,%s) RETURNING id",
        (name, email, phone, hash_password(password)),
    )
    owner_id = cur.fetchone()["id"]
    cur.execute(
        """INSERT INTO turfs
           (name, area, distance_km, surface, rating, price_per_hour, owner_id, upi_id, map_link, latitude, longitude, description, image_urls, archived_at)
           VALUES (%s,%s,%s,%s,4.5,%s,%s,%s,%s,%s,%s,%s,%s,NULL) RETURNING id""",
        (turf_name, area, distance_km, surface, price_per_hour, owner_id, upi_id, map_link, latitude, longitude, description, image_urls),
    )
    turf_id = cur.fetchone()["id"]
    default_times = ["06:00", "07:00", "08:00", "09:00", "17:00", "18:00", "19:00", "20:00"]
    for day_offset in range(7):
        slot_date = (datetime.now() + timedelta(days=day_offset)).date().isoformat()
        for slot_time in default_times:
            cur.execute(
                "INSERT INTO turf_slots (turf_id, slot_date, slot_time, is_booked, status) VALUES (%s,%s,%s,0,'available')",
                (turf_id, slot_date, slot_time),
            )
    conn.commit()
    session["owner_id"] = owner_id
    session.pop("user_id", None)
    log_event("owner_register", "/api/owner/register", {"owner_id": owner_id, "turf_id": turf_id})
    return jsonify({"ok": True}), 201


@app.route("/api/owner/login", methods=["POST"])
def api_owner_login():
    data = request.get_json() or {}
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    owner = query("SELECT id, password_hash FROM turf_owners WHERE email = %s", (email,), one=True)
    if not owner or not verify_password(password, owner["password_hash"]):
        return jsonify({"error": "Invalid email or password"}), 401
    session.permanent = True
    session["owner_id"] = owner["id"]
    session.pop("user_id", None)
    log_event("owner_login", "/api/owner/login", {"owner_id": owner["id"]})
    return jsonify({"ok": True})


@app.route("/api/owner/logout", methods=["POST"])
def api_owner_logout():
    session.pop("owner_id", None)
    return jsonify({"ok": True})


@app.route("/api/owner/dashboard")
@owner_required
def api_owner_dashboard():
    owner = query("SELECT id, name, email FROM turf_owners WHERE id = %s", (current_owner_id(),), one=True)
    turf = query("SELECT * FROM turfs WHERE owner_id = %s AND archived_at IS NULL", (current_owner_id(),), one=True)
    if not turf:
        return jsonify({
            "owner": dict(owner),
            "setup_required": True,
            "turf": {
                "name": "Set up your arena",
                "area": "Add your location",
                "surface": "Astroturf",
                "distance_km": 0,
                "rating": 4.5,
                "price_per_hour": 0,
                "upi_id": "",
                "map_link": "",
                "latitude": "",
                "longitude": "",
                "description": "",
                "image_urls": [],
                "qr_base64": "",
            },
            "bookings": [],
            "stats": {
                "pending": 0,
                "confirmed_today": 0,
                "revenue_today": 0,
                "total_bookings": 0,
            },
            "notifications": [{
                "title": "Finish arena setup",
                "message": "Add your arena name, price, UPI ID, photos, and location to make it visible to athletes.",
                "type": "owner_setup",
            }],
        })
    today = datetime.now().date().isoformat()
    bookings = [dict(r) for r in query(
        """SELECT b.id, b.player_name, b.player_email, b.utr_number, b.amount, b.status,
           ts.slot_date, ts.slot_time
           FROM bookings b JOIN turf_slots ts ON ts.id = b.slot_id
           WHERE ts.turf_id = %s ORDER BY b.id DESC""",
        (turf["id"],),
    )]
    pending_count = sum(1 for b in bookings if b["status"] == "pending")
    confirmed_today = sum(1 for b in bookings if b["status"] == "confirmed" and b["slot_date"] == today)
    revenue_today = sum(b["amount"] for b in bookings if b["status"] == "confirmed" and b["slot_date"] == today)
    turf_data = dict(turf)
    turf_data["image_urls"] = parse_image_urls(turf_data.get("image_urls"))
    return jsonify({
        "owner": dict(owner),
        "turf": turf_data,
        "bookings": bookings,
        "stats": {
            "pending": pending_count,
            "confirmed_today": confirmed_today,
            "revenue_today": revenue_today,
            "total_bookings": len(bookings),
        },
        "notifications": get_notifications(),
    })


@app.route("/api/owner/bookings/<int:booking_id>/approve", methods=["POST"])
@owner_required
def api_owner_approve(booking_id):
    booking = query(
        """SELECT b.id, b.slot_id
           FROM bookings b
           JOIN turf_slots ts ON ts.id = b.slot_id
           JOIN turfs t ON t.id = ts.turf_id
           WHERE b.id = %s AND t.owner_id = %s AND t.archived_at IS NULL""",
        (booking_id, current_owner_id()),
        one=True,
    )
    if not booking:
        return jsonify({"error": "Booking not found"}), 404
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE bookings SET status = 'confirmed' WHERE id = %s", (booking_id,))
    cur.execute("UPDATE turf_slots SET status = 'confirmed' WHERE id = %s", (booking["slot_id"],))
    conn.commit()
    log_event("owner_approve_booking", f"/api/owner/bookings/{booking_id}/approve", {"booking_id": booking_id})
    return jsonify({"ok": True})


@app.route("/api/owner/bookings/<int:booking_id>/reject", methods=["POST"])
@owner_required
def api_owner_reject(booking_id):
    booking = query(
        """SELECT b.id, b.slot_id
           FROM bookings b
           JOIN turf_slots ts ON ts.id = b.slot_id
           JOIN turfs t ON t.id = ts.turf_id
           WHERE b.id = %s AND t.owner_id = %s AND t.archived_at IS NULL""",
        (booking_id, current_owner_id()),
        one=True,
    )
    if not booking:
        return jsonify({"error": "Booking not found"}), 404
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE bookings SET status = 'rejected' WHERE id = %s", (booking_id,))
    cur.execute("UPDATE turf_slots SET is_booked = 0, status = 'available', booked_by = NULL WHERE id = %s", (booking["slot_id"],))
    conn.commit()
    log_event("owner_reject_booking", f"/api/owner/bookings/{booking_id}/reject", {"booking_id": booking_id})
    return jsonify({"ok": True})


@app.route("/api/owner/slots")
@owner_required
def api_owner_slots():
    date = request.args.get("date", datetime.now().date().isoformat())
    turf = query("SELECT id FROM turfs WHERE owner_id = %s AND archived_at IS NULL", (current_owner_id(),), one=True)
    if not turf:
        return jsonify({"slots": []})
    slots = [dict(r) for r in query(
        """SELECT ts.slot_time, ts.status, b.player_name
           FROM turf_slots ts LEFT JOIN bookings b ON b.slot_id = ts.id AND b.status != 'rejected'
           WHERE ts.turf_id = %s AND ts.slot_date = %s ORDER BY ts.slot_time""",
        (turf["id"], date),
    )]
    return jsonify({"slots": slots})


@app.route("/api/owner/settings", methods=["PUT"])
@owner_required
def api_owner_settings():
    """Save Arena Partner settings and ensure the arena stays visible to athletes."""
    data = request.get_json()
    turf_name = (data.get("turf_name") or "").strip()
    area = (data.get("area") or "").strip()
    surface = (data.get("surface") or "Astroturf").strip()
    upi_id = (data.get("upi_id") or "").strip()
    map_link = (data.get("map_link") or "").strip()
    description = data.get("description", "")
    image_urls = serialize_image_urls(data.get("image_urls", ""))
    try:
        distance_km = float(data.get("distance_km", 0) or 0)
        rating = float(data.get("rating", 4.5) or 4.5)
        price_per_hour = int(data.get("price_per_hour", 0) or 0)
        latitude = float(data["latitude"]) if data.get("latitude") not in (None, "") else None
        longitude = float(data["longitude"]) if data.get("longitude") not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"error": "Price, distance, rating, and GPS values must be valid numbers"}), 400
    if not turf_name or not area:
        return jsonify({"error": "Arena name and area are required"}), 400
    if price_per_hour <= 0:
        return jsonify({"error": "Price per hour must be greater than zero"}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """UPDATE turfs
           SET name=%s, area=%s, surface=%s, distance_km=%s, rating=%s, price_per_hour=%s,
               upi_id=%s, map_link=%s, latitude=%s, longitude=%s, description=%s, image_urls=%s,
               archived_at=NULL
           WHERE owner_id=%s
           RETURNING id""",
        (
            turf_name,
            area,
            surface,
            distance_km,
            rating,
            price_per_hour,
            upi_id,
            map_link,
            latitude,
            longitude,
            description,
            image_urls,
            current_owner_id(),
        ),
    )
    turf = cur.fetchone()
    if not turf:
        cur.execute(
            """INSERT INTO turfs
               (name, area, distance_km, surface, rating, price_per_hour, owner_id, upi_id, map_link, latitude, longitude, description, image_urls, archived_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL)
               RETURNING id""",
            (
                turf_name,
                area,
                distance_km,
                surface,
                rating,
                price_per_hour,
                current_owner_id(),
                upi_id,
                map_link,
                latitude,
                longitude,
                description,
                image_urls,
            ),
        )
        turf = cur.fetchone()
    turf_id = turf["id"]
    cur.execute("SELECT COUNT(*) AS count FROM turf_slots WHERE turf_id = %s", (turf_id,))
    if cur.fetchone()["count"] == 0:
        default_times = ["06:00", "07:00", "08:00", "09:00", "17:00", "18:00", "19:00", "20:00"]
        for day_offset in range(14):
            slot_date = (datetime.now() + timedelta(days=day_offset)).date().isoformat()
            for slot_time in default_times:
                cur.execute(
                    "INSERT INTO turf_slots (turf_id, slot_date, slot_time, is_booked, status) VALUES (%s,%s,%s,0,'available')",
                    (turf_id, slot_date, slot_time),
                )
    conn.commit()
    log_event("owner_update_settings", "/api/owner/settings")
    return jsonify({"ok": True, "turf_id": turf_id})


# ---------------------------------------------------------------------------
# Multi-page routes
# ---------------------------------------------------------------------------

@app.route("/")
def root():
    if "user_id" in session:
        return redirect("/dashboard")
    if "owner_id" in session:
        return redirect("/owner/dashboard")
    return send_from_directory(".", "landing.html")

@app.route("/dashboard")
@login_required
def dashboard_page():
    return send_from_directory(".", "dashboard.html")

@app.route("/games")
@login_required
def games_page():
    return send_from_directory(".", "games.html")

@app.route("/lobby")
@login_required
def lobby_page():
    return send_from_directory(".", "lobby.html")

@app.route("/turfs")
@login_required
def turfs_page():
    return send_from_directory(".", "turfs.html")

@app.route("/leagues")
@login_required
def leagues_page():
    return send_from_directory(".", "leagues.html")

@app.route("/profile")
@login_required
def profile_page():
    return send_from_directory(".", "profile.html")

@app.route("/about")
@login_required
def about_page():
    return send_from_directory(".", "about.html")

@app.route("/nav.js")
def serve_nav_js():
    return send_from_directory(".", "nav.js")

@app.route("/api/public/stats")
def api_public_stats():
    return jsonify({
        "players": query("SELECT COUNT(*) FROM users", one=True)["count"],
        "games": query("SELECT COUNT(*) FROM games g JOIN turfs t ON t.id = g.turf_id WHERE t.archived_at IS NULL", one=True)["count"],
        "turfs": query("SELECT COUNT(*) FROM turfs WHERE archived_at IS NULL", one=True)["count"],
        "leagues": query("SELECT COUNT(*) FROM leagues WHERE archived_at IS NULL", one=True)["count"],
    })


# ---------------------------------------------------------------------------
# Player profile — avatar + stats + ratings
# ---------------------------------------------------------------------------

@app.route("/api/profile/avatar", methods=["POST"])
@login_required
def api_upload_avatar():
    data = request.get_json() or {}
    b64 = data.get("avatar_base64", "")
    if len(b64) > 2_000_000:
        return jsonify({"error": "Image too large (max 1.5MB)"}), 400
    profile = get_profile(current_user_id()) or {}
    payload = {field: profile.get(field, "") for field in PROFILE_EDIT_FIELDS}
    payload["avatar_base64"] = b64
    payload["handle"] = profile.get("handle", "")
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """UPDATE profile_assessments
           SET status = 'superseded', reviewed_at = %s
           WHERE user_id = %s AND status = 'pending'""",
        (datetime.now().isoformat(), current_user_id()),
    )
    cur.execute(
        """INSERT INTO profile_assessments
           (user_id, requested_payload, status, created_at)
           VALUES (%s,%s,'pending',%s)""",
        (current_user_id(), json.dumps(normalize_profile_request(payload)), datetime.now().isoformat()),
    )
    conn.commit()
    return jsonify({"ok": True, "message": "Profile photo sent for admin assessment"})


@app.route("/api/profile/stats")
@login_required
def api_profile_stats():
    uid = current_user_id()
    games_played = query("SELECT COUNT(*) FROM game_players WHERE user_id = %s AND confirmed = 1", (uid,), one=True)["count"]
    games_created = query("SELECT COUNT(*) FROM games WHERE created_by = %s", (uid,), one=True)["count"]
    turfs_booked = query("SELECT COUNT(*) FROM bookings WHERE user_id = %s AND status = 'confirmed'", (uid,), one=True)["count"]
    rating_summary = get_rating_summary(uid)
    # Games played with — players who shared a game
    teammates = [dict(r) for r in query(
        """SELECT u.id, u.name, u.handle, u.avatar_base64,
           t.name AS team_name,
           ROUND(AVG(pr.rating)::numeric,1) as avg_rating,
           COUNT(pr.id) as rating_count
           FROM game_players gp1
           JOIN game_players gp2 ON gp2.game_id = gp1.game_id AND gp2.user_id != %s
           JOIN users u ON u.id = gp2.user_id
           LEFT JOIN team_memberships tm ON tm.user_id = u.id
           LEFT JOIN teams t ON t.id = tm.team_id AND t.archived_at IS NULL
           LEFT JOIN player_ratings pr ON pr.rated_id = u.id
           WHERE gp1.user_id = %s AND gp1.confirmed = 1 AND gp2.confirmed = 1
           GROUP BY u.id, u.name, u.handle, u.avatar_base64, t.name
           LIMIT 20""",
        (uid, uid),
    )]

    # Games I can rate players in (games I was in, that have other confirmed players)
    rateable_games = [dict(r) for r in query(
        """SELECT DISTINCT g.id, g.title, g.game_date
           FROM games g
           JOIN game_players gp ON gp.game_id = g.id AND gp.user_id = %s AND gp.confirmed = 1
           JOIN turfs t ON t.id = g.turf_id
           WHERE g.game_date <= %s AND t.archived_at IS NULL
           ORDER BY g.game_date DESC LIMIT 10""",
        (uid, datetime.now().date().isoformat()),
    )]

    for game in rateable_games:
        players = [dict(r) for r in query(
            """SELECT gp.user_id, u.name, u.avatar_base64,
               pr.rating as my_rating
               FROM game_players gp
               JOIN users u ON u.id = gp.user_id
               LEFT JOIN player_ratings pr ON pr.game_id = gp.game_id AND pr.rater_id = %s AND pr.rated_id = gp.user_id
               WHERE gp.game_id = %s AND gp.user_id != %s AND gp.confirmed = 1""",
            (uid, game["id"], uid),
        )]
        game["players"] = players

    open_rateable_players = [dict(r) for r in query(
        """SELECT u.id, u.name, u.handle, u.avatar_base64, por.rating AS my_rating,
                  t.name AS team_name
           FROM users u
           LEFT JOIN player_open_ratings por ON por.rater_id = %s AND por.rated_id = u.id
           LEFT JOIN team_memberships tm ON tm.user_id = u.id
               LEFT JOIN teams t ON t.id = tm.team_id AND t.archived_at IS NULL
           WHERE u.id != %s
           ORDER BY u.name ASC
           LIMIT 24""",
        (uid, uid),
    )]

    return jsonify({
        "stats": {
            "games_played": games_played,
            "games_created": games_created,
            "turfs_booked": turfs_booked,
            **rating_summary,
        },
        "teammates": teammates,
        "rateable_games": rateable_games,
        "open_rateable_players": open_rateable_players,
    })


@app.route("/api/players/<int:player_id>")
@login_required
def api_player_profile(player_id):
    user = get_profile(player_id)
    if not user:
        return jsonify({"error": "Not found"}), 404
    rating_summary = get_rating_summary(player_id)
    games_played = query("SELECT COUNT(*) FROM game_players WHERE user_id = %s AND confirmed = 1", (player_id,), one=True)["count"]
    return jsonify({
        **dict(user),
        **rating_summary,
        "games_played": games_played,
    })


@app.route("/api/ratings", methods=["POST"])
@login_required
def api_submit_rating():
    data = request.get_json()
    game_id = int(data.get("game_id"))
    rated_id = int(data.get("rated_id"))
    rating = int(data.get("rating"))
    if not 1 <= rating <= 10:
        return jsonify({"error": "Rating must be 1-10"}), 400
    if rated_id == current_user_id():
        return jsonify({"error": "Cannot rate yourself"}), 400
    game = query("SELECT id, game_date FROM games WHERE id = %s", (game_id,), one=True)
    if not game:
        return jsonify({"error": "Game not found"}), 404
    if game["game_date"] > datetime.now().date().isoformat():
        return jsonify({"error": "You can rate players only after the game"}), 400
    rater_in_game = query(
        "SELECT id FROM game_players WHERE game_id = %s AND user_id = %s AND confirmed = 1",
        (game_id, current_user_id()),
        one=True,
    )
    rated_in_game = query(
        "SELECT id FROM game_players WHERE game_id = %s AND user_id = %s AND confirmed = 1",
        (game_id, rated_id),
        one=True,
    )
    if not rater_in_game or not rated_in_game:
        return jsonify({"error": "Only confirmed players in this game can be rated"}), 403
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO player_ratings (game_id, rater_id, rated_id, rating, created_at)
           VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT (game_id, rater_id, rated_id) DO UPDATE SET rating = EXCLUDED.rating""",
        (game_id, current_user_id(), rated_id, rating, datetime.now().isoformat()),
    )
    conn.commit()
    log_event("player_rating", "/api/ratings", {"game_id": game_id, "rated_id": rated_id})
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Community
# ---------------------------------------------------------------------------

@app.route("/api/open-ratings", methods=["POST"])
@login_required
def api_submit_open_rating():
    data = request.get_json()
    rated_id = int(data.get("rated_id"))
    rating = int(data.get("rating"))
    if not 1 <= rating <= 10:
        return jsonify({"error": "Rating must be 1-10"}), 400
    if rated_id == current_user_id():
        return jsonify({"error": "Cannot rate yourself"}), 400
    if not query("SELECT id FROM users WHERE id = %s", (rated_id,), one=True):
        return jsonify({"error": "Player not found"}), 404
    can_rate, reason = can_rate_athlete(current_user_id(), rated_id)
    if not can_rate:
        return jsonify({"error": reason}), 403
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO player_open_ratings (rater_id, rated_id, rating, created_at)
           VALUES (%s,%s,%s,%s)
           ON CONFLICT (rater_id, rated_id) DO UPDATE SET rating = EXCLUDED.rating, created_at = EXCLUDED.created_at""",
        (current_user_id(), rated_id, rating, datetime.now().isoformat()),
    )
    conn.commit()
    log_event("open_player_rating", "/api/open-ratings", {"rated_id": rated_id})
    return jsonify({"ok": True})


@app.route("/api/community/users")
@login_required
def api_community_users():
    search = request.args.get("q", "").strip().lower()
    like = f"%{search}%"
    my_profile = get_profile(current_user_id()) or {}
    rows = [dict(r) for r in query(
        """SELECT u.id, u.name, u.handle, u.location, u.position, u.preferred_format, u.skill, u.avatar_base64, u.last_seen_at,
                  t.name AS team_name
           FROM users u
           LEFT JOIN team_memberships tm ON tm.user_id = u.id
           LEFT JOIN teams t ON t.id = tm.team_id AND t.archived_at IS NULL
           WHERE u.id != %s AND (
             %s = '' OR LOWER(u.name) LIKE %s OR LOWER(u.handle) LIKE %s OR LOWER(u.location) LIKE %s OR LOWER(COALESCE(t.name, '')) LIKE %s
           )
           ORDER BY u.name ASC
           LIMIT 50""",
        (current_user_id(), search, like, like, like, like),
    )]
    for row in rows:
        # Activity status helps athletes decide who is likely to reply quickly.
        row["activity_status"] = activity_status(row.get("last_seen_at"))
        one_id, two_id = normalize_friend_pair(current_user_id(), row["id"])
        friendship = query(
            """SELECT id, status, requested_by
               FROM friendships
               WHERE user_one_id = %s AND user_two_id = %s""",
            (one_id, two_id),
            one=True,
        )
        # Frontend needs the friendship id to safely retract only the sender's pending request.
        row["friendship_id"] = friendship["id"] if friendship else None
        row["friendship_status"] = friendship["status"] if friendship else "none"
        row["is_outgoing"] = bool(friendship and friendship["requested_by"] == current_user_id())
        row["can_accept"] = bool(friendship and friendship["status"] == "pending" and friendship["requested_by"] != current_user_id())
        can_rate, reason = can_rate_athlete(current_user_id(), row["id"])
        row["can_rate"] = can_rate
        row["rating_rule"] = reason
        row["is_suggested"] = bool(
            row["friendship_status"] == "none" and (
                (row.get("location") and row.get("location") == my_profile.get("location")) or
                (row.get("preferred_format") and row.get("preferred_format") == my_profile.get("preferred_format")) or
                (row.get("skill") and row.get("skill") == my_profile.get("skill")) or
                (row.get("team_name") and row.get("team_name") == (my_profile.get("team") or {}).get("name"))
            )
        )
    suggested = [row for row in rows if row["is_suggested"]][:3]
    if not search:
        rows = []
    return jsonify({"users": rows, "suggested": suggested})


@app.route("/api/friends")
@login_required
def api_friends():
    accepted = [dict(r) for r in query(
        """SELECT f.id, u.id AS user_id, u.name, u.handle, u.avatar_base64, u.location, u.last_seen_at,
                  t.name AS team_name
           FROM friendships f
           JOIN users u ON u.id = CASE WHEN f.user_one_id = %s THEN f.user_two_id ELSE f.user_one_id END
           LEFT JOIN team_memberships tm ON tm.user_id = u.id
           LEFT JOIN teams t ON t.id = tm.team_id AND t.archived_at IS NULL
           WHERE (f.user_one_id = %s OR f.user_two_id = %s) AND f.status = 'accepted'
           ORDER BY u.name ASC""",
        (current_user_id(), current_user_id(), current_user_id()),
    )]
    pending = [dict(r) for r in query(
        """SELECT f.id, f.status, f.requested_by, u.id AS user_id, u.name, u.handle, u.avatar_base64, u.last_seen_at
           FROM friendships f
           JOIN users u ON u.id = CASE WHEN f.user_one_id = %s THEN f.user_two_id ELSE f.user_one_id END
           WHERE (f.user_one_id = %s OR f.user_two_id = %s) AND f.status = 'pending'
           ORDER BY f.id DESC""",
        (current_user_id(), current_user_id(), current_user_id()),
    )]
    for row in accepted:
        row["activity_status"] = activity_status(row.get("last_seen_at"))
        can_rate, reason = can_rate_athlete(current_user_id(), row["user_id"])
        row["can_rate"] = can_rate
        row["rating_rule"] = reason
    for row in pending:
        row["activity_status"] = activity_status(row.get("last_seen_at"))
        row["is_outgoing"] = row["requested_by"] == current_user_id()
        can_rate, reason = can_rate_athlete(current_user_id(), row["user_id"])
        row["can_rate"] = can_rate
        row["rating_rule"] = reason
    return jsonify({"friends": accepted, "pending": pending})


@app.route("/api/friends/request", methods=["POST"])
@login_required
def api_friend_request():
    data = request.get_json()
    target_id = int(data.get("user_id"))
    if target_id == current_user_id():
        return jsonify({"error": "Cannot add yourself"}), 400
    if not query("SELECT id FROM users WHERE id = %s", (target_id,), one=True):
        return jsonify({"error": "Player not found"}), 404
    one_id, two_id = normalize_friend_pair(current_user_id(), target_id)
    existing = query("SELECT id, status FROM friendships WHERE user_one_id = %s AND user_two_id = %s", (one_id, two_id), one=True)
    if existing:
        return jsonify({"error": "Friend request already exists"}), 409
    query(
        """INSERT INTO friendships (user_one_id, user_two_id, requested_by, status, created_at)
           VALUES (%s,%s,%s,'pending',%s)""",
        (one_id, two_id, current_user_id(), datetime.now().isoformat()),
        commit=True,
    )
    sender = get_profile(current_user_id()) or {"name": "A Goalbazi athlete"}
    # Friend requests are important social actions, so they also trigger phone push when enabled.
    send_push_to_user(target_id, "New friend request", f"{sender['name']} wants to connect on Goalbazi.", "/dashboard")
    log_event("friend_request", "/api/friends/request", {"target_id": target_id})
    return jsonify({"ok": True}), 201


@app.route("/api/friends/<int:friendship_id>/accept", methods=["POST"])
@login_required
def api_accept_friendship(friendship_id):
    friendship = query("SELECT * FROM friendships WHERE id = %s", (friendship_id,), one=True)
    if not friendship:
        return jsonify({"error": "Request not found"}), 404
    if friendship["requested_by"] == current_user_id():
        return jsonify({"error": "You cannot accept your own request"}), 400
    if current_user_id() not in (friendship["user_one_id"], friendship["user_two_id"]):
        return jsonify({"error": "Forbidden"}), 403
    query("UPDATE friendships SET status = 'accepted' WHERE id = %s", (friendship_id,), commit=True)
    accepter = get_profile(current_user_id()) or {"name": "A Goalbazi athlete"}
    # Let the original sender know the connection is now active without needing to refresh.
    send_push_to_user(friendship["requested_by"], "Friend request accepted", f"{accepter['name']} accepted your Goalbazi request.", "/dashboard")
    log_event("friend_accept", f"/api/friends/{friendship_id}/accept", {"friendship_id": friendship_id})
    return jsonify({"ok": True})


@app.route("/api/friends/<int:friendship_id>/cancel", methods=["POST"])
@login_required
def api_cancel_friendship(friendship_id):
    friendship = query("SELECT * FROM friendships WHERE id = %s", (friendship_id,), one=True)
    if not friendship:
        return jsonify({"error": "Request not found"}), 404
    # Only the athlete who sent a still-pending request can retract it.
    if friendship["status"] != "pending" or friendship["requested_by"] != current_user_id():
        return jsonify({"error": "Only your own pending request can be cancelled"}), 403
    query("DELETE FROM friendships WHERE id = %s", (friendship_id,), commit=True)
    log_event("friend_cancel", f"/api/friends/{friendship_id}/cancel", {"friendship_id": friendship_id})
    return jsonify({"ok": True})


@app.route("/api/direct-messages")
@login_required
def api_direct_messages():
    other_user_id = request.args.get("user_id", type=int)
    if other_user_id:
        messages = [dict(r) for r in query(
            """SELECT dm.id, dm.message, dm.created_at, dm.sender_id, dm.receiver_id,
                      su.name AS sender_name
               FROM direct_messages dm
               JOIN users su ON su.id = dm.sender_id
               WHERE (dm.sender_id = %s AND dm.receiver_id = %s) OR (dm.sender_id = %s AND dm.receiver_id = %s)
               ORDER BY dm.id ASC""",
            (current_user_id(), other_user_id, other_user_id, current_user_id()),
        )]
        return jsonify({"messages": messages})

    conversations = [dict(r) for r in query(
        """SELECT DISTINCT ON (partner_id)
                  dm.id,
                  dm.message,
                  dm.created_at,
                  dm.sender_id,
                  partner_id,
                  u.name,
                  u.handle,
                  u.avatar_base64,
                  u.last_seen_at
           FROM (
             SELECT id, message, created_at, sender_id,
                    CASE WHEN sender_id = %s THEN receiver_id ELSE sender_id END AS partner_id
             FROM direct_messages
             WHERE sender_id = %s OR receiver_id = %s
           ) dm
           JOIN users u ON u.id = dm.partner_id
           ORDER BY partner_id, dm.id DESC""",
        (current_user_id(), current_user_id(), current_user_id()),
    )]
    for conversation in conversations:
        # v3.2: conversation cards can show live/recent/offline status without extra requests.
        conversation["activity_status"] = activity_status(conversation.get("last_seen_at"))
    return jsonify({"conversations": conversations})


@app.route("/api/direct-messages", methods=["POST"])
@login_required
def api_send_direct_message():
    data = request.get_json()
    receiver_id = int(data.get("receiver_id"))
    message = data.get("message", "").strip()
    if not message:
        return jsonify({"error": "Message cannot be empty"}), 400
    if receiver_id == current_user_id():
        return jsonify({"error": "Cannot message yourself"}), 400
    if not query("SELECT id FROM users WHERE id = %s", (receiver_id,), one=True):
        return jsonify({"error": "Player not found"}), 404
    query(
        "INSERT INTO direct_messages (sender_id, receiver_id, message, created_at) VALUES (%s,%s,%s,%s)",
        (current_user_id(), receiver_id, message, datetime.now().isoformat()),
        commit=True,
    )
    sender = get_profile(current_user_id()) or {"name": "Goalbazi"}
    preview = message[:90] + ("..." if len(message) > 90 else "")
    send_push_to_user(receiver_id, f"New message from {sender['name']}", preview, "/dashboard")
    log_event("direct_message", "/api/direct-messages", {"receiver_id": receiver_id})
    return jsonify({"ok": True}), 201


@app.route("/api/team-challenges", methods=["POST"])
@login_required
def api_create_team_challenge():
    data = request.get_json() or {}
    my_team = get_user_team(current_user_id())
    if not my_team:
        return jsonify({"error": "Join a team before sending challenges."}), 400
    if not user_can_manage_team(current_user_id(), my_team["id"]):
        return jsonify({"error": "Only a team captain or manager can challenge another team."}), 403
    opponent_team_id = int(data.get("opponent_team_id") or 0)
    if opponent_team_id == my_team["id"]:
        return jsonify({"error": "Choose another team to challenge."}), 400
    opponent = query("SELECT id, name FROM teams WHERE id = %s AND archived_at IS NULL", (opponent_team_id,), one=True)
    if not opponent:
        return jsonify({"error": "Opponent team not found."}), 404
    existing = query(
        """SELECT id FROM team_challenges
           WHERE challenger_team_id = %s AND opponent_team_id = %s AND status = 'pending'""",
        (my_team["id"], opponent_team_id),
        one=True,
    )
    if existing:
        return jsonify({"error": "This challenge is already pending."}), 409
    now = datetime.now().isoformat()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO team_challenges
           (challenger_team_id, opponent_team_id, created_by, format, proposed_date, proposed_time, message, status, created_at, updated_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s)
           RETURNING id""",
        (
            my_team["id"],
            opponent_team_id,
            current_user_id(),
            (data.get("format") or "5v5").strip(),
            (data.get("proposed_date") or "").strip(),
            (data.get("proposed_time") or "").strip(),
            (data.get("message") or "").strip()[:300],
            now,
            now,
        ),
    )
    challenge_id = cur.fetchone()["id"]
    conn.commit()
    notify_team_members(opponent_team_id, "New team challenge", f"{my_team['name']} challenged your team.", "/dashboard")
    log_event("team_challenge_create", "/api/team-challenges", {"challenge_id": challenge_id, "opponent_team_id": opponent_team_id})
    return jsonify({"ok": True, "id": challenge_id}), 201


@app.route("/api/team-challenges/<int:challenge_id>/<action>", methods=["POST"])
@login_required
def api_respond_team_challenge(challenge_id, action):
    if action not in {"accept", "reject"}:
        return jsonify({"error": "Invalid challenge action."}), 400
    challenge = query("SELECT * FROM team_challenges WHERE id = %s", (challenge_id,), one=True)
    if not challenge:
        return jsonify({"error": "Challenge not found."}), 404
    my_team = get_user_team(current_user_id())
    if not my_team or challenge["opponent_team_id"] != my_team["id"]:
        return jsonify({"error": "Only the challenged team can respond."}), 403
    if not user_can_manage_team(current_user_id(), my_team["id"]):
        return jsonify({"error": "Only a team captain or manager can respond."}), 403
    if challenge["status"] != "pending":
        return jsonify({"error": "This challenge is already closed."}), 409
    status = "accepted" if action == "accept" else "rejected"
    query(
        "UPDATE team_challenges SET status = %s, updated_at = %s WHERE id = %s",
        (status, datetime.now().isoformat(), challenge_id),
        commit=True,
    )
    notify_team_members(challenge["challenger_team_id"], f"Challenge {status}", f"{my_team['name']} {status} your team challenge.", "/dashboard")
    log_event("team_challenge_response", f"/api/team-challenges/{challenge_id}/{action}", {"challenge_id": challenge_id, "status": status})
    return jsonify({"ok": True, "status": status})


@app.route("/api/assistant/messages")
@login_required
def api_assistant_messages():
    profile = get_profile(current_user_id()) or {}
    messages = get_assistant_messages(current_user_id(), limit=18)
    if not messages:
        first_name = (profile.get("name") or "there").split(" ")[0]
        welcome_message = (
            f"Hi {first_name}, I’m your Goalbazi assistant. I can help you find arenas, matches, leagues, "
            "and athletes to connect with. Tap one of the suggestions below or ask me anything."
        )
        query(
            "INSERT INTO ai_assistant_messages (user_id, role, message, created_at) VALUES (%s,%s,%s,%s)",
            (current_user_id(), "assistant", welcome_message, datetime.now().isoformat()),
            commit=True,
        )
        messages = get_assistant_messages(current_user_id(), limit=18)
    return jsonify({
        "messages": messages,
        "suggestions": get_assistant_prompt_suggestions(profile),
    })


@app.route("/api/assistant/messages", methods=["POST"])
@login_required
def api_assistant_reply():
    """Store user message, generate AI/fallback reply, and save assistant memory."""
    data = request.get_json() or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Message cannot be empty"}), 400
    if len(message) > 1200:
        return jsonify({"error": "Message is too long"}), 400
    now = datetime.now().isoformat()
    query(
        "INSERT INTO ai_assistant_messages (user_id, role, message, created_at) VALUES (%s,%s,%s,%s)",
        (current_user_id(), "user", message, now),
        commit=True,
    )
    reply = generate_assistant_reply(current_user_id(), message)
    query(
        "INSERT INTO ai_assistant_messages (user_id, role, message, created_at) VALUES (%s,%s,%s,%s)",
        (current_user_id(), "assistant", reply, datetime.now().isoformat()),
        commit=True,
    )
    log_event("assistant_message", "/api/assistant/messages")
    profile = get_profile(current_user_id()) or {}
    return jsonify({
        "reply": reply,
        "messages": get_assistant_messages(current_user_id(), limit=18),
        "suggestions": get_assistant_prompt_suggestions(profile),
    }), 201


@app.route("/api/notifications")
def api_notifications():
    if "user_id" not in session and "owner_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"notifications": get_notifications()})


@app.route("/api/push/public-key")
@login_required
def api_push_public_key():
    return jsonify({
        "publicKey": VAPID_PUBLIC_KEY,
        "enabled": push_is_configured(),
    })


@app.route("/api/push/subscribe", methods=["POST"])
@login_required
def api_push_subscribe():
    """Store a phone/browser push subscription for direct-message notifications."""
    data = request.get_json() or {}
    endpoint = (data.get("endpoint") or "").strip()
    keys = data.get("keys") or {}
    p256dh = (keys.get("p256dh") or "").strip()
    auth = (keys.get("auth") or "").strip()
    if not endpoint or not p256dh or not auth:
        return jsonify({"error": "Invalid push subscription"}), 400
    now = datetime.now().isoformat()
    query(
        """INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, user_agent, created_at, updated_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (endpoint) DO UPDATE SET
             user_id = EXCLUDED.user_id,
             p256dh = EXCLUDED.p256dh,
             auth = EXCLUDED.auth,
             user_agent = EXCLUDED.user_agent,
             updated_at = EXCLUDED.updated_at""",
        (
            current_user_id(),
            endpoint,
            p256dh,
            auth,
            request.headers.get("User-Agent", "")[:500],
            now,
            now,
        ),
        commit=True,
    )
    log_event("push_subscribe", "/api/push/subscribe")
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Admin — management
# ---------------------------------------------------------------------------

@app.route("/api/admin/turfs", methods=["POST"])
@admin_required
def api_admin_add_turf():
    data = request.get_json()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO turfs (name, area, distance_km, surface, rating, price_per_hour, upi_id, map_link, latitude, longitude, owner_id, description, image_urls)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (data["name"], data["area"], float(data.get("distance_km",0)),
         data.get("surface","Astroturf"), float(data.get("rating",4.5)),
         int(data.get("price_per_hour",500)), data.get("upi_id",""), data.get("map_link", ""),
         float(data["latitude"]) if data.get("latitude") not in (None, "") else None,
         float(data["longitude"]) if data.get("longitude") not in (None, "") else None,
         int(data["owner_id"]) if data.get("owner_id") not in (None, "") else None,
         data.get("description", ""), serialize_image_urls(data.get("image_urls", ""))),
    )
    turf_id = cur.fetchone()["id"]
    # Seed slots for next 7 days
    times = ["06:00","07:00","08:00","09:00","17:00","18:00","19:00","20:00"]
    for day in range(7):
        slot_date = (datetime.now() + timedelta(days=day)).date().isoformat()
        for t in times:
            cur.execute(
                "INSERT INTO turf_slots (turf_id,slot_date,slot_time,is_booked,status) VALUES (%s,%s,%s,0,'available')",
                (turf_id, slot_date, t),
            )
    conn.commit()
    log_event("admin_add_turf", "/api/admin/turfs", {"turf_id": turf_id})
    return jsonify({"ok": True, "id": turf_id}), 201


@app.route("/api/admin/turfs/<int:turf_id>", methods=["PUT"])
@admin_required
def api_admin_edit_turf(turf_id):
    data = request.get_json()
    query(
        """UPDATE turfs SET name=%s, area=%s, distance_km=%s, surface=%s,
           rating=%s, price_per_hour=%s, upi_id=%s, map_link=%s, latitude=%s, longitude=%s, owner_id=COALESCE(%s, owner_id),
           description=%s, image_urls=%s, admin_note=%s WHERE id=%s""",
        (data["name"], data["area"], float(data.get("distance_km",0)),
         data.get("surface","Astroturf"), float(data.get("rating",4.5)),
         int(data.get("price_per_hour",500)), data.get("upi_id",""), data.get("map_link", ""),
         float(data["latitude"]) if data.get("latitude") not in (None, "") else None,
         float(data["longitude"]) if data.get("longitude") not in (None, "") else None,
         int(data["owner_id"]) if data.get("owner_id") not in (None, "") else None,
         data.get("description", ""), serialize_image_urls(data.get("image_urls", "")),
         str(data.get("admin_note", "") or "").strip()[:1000], turf_id),
        commit=True,
    )
    log_event("admin_edit_turf", f"/api/admin/turfs/{turf_id}", {"turf_id": turf_id})
    return jsonify({"ok": True})


@app.route("/api/admin/turfs/<int:turf_id>", methods=["DELETE"])
@admin_required
def api_admin_delete_turf(turf_id):
    query("UPDATE turfs SET archived_at = %s WHERE id = %s", (datetime.now().isoformat(), turf_id), commit=True)
    log_event("admin_archive_turf", f"/api/admin/turfs/{turf_id}", {"turf_id": turf_id})
    return jsonify({"ok": True})


@app.route("/api/admin/turfs/<int:turf_id>/restore", methods=["POST"])
@admin_required
def api_admin_restore_turf(turf_id):
    query("UPDATE turfs SET archived_at = NULL WHERE id = %s", (turf_id,), commit=True)
    log_event("admin_restore_turf", f"/api/admin/turfs/{turf_id}/restore", {"turf_id": turf_id})
    return jsonify({"ok": True})


@app.route("/api/admin/turfs/<int:turf_id>/permanent-delete", methods=["DELETE"])
@admin_required
def api_admin_permanent_delete_turf(turf_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM games WHERE turf_id = %s", (turf_id,))
    game_ids = [row["id"] for row in cur.fetchall()]
    for game_id in game_ids:
        cur.execute("DELETE FROM player_ratings WHERE game_id = %s", (game_id,))
        cur.execute("DELETE FROM game_players WHERE game_id = %s", (game_id,))
        cur.execute("DELETE FROM game_messages WHERE game_id = %s", (game_id,))
    cur.execute("DELETE FROM games WHERE turf_id = %s", (turf_id,))
    cur.execute("DELETE FROM bookings WHERE slot_id IN (SELECT id FROM turf_slots WHERE turf_id=%s)", (turf_id,))
    cur.execute("DELETE FROM turf_slots WHERE turf_id=%s", (turf_id,))
    cur.execute("DELETE FROM turfs WHERE id=%s", (turf_id,))
    conn.commit()
    log_event("admin_permanent_delete_turf", f"/api/admin/turfs/{turf_id}/permanent-delete", {"turf_id": turf_id})
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Turf owner — QR code upload
# ---------------------------------------------------------------------------

@app.route("/api/owner/qr", methods=["POST"])
@owner_required
def api_owner_upload_qr():
    data = request.get_json()
    qr_b64 = data.get("qr_base64", "")
    if len(qr_b64) > 2_000_000:
        return jsonify({"error": "Image too large"}), 400
    query("UPDATE turfs SET qr_base64=%s WHERE owner_id=%s AND archived_at IS NULL", (qr_b64, current_owner_id()), commit=True)
    log_event("owner_upload_qr", "/api/owner/qr")
    return jsonify({"ok": True})


@app.route("/api/bookings/<int:slot_id>/info", methods=["GET"])
def api_slot_info_updated(slot_id):
    info = query(
        """SELECT t.upi_id, t.price_per_hour, t.name as turf_name, t.qr_base64, t.map_link
           FROM turf_slots ts JOIN turfs t ON t.id = ts.turf_id
           WHERE ts.id = %s AND t.archived_at IS NULL""",
        (slot_id,), one=True,
    )
    if not info:
        return jsonify({"error": "Not found"}), 404
    return jsonify(dict(info))
