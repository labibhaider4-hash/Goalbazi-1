from __future__ import annotations

import hashlib
import json
import os
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


# ---------------------------------------------------------------------------
# Database seeding
# ---------------------------------------------------------------------------

def seed_db():
    conn = get_db()
    cur = conn.cursor()

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

    cur.execute("""
        CREATE TABLE IF NOT EXISTS turf_slots (
            id SERIAL PRIMARY KEY,
            turf_id INTEGER NOT NULL REFERENCES turfs(id),
            slot_date TEXT NOT NULL,
            slot_time TEXT NOT NULL,
            is_booked INTEGER NOT NULL DEFAULT 0,
            booked_by INTEGER REFERENCES users(id)
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

    conn.commit()


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def get_profile(user_id):
    return dict(query("SELECT * FROM users WHERE id = %s", (user_id,), one=True))


def get_stats():
    return [
        {"value": query("SELECT COUNT(*) FROM users", one=True)["count"], "label": "Players"},
        {"value": query("SELECT COUNT(*) FROM games", one=True)["count"], "label": "Games"},
        {"value": query("SELECT COUNT(*) FROM turfs", one=True)["count"], "label": "Turfs"},
        {"value": query("SELECT COUNT(*) FROM leagues", one=True)["count"], "label": "Leagues"},
    ]


def get_turfs(date_value, search=""):
    like = f"%{search.lower()}%"
    turfs = [dict(r) for r in query(
        "SELECT * FROM turfs WHERE LOWER(name) LIKE %s OR LOWER(area) LIKE %s ORDER BY distance_km ASC",
        (like, like),
    )]
    for turf in turfs:
        slots = query(
            "SELECT id, slot_time, is_booked FROM turf_slots WHERE turf_id = %s AND slot_date = %s ORDER BY slot_time",
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
    handle = data.get("handle", "").strip()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    location = data.get("location", "Delhi NCR").strip()

    if not all([name, handle, email, password]):
        return jsonify({"error": "All fields are required"}), 400
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

@app.route("/")
@login_required
def index():
    return send_from_directory(".", "index.html")


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
    leagues = [dict(r) for r in query("SELECT * FROM leagues ORDER BY id ASC")]
    standings = [dict(r) for r in query(
        "SELECT rank, team_name, played, won, points, form FROM standings ORDER BY rank ASC"
    )]
    return jsonify({
        "profile": get_profile(current_user_id()),
        "stats": get_stats(),
        "games": get_games(),
        "turfs": get_turfs(date_value, search),
        "leagues": leagues,
        "standings": standings,
    })


@app.route("/api/profile", methods=["PUT"])
@login_required
def api_profile_update():
    data = request.get_json()
    query(
        "UPDATE users SET name=%s, handle=%s, location=%s, preferred_format=%s, bio=%s WHERE id=%s",
        (data.get("name"), data.get("handle"), data.get("location"), data.get("preferred_format"), data.get("bio"), current_user_id()),
        commit=True,
    )
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
    return jsonify({"ok": True})


@app.route("/api/bookings/<int:slot_id>", methods=["POST"])
@login_required
def api_book_slot(slot_id):
    query(
        "UPDATE turf_slots SET is_booked = 1, booked_by = %s WHERE id = %s AND is_booked = 0",
        (current_user_id(), slot_id),
        commit=True,
    )
    return jsonify({"ok": True}), 201


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    with app.app_context():
        seed_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
