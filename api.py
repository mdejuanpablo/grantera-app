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
stripe.api_key = "sk_test_YOUR_SECRET_KEY" 
YOUR_DOMAIN = 'http://127.0.0.1:5500' # Or your live domain

# --- EMAIL CONFIGURATION ---
app.config['MAIL_SERVER']='smtp.gmail.com'
app.config['MAIL_PORT'] = 465
app.config['MAIL_USERNAME'] = 'your-email@gmail.com'
app.config['MAIL_PASSWORD'] = 'your-gmail-app-password'
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
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            plan TEXT DEFAULT 'trial',
            trial_end_date DATE,
            stripe_customer_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS charity_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            charity_name TEXT NOT NULL, charity_ein TEXT, charity_phone TEXT,
            address_1 TEXT, country TEXT, state TEXT, city TEXT, zip_code TEXT,
            contact_name TEXT, contact_title TEXT, contact_phone TEXT,
            mission_statement TEXT, program_description TEXT,
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

@app.route('/api/foundation/<string:ein>')
def get_foundation_profile(ein):
    """Fetches all data for a single foundation profile."""
    db = get_db()
    # Fetch foundation, officers, and grants in separate queries
    foundation = db.execute("SELECT * FROM foundations WHERE ein = ?", (ein,)).fetchone()
    if not foundation:
        return jsonify(error="Foundation not found"), 404
    officers = db.execute("SELECT name, title FROM officers WHERE foundation_ein = ?", (ein,)).fetchall()
    grants = db.execute("SELECT recipient_name, grant_amount, grant_purpose FROM grants WHERE foundation_ein = ? ORDER BY grant_amount DESC", (ein,)).fetchall()
    
    # Assemble the final data object
    profile_data = {
        "name": foundation['name'], "ein": foundation['ein'],
        "address": f"{foundation['city']}, {foundation['state']}",
        "mission": foundation['mission_statement'],
        "assets": foundation['end_of_year_assets'],
        "officers": [dict(row) for row in officers],
        "grants": [dict(row) for row in grants]
    }
    return jsonify(profile_data)

@app.route('/api/signup', methods=['POST'])
def handle_signup():
    """Handles user registration."""
    data = request.json
    email, password = data.get('email'), data.get('password')
    db = get_db()
    if db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone():
        return jsonify(error="An account with this email already exists."), 409
    
    password_hash = generate_password_hash(password)
    trial_end_date = datetime.utcnow() + timedelta(days=14)
    db.execute("INSERT INTO users (email, password_hash, trial_end_date) VALUES (?, ?, ?)",(email, password_hash, trial_end_date))
    db.commit()
    return jsonify(message="User account created successfully."), 201

@app.route('/api/login', methods=['POST'])
def handle_login():
    """Handles user login."""
    data = request.json
    email, password = data.get('email'), data.get('password')
    user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if not user or not check_password_hash(user['password_hash'], password):
        return jsonify(error="Invalid email or password."), 401
    return jsonify({"message": "Login successful!"}), 200

@app.route('/api/save-profile', methods=['POST'])
def save_profile():
    """Saves the user's onboarding profile data."""
    user_id = 1 # Placeholder for session-based user ID
    data = request.json
    sql = """
        INSERT INTO charity_profiles (user_id, charity_name, charity_ein, charity_phone, address_1, country, state, city, zip_code, contact_name, contact_title, contact_phone, mission_statement, program_description) 
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    params = (user_id, data.get('charity_name'), data.get('charity_ein'), data.get('charity_phone'), data.get('address_1'), data.get('country'), data.get('state'), data.get('city'), data.get('zip_code'), data.get('contact_name'), data.get('contact_title'), data.get('contact_phone'), data.get('mission_statement'), data.get('program_description'))
    db = get_db()
    db.execute(sql, params)
    db.commit()
    return jsonify(message="Profile saved successfully!"), 200

@app.route('/api/contact', methods=['POST'])
def handle_contact_form():
    """Receives contact form data and sends an email."""
    data = request.json
    msg = Message(f"New Contact Form: {data.get('subject')}", sender=app.config['MAIL_USERNAME'], recipients=['your-receiving-email@example.com'])
    msg.body = f"From: {data.get('name')} <{data.get('email')}>\n\n{data.get('message')}"
    mail.send(msg)
    return jsonify(message="Email sent successfully!"), 200

@app.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    """Creates a Stripe Checkout session."""
    data = request.json
    checkout_session = stripe.checkout.Session.create(
        line_items=[{'price': data.get('priceId'), 'quantity': 1}],
        mode='subscription',
        subscription_data={"trial_period_days": 14},
        success_url=f"{YOUR_DOMAIN}/success.html?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{YOUR_DOMAIN}/pricing.html",
    )
    return jsonify({'id': checkout_session.id})

# --- MAIN EXECUTION BLOCK ---
if __name__ == '__main__':
    with app.app_context():
        setup_database() # Ensure tables are created when the app starts
    print("--- Starting Flask API Server ---")
    app.run(port=5000, debug=True)
