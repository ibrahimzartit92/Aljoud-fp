from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from ..db import get_db
from ..security import verify_password
from ..audit import audit

bp = Blueprint("auth", __name__)

@bp.get("/login")
def login():
    return render_template("auth/login.html")

@bp.post("/login")
def login_post():
    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "").strip()
    db = get_db()
    row = db.execute("SELECT id, username, password_hash, is_active FROM employees WHERE username=?", (username,)).fetchone()
    if not row or not row["is_active"] or not verify_password(row["password_hash"], password):
        flash("Invalid credentials", "error")
        audit("auth.login_failed", {"username": username})
        return redirect(url_for("auth.login"))
    session["user_id"] = row["id"]
    audit("auth.login", {"username": username})
    nxt = request.args.get("next") or url_for("attendance.pending")
    return redirect(nxt)

@bp.get("/logout")
def logout():
    uid = session.get("user_id")
    session.pop("user_id", None)
    audit("auth.logout", {"user_id": uid})
    return redirect(url_for("auth.login"))

@bp.get("/lang/<code>")
def lang(code: str):
    if code in ("ar","de"):
        session["locale"] = code
    return redirect(request.referrer or url_for("attendance.pending"))
