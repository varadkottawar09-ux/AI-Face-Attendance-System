import os, sqlite3, base64, io, json
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, make_response
from werkzeug.utils import secure_filename

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "attendance.db")
UPLOAD_DIR = os.path.join(APP_DIR, "student_photos")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key-before-deployment")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg"}

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT, roll_no TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL, photo TEXT NOT NULL, encoding TEXT NOT NULL,
            created_at TEXT NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL,
            attendance_date TEXT NOT NULL, attendance_time TEXT NOT NULL,
            UNIQUE(student_id, attendance_date),
            FOREIGN KEY(student_id) REFERENCES students(id))""")
init_db()

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin"): return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("username") == ADMIN_USERNAME and request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/admin")
@admin_required
def dashboard():
    with db() as con:
        students = con.execute("SELECT id,roll_no,name,photo FROM students ORDER BY roll_no").fetchall()
        today = datetime.now().strftime("%Y-%m-%d")
        records = con.execute("""SELECT s.roll_no,s.name,a.attendance_date,a.attendance_time
            FROM attendance a JOIN students s ON s.id=a.student_id
            WHERE a.attendance_date=? ORDER BY a.attendance_time DESC""", (today,)).fetchall()
        total = con.execute("SELECT COUNT(*) FROM students").fetchone()[0]
    return render_template("dashboard.html", students=students, records=records, total=total, today=today)

@app.route("/students/add", methods=["POST"])
@admin_required
def add_student():
    roll, name = request.form.get("roll_no", "").strip(), request.form.get("name", "").strip()
    photo = request.files.get("photo")
    if not roll or not name or not photo or not photo.filename or not allowed_file(photo.filename):
        flash("Enter roll number, name, and upload a JPG/PNG photo.")
        return redirect(url_for("dashboard"))
    try:
        import face_recognition
        import numpy as np
        image = face_recognition.load_image_file(photo.stream)
        encodings = face_recognition.face_encodings(image)
        if len(encodings) != 1:
            flash("Photo must contain exactly one clearly visible face.")
            return redirect(url_for("dashboard"))
        filename = secure_filename(f"{roll}_{photo.filename}")
        photo.stream.seek(0)
        photo.save(os.path.join(UPLOAD_DIR, filename))
        with db() as con:
            con.execute("INSERT INTO students(roll_no,name,photo,encoding,created_at) VALUES(?,?,?,?,?)",
                (roll, name, filename, json.dumps(np.asarray(encodings[0]).tolist()), datetime.now().isoformat(timespec="seconds")))
        flash("Student added successfully.")
    except sqlite3.IntegrityError:
        flash("That roll number is already registered.")
    except Exception as exc:
        flash(f"Could not add student: {exc}")
    return redirect(url_for("dashboard"))

@app.route("/students/delete/<int:student_id>", methods=["POST"])
@admin_required
def delete_student(student_id):
    with db() as con:
        row = con.execute("SELECT photo FROM students WHERE id=?", (student_id,)).fetchone()
        if row:
            try: os.remove(os.path.join(UPLOAD_DIR, row["photo"]))
            except OSError: pass
            con.execute("DELETE FROM attendance WHERE student_id=?", (student_id,))
            con.execute("DELETE FROM students WHERE id=?", (student_id,))
    flash("Student deleted.")
    return redirect(url_for("dashboard"))

@app.route("/recognize", methods=["POST"])
def recognize():
    payload = request.json or {}
    data = payload.get("image", "")
    if "," in data: data = data.split(",", 1)[1]
    try:
        import face_recognition
        import numpy as np
        image = face_recognition.load_image_file(io.BytesIO(base64.b64decode(data)))
        encodings = face_recognition.face_encodings(image, face_recognition.face_locations(image, model="hog"))
        if not encodings: return jsonify(ok=False, message="No face detected. Look at the camera and try again.")
        with db() as con:
            students = con.execute("SELECT id,roll_no,name,encoding FROM students").fetchall()
        if not students: return jsonify(ok=False, message="No students are registered yet.")
        known = [np.array(json.loads(s["encoding"])) for s in students]
        for face in encodings:
            distances = face_recognition.face_distance(known, face)
            best = int(np.argmin(distances))
            if distances[best] < 0.48:
                student, now = students[best], datetime.now()
                with db() as con:
                    try:
                        con.execute("INSERT INTO attendance(student_id,attendance_date,attendance_time) VALUES(?,?,?)",
                            (student["id"], now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S")))
                        status = "Attendance marked"
                    except sqlite3.IntegrityError: status = "Already marked today"
                return jsonify(ok=True, message=status, name=student["name"], roll_no=student["roll_no"])
        return jsonify(ok=False, message="Face not matched with registered students.")
    except Exception as exc:
        return jsonify(ok=False, message=f"Recognition error: {exc}"), 400

@app.route("/attendance.csv")
@admin_required
def attendance_csv():
    import csv
    with db() as con:
        rows = con.execute("""SELECT s.roll_no,s.name,a.attendance_date,a.attendance_time
            FROM attendance a JOIN students s ON s.id=a.student_id
            ORDER BY a.attendance_date DESC,a.attendance_time DESC""").fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Roll No","Name","Date","Time","Status"])
    for r in rows: writer.writerow([r["roll_no"],r["name"],r["attendance_date"],r["attendance_time"],"Present"])
    response = make_response(output.getvalue())
    response.headers["Content-Disposition"] = "attachment; filename=attendance.csv"
    response.headers["Content-Type"] = "text/csv"
    return response

@app.route("/student_photos/<path:filename>")
@admin_required
def student_photo(filename):
    from flask import send_from_directory
    return send_from_directory(UPLOAD_DIR, filename)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
