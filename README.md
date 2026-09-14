# Intelligent Student Attendance Management System
### QR Code + Face Recognition — Flask / SQLite / HTML-CSS-JS

A complete, working full-stack web app: professors create lecture sessions
and generate a QR code; students scan it with their camera and confirm
their identity with a live face-recognition check; attendance is recorded
automatically only when **both** checks pass.

---

## 1. Project Structure

```
attendance_system/
│
├── app.py                 # Main Flask app: all routes / API endpoints
├── database.py             # SQLite connection + schema (users, sessions, attendance)
├── face_utils.py           # Face recognition helpers (encode, compare)
├── qr_utils.py              # QR code generation (base64 PNG)
├── requirements.txt         # Python dependencies
│
├── instance/
│   └── attendance.db        # SQLite database file (auto-created on first run)
│
├── templates/                # Jinja2 HTML templates
│   ├── base.html             # Shared layout, navbar, flash messages
│   ├── index.html            # Landing page
│   ├── register.html         # Registration (role select + webcam face capture)
│   ├── login.html
│   ├── professor_dashboard.html
│   ├── create_session.html   # Create lecture + QR code display
│   ├── attendance_report.html
│   └── student_dashboard.html # QR scan + face verify + attendance history
│
└── static/
    ├── css/style.css          # All styling
    └── js/
        ├── register.js        # Webcam capture for registration
        └── student.js         # QR scan (html5-qrcode) + face verify flow
```

---

## 2. Database Schema (SQLite)

**users**: `id, full_name, email, password_hash, role (professor/student), student_id, face_encoding (JSON), created_at`

**sessions**: `id, professor_id, subject, token (unique), is_active, created_at`

**attendance**: `id, session_id, student_id, marked_at, status` — `UNIQUE(session_id, student_id)` prevents double check-in.

---

## 3. API / Route Summary

| Method | Route                 | Role      | Purpose                                            |
|--------|------------------------|-----------|-----------------------------------------------------|
| GET/POST | `/register`          | public    | Create account (students also submit a face photo) |
| GET/POST | `/login`              | public    | Authenticate, starts a session cookie               |
| GET    | `/logout`              | any       | Clears the session                                   |
| GET/POST | `/create-session`    | professor | Create a lecture session + generate its QR code      |
| POST   | `/end-session/<id>`    | professor | Closes a session so its QR can no longer be used     |
| GET    | `/attendance-report`   | professor | View attendance (optionally `?session_id=`)          |
| POST   | `/scan-qr`             | student   | Validates a scanned QR code (step 1 of check-in)     |
| POST   | `/verify-face`         | student   | Matches live face vs stored encoding, marks present  |

**Attendance rule implemented exactly as required:**
`QR valid` (checked in `/scan-qr`, remembered in the session) **+** `face match`
(checked in `/verify-face`) → **only then** is a row written to `attendance`.

---

## 4. How to Run

### Step 1 — Create a virtual environment (recommended)
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

> If `dlib` fails to build, installing a prebuilt wheel (`pip install dlib-binary`)
> or using conda (`conda install -c conda-forge dlib`) is the easiest workaround.

### Step 3 — Run the app
```bash
python app.py
```
The database (`instance/attendance.db`) is created automatically on first run.

### Step 4 — Open in your browser
```
http://127.0.0.1:5000
```

> **Camera access requires HTTPS or `localhost`.** Running on `127.0.0.1` /
> `localhost` works fine in Chrome/Firefox. If you deploy elsewhere, you'll
> need HTTPS for `getUserMedia` (webcam) to work.

---

## 5. Demo Walkthrough

1. Register a **Professor** account (no photo needed).
2. Register one or more **Student** accounts — each must capture a clear,
   single-face photo during registration (this becomes their reference encoding).
3. Log in as the professor → **New Session** → enter a subject → a QR code appears.
4. Log in as a student (a different browser / incognito window works well for
   a live demo) → **Scan QR Code** → point the camera at the QR on screen.
5. Once the QR is accepted, **Step 2** appears → capture a live face photo →
   click **Verify & Mark Present**.
6. Back on the professor's **Attendance Report**, the student now appears
   with a timestamp.

---

## 6. Notes for the Thesis Write-up

- **Face matching** uses `face_recognition.face_distance()` (a Euclidean
  distance between 128-d face embeddings produced by a dlib ResNet model)
  with a tolerance threshold of `0.5` (see `FACE_MATCH_TOLERANCE` in
  `face_utils.py`) — lower distance = more similar face.
- **QR codes** encode a JSON payload `{session_id, token}` where `token` is
  a random UUID4 generated per session, so QR codes cannot be guessed or reused
  across lectures.
- **Security note (for the "future work" section):** passwords are hashed
  with Werkzeug's `generate_password_hash` (PBKDF2); for a production system
  you would add HTTPS, QR expiry timers, rate limiting, and liveness detection
  (to prevent a printed photo from being used to spoof the face check).
