"""
academic.py
-----------
The university's academic structure and the rules that decide which
student may check in to which subject.

Two parts:

  1. Rules (plain functions taking a database connection):
       - allowed e-mail domains for student registration
       - validation of a student's academic profile
       - ELIGIBILITY: is this student allowed to take this subject?

  2. The admin pages that manage all of it (a Flask blueprint under
     /admin): faculties, programs, years of study, specialisations,
     subjects + who they are for, e-mail domains and individual
     student exceptions.

Eligibility is always computed, never stored:

    a student may take a subject when
        an exception (override) for that student + subject says so,
    or, if there is no exception,
        the subject is assigned to the student's program + year of study
        (and, when the assignment names a specialisation, to theirs).
"""

import re
import sqlite3

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from database import request_db as get_db

bp = Blueprint("academic", __name__, url_prefix="/admin")

NOT_ELIGIBLE_MESSAGE = ("You are not eligible to scan this subject because it is not part "
                        "of your current academic program.")

DOMAIN_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def to_int(value):
    """Form value -> int, or None when it is empty / not a number."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# =====================================================================
# 1. RULES
# =====================================================================

# ---------------------------------------------------------------------
# Allowed e-mail domains
# ---------------------------------------------------------------------
def active_domains(db):
    return [r["domain"] for r in db.execute(
        "SELECT domain FROM email_domains WHERE is_active = 1 ORDER BY domain"
    )]


def email_domain_problem(db, email):
    """Returns an error message if a student may not register with this e-mail, else None."""
    domains = active_domains(db)
    if not domains:
        return "Student registration is not open at the moment. Please contact an administrator."
    local, _, domain = email.rpartition("@")
    if not local or domain.lower() not in domains:
        allowed = " or ".join("@" + d for d in domains)
        return f"Please register with your university email address ({allowed})."
    return None


# ---------------------------------------------------------------------
# Academic structure
# ---------------------------------------------------------------------
def structure_tree(db, include_inactive=False):
    """
    The structure as nested lists, for the dependent dropdowns
    (faculty -> program -> year of study / specialisation):

        [{id, name, programs: [{id, name, years: [{id, name}], specialisations: [{id, name}]}]}]

    Students only see what is active and usable; admins (include_inactive)
    see everything, with inactive entries labelled.
    """
    def label(row):
        return row["name"] + ("" if row["is_active"] else " (inactive)")

    def visible(row):
        return include_inactive or row["is_active"]

    years, specialisations = {}, {}
    for r in db.execute(
        """SELECT py.program_id, y.id, y.name, y.is_active
           FROM program_years py JOIN study_years y ON y.id = py.study_year_id
           ORDER BY y.number"""
    ):
        if visible(r):
            years.setdefault(r["program_id"], []).append({"id": r["id"], "name": label(r)})
    for r in db.execute("SELECT * FROM specialisations ORDER BY name COLLATE NOCASE"):
        if visible(r):
            specialisations.setdefault(r["program_id"], []).append({"id": r["id"], "name": label(r)})

    programs = {}
    for r in db.execute("SELECT * FROM programs ORDER BY name COLLATE NOCASE"):
        # A program without any year of study cannot be chosen by a student.
        if visible(r) and (include_inactive or years.get(r["id"])):
            programs.setdefault(r["faculty_id"], []).append({
                "id": r["id"], "name": label(r),
                "years": years.get(r["id"], []),
                "specialisations": specialisations.get(r["id"], []),
            })

    tree = []
    for r in db.execute("SELECT * FROM faculties ORDER BY name COLLATE NOCASE"):
        if visible(r) and (include_inactive or programs.get(r["id"])):
            tree.append({"id": r["id"], "name": label(r), "programs": programs.get(r["id"], [])})
    return tree


def validate_group(db, faculty_id, program_id, study_year_id, specialisation_id, allow_inactive=False):
    """
    Checks that program / year of study / specialisation really belong
    together - used for a student's profile and for subject assignments.
    Never trusts the browser: the dropdowns only make valid choices
    easy, this makes invalid ones impossible.

    Returns ({program_id, study_year_id, specialisation_id}, None)
    or      (None, error_message).
    """
    only_active = "" if allow_inactive else " AND p.is_active = 1 AND f.is_active = 1"
    program = db.execute(
        "SELECT p.* FROM programs p JOIN faculties f ON f.id = p.faculty_id WHERE p.id = ?" + only_active,
        (program_id,),
    ).fetchone()
    if program is None:
        return None, "Please choose a program."
    if faculty_id is not None and program["faculty_id"] != faculty_id:
        return None, "That program does not belong to the selected faculty."

    year = db.execute(
        """SELECT y.id FROM study_years y
           JOIN program_years py ON py.study_year_id = y.id
           WHERE py.program_id = ? AND y.id = ?""" + ("" if allow_inactive else " AND y.is_active = 1"),
        (program_id, study_year_id),
    ).fetchone()
    if year is None:
        return None, "Please choose a year of study that the selected program offers."

    if specialisation_id is not None:
        specialisation = db.execute(
            "SELECT id FROM specialisations WHERE id = ? AND program_id = ?"
            + ("" if allow_inactive else " AND is_active = 1"),
            (specialisation_id, program_id),
        ).fetchone()
        if specialisation is None:
            return None, "That specialisation does not belong to the selected program."

    return {"program_id": program_id, "study_year_id": study_year_id,
            "specialisation_id": specialisation_id}, None


def group_from_form(db, form, allow_inactive=False):
    """validate_group() for the fields posted by the academic dropdowns."""
    return validate_group(
        db, to_int(form.get("faculty_id")), to_int(form.get("program_id")),
        to_int(form.get("study_year_id")), to_int(form.get("specialisation_id")),
        allow_inactive=allow_inactive,
    )


def academic_profile(db, user):
    """The names behind a student's profile ids (None for whatever is not set)."""
    return db.execute(
        """SELECT f.id AS faculty_id, f.name AS faculty, p.name AS program,
                  y.name AS study_year, sp.name AS specialisation
           FROM users u
           LEFT JOIN programs p ON p.id = u.program_id
           LEFT JOIN faculties f ON f.id = p.faculty_id
           LEFT JOIN study_years y ON y.id = u.study_year_id
           LEFT JOIN specialisations sp ON sp.id = u.specialisation_id
           WHERE u.id = ?""",
        (user["id"],),
    ).fetchone()


# ---------------------------------------------------------------------
# ELIGIBILITY
# ---------------------------------------------------------------------
def eligible_sql(subject, user="u"):
    """
    SQL expression that is 1 when the student in table alias `user` may
    take the subject whose id is the SQL expression `subject`, else 0.

    An exception (override) decides when there is one; otherwise the
    subject must be assigned to the student's program + year, and to
    their specialisation when the assignment names one.

    Both arguments are fixed pieces of SQL written in this code base
    (a column name or a ":name" placeholder) - never user input.
    """
    return f"""COALESCE(
        (SELECT o.allowed FROM student_subject_overrides o
          WHERE o.student_id = {user}.id AND o.subject_id = {subject}),
        EXISTS (SELECT 1 FROM subject_assignments sa
                 WHERE sa.subject_id = {subject}
                   AND sa.program_id = {user}.program_id
                   AND sa.study_year_id = {user}.study_year_id
                   AND (sa.specialisation_id IS NULL
                        OR sa.specialisation_id = {user}.specialisation_id)))"""


def is_eligible(db, student_id, subject_id):
    """True if the student may take (check in to) the subject."""
    row = db.execute(
        f"SELECT {eligible_sql(':subject')} AS ok FROM users u WHERE u.id = :student AND u.role = 'student'",
        {"subject": subject_id, "student": student_id},
    ).fetchone()
    return bool(row and row["ok"])


def eligible_subjects(db, student_id, only_active=True):
    """
    The subjects a student may take, each with:
      override  - 1 if it comes from an exception, else NULL
      held      - lecture sessions held for it so far
      attended  - how many of those the student attended
    """
    return db.execute(
        f"""SELECT sub.*,
                   (SELECT o.allowed FROM student_subject_overrides o
                     WHERE o.student_id = u.id AND o.subject_id = sub.id) AS override,
                   (SELECT COUNT(*) FROM sessions s WHERE s.subject_id = sub.id) AS held,
                   (SELECT COUNT(*) FROM attendance a JOIN sessions s ON s.id = a.session_id
                     WHERE s.subject_id = sub.id AND a.student_id = u.id) AS attended
            FROM subjects sub JOIN users u ON u.id = :student
            WHERE {eligible_sql('sub.id')} {'AND sub.is_active = 1' if only_active else ''}
            ORDER BY sub.semester, sub.name COLLATE NOCASE""",
        {"student": student_id},
    ).fetchall()


def professor_subjects(db, professor_id):
    """The active subjects a professor may open lecture sessions for."""
    return db.execute(
        """SELECT sub.* FROM subjects sub
           JOIN subject_professors sp ON sp.subject_id = sub.id
           WHERE sp.professor_id = ? AND sub.is_active = 1
           ORDER BY sub.name COLLATE NOCASE""",
        (professor_id,),
    ).fetchall()


def subject_groups(db):
    """The groups each subject is available for: {subject_id: [rows]}."""
    groups = {}
    for r in db.execute(
        """SELECT sa.subject_id, p.name AS program, y.name AS study_year, sp.name AS specialisation
           FROM subject_assignments sa
           JOIN programs p ON p.id = sa.program_id
           JOIN study_years y ON y.id = sa.study_year_id
           LEFT JOIN specialisations sp ON sp.id = sa.specialisation_id
           ORDER BY p.name COLLATE NOCASE, y.number"""
    ):
        groups.setdefault(r["subject_id"], []).append(r)
    return groups


# =====================================================================
# 2. ADMIN PAGES
# =====================================================================
@bp.before_request
def admins_only():
    """Every page in this module is for administrators only."""
    user = g.get("user")
    if user is None:
        flash("Please log in to continue.", "error")
        return redirect(url_for("login"))
    if user["role"] != "admin":
        flash("You do not have permission to view that page.", "error")
        return redirect(url_for("index"))
    return None


def get_row(table, row_id):
    """Fetches one row by id or answers 404. `table` is always a name written in this file."""
    row = get_db().execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        abort(404)
    return row


def toggle_active(table, row, label):
    db = get_db()
    db.execute(f"UPDATE {table} SET is_active = ? WHERE id = ?", (0 if row["is_active"] else 1, row["id"]))
    db.commit()
    flash(f"{label} {'deactivated' if row['is_active'] else 'activated'}.", "success")


def delete_row(table, row, label):
    """
    Deletes a row unless something still refers to it (students,
    subjects, lecture sessions...): the database's foreign keys refuse
    that, and the admin is told to deactivate it instead.
    """
    db = get_db()
    try:
        db.execute(f"DELETE FROM {table} WHERE id = ?", (row["id"],))
        db.commit()
        flash(f"{label} deleted.", "success")
    except sqlite3.IntegrityError:
        db.rollback()
        flash(f"{label} is still in use, so it cannot be deleted. Deactivate it instead: "
              f"it disappears from the choices but existing records stay intact.", "error")


def save_name(table, row_id, name, label, extra_column=None, extra_value=None):
    """Inserts (row_id None) or renames a named row; reports duplicates. Returns True on success."""
    name = " ".join(name.split())
    if not name:
        flash(f"Please enter a name for the {label}.", "error")
        return False
    db = get_db()
    try:
        if row_id is None and extra_column:
            db.execute(f"INSERT INTO {table} ({extra_column}, name) VALUES (?, ?)", (extra_value, name))
        elif row_id is None:
            db.execute(f"INSERT INTO {table} (name) VALUES (?)", (name,))
        else:
            db.execute(f"UPDATE {table} SET name = ? WHERE id = ?", (name, row_id))
        db.commit()
        return True
    except sqlite3.IntegrityError:
        db.rollback()
        flash(f"A {label} called '{name}' already exists.", "error")
        return False


# ---------------------------------------------------------------------
# Academic structure: faculties, their programs, years of study
# ---------------------------------------------------------------------
@bp.route("/academic")
def structure():
    db = get_db()
    programs = {}
    for p in db.execute(
        """SELECT p.*,
                  (SELECT COUNT(*) FROM users u WHERE u.program_id = p.id) AS students,
                  (SELECT COUNT(*) FROM program_years py WHERE py.program_id = p.id) AS years,
                  (SELECT COUNT(*) FROM specialisations sp WHERE sp.program_id = p.id) AS specialisations
           FROM programs p ORDER BY p.name COLLATE NOCASE"""
    ):
        programs.setdefault(p["faculty_id"], []).append(p)
    faculties = db.execute("SELECT * FROM faculties ORDER BY name COLLATE NOCASE").fetchall()
    years = db.execute(
        """SELECT y.*, (SELECT COUNT(*) FROM program_years py WHERE py.study_year_id = y.id) AS programs,
                  (SELECT COUNT(*) FROM users u WHERE u.study_year_id = y.id) AS students
           FROM study_years y ORDER BY y.number"""
    ).fetchall()
    return render_template("admin_academic.html", faculties=faculties, programs=programs, years=years)


@bp.route("/academic/faculty", methods=["POST"])
def faculty_action():
    action = request.form.get("action")
    if action == "add":
        if save_name("faculties", None, request.form.get("name", ""), "faculty"):
            flash("Faculty added. Now add its programs.", "success")
        return redirect(url_for("academic.structure"))

    faculty = get_row("faculties", to_int(request.form.get("id")))
    if action == "rename":
        if save_name("faculties", faculty["id"], request.form.get("name", ""), "faculty"):
            flash("Faculty renamed.", "success")
    elif action == "toggle":
        toggle_active("faculties", faculty, f"Faculty '{faculty['name']}'")
    elif action == "delete":
        delete_row("faculties", faculty, f"Faculty '{faculty['name']}'")
    else:
        abort(400)
    return redirect(url_for("academic.structure"))


@bp.route("/academic/program", methods=["POST"])
def program_action():
    action = request.form.get("action")
    if action == "add":
        faculty = get_row("faculties", to_int(request.form.get("faculty_id")))
        name = " ".join(request.form.get("name", "").split())
        if save_name("programs", None, name, "program in this faculty", "faculty_id", faculty["id"]):
            new_id = get_db().execute(
                "SELECT id FROM programs WHERE faculty_id = ? AND name = ?", (faculty["id"], name)
            ).fetchone()["id"]
            flash("Program added. Choose the years of study it offers.", "success")
            return redirect(url_for("academic.program", program_id=new_id))
        return redirect(url_for("academic.structure"))

    program_row = get_row("programs", to_int(request.form.get("id")))
    if action == "toggle":
        toggle_active("programs", program_row, f"Program '{program_row['name']}'")
    elif action == "delete":
        delete_row("programs", program_row, f"Program '{program_row['name']}'")
    else:
        abort(400)
    return redirect(url_for("academic.structure"))


@bp.route("/academic/year", methods=["POST"])
def year_action():
    action = request.form.get("action")
    db = get_db()
    if action == "add":
        number = to_int(request.form.get("number"))
        name = " ".join(request.form.get("name", "").split())
        if number is None or number < 1 or not name:
            flash("Please enter the year's number (1, 2, 3...) and its name.", "error")
        else:
            try:
                db.execute("INSERT INTO study_years (number, name) VALUES (?, ?)", (number, name))
                db.commit()
                flash("Year of study added. Tick it on the programs that offer it.", "success")
            except sqlite3.IntegrityError:
                db.rollback()
                flash(f"There is already a year number {number}.", "error")
        return redirect(url_for("academic.structure"))

    year = get_row("study_years", to_int(request.form.get("id")))
    if action == "rename":
        if save_name("study_years", year["id"], request.form.get("name", ""), "year of study"):
            flash("Year of study renamed.", "success")
    elif action == "toggle":
        toggle_active("study_years", year, f"'{year['name']}'")
    elif action == "delete":
        delete_row("study_years", year, f"'{year['name']}'")
    else:
        abort(400)
    return redirect(url_for("academic.structure"))


# ---------------------------------------------------------------------
# One program: its faculty, years of study and specialisations
# ---------------------------------------------------------------------
@bp.route("/academic/program/<int:program_id>", methods=["GET", "POST"])
def program(program_id):
    program_row = get_row("programs", program_id)
    db = get_db()

    if request.method == "POST":
        name = " ".join(request.form.get("name", "").split())
        faculty = db.execute(
            "SELECT id FROM faculties WHERE id = ?", (to_int(request.form.get("faculty_id")),)
        ).fetchone()
        valid_years = {r["id"] for r in db.execute("SELECT id FROM study_years")}
        chosen = {to_int(v) for v in request.form.getlist("years")} & valid_years
        current = {r["study_year_id"] for r in db.execute(
            "SELECT study_year_id FROM program_years WHERE program_id = ?", (program_id,)
        )}
        # A year cannot be taken away while students or subjects of this program use it.
        blocked = [r["name"] for r in db.execute(
            """SELECT id, name FROM study_years
               WHERE id IN (SELECT study_year_id FROM users WHERE program_id = :p
                            UNION SELECT study_year_id FROM subject_assignments WHERE program_id = :p)""",
            {"p": program_id},
        ) if r["id"] in current - chosen]

        if not name or faculty is None:
            flash("Please enter the program's name and choose its faculty.", "error")
        elif blocked:
            flash(f"{', '.join(blocked)} cannot be removed from this program: students or subjects "
                  f"of the program are still in that year.", "error")
        else:
            try:
                db.execute(
                    "UPDATE programs SET name = ?, faculty_id = ?, is_active = ? WHERE id = ?",
                    (name, faculty["id"], 1 if request.form.get("is_active") else 0, program_id),
                )
                db.execute("DELETE FROM program_years WHERE program_id = ?", (program_id,))
                db.executemany(
                    "INSERT INTO program_years (program_id, study_year_id) VALUES (?, ?)",
                    [(program_id, y) for y in chosen],
                )
                db.commit()
                flash("Program saved.", "success")
            except sqlite3.IntegrityError:
                db.rollback()
                flash(f"That faculty already has a program called '{name}'.", "error")
        return redirect(url_for("academic.program", program_id=program_id))

    return render_template(
        "admin_program.html",
        program=program_row,
        faculties=db.execute("SELECT * FROM faculties ORDER BY name COLLATE NOCASE").fetchall(),
        years=db.execute(
            """SELECT y.*, EXISTS (SELECT 1 FROM program_years py
                                   WHERE py.program_id = ? AND py.study_year_id = y.id) AS offered
               FROM study_years y ORDER BY y.number""",
            (program_id,),
        ).fetchall(),
        specialisations=db.execute(
            """SELECT sp.*, (SELECT COUNT(*) FROM users u WHERE u.specialisation_id = sp.id) AS students
               FROM specialisations sp WHERE sp.program_id = ? ORDER BY sp.name COLLATE NOCASE""",
            (program_id,),
        ).fetchall(),
        assignments=db.execute(
            """SELECT sub.id, sub.code, sub.name, sub.is_active, y.name AS study_year, sp.name AS specialisation
               FROM subject_assignments sa
               JOIN subjects sub ON sub.id = sa.subject_id
               JOIN study_years y ON y.id = sa.study_year_id
               LEFT JOIN specialisations sp ON sp.id = sa.specialisation_id
               WHERE sa.program_id = ?
               ORDER BY y.number, sub.name COLLATE NOCASE""",
            (program_id,),
        ).fetchall(),
        student_count=db.execute(
            "SELECT COUNT(*) FROM users WHERE program_id = ?", (program_id,)
        ).fetchone()[0],
    )


@bp.route("/academic/program/<int:program_id>/specialisation", methods=["POST"])
def specialisation_action(program_id):
    get_row("programs", program_id)
    action = request.form.get("action")
    back = redirect(url_for("academic.program", program_id=program_id))

    if action == "add":
        if save_name("specialisations", None, request.form.get("name", ""),
                     "specialisation in this program", "program_id", program_id):
            flash("Specialisation added.", "success")
        return back

    specialisation = get_db().execute(
        "SELECT * FROM specialisations WHERE id = ? AND program_id = ?",
        (to_int(request.form.get("id")), program_id),
    ).fetchone()
    if specialisation is None:
        abort(404)
    if action == "rename":
        if save_name("specialisations", specialisation["id"], request.form.get("name", ""),
                     "specialisation in this program"):
            flash("Specialisation renamed.", "success")
    elif action == "toggle":
        toggle_active("specialisations", specialisation, f"Specialisation '{specialisation['name']}'")
    elif action == "delete":
        delete_row("specialisations", specialisation, f"Specialisation '{specialisation['name']}'")
    else:
        abort(400)
    return back


# ---------------------------------------------------------------------
# Subjects
# ---------------------------------------------------------------------
@bp.route("/subjects")
def subjects():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "")
    where, params = ["1 = 1"], []
    if q:
        where.append("(sub.name LIKE ? OR sub.code LIKE ?)")
        params += [f"%{q}%"] * 2
    if status in ("active", "inactive"):
        where.append("sub.is_active = ?")
        params.append(1 if status == "active" else 0)

    db = get_db()
    rows = db.execute(
        f"""SELECT sub.*,
                   (SELECT COUNT(*) FROM sessions s WHERE s.subject_id = sub.id) AS sessions,
                   (SELECT GROUP_CONCAT(u.full_name, ', ') FROM subject_professors sp
                      JOIN users u ON u.id = sp.professor_id WHERE sp.subject_id = sub.id) AS professors
            FROM subjects sub WHERE {' AND '.join(where)}
            ORDER BY sub.name COLLATE NOCASE""",
        params,
    ).fetchall()
    return render_template("admin_subjects.html", subjects=rows, groups=subject_groups(db), q=q, status=status)


def read_subject_form():
    form = request.form
    data = {
        "code": "".join(form.get("code", "").split()).upper(),
        "name": " ".join(form.get("name", "").split()),
        "description": form.get("description", "").strip(),
        "ects": to_int(form.get("ects")),
        "semester": to_int(form.get("semester")),
        "is_active": 1 if form.get("is_active") else 0,
    }
    if not data["code"] or not data["name"]:
        return data, "Please enter the subject's name and code."
    if form.get("ects", "").strip() and (data["ects"] is None or not 0 <= data["ects"] <= 60):
        return data, "ECTS credits must be a number between 0 and 60."
    if form.get("semester", "").strip() and (data["semester"] is None or not 1 <= data["semester"] <= 20):
        return data, "The semester must be a number (1, 2, 3...)."
    return data, None


def save_subject_professors(db, subject_id):
    """Replaces the subject's professors with the ticked ones (real, existing professors only)."""
    valid = {r["id"] for r in db.execute("SELECT id FROM users WHERE role = 'professor'")}
    chosen = {to_int(v) for v in request.form.getlist("professors")} & valid
    db.execute("DELETE FROM subject_professors WHERE subject_id = ?", (subject_id,))
    db.executemany(
        "INSERT INTO subject_professors (subject_id, professor_id) VALUES (?, ?)",
        [(subject_id, p) for p in chosen],
    )


def render_subject_form(subject, data, chosen_professors=None):
    db = get_db()
    context = {
        "subject": subject, "data": data,
        "professors": db.execute(
            "SELECT id, full_name, is_active FROM users WHERE role = 'professor' ORDER BY full_name COLLATE NOCASE"
        ).fetchall(),
        "chosen_professors": chosen_professors,
    }
    if subject is not None:
        if chosen_professors is None:
            context["chosen_professors"] = {r["professor_id"] for r in db.execute(
                "SELECT professor_id FROM subject_professors WHERE subject_id = ?", (subject["id"],)
            )}
        context["assignments"] = db.execute(
            f"""SELECT sa.id, f.name AS faculty, p.name AS program, y.name AS study_year,
                       sp.name AS specialisation,
                       (SELECT COUNT(*) FROM users u
                         WHERE u.role = 'student' AND u.is_active = 1
                           AND u.program_id = sa.program_id AND u.study_year_id = sa.study_year_id
                           AND (sa.specialisation_id IS NULL
                                OR sa.specialisation_id = u.specialisation_id)) AS students
                FROM subject_assignments sa
                JOIN programs p ON p.id = sa.program_id
                JOIN faculties f ON f.id = p.faculty_id
                JOIN study_years y ON y.id = sa.study_year_id
                LEFT JOIN specialisations sp ON sp.id = sa.specialisation_id
                WHERE sa.subject_id = ?
                ORDER BY f.name COLLATE NOCASE, p.name COLLATE NOCASE, y.number""",
            (subject["id"],),
        ).fetchall()
        context["overrides"] = db.execute(
            """SELECT o.*, u.full_name, u.email FROM student_subject_overrides o
               JOIN users u ON u.id = o.student_id
               WHERE o.subject_id = ? ORDER BY u.full_name COLLATE NOCASE""",
            (subject["id"],),
        ).fetchall()
        context["eligible_count"] = db.execute(
            f"""SELECT COUNT(*) FROM users u
                WHERE u.role = 'student' AND u.is_active = 1 AND {eligible_sql(':subject')}""",
            {"subject": subject["id"]},
        ).fetchone()[0]
        context["tree"] = structure_tree(db, include_inactive=True)
    return render_template("admin_subject_form.html", **context)


@bp.route("/subjects/new", methods=["GET", "POST"])
def subject_new():
    if request.method == "GET":
        return render_subject_form(None, {"is_active": 1}, set())

    data, error = read_subject_form()
    db = get_db()
    if not error and db.execute("SELECT 1 FROM subjects WHERE code = ?", (data["code"],)).fetchone():
        error = f"Another subject already uses the code {data['code']}."
    if error:
        flash(error, "error")
        return render_subject_form(None, data, {to_int(v) for v in request.form.getlist("professors")})

    cur = db.execute(
        """INSERT INTO subjects (code, name, description, ects, semester, is_active)
           VALUES (:code, :name, :description, :ects, :semester, :is_active)""",
        data,
    )
    save_subject_professors(db, cur.lastrowid)
    db.commit()
    flash("Subject created. Now choose which students it is for.", "success")
    return redirect(url_for("academic.subject_edit", subject_id=cur.lastrowid))


@bp.route("/subjects/<int:subject_id>", methods=["GET", "POST"])
def subject_edit(subject_id):
    subject = get_row("subjects", subject_id)
    if request.method == "GET":
        return render_subject_form(subject, subject)

    data, error = read_subject_form()
    db = get_db()
    if not error and db.execute(
        "SELECT 1 FROM subjects WHERE code = ? AND id != ?", (data["code"], subject_id)
    ).fetchone():
        error = f"Another subject already uses the code {data['code']}."
    if error:
        flash(error, "error")
        return render_subject_form(subject, data, {to_int(v) for v in request.form.getlist("professors")})

    db.execute(
        """UPDATE subjects SET code = :code, name = :name, description = :description,
                               ects = :ects, semester = :semester, is_active = :is_active
           WHERE id = :id""",
        dict(data, id=subject_id),
    )
    save_subject_professors(db, subject_id)
    db.commit()
    flash("Subject saved.", "success")
    return redirect(url_for("academic.subject_edit", subject_id=subject_id))


@bp.route("/subjects/<int:subject_id>/action", methods=["POST"])
def subject_action(subject_id):
    subject = get_row("subjects", subject_id)
    action = request.form.get("action")
    if action == "toggle":
        toggle_active("subjects", subject, f"{subject['name']}")
    elif action == "delete":
        delete_row("subjects", subject, f"{subject['name']}")
    else:
        abort(400)
    return redirect(url_for("academic.subjects"))


@bp.route("/subjects/<int:subject_id>/assignment", methods=["POST"])
def subject_assignment(subject_id):
    """Adds / removes one "available for" line: program + year [+ specialisation]."""
    get_row("subjects", subject_id)
    db = get_db()
    back = redirect(url_for("academic.subject_edit", subject_id=subject_id) + "#available-for")

    if request.form.get("action") == "delete":
        db.execute(
            "DELETE FROM subject_assignments WHERE id = ? AND subject_id = ?",
            (to_int(request.form.get("id")), subject_id),
        )
        db.commit()
        flash("Removed. Students of that group can no longer check in to this subject.", "success")
        return back

    group, error = group_from_form(db, request.form, allow_inactive=True)
    if error:
        flash(error, "error")
        return back
    duplicate = db.execute(
        """SELECT 1 FROM subject_assignments
           WHERE subject_id = ? AND program_id = ? AND study_year_id = ? AND specialisation_id IS ?""",
        (subject_id, group["program_id"], group["study_year_id"], group["specialisation_id"]),
    ).fetchone()
    if duplicate:
        flash("The subject is already available for that group.", "error")
        return back
    db.execute(
        """INSERT INTO subject_assignments (subject_id, program_id, study_year_id, specialisation_id)
           VALUES (?, ?, ?, ?)""",
        (subject_id, group["program_id"], group["study_year_id"], group["specialisation_id"]),
    )
    db.commit()
    flash("Added. Students of that group can now check in to this subject.", "success")
    return back


# ---------------------------------------------------------------------
# Individual student exceptions
# ---------------------------------------------------------------------
@bp.route("/users/<int:user_id>/override", methods=["POST"])
def student_override(user_id):
    """
    Grants or revokes one subject for one student, on top of what their
    academic profile gives. Stored as an exception: the profile itself
    is not touched.
    """
    db = get_db()
    student = db.execute("SELECT * FROM users WHERE id = ? AND role = 'student'", (user_id,)).fetchone()
    if student is None:
        abort(404)
    back = redirect(url_for("admin_user_edit", user_id=user_id) + "#subject-access")

    if request.form.get("action") == "delete":
        db.execute(
            "DELETE FROM student_subject_overrides WHERE id = ? AND student_id = ?",
            (to_int(request.form.get("id")), user_id),
        )
        db.commit()
        flash("Exception removed. The student's academic profile decides again.", "success")
        return back

    subject = db.execute(
        "SELECT * FROM subjects WHERE id = ?", (to_int(request.form.get("subject_id")),)
    ).fetchone()
    allowed = request.form.get("allowed")
    if subject is None or allowed not in ("0", "1"):
        flash("Please choose a subject and whether to allow or deny it.", "error")
        return back

    db.execute(
        """INSERT INTO student_subject_overrides (student_id, subject_id, allowed, reason, created_by)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (student_id, subject_id) DO UPDATE SET
               allowed = excluded.allowed, reason = excluded.reason,
               created_by = excluded.created_by, created_at = CURRENT_TIMESTAMP""",
        (user_id, subject["id"], int(allowed), request.form.get("reason", "").strip()[:300] or None, g.user["id"]),
    )
    db.commit()
    flash(f"{student['full_name']} is now {'allowed to take' if allowed == '1' else 'denied'} "
          f"{subject['name']}.", "success")
    return back


# ---------------------------------------------------------------------
# Settings: allowed e-mail domains for student registration
# ---------------------------------------------------------------------
@bp.route("/settings")
def settings():
    domains = get_db().execute("SELECT * FROM email_domains ORDER BY domain").fetchall()
    return render_template("admin_settings.html", domains=domains)


@bp.route("/settings/domain", methods=["POST"])
def domain_action():
    action = request.form.get("action")
    db = get_db()
    back = redirect(url_for("academic.settings"))

    if action == "add":
        domain = request.form.get("domain", "").strip().lower().lstrip("@")
        if not DOMAIN_PATTERN.match(domain):
            flash("Please enter a domain such as ubt-uni.net (without the @).", "error")
            return back
        try:
            db.execute("INSERT INTO email_domains (domain) VALUES (?)", (domain,))
            db.commit()
            flash(f"Students can now register with @{domain} addresses.", "success")
        except sqlite3.IntegrityError:
            db.rollback()
            flash(f"@{domain} is already in the list.", "error")
        return back

    domain = get_row("email_domains", to_int(request.form.get("id")))
    if action == "toggle":
        toggle_active("email_domains", domain, f"@{domain['domain']}")
    elif action == "delete":
        delete_row("email_domains", domain, f"@{domain['domain']}")
    else:
        abort(400)
    return back
