# Intelligent Student Attendance Management System
### QR Code + Face Recognition — Flask / SQLite / HTML-CSS-JS

A complete, working full-stack web app: professors create lecture sessions
and generate a QR code; students scan it with their camera and confirm
their identity with a live face-recognition check; attendance is recorded
automatically only when **both** checks pass. Administrators manage the
professors, the students and the university's academic structure
(faculties, programs, years, specialisations, subjects), and see reports
for the whole system. A student can only check in to the subjects of
their own program and year.

---

## 1. Roles

| Role          | Can do                                                                                   |
|---------------|-------------------------------------------------------------------------------------------|
| **Admin**     | Add / edit / deactivate / delete professors, students and admins; reset passwords and face photos; manage faculties, programs, years of study, specialisations, subjects and who they are for; allowed e-mail domains; individual student exceptions; see and manage every session; all reports |
| **Professor** | Create lecture sessions for the subjects assigned to them, show their QR code, end / delete them, correct attendance by hand, reports for their own sessions |
| **Student**   | Register (university e-mail, academic information, face photo), check in with QR + face to their own subjects, see their subjects and attendance, change their face photo |

Students register themselves, with an e-mail address from a domain the admin
allows (**Settings**). Professor and admin accounts are created by an admin
(**Users → Add user**); they receive a temporary password that must be
changed at first login.

### Default administrator
The first time the app starts, an administrator account is created:

```
Email:    admin@attendance.local
Password: Admin@12345
```

You are forced to choose a new password at the first login.

---

## 2. Project Structure

```
attendance_system/
│
├── app.py                 # Main Flask app: accounts, sessions, check-in, reports
├── academic.py            # Academic structure, eligibility rules and their admin pages
├── database.py             # SQLite connection, schema, migration, default admin
├── face_utils.py           # Face recognition helpers (encode, compare)
├── qr_utils.py              # QR code generation (base64 PNG)
├── requirements.txt         # Python dependencies
│
├── instance/
│   ├── attendance.db        # SQLite database file (auto-created on first run)
│   └── faces/               # Students' profile photos (<user id>.jpg, not public)
│
├── templates/                # Jinja2 HTML templates
│   ├── base.html             # Shared layout, navbar, flash messages
│   ├── _macros.html          # Reusable pieces (badges, filters, camera widget...)
│   ├── index.html            # Landing page
│   ├── login.html / register.html
│   ├── forgot_password.html / reset_password.html
│   ├── account.html          # Profile + change password (all roles)
│   ├── enroll_face.html      # Student adds / replaces their face photo
│   ├── student_dashboard.html # QR scan + face verify + attendance history
│   ├── professor_dashboard.html / create_session.html / session_qr.html
│   ├── professor_subjects.html / session_projector.html   # a professor's subjects, full-screen QR for the classroom
│   ├── projector_board.html                               # all open sessions' QR codes on one screen
│   ├── reports.html / report_students.html / report_student.html / report_session.html
│   ├── admin_dashboard.html / admin_users.html / admin_user_form.html / admin_sessions.html
│   ├── admin_academic.html / admin_program.html   # faculties, programs, years, specialisations
│   ├── admin_subjects.html / admin_subject_form.html / admin_settings.html
│   └── error.html
│
└── static/
    ├── css/style.css          # All styling (responsive + print styles for reports)
    └── js/
        ├── app.js             # Mobile menu, confirm prompts, CSRF header helper
        ├── academic.js        # Dependent dropdowns: faculty -> program -> year / specialisation
        ├── register.js        # Face photo picker: webcam capture or upload from the device
        ├── student.js         # QR scan (camera or photo, html5-qrcode) + face verify flow
        └── html5-qrcode.min.js # QR scanner library (served locally, works offline)
```

---

## 3. Database Schema (SQLite)

**users**: `id, full_name, first_name, last_name, email, password_hash, role (admin/professor/student), student_id, face_encoding (JSON), face_photo (file name), program_id, study_year_id, specialisation_id, is_active, must_change_password, created_at`

**sessions**: `id, professor_id, subject (name at the time), subject_id, token (unique), is_active, created_at`

**attendance**: `id, session_id, student_id, marked_at, status` — `UNIQUE(session_id, student_id)` prevents double check-in.
`status` is `present` (QR + face check passed) or `manual` (added by hand by the professor / an admin).

**Academic structure**

```
faculties ──< programs ──< specialisations
                 │
                 └──< program_years >── study_years      (the years each program offers)

subjects ──< subject_assignments  (program + study year + optional specialisation)
subjects ──< subject_professors   (who may open lecture sessions for it)
subjects ──< student_subject_overrides >── users   (individual allow / deny)
email_domains                                       (who may register)
```

- A student's faculty is not stored: it follows from their program.
- A subject shared by several programs has several `subject_assignments`
  rows; it is never duplicated. An assignment without a specialisation
  covers every student of that program and year.
- **Eligibility is computed, never stored** (`academic.eligible_sql`):
  an override for the student + subject decides if there is one;
  otherwise the subject must be assigned to the student's program and
  year (and to their specialisation when the assignment names one).
  Changing a student's year therefore changes their subjects at once.
- Everything has an `is_active` flag. Rows that students, subjects or
  lecture sessions still refer to cannot be deleted (foreign keys), only
  deactivated, so attendance history is never lost.

Older databases are upgraded automatically on startup. One-time backups
are written first: `instance/attendance.db.bak` (before the admin role)
and `instance/attendance.db.pre-academic.bak` (before the academic
structure). Lecture sessions from before subjects existed keep their
reports but no longer accept check-ins.

---

## 4. API / Route Summary

| Method   | Route                            | Role             | Purpose                                              |
|----------|-----------------------------------|------------------|-------------------------------------------------------|
| GET/POST | `/register`                       | public           | Create a student account (allowed e-mail domain, academic info, face photo) |
| GET/POST | `/login`, `/logout`               | public / any     | Authenticate / clear the session                      |
| GET/POST | `/forgot-password`                | public           | Request a password-reset link                          |
| GET/POST | `/reset-password/<token>`         | public           | Choose a new password (link valid 1 hour, single use) |
| GET/POST | `/account`                        | any              | Edit profile, change password                          |
| GET/POST | `/enroll-face`                    | student          | Add or replace the face photo (camera or upload)      |
| GET      | `/face-photo/<id>`                | that student, admin | The student's profile photo                        |
| GET/POST | `/create-session`                 | professor        | Create a lecture session for one of their subjects    |
| GET      | `/professor/subjects`             | professor        | Their subjects: who takes them, sessions held, start one |
| GET      | `/session/<id>/qr`                | professor, admin | Show the session's QR code + live check-in count      |
| GET      | `/session/<id>/projector`         | professor, admin | Full-screen QR code for the classroom projector       |
| GET      | `/projector`                      | professor, admin | The QR codes of every open session on one screen (a professor's own; admins: all, or one faculty with `?faculty=<id>`) |
| POST     | `/end-session/<id>`               | professor, admin | Close a session so its QR can no longer be used       |
| POST     | `/delete-session/<id>`            | professor, admin | Delete a session and its attendance                    |
| GET      | `/checkin?session_id=&token=`     | student          | The link inside the QR code (phone-camera scan)       |
| POST     | `/scan-qr`                        | student          | Validates a scanned QR code (step 1 of check-in)      |
| POST     | `/verify-face`                    | student          | Matches live face vs stored encoding, marks present   |
| GET      | `/reports`                        | professor, admin | Report by session (filters, `?format=csv`)            |
| GET      | `/reports/students`               | professor, admin | Report by student (filters, `?format=csv`)            |
| GET      | `/reports/student/<id>`           | professor, admin | One student across all sessions                        |
| GET      | `/reports/session/<id>`           | professor, admin | One session: present + absent lists                    |
| POST     | `/reports/session/<id>/mark`      | professor, admin | Mark a student present by hand / remove a record      |
| GET      | `/admin`                          | admin            | System overview                                        |
| GET      | `/admin/users`                    | admin            | List / search professors, students, admins             |
| GET/POST | `/admin/users/new`, `/admin/users/<id>/edit` | admin | Add / edit a user                                      |
| POST     | `/admin/users/<id>/action`        | admin            | Activate/deactivate, reset password, reset face, delete |
| GET      | `/admin/sessions`                 | admin            | Every lecture session                                  |
| GET      | `/admin/academic`                 | admin            | Faculties, programs and years of study                 |
| POST     | `/admin/academic/faculty`, `/program`, `/year` | admin | Add / rename / activate / delete                      |
| GET/POST | `/admin/academic/program/<id>`    | admin            | One program: faculty, years offered, specialisations  |
| GET      | `/admin/subjects`                 | admin            | List / search subjects                                 |
| GET/POST | `/admin/subjects/new`, `/admin/subjects/<id>` | admin | Add / edit a subject and its professors             |
| POST     | `/admin/subjects/<id>/assignment` | admin            | Add / remove a group the subject is available for     |
| POST     | `/admin/users/<id>/override`      | admin            | Allow / deny one subject for one student              |
| GET      | `/admin/settings`                 | admin            | Allowed student e-mail domains                         |

Professors only ever see their own sessions; admins see all of them.

**Attendance rule implemented exactly as required:**
`QR valid` (checked in `/scan-qr` or `/checkin`, remembered in the session) **+** `face match`
(checked in `/verify-face`) → **only then** is a row written to `attendance`.

**Who may check in.** Every way of checking in goes through the same
server-side gate (`subject_access_problem` in `app.py`): the student must
be eligible for the session's subject and the subject must be active. It
runs at the QR step and again right before attendance is written, so a
subject that is merely hidden in the browser is not what protects it.

**Reports.** The students expected at a lecture are those eligible for its
subject: a session's attendance rate is
`present / (present + eligible students who did not check in)`. All report
pages can be filtered by date range and subject (admins also by professor),
exported as CSV and printed.

---

## 5. How to Run

### Step 1 — Create a virtual environment (recommended)
Use **Python 3.12** (the pinned `numpy` / `Pillow` versions have no builds for newer versions).
```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
```

### Step 2 — Install dependencies
`face_recognition` depends on **dlib**, which needs `cmake` and a C++ compiler.

- **Windows:** install "Desktop development with C++" via Visual Studio Build Tools, then:
  ```bash
  pip install -r requirements.txt
  ```
- **macOS:**
  ```bash
  brew install cmake
  pip install -r requirements.txt
  ```
- **Linux (Debian/Ubuntu):**
  ```bash
  sudo apt-get install -y cmake build-essential
  pip install -r requirements.txt
  ```

> If `dlib` fails to build (or you don't want to install a compiler), use the
> prebuilt wheel instead: `pip install dlib-bin`, then
> `pip install face_recognition==1.3.0 --no-deps` and install the remaining
> lines of `requirements.txt` (everything except `dlib` and `cmake`) plus `Click`.

### Step 3 — Run the app
```bash
python app.py
```
The database (`instance/attendance.db`) is created automatically on first run.

### Step 4 — Open in your browser
```
https://127.0.0.1:5000
```
The app uses a self-signed certificate, so the browser shows a warning the
first time: choose **Advanced → Proceed**.

> **Camera access requires HTTPS or `localhost`**, which is why the app serves
> HTTPS. To check in from a phone, the phone must be on the same network as
> the computer running the app, and the computer's firewall must allow
> incoming connections on port 5000.

### Optional — e-mail for "Forgot password"
Reset links are sent by e-mail when these environment variables are set
before starting the app:

```
MAIL_SERVER, MAIL_PORT (default 587), MAIL_USERNAME, MAIL_PASSWORD, MAIL_FROM
```

Without them (e.g. during a demo) the e-mail, including the reset link, is
**printed in the terminal** where `python app.py` is running. An admin can
also reset any user's password from **Users → Edit → Reset password**.

Set `SECRET_KEY` to a random value as well when running anywhere but locally.

---

## 6. Demo Walkthrough

1. Log in as the default **admin**, choose a new password, and add a **Professor**
   under **Users → Add user**. Note the temporary password that is shown.
   Then set the system up (once):
   - **Settings** → add the allowed student e-mail domain (e.g. `ubt-uni.net`).
   - **Academic** → add a faculty and a program; open the program and tick the
     years of study it offers (and add specialisations if it has any).
   - **Subjects → Add subject** → tick the professor, save, then add the
     group(s) the subject is **available for** (program + year [+ specialisation]).
2. Register one or more **Student** accounts from the landing page — each
   chooses their faculty, program and year, and must take (webcam) or upload a
   clear, single-face photo (this becomes their reference encoding and their
   profile photo).
3. Log in as the professor (a different browser / incognito window works well),
   set a new password → **New Session** → choose the subject → the QR code
   appears, with a live list of who has checked in.
4. As a student, scan the QR with the phone camera, or open the dashboard and
   use **Start QR scanner** (or **Scan from a photo** if the camera is unavailable).
5. Once the QR is accepted, **Step 2** appears → **Start camera** →
   **Verify & mark present**.
6. Back on the professor's (or admin's) **Reports**, the student now appears
   with a timestamp. Try the filters, **Export CSV** and **Print**.
7. Register a student of another program or year and scan the same QR: the
   check-in is refused. As admin, open that student under **Users → Edit** and
   add an **exception** for the subject: now they can check in.

---

## 7. Notes for the Thesis Write-up

- **Face matching** uses `face_recognition.face_distance()` (a Euclidean
  distance between 128-d face embeddings produced by a dlib ResNet model)
  with a tolerance threshold of `0.5` (see `FACE_MATCH_TOLERANCE` in
  `face_utils.py`) — lower distance = more similar face. The embedding is
  stored in the database; the reference photo is kept in `instance/faces/`
  and shown only to the student and to administrators. The live photo taken
  at check-in is never stored and must come from the camera (no upload).
- **QR codes** encode a check-in URL carrying `session_id` and `token`, where
  `token` is a random UUID4 generated per session, so QR codes cannot be
  guessed or reused across lectures.
- **Security measures in place:** passwords hashed with Werkzeug's
  `generate_password_hash`; role-based access control on every route;
  subject eligibility enforced on the server for every check-in;
  registration restricted to admin-configured e-mail domains;
  anti-forgery (CSRF) tokens on every POST; signed, expiring, single-use
  password-reset links; temporary passwords that must be changed at first
  login; deactivated accounts are logged out immediately.
- **Future work:** e-mail verification at registration (the domain rule
  checks the address, not that the student owns it), calendar academic years
  with automatic promotion to the next year of study, QR expiry timers / rotating QR codes, login rate limiting, and liveness
  detection (to prevent a printed photo from being used to spoof the face check).
