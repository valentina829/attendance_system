"""
database.py
------------
Handles all SQLite database connection and schema setup logic for the
Attendance Management System.

We use Python's built-in sqlite3 module (no ORM) to keep the project
simple and dependency-light, which is ideal for a diploma thesis project.
"""

import sqlite3
import os

# Path to the SQLite database file. It lives inside /instance so it is
# easy to find, back up, or delete (Flask convention for instance data).
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "attendance.db")


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


def init_db():
    """
    Creates all required tables if they do not already exist.
    Safe to call every time the app starts (idempotent).
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    cur = conn.cursor()

    # ---------------------------------------------------------------
    # USERS table
    # Stores both Professors and Students in a single table, distinguished
    # by the 'role' column. Only students have a face_encoding + student_id.
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name     TEXT NOT NULL,
            email         TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL CHECK(role IN ('professor', 'student')),
            student_id    TEXT,                 -- university/matriculation number (students only)
            face_encoding TEXT,                 -- JSON array of 128 floats (students only)
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ---------------------------------------------------------------
    # SESSIONS table
    # A "session" = one lecture instance created by a professor.
    # Each session gets a unique QR token that students scan to check in.
    # ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            professor_id  INTEGER NOT NULL,
            subject       TEXT NOT NULL,
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

    conn.commit()
    conn.close()
