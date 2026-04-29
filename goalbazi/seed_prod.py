"""
Production database bootstrap helper.

Run this manually only when you need to create/update the PostgreSQL schema and
seed the starter data outside a normal web request. The main app also calls
seed_db() on startup, so this file is mostly a safety tool for deployments.

Usage:
  python seed_prod.py

Make sure DATABASE_URL is set in your environment first.
"""

from server import app, seed_db


with app.app_context():
    seed_db()
    print("Database seeded successfully.")
