"""
database.py
------------
Handles all SQLite database connection and schema setup logic for the
Attendance Management System.

We use Python's built-in sqlite3 module (no ORM) to keep the project
simple and dependency-light, which is ideal for a diploma thesis project.
"""

import os
import shutil
import sqlite3

from flask import g
from werkzeug.security import generate_password_hash

# Path to the SQLite database file. It lives inside /instance so it is
# easy to find, back up, or delete (Flask convention for instance data).
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "attendance.db")

# Credentials of the administrator account that is created automatically
# when the database contains no admin yet. The password must be changed
# at first login (must_change_password = 1).
DEFAULT_ADMIN_EMAIL = "admin@attendance.local"
DEFAULT_ADMIN_PASSWORD = "Admin@12345"

# ---------------------------------------------------------------
# USERS table
# Stores Admins, Professors and Students in a single table,
# distinguished by the 'role' column. Only students have a
# face_encoding + face_photo + student_id + an academic profile
# (program, year of study, optional specialisation). The faculty is
# not stored: it follows from the program.
# ---------------------------------------------------------------
USERS_SCHEMA = """
    CREATE TABLE {name} (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        full_name     TEXT NOT NULL,        -- "first last", kept for display
        first_name    TEXT,
        last_name     TEXT,
        email         TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        role          TEXT NOT NULL CHECK(role IN ('admin', 'professor', 'student')),
        student_id    TEXT,                 -- university/matriculation number (students only)
        face_encoding TEXT,                 -- JSON array of 128 floats (students only)
        face_photo    TEXT,                 -- file name of the profile photo in instance/faces/
        program_id        INTEGER REFERENCES programs (id),         -- academic profile (students only)
        study_year_id     INTEGER REFERENCES study_years (id),
        specialisation_id INTEGER REFERENCES specialisations (id),  -- optional
        is_active     INTEGER NOT NULL DEFAULT 1,            -- 0 = account disabled by an admin
        must_change_password INTEGER NOT NULL DEFAULT 0,     -- 1 = temporary password in use
        created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
"""


# ---------------------------------------------------------------
# ACADEMIC STRUCTURE
#   faculties -> programs -> (years offered, specialisations)
#   subjects are attached to "academic groups" through
#   subject_assignments: one row = "this subject is taken by this
#   program in this year [by this specialisation]". A subject shared
#   by several programs simply has several rows - it is never copied.
#
# Which subjects a student may check in to is NOT stored anywhere: it
# is worked out from the student's profile and these rows every time
# (see academic.py), so changing a student's year or program takes
# effect immediately. Only individual exceptions are stored
# (student_subject_overrides).
#
# Rows that history depends on are deactivated (is_active = 0), not
# deleted: the foreign keys below refuse to delete anything that is
# still referenced by a student, a subject or a lecture session.
# ---------------------------------------------------------------
ACADEMIC_SCHEMA = """
    CREATE TABLE IF NOT EXISTS email_domains (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        domain     TEXT NOT NULL UNIQUE,            -- e.g. "ubt-uni.net"
        is_active  INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS faculties (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        name      TEXT NOT NULL UNIQUE,
        is_active INTEGER NOT NULL DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS programs (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        faculty_id INTEGER NOT NULL REFERENCES faculties (id),
        name       TEXT NOT NULL,
        is_active  INTEGER NOT NULL DEFAULT 1,
        UNIQUE (faculty_id, name)
    );

    -- The years of study that exist at all ("1st Year", "2nd Year", ...).
    CREATE TABLE IF NOT EXISTS study_years (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        number    INTEGER NOT NULL UNIQUE,          -- used for ordering
        name      TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1
    );

    -- Which of those years each program offers.
    CREATE TABLE IF NOT EXISTS program_years (
        program_id    INTEGER NOT NULL REFERENCES programs (id) ON DELETE CASCADE,
        study_year_id INTEGER NOT NULL REFERENCES study_years (id) ON DELETE CASCADE,
        PRIMARY KEY (program_id, study_year_id)
    );

    CREATE TABLE IF NOT EXISTS specialisations (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        program_id INTEGER NOT NULL REFERENCES programs (id) ON DELETE CASCADE,
        name       TEXT NOT NULL,
        is_active  INTEGER NOT NULL DEFAULT 1,
        UNIQUE (program_id, name)
    );

    CREATE TABLE IF NOT EXISTS subjects (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        code        TEXT NOT NULL UNIQUE,           -- e.g. "CS204"
        name        TEXT NOT NULL,
        description TEXT,
        ects        INTEGER,
        semester    INTEGER,
        is_active   INTEGER NOT NULL DEFAULT 1,
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- specialisation_id NULL = every student of that program + year.
    CREATE TABLE IF NOT EXISTS subject_assignments (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_id        INTEGER NOT NULL REFERENCES subjects (id) ON DELETE CASCADE,
        program_id        INTEGER NOT NULL REFERENCES programs (id),
        study_year_id     INTEGER NOT NULL REFERENCES study_years (id),
        specialisation_id INTEGER REFERENCES specialisations (id)
    );

    -- The professors who may open lecture sessions for a subject.
    CREATE TABLE IF NOT EXISTS subject_professors (
        subject_id   INTEGER NOT NULL REFERENCES subjects (id) ON DELETE CASCADE,
        professor_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        PRIMARY KEY (subject_id, professor_id)
    );

    -- Individual exceptions made by an admin: allowed = 1 grants a
    -- subject the profile does not give, allowed = 0 takes one away.
    CREATE TABLE IF NOT EXISTS student_subject_overrides (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        subject_id INTEGER NOT NULL REFERENCES subjects (id) ON DELETE CASCADE,
        allowed    INTEGER NOT NULL,
        reason     TEXT,
        created_by INTEGER REFERENCES users (id) ON DELETE SET NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (student_id, subject_id)
    );
"""

DEFAULT_STUDY_YEARS = [(1, "1st Year"), (2, "2nd Year"), (3, "3rd Year"), (4, "4th Year")]


def get_db():
    """
    Opens a new database connection.
    - row_factory = sqlite3.Row lets us access columns by name (like a dict),
      e.g. row['email'] instead of row[2]. This makes the code far more
      readable than plain tuples.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Enforce foreign key constraints (SQLite disables this by default)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def request_db():
    """The connection shared by everything that handles the current web request."""
    if "db" not in g:
        g.db = get_db()
    return g.db


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _backup_before_academic_upgrade(conn):
    """One-time copy of a database that predates the academic structure."""
    if _table_exists(conn, "users") and not _table_exists(conn, "faculties"):
        backup_path = DB_PATH + ".pre-academic.bak"
        if not os.path.exists(backup_path):
            conn.commit()
            shutil.copy2(DB_PATH, backup_path)


def _migrate_users_table(conn):
    """
    Upgrades a database created by the first version of the app, whose
    users table only allowed the roles 'professor' and 'student'.

    SQLite cannot change a CHECK constraint in place, so the table is
    rebuilt: create the new table, copy every row, swap the names.
    A one-time backup (attendance.db.bak) is written first.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'"
    ).fetchone()
    if row is None or "'admin'" in row["sql"]:
        return  # fresh database, or already migrated

    backup_path = DB_PATH + ".bak"
    if not os.path.exists(backup_path):
        shutil.copy2(DB_PATH, backup_path)

    conn.commit()
    # Foreign keys must be off while the table is swapped, otherwise
    # dropping the old table would cascade-delete sessions/attendance.
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(
        "BEGIN;"
        + USERS_SCHEMA.format(name="users_new")
        + """;
        INSERT INTO users_new (id, full_name, email, password_hash, role, student_id, face_encoding, created_at)
            SELECT id, full_name, email, password_hash, role, student_id, face_encoding, created_at FROM users;
        DROP TABLE users;
        ALTER TABLE users_new RENAME TO users;
        COMMIT;
        """
    )
    conn.execute("PRAGMA foreign_keys = ON")


def _add_missing_columns(conn):
    """Adds columns introduced after a database was first created."""
    wanted = {
        "users": [
            ("face_photo", "TEXT"),
            ("first_name", "TEXT"),
            ("last_name", "TEXT"),
            ("program_id", "INTEGER REFERENCES programs (id)"),
            ("study_year_id", "INTEGER REFERENCES study_years (id)"),
            ("specialisation_id", "INTEGER REFERENCES specialisations (id)"),
        ],
        "sessions": [("subject_id", "INTEGER REFERENCES subjects (id)")],
    }
    for table, columns in wanted.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    # Accounts created before first/last names were stored separately:
    # split "full name" at the first space.
    for row in conn.execute("SELECT id, full_name FROM users WHERE first_name IS NULL").fetchall():
        first, _, last = row["full_name"].strip().partition(" ")
        conn.execute(
            "UPDATE users SET first_name = ?, last_name = ? WHERE id = ?", (first, last.strip(), row["id"])
        )


def _seed_default_admin(conn):
    """Creates the default administrator if the system has no admin at all."""
    has_admin = conn.execute("SELECT 1 FROM users WHERE role = 'admin'").fetchone()
    if has_admin:
        return
    conn.execute(
        """INSERT INTO users (full_name, first_name, last_name, email, password_hash, role, must_change_password)
           VALUES (?, ?, '', ?, ?, 'admin', 1)""",
        ("Administrator", "Administrator", DEFAULT_ADMIN_EMAIL, generate_password_hash(DEFAULT_ADMIN_PASSWORD)),
    )
    print(f" * Default admin created: {DEFAULT_ADMIN_EMAIL} / {DEFAULT_ADMIN_PASSWORD} "
          f"(you will be asked to change the password at first login)")


def init_db():
    """
    Creates all required tables if they do not already exist.
    Safe to call every time the app starts (idempotent).
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    cur = conn.cursor()

    _backup_before_academic_upgrade(conn)
    _migrate_users_table(conn)

    cur.execute(USERS_SCHEMA.format(name="IF NOT EXISTS users"))

    # Academic structure. The usual years of study are offered as a
    # starting point the first time; admins can rename/add/remove them.
    first_time = not _table_exists(conn, "study_years")
    conn.executescript(ACADEMIC_SCHEMA)
    if first_time:
        conn.executemany("INSERT INTO study_years (number, name) VALUES (?, ?)", DEFAULT_STUDY_YEARS)

    # ---------------------------------------------------------------
    # SESSIONS table
    # A "session" = one lecture instance created by a professor.
    # Each session gets a unique QR token that students scan to check in.
    # subject_id links it to the subject it belongs to (which decides
    # who may check in); `subject` keeps the name as it was at the time.
    # Sessions from before subjects existed have subject_id NULL.
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            professor_id  INTEGER NOT NULL,
            subject       TEXT NOT NULL,
            subject_id    INTEGER REFERENCES subjects (id),
            token         TEXT NOT NULL UNIQUE,
            is_active     INTEGER NOT NULL DEFAULT 1,   -- 1 = open for check-in, 0 = closed
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (professor_id) REFERENCES users (id) ON DELETE CASCADE
        )
    """)

    # ---------------------------------------------------------------
    # ATTENDANCE table
    # One row per (session, student) pair = the student was marked present.
    # The UNIQUE constraint prevents a student being marked twice for the
    # same lecture (e.g. re-scanning the QR code by accident).
    # status: 'present' = QR + face check passed,
    #         'manual'  = added by hand by the professor / an admin.
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  INTEGER NOT NULL,
            student_id  INTEGER NOT NULL,
            marked_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status      TEXT NOT NULL DEFAULT 'present',
            UNIQUE (session_id, student_id),
            FOREIGN KEY (session_id) REFERENCES sessions (id) ON DELETE CASCADE,
            FOREIGN KEY (student_id) REFERENCES users (id) ON DELETE CASCADE
        )
    """)

    _add_missing_columns(conn)
    _seed_default_admin(conn)

    conn.commit()
    conn.close()
