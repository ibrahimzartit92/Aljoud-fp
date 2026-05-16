from functools import wraps
from flask import session, redirect, url_for, request, g, abort, current_app
from werkzeug.security import generate_password_hash, check_password_hash
from .db import get_db

def hash_password(raw: str) -> str:
    return generate_password_hash(raw, method="pbkdf2:sha256", salt_length=16)

def verify_password(hash_: str, raw: str) -> bool:
    try:
        return check_password_hash(hash_, raw)
    except Exception:
        return False

def load_current_user():
    g.user = None
    uid = session.get("user_id")
    if not uid:
        return
    db = get_db()
    row = db.execute("SELECT id, employee_id, name, username, is_active, is_superadmin FROM employees WHERE id=?", (uid,)).fetchone()
    if row and row["is_active"]:
        g.user = dict(row)

def require_login(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not getattr(g, "user", None):
            return redirect(url_for("auth.login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper

login_required = require_login

def require_agent_secret(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        expected = current_app.config.get("AGENT_SHARED_SECRET")
        given = request.headers.get("X-Aljoud-Agent-Secret", "")
        if not expected or given != expected:
            abort(403)
        return fn(*args, **kwargs)
    return wrapper
