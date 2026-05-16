from flask import Blueprint, render_template, redirect, url_for, flash, request
import datetime

from app.db import get_db
from app.security import login_required
from app.rbac import require_perm
from app.audit import audit  # إذا عندك audit.py كما تستخدمه بباقي النظام

bp = Blueprint("attendance", __name__, url_prefix="/attendance")


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _current_user():
    # حاول يقرأ المستخدم الحالي بطريقة آمنة
    try:
        from flask import g
        u = getattr(g, "user", None)
        if isinstance(u, dict):
            return u.get("username") or u.get("name") or "unknown"
        if u:
            return getattr(u, "username", None) or str(u)
    except Exception:
        pass
    return "unknown"


@bp.get("/pending")
@login_required
@require_perm("attendance.manage")
def pending():
    db = get_db()
    rows = db.execute(
        """
        SELECT
          p.*,
          e.name AS emp_name,
          d.name AS device_name,
          b.name AS branch_name
        FROM pending_attendance p
        LEFT JOIN employees e ON e.employee_id = p.employee_id
        LEFT JOIN devices d ON d.id = p.device_id
        LEFT JOIN branches b ON b.id = p.branch_id
        WHERE p.status='pending'
        ORDER BY p.ts DESC, p.id DESC
        """
    ).fetchall()
    return render_template("attendance/pending.html", rows=rows)


@bp.post("/pending/<int:pid>/approve")
@login_required
@require_perm("attendance.manage")
def pending_approve(pid: int):
    db = get_db()
    row = db.execute(
        "SELECT * FROM pending_attendance WHERE id=? AND status='pending'",
        (pid,)
    ).fetchone()

    if not row:
        flash("Not found", "error")
        return redirect(url_for("attendance.pending"))

    approver = _current_user()
    now = _now_iso()

    # منع تكرار نفس الضربة عند الضغط مرتين
    db.execute(
        """
        INSERT INTO attendance(employee_id, branch_id, device_id, punch_type, ts,
                               approved_by, approved_ts, status)
        SELECT ?,?,?,?,?,?,?, 'approved'
        WHERE NOT EXISTS (
            SELECT 1 FROM attendance
            WHERE employee_id=? AND device_id=? AND ts=?
        )
        """,
        (
            row["employee_id"], row["branch_id"], row["device_id"], row["punch_type"], row["ts"],
            approver, now,
            row["employee_id"], row["device_id"], row["ts"]
        )
    )

    # احذف من pending حسب طلبك
    db.execute("DELETE FROM pending_attendance WHERE id=?", (pid,))
    db.commit()

    try:
        audit("attendance.approve", {"pending_id": pid, "by": approver})
    except Exception:
        pass

    flash(f"Approved by {approver}", "ok")
    return redirect(url_for("attendance.pending"))


@bp.post("/pending/<int:pid>/reject")
@login_required
@require_perm("attendance.manage")
def pending_reject(pid: int):
    db = get_db()
    row = db.execute(
        "SELECT * FROM pending_attendance WHERE id=? AND status='pending'",
        (pid,)
    ).fetchone()

    if not row:
        flash("Not found", "error")
        return redirect(url_for("attendance.pending"))

    # رفض = حذف (حسب طلبك)
    db.execute("DELETE FROM pending_attendance WHERE id=?", (pid,))
    db.commit()

    try:
        audit("attendance.reject", {"pending_id": pid, "by": _current_user()})
    except Exception:
        pass

    flash("Rejected (deleted)", "ok")
    return redirect(url_for("attendance.pending"))


@bp.get("/list")
@login_required
@require_perm("attendance.manage")
def attendance_list():
    db = get_db()
    rows = db.execute(
        """
        SELECT
          a.*,
          e.name AS emp_name,
          d.name AS device_name,
          b.name AS branch_name
        FROM attendance a
        LEFT JOIN employees e ON e.employee_id = a.employee_id
        LEFT JOIN devices d ON d.id = a.device_id
        LEFT JOIN branches b ON b.id = a.branch_id
        ORDER BY a.ts DESC, a.id DESC
        LIMIT 500
        """
    ).fetchall()
    return render_template("attendance/list.html", rows=rows)


@bp.post("/list/<int:aid>/edit")
@login_required
@require_perm("attendance.manage")
def attendance_edit(aid: int):
    db = get_db()
    row = db.execute("SELECT * FROM attendance WHERE id=?", (aid,)).fetchone()
    if not row:
        flash("Not found", "error")
        return redirect(url_for("attendance.attendance_list"))

    new_ts = (request.form.get("ts") or "").strip()
    note = (request.form.get("note") or "").strip()
    editor = _current_user()
    now = _now_iso()

    # التحقق من صيغة الوقت (ISO) – نخليها بسيطة
    if not new_ts or "T" not in new_ts:
        flash("Invalid time format", "error")
        return redirect(url_for("attendance.attendance_list"))

    # خزّن original_ts أول مرة فقط
    original_ts = row["original_ts"] if "original_ts" in row.keys() else None
    if not original_ts:
        original_ts = row["ts"]

    db.execute(
        """
        UPDATE attendance
        SET ts=?,
            edited_by=?,
            edited_ts=?,
            original_ts=?,
            edit_note=?
        WHERE id=?
        """,
        (new_ts, editor, now, original_ts, note, aid)
    )
    db.commit()

    try:
        audit("attendance.edit_time", {"id": aid, "by": editor, "from": row["ts"], "to": new_ts, "note": note})
    except Exception:
        pass

    flash(f"Time updated by {editor}", "ok")
    return redirect(url_for("attendance.attendance_list"))
