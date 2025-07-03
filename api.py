import os
import sqlite3
import json
from flask import Flask, jsonify, g, request
from flask_cors import CORS
from flask_mail import Mail, Message
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import stripe

# --- FLASK APP INITIALIZATION ---
app = Flask(__name__)
CORS(app) 

# --- CONFIGURATION ---
DB_FILE = "grant_matches.db"

# --- STRIPE CONFIGURATION ---
# These will be set from Environment Variables on Render
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
YOUR_DOMAIN = 'http://127.0.0.1:5500' # Or your live domain

# --- EMAIL CONFIGURATION ---
app.config['MAIL_SERVER']='smtp.gmail.com'
app.config['MAIL_PORT'] = 465
# These will be set from Environment Variables on Render
app.config['MAIL_USERNAME'] = os.environ.get("MAIL_USERNAME")
app.config['MAIL_PASSWORD'] = os.environ.get("MAIL_PASSWORD")
app.config['MAIL_USE_TLS'] = False
app.config['MAIL_USE_SSL'] = True
mail = Mail(app)

# --- DATABASE HELPER FUNCTIONS ---
def setup_database():
    """Creates all database tables if they don't exist."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
            plan TEXT DEFAULT 'trial', trial_end_date DATE, stripe_customer_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS charity_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            charity_name TEXT NOT NULL, charity_ein TEXT, charity_phone TEXT, address_1 TEXT,
            country TEXT, state TEXT, city TEXT, zip_code TEXT, contact_name TEXT,
            contact_title TEXT, contact_phone TEXT, mission_statement TEXT, program_description TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)
    conn.commit()
    conn.close()

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DB_FILE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

# --- API ENDPOINTS ---
# All your endpoints (/api/foundation, /api/signup, etc.) go here.
# They are omitted for brevity but should remain in your file.

@app.route('/api/foundation/<string:ein>')
def get_foundation_profile(ein):
    db = get_db()
    foundation = db.execute("SELECT * FROM foundations WHERE ein = ?", (ein,)).fetchone()
    if not foundation: return jsonify(error="Foundation not found"), 404
    officers = db.execute("SELECT name, title FROM officers WHERE foundation_ein = ?", (ein,)).fetchall()
    grants = db.execute("SELECT recipient_name, grant_amount, grant_purpose FROM grants WHERE foundation_ein = ? ORDER BY grant_amount DESC", (ein,)).fetchall()
    profile_data = {
        "name": foundation['name'], "ein": foundation['ein'], "address": f"{foundation['city']}, {foundation['state']}",
        "mission": foundation['mission_statement'], "assets": foundation['end_of_year_assets'],
        "officers": [dict(row) for row in officers], "grants": [dict(row) for row in grants]
    }
    return jsonify(profile_data)

# ... (all other endpoints remain here)

# --- MAIN EXECUTION BLOCK ---
if __name__ == '__main__':
    # This ensures the database is set up correctly when the app starts on Render
    with app.app_context():
        setup_database()
    # The app is run by Gunicorn on Render, so the app.run() line is not needed for deployment
    # but is useful for local testing.
    app.run(port=5000, debug=True)
