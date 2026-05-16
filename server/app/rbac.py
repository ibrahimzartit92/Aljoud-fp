from __future__ import annotations

from functools import wraps
from flask import g, redirect, url_for, flash

from .db import get_db


def _get_user():
    u = getattr(g, "user", None)
    if u is None:
        return None
    if isinstance(u, dict):
        return u
    try:
        return dict(u)
    except Exception:
        return u


def _is_superadmin(user) -> bool:
    if not user:
        return False
    try:
        v = user.get("is_superadmin") if isinstance(user, dict) else getattr(user, "is_superadmin", 0)
        return int(v or 0) == 1
    except Exception:
        return False


def _user_row_id(user) -> int | None:
    if not user:
        return None
    try:
        v = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)
        return int(v) if v is not None else None
    except Exception:
        return None


def _load_user_perms(user) -> set[str]:
    if not user:
        return set()
    if _is_superadmin(user):
        return {"*"}  # wildcard

    db = get_db()
    uid = _user_row_id(user)
    if uid is None:
        return set()

    rows = db.execute(
        """
        SELECT DISTINCT rp.perm_code
        FROM employee_roles er
        JOIN role_permissions rp ON rp.role_id = er.role_id
        WHERE er.employee_id = ?
        """,
        (uid,),
    ).fetchall()
    perms = {(r["perm_code"] or "").strip() for r in rows if (r["perm_code"] or "").strip()}

    try:
        rows2 = db.execute("SELECT perm_code FROM employee_permissions WHERE employee_id=?", (uid,)).fetchall()
        perms |= {(r["perm_code"] or "").strip() for r in rows2 if (r["perm_code"] or "").strip()}
    except Exception:
        pass

    return perms


def has_perm(code: str) -> bool:
    code = (code or "").strip()
    user = _get_user()
    if not user or not code:
        return False
    perms = getattr(g, "_perms_cache", None)
    if perms is None:
        perms = _load_user_perms(user)
        setattr(g, "_perms_cache", perms)
    return ("*" in perms) or (code in perms)


def require_perm(code: str):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if has_perm(code):
                return fn(*args, **kwargs)
            flash("ليس لديك صلاحية للوصول إلى هذه الصفحة.", "error")
            try:
                return redirect(url_for("attendance.pending"))
            except Exception:
                return redirect(url_for("auth.login"))
        return wrapper
    return deco
