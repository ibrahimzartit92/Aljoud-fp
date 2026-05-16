from __future__ import annotations

import datetime
from flask import Blueprint, render_template, request, send_file, flash, redirect, url_for

from ..security import login_required
from ..rbac import require_perm
from ..db import get_db
from ..utils.report_export import export_hours_excel, export_hours_pdf

bp = Blueprint("reports", __name__, url_prefix="/reports")


# --------- rounding helpers (your scheme) ---------
def round_hours_05(total_minutes: int) -> float:
    """
    Your rounding scheme:
      0-24  -> +0.0
      25-49 -> +0.5
      50-59 -> +1.0
    Examples:
      8h15m -> 8
      8h25m -> 8.5
      8h45m -> 8.5
      8h50m -> 9
    """
    if not total_minutes or total_minutes <= 0:
        return 0.0
    h = total_minutes // 60
    m = total_minutes % 60
    if m < 25:
        return float(h)
    if m < 50:
        return float(h) + 0.5
    return float(h + 1)


def fmt_hours(h: float) -> str:
    # show 8 or 8.5 (not 8.0)
    if abs(h - int(h)) < 1e-9:
        return str(int(h))
    return str(h).rstrip("0").rstrip(".")


# --------- shared query + session builder ---------
def _fetch_punches(
    db,
    date_from: str,
    date_to: str,
    branch_id: str | None,
    employee_id: str | None,
):
    # timestamps range for ISO strings stored in DB
    ts_from = f"{date_from}T00:00:00"
    ts_to = f"{date_to}T23:59:59"

    where = ["a.ts >= ?", "a.ts <= ?"]
    params: list[object] = [ts_from, ts_to]

    if branch_id:
        where.append("a.branch_id = ?")
        params.append(int(branch_id))

    if employee_id:
        where.append("a.employee_id = ?")
        params.append(str(employee_id).strip())

    q = f"""
        SELECT
          a.id,
          a.employee_id,
          e.name AS emp_name,
          a.ts,
          a.punch_type,
          a.branch_id,
          b.name AS branch_name,
          a.device_id,
          d.name AS device_name
        FROM attendance a
        LEFT JOIN employees e ON e.employee_id = a.employee_id
        LEFT JOIN branches  b ON b.id = a.branch_id
        LEFT JOIN devices   d ON d.id = a.device_id
        WHERE {" AND ".join(where)}
        ORDER BY a.employee_id ASC, a.ts ASC, a.id ASC
    """
    return [dict(r) for r in db.execute(q, params).fetchall()]


def _build_sessions(
    punches: list[dict],
    break_policy: str = "none",
    break_minutes_manual: int = 0,
) -> list[dict]:
    def compute_break_minutes(work_minutes: int) -> int:
        if break_policy == "manual":
            return max(0, min(600, int(break_minutes_manual or 0)))
        if break_policy == "auto":
            if work_minutes >= 9 * 60:
                return 60
            if work_minutes >= 6 * 60:
                return 30
            return 0
        return 0

    sessions: list[dict] = []
    last_by_emp: dict[str, dict] = {}

    for p in punches:
        emp = str(p.get("employee_id") or "").strip()
        pt = (p.get("punch_type") or "").strip().lower()

        # normalize variants
        if pt in ("checkin", "check-in", "in"):
            pt = "in"
        elif pt in ("checkout", "check-out", "out"):
            pt = "out"

        if pt == "in":
            if emp and emp not in last_by_emp:
                last_by_emp[emp] = p
            continue

        if pt == "out":
            if not emp or emp not in last_by_emp:
                continue

            pin = last_by_emp.pop(emp)
            try:
                din = datetime.datetime.fromisoformat(pin["ts"])
                dout = datetime.datetime.fromisoformat(p["ts"])
            except Exception:
                continue

            if dout < din:
                continue

            work_minutes = int((dout - din).total_seconds() // 60)
            br = compute_break_minutes(work_minutes)
            net_minutes = max(0, work_minutes - br)

            work_h = round_hours_05(work_minutes)
            net_h = round_hours_05(net_minutes)

            sessions.append({
                "employee_id": emp,
                "emp_name": pin.get("emp_name") or p.get("emp_name") or "",

                "in_date": din.date().isoformat(),
                "in_time": din.strftime("%H:%M"),   # no seconds
                "in_branch": pin.get("branch_name") or "",
                "in_device": pin.get("device_name") or "",

                "out_date": dout.date().isoformat(),
                "out_time": dout.strftime("%H:%M"), # no seconds
                "out_branch": p.get("branch_name") or "",
                "out_device": p.get("device_name") or "",

                "break_min": br,
                "work_min": work_minutes,
                "net_min": net_minutes,

                "work_h": work_h,
                "net_h": net_h,
                "work_h_txt": fmt_hours(work_h),
                "net_h_txt": fmt_hours(net_h),

                "status": "OK",
            })

    # open IN => incomplete (0 hours)
    for emp, pin in last_by_emp.items():
        try:
            din = datetime.datetime.fromisoformat(pin["ts"])
        except Exception:
            continue
        sessions.append({
            "employee_id": emp,
            "emp_name": pin.get("emp_name") or "",
            "in_date": din.date().isoformat(),
            "in_time": din.strftime("%H:%M"),
            "in_branch": pin.get("branch_name") or "",
            "in_device": pin.get("device_name") or "",

            "out_date": "",
            "out_time": "",
            "out_branch": "",
            "out_device": "",

            "break_min": 0,
            "work_min": 0,
            "net_min": 0,

            "work_h": 0.0,
            "net_h": 0.0,
            "work_h_txt": "0",
            "net_h_txt": "0",

            "status": "Incomplete",
        })

    # newest first
    sessions.sort(key=lambda r: (r.get("in_date", ""), r.get("in_time", "")), reverse=True)
    return sessions


def _aggregate_daily_and_totals(sessions: list[dict]) -> tuple[list[dict], list[dict], dict]:
    """
    Returns:
      daily_rows: per (employee, date)
      emp_totals: per employee total
      grand: totals
    """
    daily_map: dict[tuple[str, str], int] = {}
    emp_names: dict[str, str] = {}

    for s in sessions:
        emp = s.get("employee_id") or ""
        name = s.get("emp_name") or ""
        emp_names[emp] = name
        d = s.get("in_date") or ""
        key = (emp, d)
        daily_map[key] = daily_map.get(key, 0) + int(s.get("net_min") or 0)

    daily_rows: list[dict] = []
    for (emp, d), net_min in sorted(daily_map.items(), key=lambda x: (x[0][0], x[0][1])):
        h = round_hours_05(net_min)
        daily_rows.append({
            "EmpID": emp,
            "Name": emp_names.get(emp, ""),
            "Date": d,
            "Net Hours": fmt_hours(h),
            "_net_min": net_min,
        })

    emp_sum: dict[str, int] = {}
    for r in daily_rows:
        emp_sum[r["EmpID"]] = emp_sum.get(r["EmpID"], 0) + int(r["_net_min"])

    emp_totals: list[dict] = []
    for emp, net_min in sorted(emp_sum.items(), key=lambda x: int(x[0]) if str(x[0]).isdigit() else str(x[0])):
        h = round_hours_05(net_min)
        emp_totals.append({
            "EmpID": emp,
            "Name": emp_names.get(emp, ""),
            "Total Net Hours": fmt_hours(h),
            "_net_min": net_min,
        })

    grand_min = sum(int(r["_net_min"]) for r in emp_totals)
    grand_h = round_hours_05(grand_min)
    grand = {"net_min": grand_min, "net_h": grand_h, "net_h_txt": fmt_hours(grand_h)}

    # cleanup helper
    for r in daily_rows:
        r.pop("_net_min", None)
    for r in emp_totals:
        r.pop("_net_min", None)

    return daily_rows, emp_totals, grand


def _export_pack(sessions: list[dict]) -> dict:
    daily_rows, emp_totals, grand = _aggregate_daily_and_totals(sessions)

    sess_rows = []
    for s in sessions:
        sess_rows.append({
            "EmpID": s.get("employee_id", ""),
            "Name": s.get("emp_name", ""),
            "In Date": s.get("in_date", ""),
            "In Time": s.get("in_time", ""),
            "In Branch": s.get("in_branch", ""),
            "Out Date": s.get("out_date", ""),
            "Out Time": s.get("out_time", ""),
            "Out Branch": s.get("out_branch", ""),
            "Break (min)": s.get("break_min", 0),
            "Net Hours": s.get("net_h_txt", "0"),
            "Status": s.get("status", ""),
        })

    return {
        "sessions": sess_rows,
        "daily": daily_rows,
        "totals": emp_totals,
        "grand": grand,
    }


def _read_export_args():
    """
    Unify params between UI + export:
      use date_from/date_to (same as UI)
    keep backward compatibility with from/to
    """
    today = datetime.date.today().isoformat()

    date_from = (request.args.get("date_from") or request.args.get("from") or today).strip()
    date_to = (request.args.get("date_to") or request.args.get("to") or today).strip()

    branch_id = (request.args.get("branch_id") or "").strip() or None
    employee_id = (request.args.get("employee_id") or "").strip() or None

    break_policy = (request.args.get("break_policy") or "none").strip()
    break_minutes_manual_s = (request.args.get("break_minutes") or "").strip()
    try:
        break_minutes_manual = int(break_minutes_manual_s) if break_minutes_manual_s else 0
    except Exception:
        break_minutes_manual = 0

    return date_from, date_to, branch_id, employee_id, break_policy, break_minutes_manual


# ---------------- UI page ----------------
@bp.get("/hours")
@login_required
@require_perm("reports.view")
def hours():
    db = get_db()

    branch_id = (request.args.get("branch_id") or "").strip()
    employee_id = (request.args.get("employee_id") or "").strip()
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()

    break_policy = (request.args.get("break_policy") or "none").strip()  # none | manual | auto
    break_minutes_manual_s = (request.args.get("break_minutes") or "").strip()
    try:
        break_minutes_manual = int(break_minutes_manual_s) if break_minutes_manual_s else 0
    except Exception:
        break_minutes_manual = 0

    # defaults: last 7 days
    today = datetime.date.today()
    if not date_to:
        date_to = today.isoformat()
    if not date_from:
        date_from = (today - datetime.timedelta(days=7)).isoformat()

    # validate
    try:
        df = datetime.date.fromisoformat(date_from)
        dt = datetime.date.fromisoformat(date_to)
    except Exception:
        flash("Invalid date range", "error")
        return redirect(url_for("reports.hours"))
    if df > dt:
        flash("date_from must be <= date_to", "error")
        return redirect(url_for("reports.hours"))

    branches = db.execute("SELECT id,name FROM branches ORDER BY name").fetchall()
    employees = db.execute(
        "SELECT employee_id, name FROM employees WHERE is_active=1 ORDER BY CAST(employee_id AS INTEGER) ASC"
    ).fetchall()

    punches = _fetch_punches(db, date_from, date_to, branch_id or None, employee_id or None)
    sessions = _build_sessions(punches, break_policy=break_policy, break_minutes_manual=break_minutes_manual)

    daily_rows, total_rows, grand = _aggregate_daily_and_totals(sessions)

    return render_template(
        "reports/hours.html",
        rows=sessions,
        daily_rows=daily_rows,
        total_rows=total_rows,
        grand=grand,
        branches=branches,
        employees=employees,
        date_from=date_from,
        date_to=date_to,
        branch_id=branch_id,
        employee_id=employee_id,
        break_policy=break_policy,
        break_minutes=break_minutes_manual,
    )


# ---------------- Exports ----------------
@bp.get("/hours.xlsx")
@login_required
@require_perm("reports.export")
def hours_xlsx():
    db = get_db()
    date_from, date_to, branch_id, employee_id, break_policy, break_minutes_manual = _read_export_args()

    punches = _fetch_punches(db, date_from, date_to, branch_id, employee_id)
    sessions = _build_sessions(punches, break_policy=break_policy, break_minutes_manual=break_minutes_manual)
    pack = _export_pack(sessions)

    out = export_hours_excel(pack, "ALJOUD Hours Report")
    return send_file(
        out,
        as_attachment=True,
        download_name="hours.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@bp.get("/hours.pdf")
@login_required
@require_perm("reports.export")
def hours_pdf():
    db = get_db()
    date_from, date_to, branch_id, employee_id, break_policy, break_minutes_manual = _read_export_args()

    punches = _fetch_punches(db, date_from, date_to, branch_id, employee_id)
    sessions = _build_sessions(punches, break_policy=break_policy, break_minutes_manual=break_minutes_manual)
    pack = _export_pack(sessions)

    out = export_hours_pdf(pack, "ALJOUD Hours Report")
    return send_file(out, as_attachment=True, download_name="hours.pdf", mimetype="application/pdf")
