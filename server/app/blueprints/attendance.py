from flask import Blueprint, render_template, redirect, url_for, flash, request
import datetime
import re

from app.db import get_db
from app.security import login_required
from app.rbac import require_perm
from app.audit import audit  # إذا عندك audit.py كما تستخدمه بباقي النظام

bp = Blueprint("attendance", __name__, url_prefix="/attendance")

MISSING_CHECKOUT_START_DATE = "2026-05-16"
LONG_SESSION_THRESHOLD_MINUTES = 12 * 60
NIGHT_SHIFT_START = datetime.time(20, 0)


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


def _is_night_shift_in(in_ts) -> bool:
    try:
        return datetime.datetime.fromisoformat(str(in_ts)).time() >= NIGHT_SHIFT_START
    except Exception:
        return False


def _any_real_out_after_in(db, employee_id: str, device_id, in_ts: str) -> bool:
    return db.execute(
        """
        SELECT 1 FROM attendance
        WHERE employee_id=? AND device_id=? AND punch_type='out'
          AND ts > ?
        LIMIT 1
        """,
        (employee_id, device_id, in_ts),
    ).fetchone() is not None


def _real_out_exists(db, employee_id: str, device_id, in_ts: str) -> bool:
    if _is_night_shift_in(in_ts):
        try:
            in_dt = datetime.datetime.fromisoformat(str(in_ts))
        except Exception:
            return False
        out_until = (in_dt + datetime.timedelta(hours=12)).isoformat(timespec="seconds")
    else:
        day = str(in_ts or "")[:10]
        out_until = f"{day}T23:59:59"

    return db.execute(
        """
        SELECT 1 FROM attendance
        WHERE employee_id=? AND device_id=? AND punch_type='out'
          AND ts > ? AND ts <= ?
        LIMIT 1
        """,
        (employee_id, device_id, in_ts, out_until),
    ).fetchone() is not None


def _fmt_duration_hhmm(total_minutes) -> str:
    minutes = max(0, int(total_minutes or 0))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _parse_checkout_date(value: str) -> str | None:
    if not re.match(r"^\d{2}/\d{2}/\d{4}$", value or ""):
        return None
    try:
        day, month, year = value.split("/")
        return datetime.date(int(year), int(month), int(day)).isoformat()
    except Exception:
        return None


def _parse_checkout_time(value: str) -> str | None:
    if not re.match(r"^\d{2}:\d{2}(:\d{2})?$", value or ""):
        return None
    checkout_time = value
    if len(checkout_time) == 5:
        checkout_time += ":00"
    try:
        hour_s, minute_s, second_s = checkout_time.split(":")
        hour = int(hour_s)
        minute = int(minute_s)
        second = int(second_s)
        if hour > 23 or minute > 59 or second > 59:
            return None
        datetime.time.fromisoformat(checkout_time)
    except Exception:
        return None
    return checkout_time


def _parse_attendance_ts(value: str) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(str(value))
    except Exception:
        return None


def _ensure_long_session_review_table(db):
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS long_session_reviews (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          in_attendance_id INTEGER NOT NULL,
          out_attendance_id INTEGER NOT NULL,
          employee_id TEXT NOT NULL,
          in_ts TEXT NOT NULL,
          out_ts TEXT NOT NULL,
          duration_min INTEGER NOT NULL,
          decision TEXT NOT NULL,
          reviewed_by TEXT,
          reviewed_ts TEXT NOT NULL,
          note TEXT,
          UNIQUE(in_attendance_id, out_attendance_id)
        )
        """
    )


def _long_session_join_sql(where_clause: str) -> str:
    return f"""
        SELECT
          a.*,
          e.name AS emp_name,
          d.name AS device_name,
          b.name AS branch_name
        FROM attendance a
        LEFT JOIN employees e ON e.employee_id = a.employee_id
        LEFT JOIN devices d ON d.id = a.device_id
        LEFT JOIN branches b ON b.id = a.branch_id
        {where_clause}
        """


def _build_long_session_row(in_row, out_row) -> dict | None:
    in_dt = _parse_attendance_ts(in_row["ts"])
    out_dt = _parse_attendance_ts(out_row["ts"])
    if not in_dt or not out_dt or out_dt <= in_dt:
        return None

    duration_min = int((out_dt - in_dt).total_seconds() // 60)
    return {
        "in_id": in_row["id"],
        "out_id": out_row["id"],
        "employee_id": in_row["employee_id"],
        "emp_name": in_row["emp_name"] or out_row["emp_name"] or "",
        "branch_name": in_row["branch_name"] or out_row["branch_name"] or "",
        "device_name": in_row["device_name"] or out_row["device_name"] or "",
        "in_ts": in_row["ts"],
        "out_ts": out_row["ts"],
        "in_date": str(in_row["ts"])[:10],
        "in_time": str(in_row["ts"])[11:19],
        "out_date": str(out_row["ts"])[:10],
        "out_date_display": f"{str(out_row['ts'])[8:10]}/{str(out_row['ts'])[5:7]}/{str(out_row['ts'])[:4]}",
        "out_time": str(out_row["ts"])[11:19],
        "duration_min": duration_min,
        "duration_txt": _fmt_duration_hhmm(duration_min),
    }


def _fetch_long_session_pair(db, in_id: int, out_id: int) -> dict | None:
    in_row = db.execute(
        _long_session_join_sql("WHERE a.id=? AND a.punch_type='in'"),
        (in_id,),
    ).fetchone()
    out_row = db.execute(
        _long_session_join_sql("WHERE a.id=? AND a.punch_type='out'"),
        (out_id,),
    ).fetchone()
    if not in_row or not out_row or in_row["employee_id"] != out_row["employee_id"]:
        return None
    return _build_long_session_row(in_row, out_row)


def _insert_long_session_review(db, pair: dict, decision: str, reviewed_by: str, reviewed_ts: str, note: str):
    db.execute(
        """
        INSERT OR IGNORE INTO long_session_reviews(
          in_attendance_id, out_attendance_id, employee_id, in_ts, out_ts,
          duration_min, decision, reviewed_by, reviewed_ts, note
        )
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            pair["in_id"],
            pair["out_id"],
            pair["employee_id"],
            pair["in_ts"],
            pair["out_ts"],
            pair["duration_min"],
            decision,
            reviewed_by,
            reviewed_ts,
            note,
        ),
    )


def _fetch_long_session_rows(db) -> list[dict]:
    _ensure_long_session_review_table(db)
    rows = db.execute(
        _long_session_join_sql(
            """
            WHERE a.punch_type IN ('in', 'out')
            ORDER BY a.employee_id ASC, a.ts ASC, a.id ASC
            """
        )
    ).fetchall()

    open_in_by_employee = {}
    long_rows = []
    for row in rows:
        employee_id = row["employee_id"]
        punch_type = row["punch_type"]
        if punch_type == "in":
            if employee_id not in open_in_by_employee:
                open_in_by_employee[employee_id] = row
            continue
        if punch_type != "out" or employee_id not in open_in_by_employee:
            continue

        in_row = open_in_by_employee.pop(employee_id)
        pair = _build_long_session_row(in_row, row)
        if not pair or pair["duration_min"] <= LONG_SESSION_THRESHOLD_MINUTES:
            continue
        reviewed = db.execute(
            """
            SELECT 1 FROM long_session_reviews
            WHERE in_attendance_id=? AND out_attendance_id=?
            LIMIT 1
            """,
            (pair["in_id"], pair["out_id"]),
        ).fetchone()
        if not reviewed:
            long_rows.append(pair)

    return long_rows


def _ensure_missing_checkout_proposals(db):
    today = datetime.date.today().isoformat()
    now_dt = datetime.datetime.now()
    now = now_dt.isoformat(timespec="seconds")
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
        if _any_real_out_after_in(db, row["employee_id"], row["device_id"], row["ts"]):
            continue

        try:
            in_dt = datetime.datetime.fromisoformat(str(row["ts"]))
        except Exception:
            continue

        if _is_night_shift_in(row["ts"]):
            deadline = in_dt + datetime.timedelta(hours=12)
            if now_dt < deadline:
                continue
            proposed_ts = deadline.isoformat(timespec="seconds")
        else:
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
    missing_checkout_rows = [r for r in rows if r["source"] == "missing_checkout"]
    normal_pending_rows = [r for r in rows if r["source"] != "missing_checkout"]
    long_session_rows = _fetch_long_session_rows(db)
    return render_template(
        "attendance/pending.html",
        rows=rows,
        missing_checkout_rows=missing_checkout_rows,
        normal_pending_rows=normal_pending_rows,
        long_session_rows=long_session_rows,
    )


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
        if not re.match(r"^\d{2}/\d{2}/\d{4}$", checkout_date):
            flash("تاريخ الخروج يجب أن يكون بصيغة يوم/شهر/سنة مثل 16/05/2026.", "error")
            return redirect(url_for("attendance.pending"))
        try:
            checkout_day, checkout_month, checkout_year = checkout_date.split("/")
            checkout_date_iso = datetime.date(
                int(checkout_year), int(checkout_month), int(checkout_day)
            ).isoformat()
        except Exception:
            flash("تاريخ الخروج يجب أن يكون بصيغة يوم/شهر/سنة مثل 16/05/2026.", "error")
            return redirect(url_for("attendance.pending"))
        if not re.match(r"^\d{2}:\d{2}(:\d{2})?$", checkout_time):
            flash("وقت الخروج يجب أن يكون بصيغة 24 ساعة مثل 17:30 أو 23:59.", "error")
            return redirect(url_for("attendance.pending"))
        if len(checkout_time) == 5:
            checkout_time += ":00"
        hour_s, minute_s, second_s = checkout_time.split(":")
        hour = int(hour_s)
        minute = int(minute_s)
        second = int(second_s)
        if hour > 23:
            flash("وقت الخروج يجب أن يكون بصيغة 24 ساعة صحيحة بين 00:00 و 23:59.", "error")
            return redirect(url_for("attendance.pending"))
        if minute > 59 or second > 59:
            flash("وقت الخروج يجب أن يكون بصيغة 24 ساعة مثل 17:30 أو 23:59.", "error")
            return redirect(url_for("attendance.pending"))
        try:
            datetime.time.fromisoformat(checkout_time)
        except Exception:
            flash("وقت الخروج يجب أن يكون بصيغة 24 ساعة مثل 17:30 أو 23:59.", "error")
            return redirect(url_for("attendance.pending"))

        day = str(row["ts"])[:10]
        checkout_ts = f"{checkout_date_iso}T{checkout_time}"

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


@bp.post("/long-session/<int:in_id>/<int:out_id>/approve")
@login_required
@require_perm("attendance.manage")
def long_session_approve(in_id: int, out_id: int):
    db = get_db()
    _ensure_long_session_review_table(db)
    pair = _fetch_long_session_pair(db, in_id, out_id)
    if not pair:
        flash("لم يتم العثور على جلسة العمل الطويلة.", "error")
        return redirect(url_for("attendance.pending"))

    reviewer = _current_user()
    now = _now_iso()
    note = (request.form.get("note") or "").strip()
    _insert_long_session_review(db, pair, "approved", reviewer, now, note)
    db.commit()

    try:
        audit(
            "attendance.long_session.approve",
            {"in_id": in_id, "out_id": out_id, "by": reviewer, "duration_min": pair["duration_min"], "note": note},
        )
    except Exception:
        pass

    flash("تم اعتماد جلسة العمل الطويلة.", "ok")
    return redirect(url_for("attendance.pending"))


@bp.post("/long-session/<int:in_id>/<int:out_id>/edit-out")
@login_required
@require_perm("attendance.manage")
def long_session_edit_out(in_id: int, out_id: int):
    db = get_db()
    _ensure_long_session_review_table(db)
    pair = _fetch_long_session_pair(db, in_id, out_id)
    if not pair:
        flash("لم يتم العثور على جلسة العمل الطويلة.", "error")
        return redirect(url_for("attendance.pending"))

    checkout_date = (request.form.get("checkout_date") or "").strip()
    checkout_time = (request.form.get("checkout_time") or "").strip()
    note = (request.form.get("note") or "").strip()
    checkout_date_iso = _parse_checkout_date(checkout_date)
    if not checkout_date_iso:
        flash("تاريخ الخروج يجب أن يكون بصيغة يوم/شهر/سنة مثل 16/05/2026.", "error")
        return redirect(url_for("attendance.pending"))
    checkout_time_iso = _parse_checkout_time(checkout_time)
    if not checkout_time_iso:
        flash("وقت الخروج يجب أن يكون بصيغة 24 ساعة صحيحة بين 00:00 و 23:59.", "error")
        return redirect(url_for("attendance.pending"))

    checkout_ts = f"{checkout_date_iso}T{checkout_time_iso}"
    in_dt = _parse_attendance_ts(pair["in_ts"])
    out_dt = _parse_attendance_ts(checkout_ts)
    if not in_dt or not out_dt or out_dt <= in_dt:
        flash("وقت الخروج يجب أن يكون بعد وقت الدخول.", "error")
        return redirect(url_for("attendance.pending"))

    out_row = db.execute("SELECT * FROM attendance WHERE id=?", (out_id,)).fetchone()
    if not out_row:
        flash("لم يتم العثور على ضربة الخروج.", "error")
        return redirect(url_for("attendance.pending"))

    editor = _current_user()
    now = _now_iso()
    attendance_cols = _table_cols(db, "attendance")
    set_parts = ["ts=?"]
    vals = [checkout_ts]
    if "edited_by" in attendance_cols:
        set_parts.append("edited_by=?")
        vals.append(editor)
    if "edited_ts" in attendance_cols:
        set_parts.append("edited_ts=?")
        vals.append(now)
    if "original_ts" in attendance_cols:
        original_ts = out_row["original_ts"] if "original_ts" in out_row.keys() else None
        set_parts.append("original_ts=?")
        vals.append(original_ts or out_row["ts"])
    if "edit_note" in attendance_cols:
        set_parts.append("edit_note=?")
        vals.append(note)
    vals.append(out_id)
    db.execute(f"UPDATE attendance SET {', '.join(set_parts)} WHERE id=?", vals)

    duration_min = int((out_dt - in_dt).total_seconds() // 60)
    if duration_min > LONG_SESSION_THRESHOLD_MINUTES:
        reviewed_pair = dict(pair)
        reviewed_pair["out_ts"] = checkout_ts
        reviewed_pair["out_date"] = checkout_ts[:10]
        reviewed_pair["out_date_display"] = f"{checkout_ts[8:10]}/{checkout_ts[5:7]}/{checkout_ts[:4]}"
        reviewed_pair["out_time"] = checkout_ts[11:19]
        reviewed_pair["duration_min"] = duration_min
        reviewed_pair["duration_txt"] = _fmt_duration_hhmm(duration_min)
        _insert_long_session_review(db, reviewed_pair, "corrected_reviewed", editor, now, note)

    db.commit()

    try:
        audit(
            "attendance.long_session.edit_out",
            {"in_id": in_id, "out_id": out_id, "by": editor, "from": pair["out_ts"], "to": checkout_ts, "note": note},
        )
    except Exception:
        pass

    flash("تم تعديل وقت الخروج.", "ok")
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
