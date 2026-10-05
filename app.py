"""
app.py
======
Intelligent Student Attendance Management System
using QR Code and Face Recognition.

Main Flask application. Run with:  python app.py
Then open:  https://127.0.0.1:5000

Architecture (kept intentionally simple for a diploma thesis project):
    - Flask serves both the HTML pages (Jinja2 templates) AND a small
      JSON API consumed by JavaScript on the client (for QR scanning
      and face capture, which need the browser's camera).
    - SQLite (via database.py) stores users, sessions and attendance.
    - face_utils.py wraps the `face_recognition` (dlib) library.
    - qr_utils.py generates QR codes as base64 images.
    - Authentication uses Flask's built-in signed-cookie session
      (no external auth library needed).

Three roles:
    - admin      manages professors, students and the academic structure,
                 sees every session/report
    - professor  creates lecture sessions for their subjects and views
                 reports for them
    - student    checks in (QR + face) to the subjects of their academic
                 program and sees their own attendance

The academic structure (faculties, programs, subjects...) and the rules
for who may check in to which subject live in academic.py.
"""

import csv
import io
import os
import secrets
import smtplib
import socket
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from functools import wraps

from flask import (
    Flask, Response, abort, flash, g, jsonify, redirect, render_template,
    request, send_file, session, url_for,
)
from PIL import Image
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

import academic
import database
import face_utils
import qr_utils

# ---------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------
app = Flask(__name__)

# IMPORTANT: set the SECRET_KEY environment variable to a random value in
# production. It is used to cryptographically sign the session cookie and
# the password-reset links so users can't forge them.
app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY", "diploma-thesis-attendance-system-secret-key-change-me"
)
app.config["MAX_FORM_MEMORY_SIZE"] = 8 * 1024 * 1024   # 8 MB per form field
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024    # 16 MB total request

# Optional e-mail settings (used for "forgot password" links). If
# MAIL_SERVER is not set, reset links are printed in the server console.
MAIL_SERVER = os.environ.get("MAIL_SERVER")
MAIL_PORT = int(os.environ.get("MAIL_PORT", "587"))
MAIL_USERNAME = os.environ.get("MAIL_USERNAME")
MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD")
MAIL_FROM = os.environ.get("MAIL_FROM", MAIL_USERNAME or "no-reply@attendance.local")

RESET_LINK_MAX_AGE = 60 * 60   # password-reset links are valid for 1 hour
MIN_PASSWORD_LENGTH = 8
ROLES = ("admin", "professor", "student")

# Create the database tables on startup (safe to run every time).
database.init_db()

# Admin pages for the academic structure, subjects and e-mail domains.
app.register_blueprint(academic.bp)


# ---------------------------------------------------------------------
# Database connection (one per request, closed automatically)
# ---------------------------------------------------------------------
get_db = database.request_db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------------------------------------------------------------------
# Per-request checks: who is logged in, CSRF, forced password change
# ---------------------------------------------------------------------
def csrf_token():
    """Returns the per-login anti-forgery token (created on first use)."""
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


@app.before_request
def load_user_and_protect():
    g.user = None
    if request.endpoint == "static":
        return None

    # ---- Load the logged-in user fresh from the DB on every request, so
    #      role changes / deactivation by an admin apply immediately. ----
    user_id = session.get("user_id")
    if user_id is not None:
        g.user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if g.user is None or not g.user["is_active"]:
            session.clear()
            g.user = None
            flash("Your account is no longer available. Please log in again.", "error")

    # ---- CSRF protection: every POST must carry the token that was
    #      rendered into the page (form field or X-CSRFToken header). ----
    if request.method == "POST":
        sent = request.form.get("csrf_token") or request.headers.get("X-CSRFToken", "")
        expected = session.get("_csrf", "")
        if not expected or not secrets.compare_digest(sent.encode(), expected.encode()):
            message = "Your session has expired. Please try again."
            if request.is_json:
                return jsonify({"success": False, "message": message}), 400
            flash(message, "error")
            return redirect(url_for("index"))

    # ---- Users on a temporary password must replace it first. ----
    if g.user and g.user["must_change_password"] and request.endpoint not in ("account", "logout"):
        if request.is_json:
            return jsonify({"success": False, "message": "Please set a new password first."}), 403
        return redirect(url_for("account"))

    return None


@app.context_processor
def inject_globals():
    return {"current_user": g.get("user"), "csrf_token": csrf_token}


# ---------------------------------------------------------------------
# Template filters
# ---------------------------------------------------------------------
def to_local(value):
    """SQLite stores CURRENT_TIMESTAMP in UTC; convert it to local time."""
    if not value:
        return None
    if isinstance(value, str):
        value = datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")
    return value.replace(tzinfo=timezone.utc).astimezone()


@app.template_filter("localtime")
def localtime_filter(value, fmt="%d %b %Y, %H:%M"):
    local = to_local(value)
    return local.strftime(fmt) if local else "-"


def percent(part, whole):
    """Whole-number percentage, safe when `whole` is 0."""
    return round(100 * part / whole) if whole else 0


app.jinja_env.globals["percent"] = percent


# ---------------------------------------------------------------------
# Auth helper decorators
# ---------------------------------------------------------------------
def login_required(f):
    """Redirects to /login if nobody is logged in."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if g.user is None:
            flash("Please log in to continue.", "error")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def role_required(*roles):
    """Restricts a route to one or more roles ('admin', 'professor', 'student')."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if g.user is None:
                flash("Please log in to continue.", "error")
                return redirect(url_for("login"))
            if g.user["role"] not in roles:
                flash("You do not have permission to view that page.", "error")
                return redirect(url_for("index"))
            return f(*args, **kwargs)
        return wrapper
    return decorator


def clean_name(value):
    """Trims a name and collapses repeated spaces."""
    return " ".join((value or "").split())


def home_url(user):
    """The landing page of each role."""
    return url_for({
        "admin": "admin_dashboard",
        "professor": "professor_dashboard",
        "student": "student_dashboard",
    }[user["role"]])


# ---------------------------------------------------------------------
# Password / e-mail helpers
# ---------------------------------------------------------------------
def password_problem(password, confirm=None):
    """Returns an error message if the password is not acceptable, else None."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"The password must be at least {MIN_PASSWORD_LENGTH} characters long."
    if confirm is not None and password != confirm:
        return "The two passwords do not match."
    return None


def reset_serializer():
    return URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="password-reset")


def make_reset_token(user):
    # Part of the current password hash is signed into the token, so the
    # link stops working as soon as the password is changed (single use).
    return reset_serializer().dumps([user["id"], user["password_hash"][-16:]])


def user_from_reset_token(token):
    """Returns the user a reset link belongs to, or None if invalid/expired/used."""
    try:
        user_id, hash_tail = reset_serializer().loads(token, max_age=RESET_LINK_MAX_AGE)
    except (BadSignature, SignatureExpired, ValueError, TypeError):
        return None
    user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None or not user["is_active"] or user["password_hash"][-16:] != hash_tail:
        return None
    return user


def send_email(to, subject, body):
    """
    Sends an e-mail through the SMTP server configured in the MAIL_*
    environment variables. Without that configuration (e.g. during a
    thesis demo) the message is printed in the server console instead.
    """
    if not MAIL_SERVER:
        print(f"\n--- E-mail (not sent: MAIL_SERVER is not configured) ---\n"
              f"To: {to}\nSubject: {subject}\n\n{body}\n--- end ---\n", flush=True)
        return False

    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = MAIL_FROM, to, subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(MAIL_SERVER, MAIL_PORT, timeout=15) as smtp:
            smtp.starttls()
            if MAIL_USERNAME:
                smtp.login(MAIL_USERNAME, MAIL_PASSWORD or "")
            smtp.send_message(msg)
        return True
    except (OSError, smtplib.SMTPException) as e:
        print(f" * Could not send e-mail to {to}: {e}", flush=True)
        return False


def face_from_form(face_image_data):
    """
    AI step: turns a face photo (captured with the webcam or uploaded
    from the device) into a JSON face encoding.
    Returns (encoding_json, image_array, None) on success
    or      (None, None, error_message).
    """
    if not face_image_data:
        return None, None, "Please take or upload a face photo (needed for face recognition)."
    try:
        image_array = face_utils.decode_base64_image(face_image_data)
        encoding = face_utils.extract_face_encoding(image_array)
    except ValueError as e:
        return None, None, str(e)
    except Exception:
        return None, None, "The photo could not be read. Please try another one."
    if encoding is None:
        return None, None, ("No face was detected in the photo. Please use one with good lighting, "
                            "facing the camera directly.")
    return face_utils.encoding_to_json(encoding), image_array, None


def face_photo_dir():
    """Folder holding the students' profile photos (next to the database, not public)."""
    return os.path.join(os.path.dirname(database.DB_PATH), "faces")


def save_face(db, user_id, encoding_json, image_array):
    """Stores a student's face encoding and the photo it was made from."""
    os.makedirs(face_photo_dir(), exist_ok=True)
    filename = f"{user_id}.jpg"
    photo = Image.fromarray(image_array)
    photo.thumbnail((600, 600))
    photo.save(os.path.join(face_photo_dir(), filename), "JPEG", quality=88)
    db.execute(
        "UPDATE users SET face_encoding = ?, face_photo = ? WHERE id = ?",
        (encoding_json, filename, user_id),
    )


def delete_face_photo(user):
    """Removes a user's photo file (the caller clears/deletes the DB row)."""
    if user["face_photo"]:
        try:
            os.remove(os.path.join(face_photo_dir(), user["face_photo"]))
        except OSError:
            pass


# ---------------------------------------------------------------------
# QR / check-in helpers
# ---------------------------------------------------------------------
def get_lan_ip():
    """Returns this machine's local-network IP (or None if it can't be found)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No packet is actually sent; this just asks the OS which
        # network interface it would use to reach the local network.
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def build_checkin_url(session_id, token):
    """
    Builds the URL that is encoded in a session's QR code.

    If the professor opened the app via localhost/127.0.0.1, that address
    would be useless on a student's phone, so we swap in the machine's
    LAN IP so the link is reachable from other devices on the same network.
    """
    base = request.host_url.rstrip("/")
    hostname = request.host.split(":")[0]
    if hostname in ("127.0.0.1", "localhost"):
        lan_ip = get_lan_ip()
        if lan_ip:
            base = base.replace(hostname, lan_ip, 1)
    return f"{base}{url_for('checkin', session_id=session_id, token=token)}"


def subject_access_problem(db, lecture):
    """
    THE eligibility gate: may the logged-in student check in to this
    lecture's subject? Called on the server for every way of checking
    in (QR link, in-page scanner, and again right before attendance is
    written), so hiding a subject in the browser is never what protects it.

    Returns (error_message, http_status), or None when access is allowed.
    """
    if lecture["subject_id"] is None:
        # Sessions from before subjects existed: kept for their reports only.
        return "This lecture session can no longer accept check-ins.", 400
    if not academic.is_eligible(db, g.user["id"], lecture["subject_id"]):
        return academic.NOT_ELIGIBLE_MESSAGE, 403
    subject = db.execute("SELECT is_active FROM subjects WHERE id = ?", (lecture["subject_id"],)).fetchone()
    if not subject["is_active"]:
        return "This subject is not active at the moment.", 400
    return None


def validate_checkin(db, session_id, token):
    """
    Checks that a scanned QR code refers to an open session of a subject
    the current student is eligible for, and that they haven't already
    checked in to it.

    Returns (lecture_row, None, None) on success,
    or      (None, error_message, http_status) on failure.
    """
    if session_id is None or not token:
        return None, "Malformed QR code.", 400

    lecture = db.execute(
        "SELECT * FROM sessions WHERE id = ? AND token = ?", (session_id, token)
    ).fetchone()

    if lecture is None:
        return None, "This QR code is not valid.", 404

    if not lecture["is_active"]:
        return None, "This lecture session has been closed.", 400

    problem = subject_access_problem(db, lecture)
    if problem:
        return None, problem[0], problem[1]

    already_marked = db.execute(
        "SELECT 1 FROM attendance WHERE session_id = ? AND student_id = ?",
        (lecture["id"], g.user["id"]),
    ).fetchone()
    if already_marked:
        return None, "You are already marked present for this lecture.", 400

    return lecture, None, None


def get_owned_session(session_id):
    """
    Fetches a lecture session the current user may manage: professors
    only their own, admins any. Aborts with 404 otherwise.
    """
    lecture = get_db().execute(
        """SELECT s.*, p.full_name AS professor_name
           FROM sessions s JOIN users p ON p.id = s.professor_id
           WHERE s.id = ?""",
        (session_id,),
    ).fetchone()
    if lecture is None or (g.user["role"] != "admin" and lecture["professor_id"] != g.user["id"]):
        abort(404)
    return lecture


def sessions_home():
    """Where to send a professor/admin back to after acting on a session."""
    return url_for("admin_sessions" if g.user["role"] == "admin" else "professor_dashboard")


# ---------------------------------------------------------------------
# Home / index
# ---------------------------------------------------------------------
@app.route("/")
def index():
    if g.user:
        return redirect(home_url(g.user))
    return render_template("index.html")


# ---------------------------------------------------------------------
# REGISTER  (students only - professors/admins are created by an admin)
# ---------------------------------------------------------------------
def render_register(form):
    db = get_db()
    return render_template(
        "register.html", form=form,
        tree=academic.structure_tree(db), domains=academic.active_domains(db),
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    """
    GET  -> shows the registration form.
    POST -> creates a new student. Checked here on the server:
              - the e-mail belongs to an allowed university domain
              - faculty / program / year of study / specialisation are
                real, active, and belong together
            The student MUST also submit one clear photo of their face
            (taken with the webcam or uploaded from the device, sent as
            a base64 image), which is converted into a 128-d face
            encoding and stored in the DB together with the photo.
            This encoding is later used by /verify-face to recognize them.
    """
    if g.user:
        return redirect(home_url(g.user))
    if request.method == "GET":
        return render_register({})

    form = request.form
    first_name = clean_name(form.get("first_name"))
    last_name = clean_name(form.get("last_name"))
    email = form.get("email", "").strip().lower()
    password = form.get("password", "")
    student_id = form.get("student_id", "").strip()

    def fail(message):
        flash(message, "error")
        return render_register(form)

    # ---- Basic validation ----
    if not first_name or not last_name or not email or not student_id:
        return fail("Please fill in all required fields.")

    db = get_db()
    problem = academic.email_domain_problem(db, email) or password_problem(password, form.get("confirm_password", ""))
    if problem:
        return fail(problem)

    if db.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        return fail("An account with that email already exists.")

    group, error = academic.group_from_form(db, form)
    if error:
        return fail(error)

    face_encoding_json, image_array, error = face_from_form(form.get("face_image", ""))
    if error:
        return fail(error)

    cur = db.execute(
        """INSERT INTO users (full_name, first_name, last_name, email, password_hash, role, student_id,
                              program_id, study_year_id, specialisation_id)
           VALUES (?, ?, ?, ?, ?, 'student', ?, ?, ?, ?)""",
        (f"{first_name} {last_name}", first_name, last_name, email, generate_password_hash(password),
         student_id, group["program_id"], group["study_year_id"], group["specialisation_id"]),
    )
    save_face(db, cur.lastrowid, face_encoding_json, image_array)
    db.commit()

    flash("Registration successful! You can now log in.", "success")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------
# LOGIN / LOGOUT
# ---------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if g.user:
            return redirect(home_url(g.user))
        return render_template("login.html", email="")

    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

    if user is None or not check_password_hash(user["password_hash"], password):
        flash("Invalid email or password.", "error")
        return render_template("login.html", email=email)

    if not user["is_active"]:
        flash("This account has been deactivated. Please contact an administrator.", "error")
        return render_template("login.html", email=email)

    # Start a fresh signed session cookie holding only the user's id.
    next_checkin = session.get("next_checkin")
    session.clear()
    session["user_id"] = user["id"]

    if user["must_change_password"]:
        flash("You are using a temporary password. Please choose a new one.", "success")
        return redirect(url_for("account"))

    flash(f"Welcome back, {user['full_name']}!", "success")

    # If the student got here by scanning a QR code with their phone
    # camera before logging in, continue that check-in now.
    if user["role"] == "student" and next_checkin:
        return redirect(url_for("checkin", **next_checkin))
    return redirect(home_url(user))


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------
# FORGOT / RESET PASSWORD
# ---------------------------------------------------------------------
@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """
    Sends a one-hour, single-use reset link to the given e-mail address.
    The response is identical whether or not the address is registered,
    so the form cannot be used to discover who has an account.
    """
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = get_db().execute(
            "SELECT * FROM users WHERE email = ? AND is_active = 1", (email,)
        ).fetchone()
        if user:
            link = url_for("reset_password", token=make_reset_token(user), _external=True)
            send_email(
                user["email"],
                "Reset your Smart Attendance password",
                f"Hello {user['full_name']},\n\n"
                f"Use the link below to choose a new password. It is valid for 1 hour "
                f"and can be used once.\n\n{link}\n\n"
                f"If you did not ask for this, you can ignore this e-mail.",
            )
        flash("If an account exists for that email, a password reset link has been sent.", "success")
        return redirect(url_for("login"))

    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    user = user_from_reset_token(token)
    if user is None:
        flash("This password reset link is invalid or has expired. Please request a new one.", "error")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        password = request.form.get("password", "")
        problem = password_problem(password, request.form.get("confirm_password", ""))
        if problem:
            flash(problem, "error")
            return render_template("reset_password.html", token=token, email=user["email"])

        db = get_db()
        db.execute(
            "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
            (generate_password_hash(password), user["id"]),
        )
        db.commit()
        session.clear()
        flash("Your password has been changed. You can now log in.", "success")
        return redirect(url_for("login"))

    return render_template("reset_password.html", token=token, email=user["email"])


# ---------------------------------------------------------------------
# MY ACCOUNT  (profile + change password, all roles)
# ---------------------------------------------------------------------
@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    db = get_db()

    if request.method == "POST":
        action = request.form.get("action")

        if action == "profile":
            first_name = clean_name(request.form.get("first_name"))
            last_name = clean_name(request.form.get("last_name"))
            if not first_name:
                flash("Your name cannot be empty.", "error")
            else:
                db.execute(
                    "UPDATE users SET first_name = ?, last_name = ?, full_name = ? WHERE id = ?",
                    (first_name, last_name, f"{first_name} {last_name}".strip(), g.user["id"]),
                )
                db.commit()
                flash("Profile updated.", "success")

        elif action == "password":
            new_password = request.form.get("new_password", "")
            problem = password_problem(new_password, request.form.get("confirm_password", ""))
            if not check_password_hash(g.user["password_hash"], request.form.get("current_password", "")):
                flash("Your current password is not correct.", "error")
            elif problem:
                flash(problem, "error")
            elif check_password_hash(g.user["password_hash"], new_password):
                flash("The new password must be different from the current one.", "error")
            else:
                db.execute(
                    "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
                    (generate_password_hash(new_password), g.user["id"]),
                )
                db.commit()
                flash("Password changed.", "success")
                return redirect(home_url(g.user))

        return redirect(url_for("account"))

    return render_template("account.html", profile=academic.academic_profile(db, g.user))


# ---------------------------------------------------------------------
# FACE PHOTO  (students add or replace their own photo; it is shown on
#              their profile and to administrators)
# ---------------------------------------------------------------------
@app.route("/enroll-face", methods=["GET", "POST"])
@role_required("student")
def enroll_face():
    """
    GET  -> page to take a photo with the camera or upload one.
    POST -> stores the new photo + face encoding. Replacing an existing
            photo needs the account password, so that somebody holding
            an unlocked phone cannot swap in their own face.
    """
    if request.method == "POST":
        replacing = bool(g.user["face_encoding"])
        if replacing and not check_password_hash(g.user["password_hash"], request.form.get("current_password", "")):
            flash("Your password is not correct.", "error")
            return render_template("enroll_face.html")

        face_encoding_json, image_array, error = face_from_form(request.form.get("face_image", ""))
        if error:
            flash(error, "error")
            return render_template("enroll_face.html")

        db = get_db()
        save_face(db, g.user["id"], face_encoding_json, image_array)
        db.commit()
        flash("Face photo saved. You can now check in to lectures.", "success")
        return redirect(url_for("account" if replacing else "student_dashboard"))

    return render_template("enroll_face.html")


@app.route("/face-photo/<int:user_id>")
@login_required
def face_photo(user_id):
    """Serves a student's profile photo: only to that student and to admins."""
    if g.user["id"] != user_id and g.user["role"] != "admin":
        abort(404)
    user = get_user_or_404(user_id)
    path = os.path.join(face_photo_dir(), user["face_photo"] or "")
    if not user["face_photo"] or not os.path.isfile(path):
        abort(404)
    response = send_file(path, mimetype="image/jpeg", max_age=0)
    response.headers["Cache-Control"] = "private, no-store"
    return response


# ---------------------------------------------------------------------
# PROFESSOR DASHBOARD + LECTURE SESSIONS
# ---------------------------------------------------------------------
@app.route("/professor/dashboard")
@role_required("professor")
def professor_dashboard():
    my_sessions = get_db().execute(
        """SELECT s.*,
                  (SELECT COUNT(*) FROM attendance a WHERE a.session_id = s.id) AS present_count
           FROM sessions s
           WHERE s.professor_id = ?
           ORDER BY s.created_at DESC""",
        (g.user["id"],),
    ).fetchall()
    stats = {
        "sessions": len(my_sessions),
        "active": sum(1 for s in my_sessions if s["is_active"]),
        "checkins": sum(s["present_count"] for s in my_sessions),
    }
    return render_template("professor_dashboard.html", sessions=my_sessions, stats=stats)


@app.route("/professor/subjects")
@role_required("professor")
def professor_subjects():
    """
    Every subject an admin assigned to this professor: who takes it, how
    many sessions were held, and a shortcut to start one / project its QR.
    """
    db = get_db()
    subjects = db.execute(
        f"""SELECT sub.*,
                   (SELECT COUNT(*) FROM users u
                     WHERE u.role = 'student' AND u.is_active = 1
                       AND {academic.eligible_sql('sub.id')}) AS students,
                   (SELECT COUNT(*) FROM sessions s
                     WHERE s.subject_id = sub.id AND s.professor_id = sp.professor_id) AS sessions,
                   (SELECT MAX(s.created_at) FROM sessions s
                     WHERE s.subject_id = sub.id AND s.professor_id = sp.professor_id) AS last_session,
                   (SELECT s.id FROM sessions s
                     WHERE s.subject_id = sub.id AND s.professor_id = sp.professor_id AND s.is_active = 1
                     ORDER BY s.created_at DESC LIMIT 1) AS open_session
            FROM subjects sub
            JOIN subject_professors sp ON sp.subject_id = sub.id
            WHERE sp.professor_id = ?
            ORDER BY sub.is_active DESC, sub.name COLLATE NOCASE""",
        (g.user["id"],),
    ).fetchall()
    return render_template("professor_subjects.html", subjects=subjects, groups=academic.subject_groups(db))


@app.route("/create-session", methods=["GET", "POST"])
@role_required("professor")
def create_session():
    """
    GET  -> shows the "new lecture session" form with the professor's subjects.
    POST -> creates the session row, then redirects to its QR page
            (/session/<id>/qr). Redirecting means refreshing the QR page
            doesn't re-submit the form and create a duplicate session.

    The session belongs to a subject: that is what decides which
    students may check in. Only subjects an admin assigned to this
    professor can be chosen (checked here, not just in the dropdown).
    """
    db = get_db()
    subjects = academic.professor_subjects(db, g.user["id"])

    if request.method == "POST":
        subject_id = request.form.get("subject_id", type=int)
        subject = next((s for s in subjects if s["id"] == subject_id), None)
        if subject is None:
            flash("Please choose one of your subjects.", "error")
        else:
            cur = db.execute(
                """INSERT INTO sessions (professor_id, subject, subject_id, token, is_active)
                   VALUES (?, ?, ?, ?, 1)""",
                (g.user["id"], f"{subject['name']} ({subject['code']})", subject["id"], uuid.uuid4().hex),
            )
            db.commit()
            page = "session_projector" if request.form.get("projector") else "session_qr"
            return redirect(url_for(page, session_id=cur.lastrowid))

    return render_template("create_session.html", subjects=subjects)


@app.route("/session/<int:session_id>/qr")
@role_required("professor", "admin")
def session_qr(session_id):
    """Shows the QR code of an open session (can be re-opened any time)."""
    lecture = get_owned_session(session_id)
    if not lecture["is_active"]:
        flash("This session is closed, so its QR code is no longer available.", "error")
        return redirect(url_for("report_session", session_id=session_id))

    checkin_url = build_checkin_url(lecture["id"], lecture["token"])
    return render_template(
        "session_qr.html",
        lecture=lecture,
        qr_image=qr_utils.generate_qr_base64(checkin_url),
        checkin_url=checkin_url,
    )


@app.route("/session/<int:session_id>/projector")
@role_required("professor", "admin")
def session_projector(session_id):
    """
    The QR code on a page of its own, made for the classroom projector:
    no menus, as large as the screen allows, with a live count.

    A closed session shows "session has ended" here rather than
    redirecting to its report, so that the attendance list never ends
    up in front of the whole class.
    """
    lecture = get_owned_session(session_id)
    qr_image = None
    if lecture["is_active"]:
        qr_image = qr_utils.generate_qr_base64(build_checkin_url(lecture["id"], lecture["token"]))
    return render_template("session_projector.html", lecture=lecture, qr_image=qr_image)


def open_sessions_for_board(db):
    """
    The open sessions the projector board shows: a professor's own, or
    for an admin every one (optionally only those of one faculty, i.e.
    whose subject is taught in a program of that faculty).
    Returns (sessions, faculty_id).
    """
    where, params = ["s.is_active = 1", "s.subject_id IS NOT NULL"], []
    if g.user["role"] == "professor":
        where.append("s.professor_id = ?")
        params.append(g.user["id"])
    faculty_id = request.args.get("faculty", type=int)
    if faculty_id:
        where.append("""EXISTS (SELECT 1 FROM subject_assignments sa
                                JOIN programs pr ON pr.id = sa.program_id
                                WHERE sa.subject_id = s.subject_id AND pr.faculty_id = ?)""")
        params.append(faculty_id)
    rows = db.execute(
        f"""SELECT s.id, s.subject, s.subject_id, s.token, p.full_name AS professor_name,
                   (SELECT COUNT(*) FROM attendance a WHERE a.session_id = s.id) AS present_count,
                   {ABSENT_COUNT_SQL} AS absent_count
            FROM sessions s JOIN users p ON p.id = s.professor_id
            WHERE {' AND '.join(where)}
            ORDER BY s.subject COLLATE NOCASE, s.id""",
        params,
    ).fetchall()
    return rows, faculty_id


@app.route("/projector")
@role_required("professor", "admin")
def projector_board():
    """
    One screen with the QR code of every session that is open right now,
    for when several subjects are held at the same time: each student
    scans the code of their own subject. (Scanning another one is refused
    by the usual eligibility check - the board is only a convenience.)
    """
    db = get_db()
    rows, faculty_id = open_sessions_for_board(db)
    faculties = db.execute("SELECT id, name FROM faculties WHERE is_active = 1 ORDER BY name COLLATE NOCASE").fetchall()
    n = len(rows)
    # One row up to 4 codes, two rows up to 8, three after that (rounded up).
    columns = n if n <= 4 else -(-n // 2) if n <= 8 else -(-n // 3)
    return render_template(
        "projector_board.html",
        sessions=[
            dict(r, qr_image=qr_utils.generate_qr_base64(build_checkin_url(r["id"], r["token"])))
            for r in rows
        ],
        groups=academic.subject_groups(db),
        faculties=faculties,
        faculty=next((f for f in faculties if f["id"] == faculty_id), None),
        columns=max(columns, 1),
    )


@app.route("/projector/status")
@role_required("professor", "admin")
def projector_board_status():
    """JSON polled by the projector board: which sessions are open, and their counts."""
    rows, _ = open_sessions_for_board(get_db())
    return jsonify({"sessions": [
        {"id": r["id"], "present_count": r["present_count"],
         "expected_count": r["present_count"] + r["absent_count"]}
        for r in rows
    ]})


@app.route("/session/<int:session_id>/status")
@role_required("professor", "admin")
def session_status(session_id):
    """JSON polled by the QR and projector pages to show check-ins as they happen."""
    lecture = get_owned_session(session_id)
    db = get_db()
    rows = db.execute(
        """SELECT u.full_name, a.marked_at
           FROM attendance a JOIN users u ON u.id = a.student_id
           WHERE a.session_id = ? ORDER BY a.marked_at DESC""",
        (session_id,),
    ).fetchall()
    absent = db.execute(
        f"SELECT {ABSENT_COUNT_SQL} FROM sessions s WHERE s.id = ?", (session_id,)
    ).fetchone()[0]
    return jsonify({
        "is_active": bool(lecture["is_active"]),
        "present_count": len(rows),
        "expected_count": len(rows) + absent,
        "recent": [
            {"name": r["full_name"], "time": localtime_filter(r["marked_at"], "%H:%M")}
            for r in rows[:8]
        ],
    })


@app.route("/end-session/<int:session_id>", methods=["POST"])
@role_required("professor", "admin")
def end_session(session_id):
    """Closes a session so its QR code can no longer be used to check in."""
    get_owned_session(session_id)
    db = get_db()
    db.execute("UPDATE sessions SET is_active = 0 WHERE id = ?", (session_id,))
    db.commit()
    flash("Session closed.", "success")
    return redirect(sessions_home())


@app.route("/delete-session/<int:session_id>", methods=["POST"])
@role_required("professor", "admin")
def delete_session(session_id):
    """Deletes a session together with its attendance records."""
    get_owned_session(session_id)
    db = get_db()
    db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    db.commit()
    flash("Session and its attendance records deleted.", "success")
    return redirect(sessions_home())


# ---------------------------------------------------------------------
# REPORTS  (professors: their own sessions - admins: every session)
# ---------------------------------------------------------------------
# Who is "expected" at a lecture: the students who are eligible for its
# subject (worked out from their academic profile, see academic.py).
# Sessions from before subjects existed have no subject; for those the
# old rule still applies (every student registered at the time was expected).
EXPECTED_SQL = f"""((s.subject_id IS NULL AND u.created_at <= s.created_at)
                    OR {academic.eligible_sql('s.subject_id')})"""

ABSENT_COUNT_SQL = f"""(SELECT COUNT(*) FROM users u
                        WHERE u.role = 'student' AND u.is_active = 1 AND {EXPECTED_SQL}
                          AND NOT EXISTS (SELECT 1 FROM attendance a
                                          WHERE a.session_id = s.id AND a.student_id = u.id))"""


def report_scope():
    """
    Builds the SQL filter for the sessions a report covers, from the
    user's role and the filter form (?date_from=&date_to=&subject=&professor_id=).

    Returns (where_sql, params, filters) where `filters` echoes the
    cleaned values back to the template.
    """
    where, params = ["1 = 1"], []
    # Empty values become None so url_for() leaves them out of links.
    filters = {
        "date_from": request.args.get("date_from", "").strip() or None,
        "date_to": request.args.get("date_to", "").strip() or None,
        "subject": request.args.get("subject", "").strip() or None,
        "professor_id": request.args.get("professor_id", type=int),
    }

    if g.user["role"] == "professor":
        filters["professor_id"] = None
        where.append("s.professor_id = ?")
        params.append(g.user["id"])
    elif filters["professor_id"]:
        where.append("s.professor_id = ?")
        params.append(filters["professor_id"])

    # Dates are compared in local time, matching what the pages display.
    if filters["date_from"]:
        where.append("date(s.created_at, 'localtime') >= ?")
        params.append(filters["date_from"])
    if filters["date_to"]:
        where.append("date(s.created_at, 'localtime') <= ?")
        params.append(filters["date_to"])
    if filters["subject"]:
        where.append("s.subject = ?")
        params.append(filters["subject"])

    return " AND ".join(where), params, filters


def report_filter_options():
    """Values for the subject / professor dropdowns of the filter form."""
    db = get_db()
    if g.user["role"] == "professor":
        subjects = db.execute(
            "SELECT DISTINCT subject FROM sessions WHERE professor_id = ? ORDER BY subject", (g.user["id"],)
        ).fetchall()
        professors = []
    else:
        subjects = db.execute("SELECT DISTINCT subject FROM sessions ORDER BY subject").fetchall()
        professors = db.execute(
            "SELECT id, full_name FROM users WHERE role = 'professor' ORDER BY full_name"
        ).fetchall()
    return {"subjects": [r["subject"] for r in subjects], "professors": professors}


def csv_response(filename, header, rows):
    """Builds a downloadable CSV file (with a BOM so Excel reads UTF-8 correctly)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return Response(
        "﻿" + buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def fmt_csv_time(value):
    return localtime_filter(value, "%Y-%m-%d %H:%M:%S") if value else ""


@app.route("/reports")
@role_required("professor", "admin")
def reports():
    """Sessions report: one row per lecture with its attendance rate."""
    where, params, filters = report_scope()
    rows = get_db().execute(
        f"""SELECT s.*, p.full_name AS professor_name,
                   (SELECT COUNT(*) FROM attendance a WHERE a.session_id = s.id) AS present_count,
                   {ABSENT_COUNT_SQL} AS absent_count
            FROM sessions s JOIN users p ON p.id = s.professor_id
            WHERE {where}
            ORDER BY s.created_at DESC""",
        params,
    ).fetchall()

    if request.args.get("format") == "csv":
        return csv_response(
            "sessions_report.csv",
            ["Session ID", "Subject", "Professor", "Date", "Status", "Present", "Absent", "Attendance %"],
            [
                [r["id"], r["subject"], r["professor_name"], fmt_csv_time(r["created_at"]),
                 "Active" if r["is_active"] else "Closed", r["present_count"], r["absent_count"],
                 percent(r["present_count"], r["present_count"] + r["absent_count"])]
                for r in rows
            ],
        )

    total_present = sum(r["present_count"] for r in rows)
    total_expected = total_present + sum(r["absent_count"] for r in rows)
    summary = {
        "sessions": len(rows),
        "checkins": total_present,
        "rate": percent(total_present, total_expected),
        "active": sum(1 for r in rows if r["is_active"]),
    }
    return render_template(
        "reports.html", rows=rows, summary=summary, filters=filters, options=report_filter_options(),
    )


@app.route("/reports/students")
@role_required("professor", "admin")
def report_students():
    """
    Students report: how many of the sessions they were expected at
    (sessions in scope for subjects they are eligible for) each student attended.
    """
    where, params, filters = report_scope()
    attended_here = "EXISTS (SELECT 1 FROM attendance a WHERE a.session_id = s.id AND a.student_id = u.id)"
    rows = get_db().execute(
        f"""SELECT * FROM (
                SELECT u.id, u.full_name, u.student_id, u.email, u.is_active,
                       (SELECT COUNT(*) FROM sessions s WHERE {where} AND {attended_here}) AS attended,
                       (SELECT COUNT(*) FROM sessions s
                         WHERE {where} AND ({EXPECTED_SQL} OR {attended_here})) AS expected,
                       (SELECT MAX(a.marked_at) FROM attendance a JOIN sessions s ON s.id = a.session_id
                         WHERE {where} AND a.student_id = u.id) AS last_seen
                FROM users u WHERE u.role = 'student')
            WHERE attended > 0 OR (is_active = 1 AND expected > 0)
            ORDER BY full_name COLLATE NOCASE""",
        params * 3,
    ).fetchall()

    if request.args.get("format") == "csv":
        return csv_response(
            "students_report.csv",
            ["Student", "Student ID", "Email", "Sessions attended", "Sessions expected", "Attendance %", "Last check-in"],
            [
                [r["full_name"], r["student_id"] or "", r["email"], r["attended"], r["expected"],
                 percent(r["attended"], r["expected"]), fmt_csv_time(r["last_seen"])]
                for r in rows
            ],
        )

    return render_template(
        "report_students.html", rows=rows, filters=filters, options=report_filter_options(),
    )


@app.route("/reports/student/<int:student_id>")
@role_required("professor", "admin")
def report_student(student_id):
    """One student's attendance across the sessions in scope they were expected at."""
    where, params, filters = report_scope()
    db = get_db()
    student = db.execute(
        "SELECT * FROM users WHERE id = ? AND role = 'student'", (student_id,)
    ).fetchone()
    if student is None:
        abort(404)

    rows = db.execute(
        f"""SELECT s.id, s.subject, s.created_at, p.full_name AS professor_name,
                   a.marked_at, a.status
            FROM sessions s
            JOIN users p ON p.id = s.professor_id
            JOIN users u ON u.id = ?
            LEFT JOIN attendance a ON a.session_id = s.id AND a.student_id = u.id
            WHERE {where} AND ({EXPECTED_SQL} OR a.id IS NOT NULL)
            ORDER BY s.created_at DESC""",
        [student_id] + params,
    ).fetchall()

    if request.args.get("format") == "csv":
        return csv_response(
            f"student_{student['student_id'] or student['id']}_report.csv",
            ["Subject", "Professor", "Session date", "Status", "Checked in at"],
            [
                [r["subject"], r["professor_name"], fmt_csv_time(r["created_at"]),
                 attendance_label(r["status"]), fmt_csv_time(r["marked_at"])]
                for r in rows
            ],
        )

    attended = sum(1 for r in rows if r["status"])
    return render_template(
        "report_student.html", student=student, rows=rows, attended=attended, filters=filters,
    )


def attendance_label(status):
    return {"present": "Present", "manual": "Present (manual)"}.get(status, "Absent")


app.jinja_env.globals["attendance_label"] = attendance_label


@app.route("/reports/session/<int:session_id>")
@role_required("professor", "admin")
def report_session(session_id):
    """Full report of one lecture: who was present, when, and which eligible students were absent."""
    lecture = get_owned_session(session_id)
    db = get_db()
    present = db.execute(
        """SELECT u.id, u.full_name, u.student_id, u.email, a.marked_at, a.status
           FROM attendance a JOIN users u ON u.id = a.student_id
           WHERE a.session_id = ?
           ORDER BY a.marked_at ASC""",
        (session_id,),
    ).fetchall()
    absent = db.execute(
        f"""SELECT u.id, u.full_name, u.student_id, u.email
            FROM users u JOIN sessions s ON s.id = ?
            WHERE u.role = 'student' AND u.is_active = 1 AND {EXPECTED_SQL}
              AND NOT EXISTS (SELECT 1 FROM attendance a
                              WHERE a.session_id = s.id AND a.student_id = u.id)
            ORDER BY u.full_name COLLATE NOCASE""",
        (session_id,),
    ).fetchall()

    if request.args.get("format") == "csv":
        return csv_response(
            f"session_{session_id}_attendance.csv",
            ["Student", "Student ID", "Email", "Status", "Checked in at"],
            [[r["full_name"], r["student_id"] or "", r["email"], attendance_label(r["status"]),
              fmt_csv_time(r["marked_at"])] for r in present]
            + [[r["full_name"], r["student_id"] or "", r["email"], "Absent", ""] for r in absent],
        )

    return render_template("report_session.html", lecture=lecture, present=present, absent=absent)


@app.route("/reports/session/<int:session_id>/mark", methods=["POST"])
@role_required("professor", "admin")
def mark_attendance(session_id):
    """
    Manual correction by the professor/admin: mark a student present
    (e.g. the camera failed) or remove a wrong record. Manual entries
    are stored with status 'manual' so they stay distinguishable from
    QR + face check-ins.
    """
    lecture = get_owned_session(session_id)
    db = get_db()
    student_id = request.form.get("student_id", type=int)
    student = db.execute(
        "SELECT * FROM users WHERE id = ? AND role = 'student'", (student_id,)
    ).fetchone()
    if student is None:
        abort(404)

    if request.form.get("action") == "remove":
        db.execute(
            "DELETE FROM attendance WHERE session_id = ? AND student_id = ?", (session_id, student_id)
        )
        flash(f"Removed the attendance record of {student['full_name']}.", "success")
    elif lecture["subject_id"] and not academic.is_eligible(db, student_id, lecture["subject_id"]):
        # Manual marking follows the same rule as scanning. An admin can
        # grant an individual exception on the student's page first.
        flash(f"{student['full_name']} is not eligible for this subject, so they cannot be marked present.", "error")
    else:
        db.execute(
            "INSERT OR IGNORE INTO attendance (session_id, student_id, status) VALUES (?, ?, 'manual')",
            (session_id, student_id),
        )
        flash(f"{student['full_name']} marked present manually.", "success")
    db.commit()
    return redirect(url_for("report_session", session_id=session_id))


# ---------------------------------------------------------------------
# STUDENT DASHBOARD
# ---------------------------------------------------------------------
@app.route("/student/dashboard")
@role_required("student")
def student_dashboard():
    db = get_db()
    history = db.execute(
        """SELECT a.marked_at, a.status, s.subject, p.full_name AS professor_name
           FROM attendance a
           JOIN sessions s ON s.id = a.session_id
           JOIN users p ON p.id = s.professor_id
           WHERE a.student_id = ?
           ORDER BY a.marked_at DESC""",
        (g.user["id"],),
    ).fetchall()
    # Sessions this student was expected at: those of subjects they are
    # eligible for (plus any they attended anyway).
    total_sessions = db.execute(
        f"""SELECT COUNT(*) FROM sessions s JOIN users u ON u.id = ?
            WHERE {EXPECTED_SQL} OR EXISTS (SELECT 1 FROM attendance a
                                            WHERE a.session_id = s.id AND a.student_id = u.id)""",
        (g.user["id"],),
    ).fetchone()[0]

    # If the QR step was already passed (e.g. via the /checkin link, or
    # the page was reloaded), go straight to the face-verification step.
    pending_subject = None
    if session.get("pending_session_id"):
        pending = db.execute(
            "SELECT subject FROM sessions WHERE id = ? AND is_active = 1",
            (session["pending_session_id"],),
        ).fetchone()
        if pending:
            pending_subject = pending["subject"]
        else:
            session.pop("pending_session_id", None)

    return render_template(
        "student_dashboard.html",
        history=history,
        total_sessions=total_sessions,
        pending_subject=pending_subject,
        subjects=academic.eligible_subjects(db, g.user["id"]),
        profile=academic.academic_profile(db, g.user),
    )


# ---------------------------------------------------------------------
# CHECK IN VIA QR LINK  (GET /checkin)
# ---------------------------------------------------------------------
@app.route("/checkin")
def checkin():
    """
    The URL encoded in every QR code. Opened when a student scans the QR
    with their phone's normal camera app. Does the same validation as
    /scan-qr, then sends the student to the face-verification step.
    """
    session_id = request.args.get("session_id", type=int)
    token = request.args.get("token", "")

    if g.user is None:
        # Remember the scan so the check-in continues right after login.
        session["next_checkin"] = {"session_id": session_id, "token": token}
        flash("Please log in to check in to this lecture.", "error")
        return redirect(url_for("login"))

    if g.user["role"] != "student":
        flash("Only students can check in to a lecture.", "error")
        return redirect(url_for("index"))

    lecture, error, _ = validate_checkin(get_db(), session_id, token)
    if error:
        flash(error, "error")
    else:
        session["pending_session_id"] = lecture["id"]

    return redirect(url_for("student_dashboard"))


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

    lecture, error, status = validate_checkin(get_db(), data.get("session_id"), data.get("token"))
    if error:
        return jsonify({"success": False, "message": error}), status

    # Remember that this student passed the QR check for this session.
    session["pending_session_id"] = lecture["id"]

    return jsonify({
        "success": True,
        "message": f"QR verified for '{lecture['subject']}'. Now confirm your face.",
        "subject": lecture["subject"],
    })


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

    Logic:  QR valid (already checked)  +  face match  +  still eligible
            for the subject  ->  mark present.
    """
    pending_session_id = session.get("pending_session_id")
    if not pending_session_id:
        return jsonify({"success": False, "message": "Please scan the QR code first."}), 400

    data = request.get_json(silent=True) or {}
    image_data = data.get("image")
    if not image_data:
        return jsonify({"success": False, "message": "No image received."}), 400

    student = g.user
    if not student["face_encoding"]:
        return jsonify({
            "success": False,
            "message": "No face photo is registered on your account yet. "
                       "Add it from your dashboard first."
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
    db = get_db()
    lecture = db.execute(
        "SELECT * FROM sessions WHERE id = ? AND is_active = 1", (pending_session_id,)
    ).fetchone()
    if lecture is None:
        session.pop("pending_session_id", None)
        return jsonify({"success": False, "message": "This lecture session is no longer active."}), 400

    # Eligibility is checked again at the moment attendance is written:
    # the student's profile, the subject or an exception may have
    # changed since the QR step, and this endpoint can be called directly.
    problem = subject_access_problem(db, lecture)
    if problem:
        session.pop("pending_session_id", None)
        return jsonify({"success": False, "message": problem[0]}), problem[1]

    # INSERT OR IGNORE: the UNIQUE constraint makes a double-submit harmless.
    db.execute(
        "INSERT OR IGNORE INTO attendance (session_id, student_id, status) VALUES (?, ?, 'present')",
        (pending_session_id, student["id"]),
    )
    db.commit()

    session.pop("pending_session_id", None)

    return jsonify({
        "success": True,
        "message": f"Attendance marked present for '{lecture['subject']}'.",
        "distance": round(distance, 3),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


# ---------------------------------------------------------------------
# ADMIN: overview
# ---------------------------------------------------------------------
@app.route("/admin")
@role_required("admin")
def admin_dashboard():
    db = get_db()
    stats = db.execute(
        """SELECT
             (SELECT COUNT(*) FROM users WHERE role = 'student')   AS students,
             (SELECT COUNT(*) FROM users WHERE role = 'professor') AS professors,
             (SELECT COUNT(*) FROM users WHERE role = 'admin')     AS admins,
             (SELECT COUNT(*) FROM users WHERE is_active = 0)      AS inactive,
             (SELECT COUNT(*) FROM users WHERE role = 'student' AND face_encoding IS NULL) AS no_face,
             (SELECT COUNT(*) FROM users WHERE role = 'student' AND program_id IS NULL) AS no_profile,
             (SELECT COUNT(*) FROM subjects WHERE is_active = 1)   AS subjects,
             (SELECT COUNT(*) FROM email_domains WHERE is_active = 1) AS domains,
             (SELECT COUNT(*) FROM sessions)                       AS sessions,
             (SELECT COUNT(*) FROM sessions WHERE is_active = 1)   AS active_sessions,
             (SELECT COUNT(*) FROM attendance)                     AS checkins,
             (SELECT COUNT(*) FROM attendance
               WHERE date(marked_at, 'localtime') = date('now', 'localtime')) AS checkins_today"""
    ).fetchone()
    recent_sessions = db.execute(
        """SELECT s.*, p.full_name AS professor_name,
                  (SELECT COUNT(*) FROM attendance a WHERE a.session_id = s.id) AS present_count
           FROM sessions s JOIN users p ON p.id = s.professor_id
           ORDER BY s.created_at DESC LIMIT 6"""
    ).fetchall()
    recent_users = db.execute(
        "SELECT id, full_name, email, role, created_at FROM users ORDER BY created_at DESC, id DESC LIMIT 6"
    ).fetchall()
    return render_template(
        "admin_dashboard.html", stats=stats, recent_sessions=recent_sessions, recent_users=recent_users,
    )


# ---------------------------------------------------------------------
# ADMIN: manage users (professors, students, other admins)
# ---------------------------------------------------------------------
@app.route("/admin/users")
@role_required("admin")
def admin_users():
    role = request.args.get("role", "")
    status = request.args.get("status", "")
    q = request.args.get("q", "").strip()

    where, params = ["1 = 1"], []
    if role in ROLES:
        where.append("u.role = ?")
        params.append(role)
    if status in ("active", "inactive"):
        where.append("u.is_active = ?")
        params.append(1 if status == "active" else 0)
    if q:
        where.append("(u.full_name LIKE ? OR u.email LIKE ? OR u.student_id LIKE ?)")
        params += [f"%{q}%"] * 3

    db = get_db()
    users = db.execute(
        f"""SELECT u.*,
                   (SELECT name FROM programs WHERE id = u.program_id) AS program_name,
                   (SELECT name FROM study_years WHERE id = u.study_year_id) AS year_name,
                   (SELECT COUNT(*) FROM attendance a WHERE a.student_id = u.id) AS attended_count,
                   (SELECT COUNT(*) FROM sessions s WHERE s.professor_id = u.id) AS session_count
            FROM users u
            WHERE {' AND '.join(where)}
            ORDER BY u.role, u.full_name COLLATE NOCASE""",
        params,
    ).fetchall()
    counts = {r["role"]: r["n"] for r in db.execute("SELECT role, COUNT(*) AS n FROM users GROUP BY role")}
    return render_template(
        "admin_users.html", users=users, counts=counts, role=role, status=status, q=q,
    )


def read_user_form():
    """
    Reads + validates the fields shared by the add/edit user forms.
    For students this includes the academic profile, which must be a
    program + year of study (+ specialisation) that really belong together.
    """
    form = request.form
    first_name, last_name = clean_name(form.get("first_name")), clean_name(form.get("last_name"))
    data = {
        "first_name": first_name,
        "last_name": last_name,
        "full_name": f"{first_name} {last_name}".strip(),
        "email": form.get("email", "").strip().lower(),
        "role": form.get("role", ""),
        "student_id": form.get("student_id", "").strip(),
        # What was picked, so the form can be shown again after an error.
        "program_id": academic.to_int(form.get("program_id")),
        "study_year_id": academic.to_int(form.get("study_year_id")),
        "specialisation_id": academic.to_int(form.get("specialisation_id")),
    }
    if not first_name or not data["email"] or data["role"] not in ROLES:
        return data, "Please fill in the name, email and role."
    if "@" not in data["email"]:
        return data, "Please enter a valid email address."

    if data["role"] != "student":
        data.update(student_id="", program_id=None, study_year_id=None, specialisation_id=None)
        return data, None

    if not data["student_id"]:
        return data, "A student ID is required for students."
    # The academic profile may be left empty (the student is then not
    # eligible for any subject); if anything is chosen it must be valid.
    if data["program_id"] or data["study_year_id"] or data["specialisation_id"]:
        group, error = academic.group_from_form(get_db(), form, allow_inactive=True)
        if error:
            return data, error
        data.update(group)
    return data, None


def render_user_form(user, data):
    """The add/edit user page; for a student it also shows their subject access."""
    db = get_db()
    context = {"user": user, "data": data, "tree": academic.structure_tree(db, include_inactive=True)}
    if user is not None and user["role"] == "student":
        context["eligible"] = academic.eligible_subjects(db, user["id"], only_active=False)
        context["overrides"] = db.execute(
            """SELECT o.*, sub.code, sub.name, a.full_name AS admin_name
               FROM student_subject_overrides o
               JOIN subjects sub ON sub.id = o.subject_id
               LEFT JOIN users a ON a.id = o.created_by
               WHERE o.student_id = ? ORDER BY sub.name COLLATE NOCASE""",
            (user["id"],),
        ).fetchall()
        context["all_subjects"] = db.execute(
            "SELECT id, code, name, is_active FROM subjects ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return render_template("admin_user_form.html", **context)


@app.route("/admin/users/new", methods=["GET", "POST"])
@role_required("admin")
def admin_user_new():
    """
    Creates an account for a professor, student or admin. The account
    gets a temporary password (typed by the admin or generated) that the
    user must replace at first login. Students add their face photo
    themselves from their dashboard.
    """
    if request.method == "GET":
        return render_user_form(None, {"role": request.args.get("role", "professor")})

    data, error = read_user_form()
    password = request.form.get("password", "")
    if not error and password:
        error = password_problem(password)
    db = get_db()
    if not error and db.execute("SELECT 1 FROM users WHERE email = ?", (data["email"],)).fetchone():
        error = "An account with that email already exists."
    if error:
        flash(error, "error")
        return render_user_form(None, data)

    temporary_password = password or secrets.token_urlsafe(9)
    db.execute(
        """INSERT INTO users (full_name, first_name, last_name, email, password_hash, role, student_id,
                              program_id, study_year_id, specialisation_id, must_change_password)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
        (data["full_name"], data["first_name"], data["last_name"], data["email"],
         generate_password_hash(temporary_password), data["role"], data["student_id"] or None,
         data["program_id"], data["study_year_id"], data["specialisation_id"]),
    )
    db.commit()
    flash(f"Account created for {data['full_name']}. Temporary password: {temporary_password} "
          f"- pass it on to them; they will be asked to change it at first login.", "success")
    return redirect(url_for("admin_users", role=data["role"]))


def get_user_or_404(user_id):
    user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        abort(404)
    return user


@app.route("/admin/users/<int:user_id>/edit", methods=["GET", "POST"])
@role_required("admin")
def admin_user_edit(user_id):
    """
    Edits an account. Changing a student's program / year of study /
    specialisation changes which subjects they can check in to straight
    away: eligibility is worked out from the profile, not stored.
    """
    user = get_user_or_404(user_id)
    if request.method == "GET":
        return render_user_form(user, user)

    data, error = read_user_form()
    is_active = 1 if request.form.get("is_active") else 0
    db = get_db()

    is_self = user["id"] == g.user["id"]
    if not error and is_self and (data["role"] != "admin" or not is_active):
        error = "You cannot change your own role or deactivate your own account."
    if not error and db.execute(
        "SELECT 1 FROM users WHERE email = ? AND id != ?", (data["email"], user_id)
    ).fetchone():
        error = "Another account already uses that email."
    if error:
        flash(error, "error")
        return render_user_form(user, data)

    db.execute(
        """UPDATE users SET full_name = ?, first_name = ?, last_name = ?, email = ?, role = ?, student_id = ?,
                            program_id = ?, study_year_id = ?, specialisation_id = ?, is_active = ?
           WHERE id = ?""",
        (data["full_name"], data["first_name"], data["last_name"], data["email"], data["role"],
         data["student_id"] or None, data["program_id"], data["study_year_id"], data["specialisation_id"],
         is_active, user_id),
    )
    if data["role"] != "professor":
        db.execute("DELETE FROM subject_professors WHERE professor_id = ?", (user_id,))
    if data["role"] != "student":
        db.execute("DELETE FROM student_subject_overrides WHERE student_id = ?", (user_id,))
    db.commit()
    flash(f"Changes to {data['full_name']} saved.", "success")
    if data["role"] == "student":
        return redirect(url_for("admin_user_edit", user_id=user_id))
    return redirect(url_for("admin_users", role=data["role"]))


@app.route("/admin/users/<int:user_id>/action", methods=["POST"])
@role_required("admin")
def admin_user_action(user_id):
    """Quick actions from the user list: activate/deactivate, reset password, reset face, delete."""
    user = get_user_or_404(user_id)
    action = request.form.get("action")
    db = get_db()
    # Return to the (filtered) list the admin came from; local paths only.
    next_url = request.form.get("next", "")
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = url_for("admin_users")
    back = redirect(next_url)

    if user["id"] == g.user["id"] and action in ("toggle_active", "delete"):
        flash("You cannot deactivate or delete your own account.", "error")
        return back

    if action == "toggle_active":
        db.execute("UPDATE users SET is_active = ? WHERE id = ?", (0 if user["is_active"] else 1, user_id))
        flash(f"{user['full_name']} has been {'deactivated' if user['is_active'] else 'activated'}.", "success")

    elif action == "reset_password":
        temporary_password = secrets.token_urlsafe(9)
        db.execute(
            "UPDATE users SET password_hash = ?, must_change_password = 1 WHERE id = ?",
            (generate_password_hash(temporary_password), user_id),
        )
        flash(f"New temporary password for {user['full_name']}: {temporary_password} "
              f"- they will be asked to change it at next login.", "success")

    elif action == "reset_face":
        delete_face_photo(user)
        db.execute("UPDATE users SET face_encoding = NULL, face_photo = NULL WHERE id = ?", (user_id,))
        flash(f"The face photo of {user['full_name']} was removed. "
              f"They will be asked to register a new one.", "success")

    elif action == "delete":
        # ON DELETE CASCADE also removes the user's sessions / attendance.
        delete_face_photo(user)
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        flash(f"{user['full_name']} and all of their records were deleted.", "success")

    else:
        abort(400)

    db.commit()
    return back


# ---------------------------------------------------------------------
# ADMIN: all lecture sessions
# ---------------------------------------------------------------------
@app.route("/admin/sessions")
@role_required("admin")
def admin_sessions():
    sessions_ = get_db().execute(
        """SELECT s.*, p.full_name AS professor_name,
                  (SELECT COUNT(*) FROM attendance a WHERE a.session_id = s.id) AS present_count
           FROM sessions s JOIN users p ON p.id = s.professor_id
           ORDER BY s.created_at DESC"""
    ).fetchall()
    return render_template("admin_sessions.html", sessions=sessions_)


# ---------------------------------------------------------------------
# Error pages
# ---------------------------------------------------------------------
@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, message="We couldn't find that page."), 404


@app.errorhandler(413)
def too_large(e):
    return render_template("error.html", code=413, message="That upload is too large."), 413


# ---------------------------------------------------------------------
if __name__ == "__main__":
    # debug=True is convenient for a thesis demo; turn off in production.
    app.run(debug=True, host="0.0.0.0", port=5000, ssl_context="adhoc")
