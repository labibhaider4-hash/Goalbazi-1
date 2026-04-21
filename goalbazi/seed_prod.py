"""
Run once after deploying to Railway to create all tables and seed initial data.

  python seed_prod.py

Make sure DATABASE_URL is set in your environment first.
"""
import os
from server import app, seed_db

with app.app_context():
    seed_db()
    print("✅ Database seeded successfully.")
