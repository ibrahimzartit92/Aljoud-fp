#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ALJOUD Auto Export Hours

Modes:
- daily:   per branch -> per day folder -> Excel + PDF
- weekly:  per ISO week folder -> per branch + per employee (Daily + Totals + Sessions)
- monthly: per month folder    -> per branch + per employee (Daily + Totals + Sessions)

Language: German (filenames + column headers are German via rows_pack)

Env:
  ALJOUD_DB=/mnt/nvme/data/aljoud.db
  ALJOUD_EXPORT_DIR=/mnt/nvme/exports
  ALJOUD_BREAK_POLICY=manual|none|auto
  ALJOUD_BREAK_MINUTES=30
  ALJOUD_REPORT_TITLE="ALJOUD – Stundenbericht (Arbeitszeiten)"
  ALJOUD_EXPORT_PDF=1|0

Notes:
- Daily output structure:
    daily/<Filiale>/<YYYY-MM-DD>/Stundenbericht_Filiale_<Filiale>_Tag_<YYYY-MM-DD>.xlsx|pdf
    daily/<Filiale>/Stundenbericht_latest.xlsx|pdf  (optional convenience, enabled by default)
- Weekly/Monthly output structure:
    weekly/<KW_YYYY-WW>/Filialen/*.xlsx|pdf
    weekly/<KW_YYYY-WW>/Mitarbeiter/*.xlsx|pdf
    monthly/<YYYY-MM>/Filialen/*.xlsx|pdf
    monthly/<YYYY-MM>/Mitarbeiter/*.xlsx|pdf

Examples:
  ./auto_export_hours.py --mode daily
  ./auto_export_hours.py --mode daily --date-from 2025-12-26 --date-to 2025-12-26
  ./auto_export_hours.py --mode weekly
  ./auto_export_hours.py --mode monthly
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Tuple

# Ensure "app" package import works when executed by systemd (WorkingDirectory=/mnt/nvme/server)
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

try:
    from app.utils.report_export import export_hours_excel, export_hours_pdf
except Exception:
    print("FEHLER: Konnte app.utils.report_export nicht importieren.")
    print("Tipp: WorkingDirectory muss /mnt/nvme/server sein und venv-python verwenden.")
    raise


# -------------------------
# small helpers
# -------------------------
def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def as_int(x: Any, default: int = 0) -> int:
    try:
        return int(str(x).strip())
    except Exception:
        return default


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def slugify(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return "unbekannt"
    s = s.replace("–", "-").replace("—", "-")
    s = re.sub(r"[^\w\s\-\.\(\)]", "", s, flags=re.UNICODE)
    s = re.sub(r"\s+", "_", s, flags=re.UNICODE)
    return s[:120] if len(s) > 120 else s


def parse_date_iso(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def prev_day_range(today: dt.date) -> Tuple[dt.date, dt.date, str]:
    d = today - dt.timedelta(days=1)
    return d, d, d.isoformat()


def iso_week_range(day: dt.date) -> Tuple[dt.date, dt.date, str]:
    # Monday..Sunday
    iso_year, iso_week, iso_wday = day.isocalendar()
    monday = day - dt.timedelta(days=iso_wday - 1)
    sunday = monday + dt.timedelta(days=6)
    label = f"KW_{iso_year}-{iso_week:02d}"
    return monday, sunday, label


def prev_iso_week_range(today: dt.date) -> Tuple[dt.date, dt.date, str]:
    return iso_week_range(today - dt.timedelta(days=7))


def prev_month_range(today: dt.date) -> Tuple[dt.date, dt.date, str]:
    first_this = today.replace(day=1)
    last_prev = first_this - dt.timedelta(days=1)
    first_prev = last_prev.replace(day=1)
    label = f"{first_prev.year:04d}-{first_prev.month:02d}"
    return first_prev, last_prev, label


# -------------------------
# rounding + break policy
# -------------------------
def round_hours_05(total_minutes: int) -> float:
    """
    User scheme:
      0-24  -> +0.0
      25-49 -> +0.5
      50-59 -> +1.0
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
    # show "8" or "8.5" (not 8.0)
    if abs(h - int(h)) < 1e-9:
        return str(int(h))
    s = str(h)
    return s.rstrip("0").rstrip(".")


def compute_break_minutes(work_minutes: int, policy: str, manual_min: int) -> int:
    """
    policy:
      - none
      - manual (manual_min)
      - auto: <6h=0, 6-9h=30, >=9h=60
    """
    p = (policy or "none").strip().lower()
    if p == "manual":
        return max(0, min(600, int(manual_min or 0)))
    if p == "auto":
        if work_minutes >= 9 * 60:
            return 60
        if work_minutes >= 6 * 60:
            return 30
        return 0
    return 0


# -------------------------
# DB access
# -------------------------
def db_connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def fetch_branches(con: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = con.execute("SELECT id, name FROM branches ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def fetch_employees(con: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = con.execute(
        "SELECT employee_id, name FROM employees WHERE is_active=1 ORDER BY CAST(employee_id AS INTEGER) ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_punches(
    con: sqlite3.Connection,
    date_from: dt.date,
    date_to: dt.date,
    branch_id: Optional[int] = None,
    employee_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    ts_from = f"{date_from.isoformat()}T00:00:00"
    ts_to = f"{date_to.isoformat()}T23:59:59"

    where = ["a.ts >= ?", "a.ts <= ?"]
    params: List[Any] = [ts_from, ts_to]

    if branch_id is not None:
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
    rows = con.execute(q, params).fetchall()
    return [dict(r) for r in rows]


# -------------------------
# sessions + aggregates
# -------------------------
def build_sessions(
    punches: List[Dict[str, Any]],
    break_policy: str,
    break_minutes_manual: int,
) -> List[Dict[str, Any]]:
    """
    Convert punch stream into sessions (IN -> next OUT), per employee.
    IN without OUT -> Unvollständig (0h).
    """
    def norm_pt(pt: str) -> str:
        pt = (pt or "").strip().lower()
        if pt in ("checkin", "check-in", "in", "in "):
            return "in"
        if pt in ("checkout", "check-out", "out", "out "):
            return "out"
        return pt

    sessions: List[Dict[str, Any]] = []
    last_by_emp: Dict[str, Dict[str, Any]] = {}

    for p in punches:
        emp = str(p.get("employee_id") or "").strip()
        if not emp:
            continue

        pt = norm_pt(p.get("punch_type") or "")
        if pt == "in":
            if emp not in last_by_emp:
                last_by_emp[emp] = p
            continue

        if pt == "out":
            if emp not in last_by_emp:
                continue

            pin = last_by_emp.pop(emp)
            try:
                din = dt.datetime.fromisoformat(str(pin["ts"]))
                dout = dt.datetime.fromisoformat(str(p["ts"]))
            except Exception:
                continue

            if dout < din:
                continue

            work_min = int((dout - din).total_seconds() // 60)
            br = compute_break_minutes(work_min, break_policy, break_minutes_manual)
            net_min = max(0, work_min - br)

            work_h = round_hours_05(work_min)
            net_h = round_hours_05(net_min)

            sessions.append({
                "employee_id": emp,
                "emp_name": pin.get("emp_name") or p.get("emp_name") or "",

                "in_date": din.date().isoformat(),
                "in_time": din.strftime("%H:%M"),
                "in_branch": pin.get("branch_name") or "",

                "out_date": dout.date().isoformat(),
                "out_time": dout.strftime("%H:%M"),
                "out_branch": p.get("branch_name") or "",

                "pause_min": br,
                "arbeitszeit_min": work_min,
                "netto_min": net_min,

                "arbeitszeit_h": fmt_hours(work_h),
                "netto_h": fmt_hours(net_h),

                "status": "OK",
            })

    # open IN => incomplete
    for emp, pin in last_by_emp.items():
        try:
            din = dt.datetime.fromisoformat(str(pin["ts"]))
        except Exception:
            continue
        sessions.append({
            "employee_id": emp,
            "emp_name": pin.get("emp_name") or "",
            "in_date": din.date().isoformat(),
            "in_time": din.strftime("%H:%M"),
            "in_branch": pin.get("branch_name") or "",
            "out_date": "",
            "out_time": "",
            "out_branch": "",
            "pause_min": 0,
            "arbeitszeit_min": 0,
            "netto_min": 0,
            "arbeitszeit_h": "0",
            "netto_h": "0",
            "status": "Unvollständig",
        })

    sessions.sort(key=lambda r: (r.get("in_date", ""), r.get("in_time", "")), reverse=True)
    return sessions


def aggregate_daily_and_totals(
    sessions: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """
    - Daily totals: per (employee, date) sum of netto_min
    - Totals: per employee sum of netto_min
    - Grand total: sum of all employees
    """
    daily_map: Dict[Tuple[str, str], int] = {}
    emp_names: Dict[str, str] = {}

    for s in sessions:
        emp = s.get("employee_id") or ""
        if not emp:
            continue
        emp_names[emp] = s.get("emp_name") or emp_names.get(emp, "")
        d = s.get("in_date") or ""
        daily_map[(emp, d)] = daily_map.get((emp, d), 0) + int(s.get("netto_min") or 0)

    daily_rows: List[Dict[str, Any]] = []
    for (emp, d), net_min in sorted(daily_map.items(), key=lambda x: (x[0][0], x[0][1])):
        h = round_hours_05(net_min)
        daily_rows.append({
            "Mitarbeiter-ID": emp,
            "Name": emp_names.get(emp, ""),
            "Datum": d,
            "Netto-Stunden": fmt_hours(h),
        })

    emp_sum: Dict[str, int] = {}
    for (emp, _d), net_min in daily_map.items():
        emp_sum[emp] = emp_sum.get(emp, 0) + int(net_min or 0)

    totals_rows: List[Dict[str, Any]] = []
    for emp, net_min in sorted(emp_sum.items(), key=lambda x: int(x[0]) if str(x[0]).isdigit() else str(x[0])):
        h = round_hours_05(net_min)
        totals_rows.append({
            "Mitarbeiter-ID": emp,
            "Name": emp_names.get(emp, ""),
            "Gesamt Netto-Stunden": fmt_hours(h),
        })

    grand_min = sum(emp_sum.values())
    grand_h = round_hours_05(grand_min)
    grand = {"net_min": grand_min, "net_h": grand_h, "net_h_txt": fmt_hours(grand_h)}
    return daily_rows, totals_rows, grand


def export_pack_from_sessions(sessions: List[Dict[str, Any]]) -> Dict[str, Any]:
    daily_rows, totals_rows, grand = aggregate_daily_and_totals(sessions)

    sess_rows: List[Dict[str, Any]] = []
    for s in sessions:
        sess_rows.append({
            "Mitarbeiter-ID": s.get("employee_id", ""),
            "Name": s.get("emp_name", ""),
            "Datum (Eingang)": s.get("in_date", ""),
            "Zeit (Eingang)": s.get("in_time", ""),
            "Filiale (Eingang)": s.get("in_branch", ""),
            "Datum (Ausgang)": s.get("out_date", ""),
            "Zeit (Ausgang)": s.get("out_time", ""),
            "Filiale (Ausgang)": s.get("out_branch", ""),
            "Pause (Min.)": s.get("pause_min", 0),
            "Arbeitszeit (h)": s.get("arbeitszeit_h", "0"),
            "Netto (h)": s.get("netto_h", "0"),
            "Status": s.get("status", ""),
        })

    return {
        "daily": daily_rows,
        "totals": totals_rows,
        "sessions": sess_rows,
        "grand": grand,
    }


# -------------------------
# writing exports
# -------------------------
def write_exports(
    rows_pack: Dict[str, Any],
    title: str,
    out_xlsx: str,
    out_pdf: Optional[str],
    do_xlsx: bool,
    do_pdf: bool,
) -> None:
    ensure_dir(os.path.dirname(out_xlsx))

    if do_xlsx:
        x = export_hours_excel(rows_pack, title)
        with open(out_xlsx, "wb") as f:
            f.write(x.read())

    if do_pdf and out_pdf:
        ensure_dir(os.path.dirname(out_pdf))
        p = export_hours_pdf(rows_pack, title)
        with open(out_pdf, "wb") as f:
            f.write(p.read())


def copy_latest(src: str, dst: str) -> None:
    try:
        import shutil
        shutil.copyfile(src, dst)
    except Exception:
        pass


# -------------------------
# export modes
# -------------------------
def export_daily_per_branch(
    con: sqlite3.Connection,
    export_root: str,
    date_from: dt.date,
    date_to: dt.date,
    break_policy: str,
    break_minutes_manual: int,
    title: str,
    do_xlsx: bool,
    do_pdf: bool,
) -> None:
    """
    daily/<Filiale>/<YYYY-MM-DD>/Stundenbericht_Filiale_<Filiale>_Tag_<date>.xlsx|pdf
    + daily/<Filiale>/Stundenbericht_latest.* (optional convenience)
    """
    branches = fetch_branches(con)
    base_dir = os.path.join(export_root, "daily")
    ensure_dir(base_dir)

    ds = date_from.isoformat()
    de = date_to.isoformat()
    stamp = ds if ds == de else f"{ds}_bis_{de}"

    for b in branches:
        bid = int(b["id"])
        bname = b.get("name") or f"Filiale_{bid}"
        bslug = slugify(bname)

        punches = fetch_punches(con, date_from, date_to, branch_id=bid, employee_id=None)
        sessions = build_sessions(punches, break_policy, break_minutes_manual)
        rows_pack = export_pack_from_sessions(sessions)

        branch_dir = os.path.join(base_dir, bslug)
        day_dir = os.path.join(branch_dir, stamp)  # always under date/range folder
        ensure_dir(day_dir)

        base = f"Stundenbericht_Filiale_{bslug}_Tag_{stamp}"
        out_xlsx = os.path.join(day_dir, base + ".xlsx")
        out_pdf = os.path.join(day_dir, base + ".pdf")

        write_exports(rows_pack, title, out_xlsx, out_pdf, do_xlsx=do_xlsx, do_pdf=do_pdf)

        # latest inside branch folder
        if do_xlsx:
            copy_latest(out_xlsx, os.path.join(branch_dir, "Stundenbericht_latest.xlsx"))
        if do_pdf:
            copy_latest(out_pdf, os.path.join(branch_dir, "Stundenbericht_latest.pdf"))


def export_weekly_or_monthly(
    con: sqlite3.Connection,
    export_root: str,
    mode: str,  # weekly|monthly
    date_from: dt.date,
    date_to: dt.date,
    label: str,  # KW_... or YYYY-MM
    break_policy: str,
    break_minutes_manual: int,
    title: str,
    do_xlsx: bool,
    do_pdf: bool,
) -> None:
    """
    weekly/<KW>/Filialen/*.xlsx|pdf + Mitarbeiter/*.xlsx|pdf
    monthly/<YYYY-MM>/Filialen/*.xlsx|pdf + Mitarbeiter/*.xlsx|pdf
    """
    assert mode in ("weekly", "monthly")

    folder = os.path.join(export_root, mode, label)
    dir_branches = os.path.join(folder, "Filialen")
    dir_emps = os.path.join(folder, "Mitarbeiter")
    ensure_dir(dir_branches)
    ensure_dir(dir_emps)

    branches = fetch_branches(con)
    emps = fetch_employees(con)

    ds = date_from.isoformat()
    de = date_to.isoformat()
    range_tag = f"{ds}_bis_{de}"

    # per branch
    for b in branches:
        bid = int(b["id"])
        bname = b.get("name") or f"Filiale_{bid}"
        bslug = slugify(bname)

        punches = fetch_punches(con, date_from, date_to, branch_id=bid, employee_id=None)
        sessions = build_sessions(punches, break_policy, break_minutes_manual)
        rows_pack = export_pack_from_sessions(sessions)

        if mode == "weekly":
            base = f"Stundenbericht_Filiale_{bslug}_{label}_{range_tag}"
        else:
            base = f"Stundenbericht_Filiale_{bslug}_Monat_{label}_{range_tag}"

        out_xlsx = os.path.join(dir_branches, base + ".xlsx")
        out_pdf = os.path.join(dir_branches, base + ".pdf")
        write_exports(rows_pack, title, out_xlsx, out_pdf, do_xlsx=do_xlsx, do_pdf=do_pdf)

    # per employee
    for e in emps:
        eid = str(e["employee_id"])
        ename = e.get("name") or ""
        eslug = slugify(f"{eid}_{ename}" if ename else eid)

        punches = fetch_punches(con, date_from, date_to, branch_id=None, employee_id=eid)
        sessions = build_sessions(punches, break_policy, break_minutes_manual)
        rows_pack = export_pack_from_sessions(sessions)

        if mode == "weekly":
            base = f"Stundenbericht_Mitarbeiter_{eslug}_{label}_{range_tag}"
        else:
            base = f"Stundenbericht_Mitarbeiter_{eslug}_Monat_{label}_{range_tag}"

        out_xlsx = os.path.join(dir_emps, base + ".xlsx")
        out_pdf = os.path.join(dir_emps, base + ".pdf")
        write_exports(rows_pack, title, out_xlsx, out_pdf, do_xlsx=do_xlsx, do_pdf=do_pdf)


# -------------------------
# args + main
# -------------------------
def parse_args(argv: List[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="ALJOUD Auto Export Hours (German)")
    ap.add_argument("--mode", required=True, choices=["daily", "weekly", "monthly"], help="Exportmodus")
    ap.add_argument("--date-from", default="", help="YYYY-MM-DD (optional)")
    ap.add_argument("--date-to", default="", help="YYYY-MM-DD (optional)")
    ap.add_argument("--no-pdf", action="store_true", help="PDF Export deaktivieren")
    ap.add_argument("--no-xlsx", action="store_true", help="Excel Export deaktivieren")
    return ap.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)

    db_path = env("ALJOUD_DB", "/mnt/nvme/data/aljoud.db")
    export_root = env("ALJOUD_EXPORT_DIR", "/mnt/nvme/exports")
    break_policy = env("ALJOUD_BREAK_POLICY", "manual") or "manual"
    break_minutes_manual = as_int(env("ALJOUD_BREAK_MINUTES", "30"), 30)
    title = env("ALJOUD_REPORT_TITLE", "ALJOUD – Stundenbericht (Arbeitszeiten)") or "ALJOUD – Stundenbericht (Arbeitszeiten)"

    # toggles
    do_xlsx = not args.no_xlsx
    pdf_env = env("ALJOUD_EXPORT_PDF", "1")
    do_pdf = (not args.no_pdf) and (pdf_env not in ("0", "false", "False", "no", "NO"))

    today = dt.date.today()

    # Determine date range by mode unless overridden
    if args.date_from and args.date_to:
        date_from = parse_date_iso(args.date_from)
        date_to = parse_date_iso(args.date_to)
        if date_from > date_to:
            print("FEHLER: date-from muss <= date-to sein.")
            return 2

        # label: keep ISO folder naming for weekly/monthly even if override is used
        if args.mode == "weekly":
            _m, _s, label = iso_week_range(date_from)
        elif args.mode == "monthly":
            label = f"{date_from.year:04d}-{date_from.month:02d}"
        else:
            label = date_from.isoformat() if date_from == date_to else f"{date_from.isoformat()}_bis_{date_to.isoformat()}"
    else:
        if args.mode == "daily":
            date_from, date_to, label = prev_day_range(today)
        elif args.mode == "weekly":
            date_from, date_to, label = prev_iso_week_range(today)
        else:
            date_from, date_to, label = prev_month_range(today)

    ensure_dir(export_root)

    con = db_connect(db_path)
    try:
        if args.mode == "daily":
            export_daily_per_branch(
                con=con,
                export_root=export_root,
                date_from=date_from,
                date_to=date_to,
                break_policy=break_policy,
                break_minutes_manual=break_minutes_manual,
                title=title,
                do_xlsx=do_xlsx,
                do_pdf=do_pdf,
            )
        elif args.mode == "weekly":
            # label already KW_...
            monday, sunday, kw_label = iso_week_range(date_from)
            export_weekly_or_monthly(
                con=con,
                export_root=export_root,
                mode="weekly",
                date_from=monday,
                date_to=sunday,
                label=kw_label,
                break_policy=break_policy,
                break_minutes_manual=break_minutes_manual,
                title=title,
                do_xlsx=do_xlsx,
                do_pdf=do_pdf,
            )
        else:
            # monthly folder name YYYY-MM
            month_label = f"{date_from.year:04d}-{date_from.month:02d}"
            export_weekly_or_monthly(
                con=con,
                export_root=export_root,
                mode="monthly",
                date_from=date_from,
                date_to=date_to,
                label=month_label,
                break_policy=break_policy,
                break_minutes_manual=break_minutes_manual,
                title=title,
                do_xlsx=do_xlsx,
                do_pdf=do_pdf,
            )
    finally:
        try:
            con.close()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
