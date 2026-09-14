"""
app.py
======
Intelligent Student Attendance Management System
using QR Code and Face Recognition.

Main Flask application. Run with:  python app.py
Then open:  http://127.0.0.1:5000

Architecture (kept intentionally simple for a diploma thesis project):
    - Flask serves both the HTML pages (Jinja2 templates) AND a small
      JSON API consumed by JavaScript on the client (for QR scanning
      and face capture, which need the browser's camera).
    - SQLite (via database.py) stores users, sessions and attendance.
    - face_utils.py wraps the `face_recognition` (dlib) library.
    - qr_utils.py generates QR codes as base64 images.
    - Authentication uses Flask's built-in signed-cookie session
      (no external auth library needed).
"""

import uuid
from datetime import datetime
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash

import database
import face_utils
import qr_utils

# ---------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------
app = Flask(__name__)

# IMPORTANT: change this to a random secret in production. It is used to
# cryptographically sign the session cookie so users can't forge it.
app.config["SECRET_KEY"] = "diploma-thesis-attendance-system-secret-key-change-me"
app.config["MAX_FORM_MEMORY_SIZE"] = 8 * 1024 * 1024   # 8 MB per form field
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024    # 16 MB total request

# Create the database tables on startup (safe to run every time).
database.init_db()


# ---------------------------------------------------------------------
# Auth helper decorators
# ---------------------------------------------------------------------
def login_required(f):
    """Redirects to /login if there is no logged-in user in the session."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "error")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def role_required(role):
    """Restricts a route to a specific role ('professor' or 'student')."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if "user_id" not in session:
                flash("Please log in to continue.", "error")
                return redirect(url_for("login"))
            if session.get("role") != role:
                flash("You do not have permission to view that page.", "error")
                return redirect(url_for("index"))
            return f(*args, **kwargs)
        return wrapper
    return decorator


def get_current_user(db):
    """Fetches the full user row for the currently logged-in user."""
    return db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()


# ---------------------------------------------------------------------
# Home / index
# ---------------------------------------------------------------------
@app.route("/")
def index():
    if "user_id" in session:
        if session["role"] == "professor":
            return redirect(url_for("professor_dashboard"))
        return redirect(url_for("student_dashboard"))
    return render_template("index.html")


# ---------------------------------------------------------------------
# REGISTER
# ---------------------------------------------------------------------
@app.route("/register", methods=["GET", "POST"])
def register():
    """
    GET  -> shows the registration form.
    POST -> creates a new user.
            Students MUST also submit one clear photo of their face
            (captured via the browser's webcam as a base64 image), which
            is converted into a 128-d face encoding and stored in the DB.
            This encoding is later used by /verify-face to recognize them.
    """
    if request.method == "GET":
        return render_template("register.html")

    full_name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    role = request.form.get("role", "")
    student_id = request.form.get("student_id", "").strip()
    face_image_data = request.form.get("face_image", "")  # base64 data-URL, students only

    # ---- Basic validation ----
    if not full_name or not email or not password or role not in ("professor", "student"):
        flash("Please fill in all required fields.", "error")
        return render_template("register.html")

    if role == "student" and not face_image_data:
        flash("Students must capture a face photo to register (needed for face recognition).", "error")
        return render_template("register.html")

    from werkzeug.security import generate_password_hash

    db = database.get_db()
    try:
        # ---- Check email uniqueness ----
        existing = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing:
            flash("An account with that email already exists.", "error")
            return render_template("register.html")

        face_encoding_json = None

        if role == "student":
            # ---- AI step: extract the face encoding from the captured photo ----
            try:
                image_array = face_utils.decode_base64_image(face_image_data)
                encoding = face_utils.extract_face_encoding(image_array)
            except ValueError as e:
                flash(str(e), "error")
                return render_template("register.html")

            if encoding is None:
                flash("No face was detected in the photo. Please retake it with good lighting, "
                      "facing the camera directly.", "error")
                return render_template("register.html")

            face_encoding_json = face_utils.encoding_to_json(encoding)

        password_hash = generate_password_hash(password)

        db.execute(
            """INSERT INTO users (full_name, email, password_hash, role, student_id, face_encoding)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (full_name, email, password_hash, role, student_id or None, face_encoding_json),
        )
        db.commit()

        flash("Registration successful! You can now log in.", "success")
        return redirect(url_for("login"))
    finally:
        db.close()


# ---------------------------------------------------------------------
# LOGIN / LOGOUT
# ---------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    from werkzeug.security import check_password_hash

    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    db = database.get_db()
    try:
        user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Invalid email or password.", "error")
            return render_template("login.html")

        # Store minimal identifying info in the signed session cookie.
        session["user_id"] = user["id"]
        session["full_name"] = user["full_name"]
        session["role"] = user["role"]

        flash(f"Welcome back, {user['full_name']}!", "success")
        if user["role"] == "professor":
            return redirect(url_for("professor_dashboard"))
        return redirect(url_for("student_dashboard"))
    finally:
        db.close()


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------
# PROFESSOR DASHBOARD
# ---------------------------------------------------------------------
@app.route("/professor/dashboard")
@role_required("professor")
def professor_dashboard():
    db = database.get_db()
    try:
        my_sessions = db.execute(
            """SELECT s.*,
                      (SELECT COUNT(*) FROM attendance a WHERE a.session_id = s.id) AS present_count
               FROM sessions s
               WHERE s.professor_id = ?
               ORDER BY s.created_at DESC""",
            (session["user_id"],),
        ).fetchall()
        return render_template("professor_dashboard.html", sessions=my_sessions)
    finally:
        db.close()


@app.route("/create-session", methods=["GET", "POST"])
@role_required("professor")
def create_session():
    """
    GET  -> shows the "new lecture session" form.
    POST -> creates the session row + generates its QR code, then shows
            the QR code on-screen for students to scan.
    """
    if request.method == "GET":
        return render_template("create_session.html", qr_image=None, subject=None)

    subject = request.form.get("subject", "").strip()
    if not subject:
        flash("Please enter a subject/lecture name.", "error")
        return render_template("create_session.html", qr_image=None, subject=None)

    token = uuid.uuid4().hex

    db = database.get_db()
    try:
        cur = db.execute(
            "INSERT INTO sessions (professor_id, subject, token, is_active) VALUES (?, ?, ?, 1)",
            (session["user_id"], subject, token),
        )
        db.commit()
        new_session_id = cur.lastrowid

        qr_image = qr_utils.generate_qr_base64(new_session_id, token)

        return render_template(
            "create_session.html",
            qr_image=qr_image,
            subject=subject,
            session_id=new_session_id,
        )
    finally:
        db.close()


@app.route("/end-session/<int:session_id>", methods=["POST"])
@role_required("professor")
def end_session(session_id):
    """Closes a session so its QR code can no longer be used to check in."""
    db = database.get_db()
    try:
        db.execute(
            "UPDATE sessions SET is_active = 0 WHERE id = ? AND professor_id = ?",
            (session_id, session["user_id"]),
        )
        db.commit()
        flash("Session closed.", "success")
    finally:
        db.close()
    return redirect(url_for("professor_dashboard"))


# ---------------------------------------------------------------------
# ATTENDANCE REPORT  (GET /attendance-report)
# ---------------------------------------------------------------------
@app.route("/attendance-report")
@role_required("professor")
def attendance_report():
    """
    Shows attendance for all of this professor's sessions.
    Optional ?session_id=<id> query param filters to a single session.
    """
    selected_session_id = request.args.get("session_id", type=int)

    db = database.get_db()
    try:
        my_sessions = db.execute(
            "SELECT * FROM sessions WHERE professor_id = ? ORDER BY created_at DESC",
            (session["user_id"],),
        ).fetchall()

        if selected_session_id:
            records = db.execute(
                """SELECT a.*, u.full_name, u.student_id, u.email, s.subject
                   FROM attendance a
                   JOIN users u ON u.id = a.student_id
                   JOIN sessions s ON s.id = a.session_id
                   WHERE a.session_id = ? AND s.professor_id = ?
                   ORDER BY a.marked_at ASC""",
                (selected_session_id, session["user_id"]),
            ).fetchall()
        else:
            records = db.execute(
                """SELECT a.*, u.full_name, u.student_id, u.email, s.subject
                   FROM attendance a
                   JOIN users u ON u.id = a.student_id
                   JOIN sessions s ON s.id = a.session_id
                   WHERE s.professor_id = ?
                   ORDER BY a.marked_at DESC""",
                (session["user_id"],),
            ).fetchall()

        return render_template(
            "attendance_report.html",
            sessions=my_sessions,
            records=records,
            selected_session_id=selected_session_id,
        )
    finally:
        db.close()


# ---------------------------------------------------------------------
# STUDENT DASHBOARD
# ---------------------------------------------------------------------
@app.route("/student/dashboard")
@role_required("student")
def student_dashboard():
    db = database.get_db()
    try:
        history = db.execute(
            """SELECT a.marked_at, a.status, s.subject
               FROM attendance a
               JOIN sessions s ON s.id = a.session_id
               WHERE a.student_id = ?
               ORDER BY a.marked_at DESC
               LIMIT 20""",
            (session["user_id"],),
        ).fetchall()
        return render_template("student_dashboard.html", history=history)
    finally:
        db.close()


# ---------------------------------------------------------------------
# SCAN QR  (POST /scan-qr)
# ---------------------------------------------------------------------
@app.route("/scan-qr", methods=["POST"])
@role_required("student")
def scan_qr():
    """
    Called by student.js after the browser's QR scanner decodes a QR
    code into text. We validate that the session exists, is still open,
    and that the student hasn't already checked in.

    On success we DON'T mark attendance yet -- we just remember
    ("pending_session_id") that this student passed the QR step, and
    the frontend then moves on to the face-verification step.
    Final attendance is only written in /verify-face, once BOTH
    QR + face checks have passed (see requirement: "QR valid + face match").
    """
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id")
    token = data.get("token")

    if session_id is None or not token:
        return jsonify({"success": False, "message": "Malformed QR code."}), 400

    db = database.get_db()
    try:
        lecture = db.execute(
            "SELECT * FROM sessions WHERE id = ? AND token = ?", (session_id, token)
        ).fetchone()

        if lecture is None:
            return jsonify({"success": False, "message": "This QR code is not valid."}), 404

        if not lecture["is_active"]:
            return jsonify({"success": False, "message": "This lecture session has been closed."}), 400

        already_marked = db.execute(
            "SELECT 1 FROM attendance WHERE session_id = ? AND student_id = ?",
            (session_id, session["user_id"]),
        ).fetchone()
        if already_marked:
            return jsonify({"success": False, "message": "You are already marked present for this lecture."}), 400

        # Remember that this student passed the QR check for this session.
        session["pending_session_id"] = session_id

        return jsonify({
            "success": True,
            "message": f"QR verified for '{lecture['subject']}'. Now confirm your face.",
            "subject": lecture["subject"],
        })
    finally:
        db.close()


# ---------------------------------------------------------------------
# VERIFY FACE  (POST /verify-face)
# ---------------------------------------------------------------------
@app.route("/verify-face", methods=["POST"])
@role_required("student")
def verify_face():
    """
    Called by student.js after capturing a live photo from the webcam.
    Requires that /scan-qr was already called successfully in this
    session (session['pending_session_id'] must be set).

    Logic:  QR valid (already checked)  +  face match  ->  mark present.
    """
    pending_session_id = session.get("pending_session_id")
    if not pending_session_id:
        return jsonify({"success": False, "message": "Please scan the QR code first."}), 400

    data = request.get_json(silent=True) or {}
    image_data = data.get("image")
    if not image_data:
        return jsonify({"success": False, "message": "No image received."}), 400

    db = database.get_db()
    try:
        student = get_current_user(db)
        if not student["face_encoding"]:
            return jsonify({
                "success": False,
                "message": "No face is registered on your account. Please contact your professor."
            }), 400

        # ---- AI step: extract encoding from the live camera capture ----
        try:
            image_array = face_utils.decode_base64_image(image_data)
            live_encoding = face_utils.extract_face_encoding(image_array)
        except ValueError as e:
            return jsonify({"success": False, "message": str(e)}), 400

        if live_encoding is None:
            return jsonify({"success": False, "message": "No face detected. Please face the camera directly."}), 400

        known_encoding = face_utils.encoding_from_json(student["face_encoding"])
        is_match, distance = face_utils.compare_faces(known_encoding, live_encoding)

        if not is_match:
            return jsonify({
                "success": False,
                "message": "Face does not match our records. Attendance not marked.",
                "distance": round(distance, 3),
            }), 401

        # Re-verify the session is still valid & not already marked (race-safety).
        lecture = db.execute(
            "SELECT * FROM sessions WHERE id = ? AND is_active = 1", (pending_session_id,)
        ).fetchone()
        if lecture is None:
            session.pop("pending_session_id", None)
            return jsonify({"success": False, "message": "This lecture session is no longer active."}), 400

        try:
            db.execute(
                "INSERT INTO attendance (session_id, student_id, status) VALUES (?, ?, 'present')",
                (pending_session_id, session["user_id"]),
            )
            db.commit()
        except Exception:
            # UNIQUE constraint -> already marked (e.g. double-submit)
            db.rollback()

        session.pop("pending_session_id", None)

        return jsonify({
            "success": True,
            "message": f"Attendance marked present for '{lecture['subject']}'.",
            "distance": round(distance, 3),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
    finally:
        db.close()


# ---------------------------------------------------------------------
if __name__ == "__main__":
    # debug=True is convenient for a thesis demo; turn off in production.
    app.run(debug=True, host="0.0.0.0", port=5000, ssl_context="adhoc")
