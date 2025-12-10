import os
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, session, abort, jsonify
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from pymongo import MongoClient
from bson import ObjectId
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv


# cleannig name
def clean_name(name: str) -> str:
    """
    Normalize student name:
      - returns empty string for falsy input
      - strips leading/trailing spaces
      - collapses multiple internal spaces to one
      - converts to Title Case (each word capitalized)
    Example: "  moHaMmAd   fairoz  " -> "Mohammad Fairoz"
    """
    if not name:
        return ""
    # split() collapses multiple whitespace and trims ends, then join with single spaces
    cleaned = " ".join(name.split())
    return cleaned.title()


load_dotenv()

# --- Configuration ---
MONGO_URI = os.getenv("MONGO_URI",)
SECRET_KEY = os.getenv("SECRET_KEY")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")  # optional convenience for first-run
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH")  # optional pre-hash

# --- Flask / DB setup ---
app = Flask(__name__)
app.secret_key = SECRET_KEY

client = MongoClient(MONGO_URI)
db = client.bright_horizon
students_col = db.students
admins_col = db.admins
logs_col = db.logs

login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.init_app(app)


class AdminUser(UserMixin):
    def __init__(self, admin_doc):
        self.id = str(admin_doc["_id"])
        self.username = admin_doc.get("username")


@login_manager.user_loader
def load_user(user_id):
    try:
        admin_doc = admins_col.find_one({"_id": ObjectId(user_id)})
    except:
        return None
    if not admin_doc:
        return None
    return AdminUser(admin_doc)


@login_manager.unauthorized_handler
def unauthorized():
    flash("Please log in to access this page.")
    return redirect(url_for("login"))


def ensure_admin():
    """On first run: if no admin exists, create one from env var."""
    if admins_col.count_documents({}) == 0:
        if ADMIN_PASSWORD_HASH:
            pw_hash = ADMIN_PASSWORD_HASH
        elif ADMIN_PASSWORD:
            pw_hash = generate_password_hash(ADMIN_PASSWORD)
        else:
            print("WARNING: No admin credentials provided! Set ADMIN_PASSWORD or ADMIN_PASSWORD_HASH env var.")
            return
        admins_col.insert_one({
            "username": ADMIN_USERNAME,
            "password_hash": pw_hash,
            "created_at": datetime.utcnow()
        })
        print(f"Admin user '{ADMIN_USERNAME}' created.")


ensure_admin()


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        return f(*args, **kwargs)
    return wrapper


def calc_unpaid(student):
    total_fee = student.get("total_fee", 0)
    payments = student.get("payments", [])
    received = sum(p.get("amount", 0) for p in payments)
    return total_fee - received


def log_action(act, details):
    logs_col.insert_one({
        "action": act,
        "details": details,
        "by": current_user.username if current_user.is_authenticated else "system",
        "at": datetime.utcnow()
    })


# --- Routes ---

@app.route("/")
def home():
    return render_template("index.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        admin = admins_col.find_one({"username": username})
        if not admin or not check_password_hash(admin["password_hash"], password):
            flash("Invalid credentials", "error")
            return redirect(url_for("login"))

        user = AdminUser(admin)
        login_user(user)

        # FIX: remove any earlier “unauthorized” flashes so user sees only “Logged in”
        session.pop("_flashes", None)
        flash("Logged in", "success")
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out", "info")
    return redirect(url_for("login"))


@app.route("/admin")
@admin_required
def dashboard():
    classes = students_col.distinct("class")
    class_stats = []
    total_collected = 0
    total_outstanding = 0
    for cls in classes:
        students = list(students_col.find({"class": cls}))
        class_total = sum(s.get("total_fee", 0) for s in students)
        collected = sum(
            sum(p.get("amount", 0) for p in s.get("payments", []))
            for s in students
        )
        outstanding = class_total - collected
        class_stats.append({
            "class": cls,
            "class_total": class_total,
            "collected": collected,
            "outstanding": outstanding,
            "students_count": len(students)
        })
        total_collected += collected
        total_outstanding += outstanding

    latest_students = list(students_col.find().sort("created_at", -1).limit(6))
    return render_template("dashboard.html",
                           class_stats=class_stats,
                           total_collected=total_collected,
                           total_outstanding=total_outstanding,
                           latest_students=latest_students)


@app.route("/admin/students")
@admin_required
def students_list():
    cls = request.args.get("class")
    query = {}
    if cls:
        query["class"] = cls
    docs = list(students_col.find(query).sort("name", 1))
    students = []
    for s in docs:
        s["_id"] = str(s["_id"])
        s["unpaid"] = calc_unpaid(s)
        students.append(s)
    classes = students_col.distinct("class")
    return render_template("students_list.html",
                           students=students,
                           classes=classes,
                           selected_class=cls)


@app.route("/admin/student/add", methods=["GET", "POST"])
@admin_required
def add_student():
    if request.method == "POST":
        raw_name = request.form.get("name")
        name = clean_name(raw_name)                      # normalize
        cls = request.form.get("class", "").strip()
        contact = request.form.get("contact", "").strip()
        total_fee = float(request.form.get("total_fee") or 0)

        # Duplicate check (exact match on normalized name + same class)
        existing = students_col.find_one({
            "name": name,
            "class": cls
        })

        if existing:
            flash("⚠ A student with this name already exists in this class!", "warning")
            return redirect(url_for("add_student"))

        now = datetime.utcnow()
        student = {
            "name": name,
            "class": cls,
            "contact": contact,
            "total_fee": total_fee,
            "payments": [],
            "created_at": now,
            "updated_at": now
        }
        res = students_col.insert_one(student)
        log_action("add_student", {"student_id": str(res.inserted_id), "name": name})
        flash("Student added", "success")
        return redirect(url_for("students_list"))

    return render_template("student_form.html", action="Add", student=None)




@app.route("/admin/student/<sid>/edit", methods=["GET", "POST"])
@admin_required
def edit_student(sid):
    from bson import ObjectId
    student = students_col.find_one({"_id": ObjectId(sid)})
    if not student:
        flash("Student not found", "error")
        return redirect(url_for("students_list"))

    if request.method == "POST":
        raw_name = request.form.get("name")
        name = clean_name(raw_name)                      # normalize
        cls = request.form.get("class", "").strip()
        contact = request.form.get("contact", "").strip()
        total_fee = float(request.form.get("total_fee") or 0)

        # Optional: check duplicates when editing (if name/class changed)
        existing = students_col.find_one({
            "name": name,
            "class": cls,
            "_id": {"$ne": ObjectId(sid)}   # ignore the same document
        })
        if existing:
            flash("⚠ Another student with this name already exists in this class!", "warning")
            return redirect(url_for("edit_student", sid=sid))

        students_col.update_one({"_id": ObjectId(sid)}, {"$set": {
            "name": name,
            "class": cls,
            "contact": contact,
            "total_fee": total_fee,
            "updated_at": datetime.utcnow()
        }})
        log_action("edit_student", {"student_id": sid})
        flash("Student updated", "success")
        return redirect(url_for("students_list"))

    # prepare student for display
    student["_id"] = str(student.get("_id"))
    return render_template("student_form.html", action="Edit", student=student)



@app.route("/admin/student/<sid>/delete", methods=["POST"])
@admin_required
def delete_student(sid):
    students_col.delete_one({"_id": ObjectId(sid)})
    log_action("delete_student", {"student_id": sid})
    flash("Student deleted", "success")
    return redirect(url_for("students_list"))


@app.route("/admin/student/<sid>/add_payment", methods=["POST"])
@admin_required
def add_payment(sid):
    try:
        amount = float(request.form.get("amount") or 0)
    except ValueError:
        amount = 0
    note = request.form.get("note")
    payment = {
        "amount": amount,
        "date": datetime.utcnow(),
        "note": note
    }
    students_col.update_one(
        {"_id": ObjectId(sid)},
        {"$push": {"payments": payment},
         "$set": {"updated_at": datetime.utcnow()}}
    )
    log_action("add_payment", {"student_id": sid, "amount": amount})
    flash("Payment recorded", "success")
    return redirect(url_for("edit_student", sid=sid))


@app.route("/api/stats/class")
@admin_required
def api_class_stats():
    classes = students_col.distinct("class")
    labels = []
    collected = []
    outstanding = []
    for cls in classes:
        students = list(students_col.find({"class": cls}))
        coll = sum(sum(p.get("amount", 0) for p in s.get("payments", [])) for s in students)
        tot = sum(s.get("total_fee", 0) for s in students)
        out = tot - coll
        labels.append(cls)
        collected.append(coll)
        outstanding.append(out)
    return jsonify({"labels": labels, "collected": collected, "outstanding": outstanding})


@app.route("/admin/unpaid")
@admin_required
def unpaid():
    docs = list(students_col.find())
    unpaid_list = []
    for s in docs:
        unpaid_amt = calc_unpaid(s)
        if unpaid_amt > 0:
            unpaid_list.append({
                "_id": str(s["_id"]),
                "name": s.get("name"),
                "class": s.get("class"),
                "unpaid": unpaid_amt
            })
    return render_template("unpaid.html", unpaid_list=unpaid_list)


@app.route("/admin/logs")
@admin_required
def logs():
    docs = list(logs_col.find().sort("at", -1).limit(200))
    # convert ObjectId and datetime to string for template safety
    for l in docs:
        l["at"] = l.get("at").strftime("%Y-%m-%d %H:%M:%S")
        l["details"] = str(l.get("details"))
    return render_template("logs.html", logs=docs)

@app.route("/admin/summary")
@admin_required
def summary():
    # Total students
    total_students = students_col.count_documents({})

    # Class-wise counts
    pipeline = [
        {"$group": {"_id": "$class", "count": {"$sum": 1}}},
        {"$sort": {"_id": 1}}
    ]
    class_counts = list(students_col.aggregate(pipeline))

    # Free students (monthly_fee = 0)
    free_students = students_col.count_documents({"total_fee": 0})

    return render_template(
        "summary.html",
        total_students=total_students,
        class_counts=class_counts,
        free_students=free_students
    )


# --- Error handler ---
@app.errorhandler(403)
def forbidden(e):
    return render_template("403.html"), 403


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
