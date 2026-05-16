from flask import Blueprint, render_template, redirect, url_for, flash, request
import datetime
import re

from app.db import get_db
from app.security import login_required
from app.rbac import require_perm
from app.audit import audit  # إذا عندك audit.py كما تستخدمه بباقي النظام

bp = Blueprint("attendance", __name__, url_prefix="/attendance")

MISSING_CHECKOUT_START_DATE = "2026-05-16"


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


def _table_cols(db, table: str) -> set[str]:
    try:
        rows = db.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(r[1]) for r in rows}
    except Exception:
        return set()


def _real_out_exists(db, employee_id: str, device_id, in_ts: str) -> bool:
    next_in = db.execute(
        """
        SELECT ts FROM attendance
        WHERE employee_id=? AND device_id=? AND punch_type='in' AND ts > ?
        ORDER BY ts ASC, id ASC
        LIMIT 1
        """,
        (employee_id, device_id, in_ts),
    ).fetchone()
    upper = next_in["ts"] if next_in else None

    params = [employee_id, device_id, in_ts]
    where = "employee_id=? AND device_id=? AND punch_type='out' AND ts > ?"
    if upper:
        where += " AND ts < ?"
        params.append(upper)

    return db.execute(f"SELECT 1 FROM attendance WHERE {where} LIMIT 1", params).fetchone() is not None


def _ensure_missing_checkout_proposals(db):
    today = datetime.date.today().isoformat()
    now = _now_iso()
    pending_cols = _table_cols(db, "pending_attendance")

    pending_rows = db.execute(
        """
        SELECT * FROM pending_attendance
        WHERE source='missing_checkout' AND status='pending'
        """
    ).fetchall()

    for p in pending_rows:
        day = str(p["ts"] or "")[:10]
        in_row = db.execute(
            """
            SELECT * FROM attendance
            WHERE employee_id=? AND device_id=? AND punch_type='in'
              AND ts >= ? AND ts <= ?
            ORDER BY ts DESC, id DESC
            LIMIT 1
            """,
            (p["employee_id"], p["device_id"], f"{day}T00:00:00", p["ts"]),
        ).fetchone()
        if in_row and _real_out_exists(db, p["employee_id"], p["device_id"], in_row["ts"]):
            db.execute("DELETE FROM pending_attendance WHERE id=?", (p["id"],))

    in_rows = db.execute(
        """
        SELECT * FROM attendance
        WHERE punch_type='in' AND date(ts) >= ? AND date(ts) < ?
        ORDER BY employee_id ASC, device_id ASC, ts ASC, id ASC
        """,
        (MISSING_CHECKOUT_START_DATE, today),
    ).fetchall()

    for row in in_rows:
        if _real_out_exists(db, row["employee_id"], row["device_id"], row["ts"]):
            continue

        proposed_ts = f"{str(row['ts'])[:10]}T23:59:00"
        exists = db.execute(
            """
            SELECT 1 FROM pending_attendance
            WHERE source='missing_checkout'
              AND employee_id=? AND device_id=? AND ts=? AND status='pending'
            LIMIT 1
            """,
            (row["employee_id"], row["device_id"], proposed_ts),
        ).fetchone()
        if exists:
            continue

        cols = ["employee_id", "branch_id", "device_id", "punch_type", "ts", "source", "status"]
        vals = [row["employee_id"], row["branch_id"], row["device_id"], "out", proposed_ts, "missing_checkout", "pending"]
        if "requested_edit" in pending_cols:
            cols.append("requested_edit")
            vals.append(1)
        if "requested_by" in pending_cols:
            cols.append("requested_by")
            vals.append("auto_missing_checkout")
        if "requested_ts" in pending_cols:
            cols.append("requested_ts")
            vals.append(now)

        placeholders = ",".join("?" for _ in cols)
        db.execute(f"INSERT INTO pending_attendance ({','.join(cols)}) VALUES ({placeholders})", vals)

    db.commit()


@bp.get("/pending")
@login_required
@require_perm("attendance.manage")
def pending():
    db = get_db()
    _ensure_missing_checkout_proposals(db)
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
    employee_id = row["employee_id"]
    branch_id = row["branch_id"]
    device_id = row["device_id"]
    punch_type = row["punch_type"]
    ts = row["ts"]

    if row["source"] == "missing_checkout":
        checkout_date = (request.form.get("checkout_date") or "").strip()
        checkout_time = (request.form.get("checkout_time") or "").strip()
        try:
            datetime.date.fromisoformat(checkout_date)
        except Exception:
            flash("تاريخ الخروج غير صحيح.", "error")
            return redirect(url_for("attendance.pending"))
        if not re.match(r"^\d{2}:\d{2}(:\d{2})?$", checkout_time):
            flash("وقت الخروج يجب أن يكون بصيغة 24 ساعة مثل 17:30 أو 23:59.", "error")
            return redirect(url_for("attendance.pending"))
        if len(checkout_time) == 5:
            checkout_time += ":00"
        try:
            datetime.time.fromisoformat(checkout_time)
        except Exception:
            flash("وقت الخروج يجب أن يكون بصيغة 24 ساعة مثل 17:30 أو 23:59.", "error")
            return redirect(url_for("attendance.pending"))

        day = str(row["ts"])[:10]
        checkout_ts = f"{checkout_date}T{checkout_time}"

        in_row = db.execute(
            """
            SELECT * FROM attendance
            WHERE employee_id=? AND device_id=? AND punch_type='in'
              AND ts >= ? AND ts <= ?
            ORDER BY ts DESC, id DESC
            LIMIT 1
            """,
            (employee_id, device_id, f"{day}T00:00:00", row["ts"]),
        ).fetchone()
        if not in_row:
            flash("لم يتم العثور على ضربة دخول مطابقة لهذا التنبيه.", "error")
            return redirect(url_for("attendance.pending"))
        if checkout_ts <= in_row["ts"]:
            flash("وقت الخروج يجب أن يكون بعد وقت الدخول.", "error")
            return redirect(url_for("attendance.pending"))

        ts = checkout_ts
        punch_type = "out"

        if in_row and _real_out_exists(db, employee_id, device_id, in_row["ts"]):
            db.execute("DELETE FROM pending_attendance WHERE id=?", (pid,))
            db.commit()
            flash("تم تجاهل التنبيه لأن ضربة خروج حقيقية موجودة.", "ok")
            return redirect(url_for("attendance.pending"))

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
            employee_id, branch_id, device_id, punch_type, ts,
            approver, now,
            employee_id, device_id, ts
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
