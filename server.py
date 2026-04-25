from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import parse_qs, urlparse

import psycopg2
import psycopg2.extras
from flask import Flask, g, jsonify, redirect, render_template, request, send_from_directory, session

app = Flask(__name__, static_folder="static", template_folder=".")
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

DATABASE_URL = os.environ.get("DATABASE_URL", "")


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
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


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated


def current_user_id():
    return session.get("user_id")


def sanitize_handle(handle: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (handle or "").strip().lower())


def display_handle(handle: str) -> str:
    clean = sanitize_handle(handle)
    return f"@{clean}" if clean else "@player"


def haversine_km(lat1, lon1, lat2, lon2):
    from math import asin, cos, radians, sin, sqrt

    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    lat1 = radians(lat1)
    lat2 = radians(lat2)
    a = sin(d_lat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(d_lon / 2) ** 2
    return 6371 * 2 * asin(sqrt(a))


def log_event(event_type: str, path_value: str, meta: dict | None = None) -> None:
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


def current_user_is_admin() -> bool:
    if "user_id" not in session:
        return False
    user = query("SELECT is_admin FROM users WHERE id = %s", (current_user_id(),), one=True)
    return bool(user and user.get("is_admin"))


# ---------------------------------------------------------------------------
# Database seeding
# ---------------------------------------------------------------------------

def seed_db():
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
    cur.execute("ALTER TABLE turfs ADD COLUMN IF NOT EXISTS qr_base64 TEXT DEFAULT ''")
    cur.execute("ALTER TABLE turfs ADD COLUMN IF NOT EXISTS map_link TEXT DEFAULT ''")
    cur.execute("ALTER TABLE turfs ADD COLUMN IF NOT EXISTS latitude DOUBLE PRECISION")
    cur.execute("ALTER TABLE turfs ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION")

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
            rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
            created_at TEXT NOT NULL,
            UNIQUE(game_id, rater_id, rated_id)
        )
    """)

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
        CREATE TABLE IF NOT EXISTS player_open_ratings (
            id SERIAL PRIMARY KEY,
            rater_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            rated_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
            created_at TEXT NOT NULL,
            UNIQUE(rater_id, rated_id)
        )
    """)

    if ADMIN_EMAIL:
        cur.execute("UPDATE users SET is_admin = TRUE WHERE LOWER(email) = %s", (ADMIN_EMAIL.lower(),))

    # Seed turfs
    turf_count = query("SELECT COUNT(*) FROM turfs", one=True)["count"]
    if turf_count == 0:
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

    # Seed leagues
    league_count = query("SELECT COUNT(*) FROM leagues", one=True)["count"]
    if league_count == 0:
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

    team_count = query("SELECT COUNT(*) FROM teams", one=True)["count"]
    if team_count == 0:
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

    league_team_count = query("SELECT COUNT(*) FROM league_teams", one=True)["count"]
    if league_team_count == 0:
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

    conn.commit()


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def get_profile(user_id):
    row = query("SELECT * FROM users WHERE id = %s", (user_id,), one=True)
    if not row:
        return None
    profile = dict(row)
    profile["handle"] = sanitize_handle(profile.get("handle", ""))
    profile["handle_display"] = display_handle(profile["handle"])
    team = query(
        """SELECT t.id, t.name, t.logo_url, tm.role, tm.jersey_number
           FROM team_memberships tm
           JOIN teams t ON t.id = tm.team_id
           WHERE tm.user_id = %s""",
        (user_id,),
        one=True,
    )
    profile["team"] = dict(team) if team else None
    return profile


def get_stats():
    return [
        {"value": query("SELECT COUNT(*) FROM users", one=True)["count"], "label": "Players"},
        {"value": query("SELECT COUNT(*) FROM games", one=True)["count"], "label": "Games"},
        {"value": query("SELECT COUNT(*) FROM turfs", one=True)["count"], "label": "Arenas"},
        {"value": query("SELECT COUNT(*) FROM leagues", one=True)["count"], "label": "Leagues"},
    ]


def normalize_friend_pair(user_a, user_b):
    return (min(int(user_a), int(user_b)), max(int(user_a), int(user_b)))


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


def get_leagues_with_teams():
    leagues = [dict(r) for r in query("SELECT * FROM leagues ORDER BY id ASC")]
    league_ids = [league["id"] for league in leagues]
    league_rows = [dict(r) for r in query(
        """SELECT lt.id, lt.league_id, lt.team_id, lt.played, lt.won, lt.drawn, lt.lost,
                  lt.goals_for, lt.goals_against, lt.points, lt.rank, lt.form, lt.notes,
                  t.name AS team_name, t.logo_url, t.city, t.skill_level
           FROM league_teams lt
           JOIN teams t ON t.id = lt.team_id
           ORDER BY lt.league_id ASC, lt.rank ASC, lt.points DESC, t.name ASC"""
    )]
    for league in leagues:
        league["teams"] = [row for row in league_rows if row["league_id"] == league["id"]]
        league["team_count"] = len(league["teams"])
    primary_standings = leagues[0]["teams"] if leagues else []
    return leagues, primary_standings


def get_turfs(date_value, search="", user_lat=None, user_lng=None):
    like = f"%{search.lower()}%"
    turfs = [dict(r) for r in query(
        "SELECT * FROM turfs WHERE LOWER(name) LIKE %s OR LOWER(area) LIKE %s ORDER BY distance_km ASC",
        (like, like),
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
        FROM games g JOIN turfs t ON t.id = g.turf_id WHERE g.id = %s
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
    rows = query("SELECT id FROM games ORDER BY game_date ASC, game_time ASC")
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
    session["user_id"] = user["id"]
    log_event("auth_login", "/api/auth/login", {"user_id": user["id"]})
    return jsonify({"ok": True})


@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


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


@app.route("/assets/<path:filename>")
def serve_assets(filename):
    return send_from_directory("assets", filename)


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.route("/api/dashboard")
@login_required
def api_dashboard():
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
    return jsonify({
        "profile": get_profile(current_user_id()),
        "stats": get_stats(),
        "games": get_games(),
        "turfs": get_turfs(date_value, search, user_lat, user_lng),
        "leagues": leagues,
        "standings": standings,
        "community": {
            "friend_count": friend_count,
            "message_count": unread_messages,
        },
    })


@app.route("/api/profile", methods=["PUT"])
@login_required
def api_profile_update():
    data = request.get_json()
    handle = sanitize_handle(data.get("handle", ""))
    if not handle:
        return jsonify({"error": "Username can only contain lowercase letters and numbers"}), 400
    existing = query(
        "SELECT id FROM users WHERE handle = %s AND id != %s",
        (handle, current_user_id()),
        one=True,
    )
    if existing:
        return jsonify({"error": "Username already taken"}), 409
    query(
        "UPDATE users SET name=%s, handle=%s, location=%s, preferred_format=%s, bio=%s WHERE id=%s",
        (data.get("name"), handle, data.get("location"), data.get("preferred_format"), data.get("bio"), current_user_id()),
        commit=True,
    )
    log_event("profile_update", "/api/profile")
    return jsonify(get_profile(current_user_id()))


@app.route("/api/games", methods=["POST"])
@login_required
def api_create_game():
    data = request.get_json()
    kickoff = datetime.fromisoformat(f"{data['date']}T{data['time']}:00").isoformat()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO games (title, format, skill_level, visibility, game_date, game_time, kickoff_at, turf_id, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (data["title"], data["format"], data["skill"], data["visibility"], data["date"], data["time"], kickoff, int(data["turf_id"]), current_user_id()),
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
    stats = [
        {"value": query("SELECT COUNT(*) FROM users", one=True)["count"], "label": "Total users"},
        {"value": query("SELECT COUNT(*) FROM games", one=True)["count"], "label": "Total games"},
        {"value": query("SELECT COUNT(*) FROM turf_slots WHERE is_booked = 1", one=True)["count"], "label": "Bookings"},
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
           FROM games g LEFT JOIN game_players gp ON gp.game_id = g.id
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
    return jsonify({"stats": stats, "recent_users": recent_users, "recent_games": recent_games, "analytics": analytics})


@app.route("/api/admin/users")
@admin_required
def api_admin_users():
    users = [dict(r) for r in query(
        """SELECT u.id, u.name, u.handle, u.email, u.location, u.position, u.skill, u.preferred_format,
                  u.bio, u.avatar_base64, u.is_admin, t.name AS team_name
           FROM users u
           LEFT JOIN team_memberships tm ON tm.user_id = u.id
           LEFT JOIN teams t ON t.id = tm.team_id
           ORDER BY u.id DESC"""
    )]
    return jsonify({"users": users})


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
    data = request.get_json()
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
               preferred_format = %s, skill = %s, bio = %s, avatar_base64 = %s, is_admin = %s
           WHERE id = %s""",
        (
            data.get("name", "").strip(),
            handle,
            data.get("email", "").strip().lower(),
            data.get("location", "").strip(),
            data.get("position", "Midfielder").strip(),
            data.get("preferred_format", "5v5").strip(),
            data.get("skill", "Intermediate").strip(),
            data.get("bio", ""),
            data.get("avatar_base64", ""),
            bool(data.get("is_admin")),
            user_id,
        ),
        commit=True,
    )
    log_event("admin_update_user", f"/api/admin/users/{user_id}", {"user_id": user_id})
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
           FROM games g LEFT JOIN game_players gp ON gp.game_id = g.id
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
    turfs = [dict(r) for r in query("SELECT * FROM turfs ORDER BY id ASC")]
    owners = [dict(r) for r in query("SELECT id, name FROM turf_owners ORDER BY name ASC")]
    players = [dict(r) for r in query("SELECT id, name FROM users ORDER BY name ASC LIMIT 200")]
    return jsonify({"turfs": turfs, "owners": owners, "players": players})


@app.route("/api/admin/bookings")
@admin_required
def api_admin_bookings():
    bookings = [dict(r) for r in query(
        """SELECT ts.id, t.name as turf_name, ts.slot_date, ts.slot_time,
           u.name as booked_by_name
           FROM turf_slots ts
           JOIN turfs t ON t.id = ts.turf_id
           LEFT JOIN users u ON u.id = ts.booked_by
           WHERE ts.is_booked = 1
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
    teams = [dict(r) for r in query(
        """SELECT t.*,
                  COUNT(tm.id) AS member_count
           FROM teams t
           LEFT JOIN team_memberships tm ON tm.team_id = t.id
           GROUP BY t.id
           ORDER BY t.id DESC"""
    )]
    members = [dict(r) for r in query(
        """SELECT tm.id, tm.team_id, tm.user_id, tm.role, tm.jersey_number, tm.joined_at,
                  u.name AS user_name, u.handle
           FROM team_memberships tm
           JOIN users u ON u.id = tm.user_id
           ORDER BY tm.team_id ASC, u.name ASC"""
    )]
    players = [dict(r) for r in query(
        """SELECT u.id, u.name, u.handle, t.name AS team_name
           FROM users u
           LEFT JOIN team_memberships tm ON tm.user_id = u.id
           LEFT JOIN teams t ON t.id = tm.team_id
           ORDER BY u.name ASC"""
    )]
    return jsonify({"teams": teams, "memberships": members, "players": players})


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
            data.get("city", "Delhi NCR").strip(),
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
           SET name = %s, city = %s, short_name = %s, logo_url = %s, skill_level = %s, description = %s
           WHERE id = %s""",
        (
            data.get("name", "").strip(),
            data.get("city", "Delhi NCR").strip(),
            data.get("short_name", "").strip(),
            data.get("logo_url", "").strip(),
            data.get("skill_level", "Intermediate").strip(),
            data.get("description", "").strip(),
            team_id,
        ),
        commit=True,
    )
    log_event("admin_update_team", f"/api/admin/teams/{team_id}", {"team_id": team_id})
    return jsonify({"ok": True})


@app.route("/api/admin/teams/<int:team_id>", methods=["DELETE"])
@admin_required
def api_admin_delete_team(team_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM league_teams WHERE team_id = %s", (team_id,))
    cur.execute("DELETE FROM team_memberships WHERE team_id = %s", (team_id,))
    cur.execute("DELETE FROM teams WHERE id = %s", (team_id,))
    conn.commit()
    log_event("admin_delete_team", f"/api/admin/teams/{team_id}", {"team_id": team_id})
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
    leagues, _ = get_leagues_with_teams()
    teams = [dict(r) for r in query("SELECT id, name, city, logo_url, skill_level FROM teams ORDER BY name ASC")]
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
           SET name = %s, description = %s, format = %s, stage = %s, status = %s, city = %s, season = %s, banner_url = %s
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
    cur.execute("DELETE FROM league_teams WHERE league_id = %s", (league_id,))
    cur.execute("DELETE FROM standings WHERE league_id = %s", (league_id,))
    cur.execute("DELETE FROM leagues WHERE id = %s", (league_id,))
    conn.commit()
    log_event("admin_delete_league", f"/api/admin/leagues/{league_id}", {"league_id": league_id})
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
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)


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


@app.route("/api/owner/register", methods=["POST"])
def api_owner_register():
    data = request.get_json()
    name = data.get("name", "").strip()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    phone = data.get("phone", "").strip()
    turf_name = data.get("turf_name", "").strip()
    area = data.get("area", "").strip()
    price_per_hour = int(data.get("price_per_hour", 0))
    surface = data.get("surface", "Astroturf")
    distance_km = float(data.get("distance_km", 0))
    upi_id = data.get("upi_id", "").strip()
    map_link = data.get("map_link", "").strip()
    latitude = float(data["latitude"]) if data.get("latitude") not in (None, "") else None
    longitude = float(data["longitude"]) if data.get("longitude") not in (None, "") else None
    if not all([name, email, password, turf_name, area, upi_id]):
        return jsonify({"error": "All fields are required"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
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
           (name, area, distance_km, surface, rating, price_per_hour, owner_id, upi_id, map_link, latitude, longitude)
           VALUES (%s,%s,%s,%s,4.5,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (turf_name, area, distance_km, surface, price_per_hour, owner_id, upi_id, map_link, latitude, longitude),
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
    log_event("owner_register", "/api/owner/register", {"owner_id": owner_id, "turf_id": turf_id})
    return jsonify({"ok": True}), 201


@app.route("/api/owner/login", methods=["POST"])
def api_owner_login():
    data = request.get_json()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    owner = query("SELECT id, password_hash FROM turf_owners WHERE email = %s", (email,), one=True)
    if not owner or not verify_password(password, owner["password_hash"]):
        return jsonify({"error": "Invalid email or password"}), 401
    session["owner_id"] = owner["id"]
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
    turf = query("SELECT * FROM turfs WHERE owner_id = %s", (current_owner_id(),), one=True)
    if not turf:
        return jsonify({"error": "No turf found"}), 404
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
    return jsonify({
        "owner": dict(owner),
        "turf": dict(turf),
        "bookings": bookings,
        "stats": {
            "pending": pending_count,
            "confirmed_today": confirmed_today,
            "revenue_today": revenue_today,
            "total_bookings": len(bookings),
        },
    })


@app.route("/api/owner/bookings/<int:booking_id>/approve", methods=["POST"])
@owner_required
def api_owner_approve(booking_id):
    booking = query(
        """SELECT b.id, b.slot_id
           FROM bookings b
           JOIN turf_slots ts ON ts.id = b.slot_id
           JOIN turfs t ON t.id = ts.turf_id
           WHERE b.id = %s AND t.owner_id = %s""",
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
           WHERE b.id = %s AND t.owner_id = %s""",
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
    turf = query("SELECT id FROM turfs WHERE owner_id = %s", (current_owner_id(),), one=True)
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
    data = request.get_json()
    query(
        """UPDATE turfs
           SET name=%s, area=%s, price_per_hour=%s, upi_id=%s, map_link=%s, latitude=%s, longitude=%s
           WHERE owner_id=%s""",
        (
            data.get("turf_name"),
            data.get("area"),
            int(data.get("price_per_hour", 0)),
            data.get("upi_id"),
            data.get("map_link", ""),
            float(data["latitude"]) if data.get("latitude") not in (None, "") else None,
            float(data["longitude"]) if data.get("longitude") not in (None, "") else None,
            current_owner_id(),
        ),
        commit=True,
    )
    log_event("owner_update_settings", "/api/owner/settings")
    return jsonify({"ok": True})


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

@app.route("/nav.js")
def serve_nav_js():
    return send_from_directory(".", "nav.js")

@app.route("/api/public/stats")
def api_public_stats():
    return jsonify({
        "players": query("SELECT COUNT(*) FROM users", one=True)["count"],
        "games": query("SELECT COUNT(*) FROM games", one=True)["count"],
        "turfs": query("SELECT COUNT(*) FROM turfs", one=True)["count"],
        "leagues": query("SELECT COUNT(*) FROM leagues", one=True)["count"],
    })


# ---------------------------------------------------------------------------
# Player profile — avatar + stats + ratings
# ---------------------------------------------------------------------------

@app.route("/api/profile/avatar", methods=["POST"])
@login_required
def api_upload_avatar():
    data = request.get_json()
    b64 = data.get("avatar_base64", "")
    if len(b64) > 2_000_000:
        return jsonify({"error": "Image too large (max 1.5MB)"}), 400
    query("UPDATE users SET avatar_base64 = %s WHERE id = %s", (b64, current_user_id()), commit=True)
    return jsonify({"ok": True})


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
           LEFT JOIN teams t ON t.id = tm.team_id
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
           WHERE g.game_date <= %s
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
           LEFT JOIN teams t ON t.id = tm.team_id
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
    if not 1 <= rating <= 5:
        return jsonify({"error": "Rating must be 1-5"}), 400
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
    if not 1 <= rating <= 5:
        return jsonify({"error": "Rating must be 1-5"}), 400
    if rated_id == current_user_id():
        return jsonify({"error": "Cannot rate yourself"}), 400
    if not query("SELECT id FROM users WHERE id = %s", (rated_id,), one=True):
        return jsonify({"error": "Player not found"}), 404
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
    rows = [dict(r) for r in query(
        """SELECT u.id, u.name, u.handle, u.location, u.position, u.preferred_format, u.skill, u.avatar_base64,
                  t.name AS team_name
           FROM users u
           LEFT JOIN team_memberships tm ON tm.user_id = u.id
           LEFT JOIN teams t ON t.id = tm.team_id
           WHERE u.id != %s AND (
             %s = '' OR LOWER(u.name) LIKE %s OR LOWER(u.handle) LIKE %s OR LOWER(u.location) LIKE %s OR LOWER(COALESCE(t.name, '')) LIKE %s
           )
           ORDER BY u.name ASC
           LIMIT 50""",
        (current_user_id(), search, like, like, like, like),
    )]
    for row in rows:
        one_id, two_id = normalize_friend_pair(current_user_id(), row["id"])
        friendship = query(
            """SELECT status, requested_by
               FROM friendships
               WHERE user_one_id = %s AND user_two_id = %s""",
            (one_id, two_id),
            one=True,
        )
        row["friendship_status"] = friendship["status"] if friendship else "none"
        row["can_accept"] = bool(friendship and friendship["status"] == "pending" and friendship["requested_by"] != current_user_id())
    return jsonify({"users": rows})


@app.route("/api/friends")
@login_required
def api_friends():
    accepted = [dict(r) for r in query(
        """SELECT f.id, u.id AS user_id, u.name, u.handle, u.avatar_base64, u.location,
                  t.name AS team_name
           FROM friendships f
           JOIN users u ON u.id = CASE WHEN f.user_one_id = %s THEN f.user_two_id ELSE f.user_one_id END
           LEFT JOIN team_memberships tm ON tm.user_id = u.id
           LEFT JOIN teams t ON t.id = tm.team_id
           WHERE (f.user_one_id = %s OR f.user_two_id = %s) AND f.status = 'accepted'
           ORDER BY u.name ASC""",
        (current_user_id(), current_user_id(), current_user_id()),
    )]
    pending = [dict(r) for r in query(
        """SELECT f.id, f.status, f.requested_by, u.id AS user_id, u.name, u.handle, u.avatar_base64
           FROM friendships f
           JOIN users u ON u.id = CASE WHEN f.user_one_id = %s THEN f.user_two_id ELSE f.user_one_id END
           WHERE (f.user_one_id = %s OR f.user_two_id = %s) AND f.status = 'pending'
           ORDER BY f.id DESC""",
        (current_user_id(), current_user_id(), current_user_id()),
    )]
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
    log_event("friend_accept", f"/api/friends/{friendship_id}/accept", {"friendship_id": friendship_id})
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
                  partner_id,
                  u.name,
                  u.handle,
                  u.avatar_base64
           FROM (
             SELECT id, message, created_at,
                    CASE WHEN sender_id = %s THEN receiver_id ELSE sender_id END AS partner_id
             FROM direct_messages
             WHERE sender_id = %s OR receiver_id = %s
           ) dm
           JOIN users u ON u.id = dm.partner_id
           ORDER BY partner_id, dm.id DESC""",
        (current_user_id(), current_user_id(), current_user_id()),
    )]
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
    log_event("direct_message", "/api/direct-messages", {"receiver_id": receiver_id})
    return jsonify({"ok": True}), 201


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
        """INSERT INTO turfs (name, area, distance_km, surface, rating, price_per_hour, upi_id, map_link, latitude, longitude, owner_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (data["name"], data["area"], float(data.get("distance_km",0)),
         data.get("surface","Astroturf"), float(data.get("rating",4.5)),
         int(data.get("price_per_hour",500)), data.get("upi_id",""), data.get("map_link", ""),
         float(data["latitude"]) if data.get("latitude") not in (None, "") else None,
         float(data["longitude"]) if data.get("longitude") not in (None, "") else None,
         int(data["owner_id"]) if data.get("owner_id") not in (None, "") else None),
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
           rating=%s, price_per_hour=%s, upi_id=%s, map_link=%s, latitude=%s, longitude=%s, owner_id=%s WHERE id=%s""",
        (data["name"], data["area"], float(data.get("distance_km",0)),
         data.get("surface","Astroturf"), float(data.get("rating",4.5)),
         int(data.get("price_per_hour",500)), data.get("upi_id",""), data.get("map_link", ""),
         float(data["latitude"]) if data.get("latitude") not in (None, "") else None,
         float(data["longitude"]) if data.get("longitude") not in (None, "") else None,
         int(data["owner_id"]) if data.get("owner_id") not in (None, "") else None, turf_id),
        commit=True,
    )
    log_event("admin_edit_turf", f"/api/admin/turfs/{turf_id}", {"turf_id": turf_id})
    return jsonify({"ok": True})


@app.route("/api/admin/turfs/<int:turf_id>", methods=["DELETE"])
@admin_required
def api_admin_delete_turf(turf_id):
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
    log_event("admin_delete_turf", f"/api/admin/turfs/{turf_id}", {"turf_id": turf_id})
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
    query("UPDATE turfs SET qr_base64=%s WHERE owner_id=%s", (qr_b64, current_owner_id()), commit=True)
    log_event("owner_upload_qr", "/api/owner/qr")
    return jsonify({"ok": True})


@app.route("/api/bookings/<int:slot_id>/info", methods=["GET"])
def api_slot_info_updated(slot_id):
    info = query(
        """SELECT t.upi_id, t.price_per_hour, t.name as turf_name, t.qr_base64, t.map_link
           FROM turf_slots ts JOIN turfs t ON t.id = ts.turf_id
           WHERE ts.id = %s""",
        (slot_id,), one=True,
    )
    if not info:
        return jsonify({"error": "Not found"}), 404
    return jsonify(dict(info))
