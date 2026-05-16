from __future__ import annotations

import os
import json
import datetime
import subprocess
from typing import Dict, Set, List, Tuple

from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file, current_app

from ..security import login_required, hash_password
from ..rbac import require_perm
from ..db import get_db
from ..audit import audit
from ..utils.net import tcp_ping, grade_latency
from ..utils.backup import create_backup, rotate_backups

bp = Blueprint("admin", __name__, url_prefix="/admin")


# ---------------- RBAC catalog (for UI + seeding) ----------------
# NOTE: Permission *codes* are what matter; labels are only for UI.
PERM_GROUPS = [
    {
        "title": "Attendance",
        "perms": [
            {"code": "attendance.manage", "label": "Attendance: Pending + List (approve/edit)"},
        ],
    },
    {
        "title": "Reports",
        "perms": [
            {"code": "reports.view", "label": "Reports: Hours (view)"},
            {"code": "reports.export", "label": "Reports: Export (Excel/PDF)"},
        ],
    },
    {
        "title": "Admin",
        "perms": [
            {"code": "employees.manage", "label": "Admin: Employees"},
            {"code": "exports.manage", "label": "Admin: Exports"},
            {"code": "devices.manage", "label": "Admin: Devices"},
            {"code": "roles.manage", "label": "Admin: Roles & Permissions"},
            {"code": "branches.manage", "label": "Admin: Branches"},
            {"code": "backups.manage", "label": "Admin: Backups"},
            {"code": "branding.manage", "label": "Admin: Branding"},
        ],
    },
]

# Quick lookup for template
PERM_TITLES = {p["code"]: p["label"] for g in PERM_GROUPS for p in g["perms"]}

# ---------- helpers ----------
def _table_cols(db, table: str) -> Set[str]:
    try:
        rows = db.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(r[1]) for r in rows}  # (cid, name, type, notnull, dflt, pk)
    except Exception:
        return set()


def _ensure_default_rbac(db, *, force: bool = False) -> None:
    """Seed the RBAC catalog in a safe, idempotent way.

    - Ensures required permission codes exist (and updates titles if a title-like column exists).
    - Ensures standard roles exist: cashier, manager, superadmin.
    - Optionally seeds default role -> permission mappings.
      By default (force=False) it will only seed a role if it currently has NO permissions.
      This prevents accidentally overwriting custom RBAC.

    Target access policy:
      - cashier:   /attendance/pending + /attendance/list
      - manager:   cashier pages + /reports/hours + /admin/employees + /admin/exports
      - superadmin: all pages (bypass in RBAC via is_superadmin; and/or all perms)
    """

    # --- Permission catalog (must match require_perm() usage across the app) ---
    perms = [
        ("attendance.manage", "Attendance: Pending + List (approve/edit)")
        ,("reports.view", "Reports: Hours (view)")
        ,("reports.export", "Reports: Export (Excel/PDF)")
        ,("employees.manage", "Admin: Employees")
        ,("exports.manage", "Admin: Exports")
        ,("roles.manage", "Admin: Roles & Permissions")
        ,("devices.manage", "Admin: Devices")
        ,("branches.manage", "Admin: Branches")
        ,("branding.manage", "Admin: Branding")
        ,("backups.manage", "Admin: Backups")
    ]

    perm_cols = _table_cols(db, "permissions")
    # Identify a title column if present (support a few common names)
    title_col = None
    for c in ("title", "display_title", "name"):
        if c in perm_cols:
            title_col = c
            break

    for code, title in perms:
        if title_col:
            db.execute(
                f"INSERT INTO permissions(code,{title_col}) VALUES(?,?) "
                f"ON CONFLICT(code) DO UPDATE SET {title_col}=excluded.{title_col}",
                (code, title),
            )
        else:
            db.execute("INSERT OR IGNORE INTO permissions(code) VALUES(?)", (code,))

    # --- Roles ---
    for rname in ("cashier", "manager", "superadmin"):
        db.execute("INSERT OR IGNORE INTO roles(name) VALUES(?)", (rname,))

    role_ids = {r["name"]: int(r["id"]) for r in db.execute("SELECT id,name FROM roles").fetchall()}

    # --- Defaults (only if empty unless forced) ---
    cashier_perms = {"attendance.manage"}
    manager_perms = {"attendance.manage", "reports.view", "employees.manage", "exports.manage"}

    # superadmin: keep complete set (safe even if bypass exists)
    super_perms = {str(r["code"]) for r in db.execute("SELECT code FROM permissions").fetchall()}

    desired = {
        "cashier": cashier_perms,
        "manager": manager_perms,
        "superadmin": super_perms,
    }

    for rname, perm_set in desired.items():
        rid = role_ids.get(rname)
        if not rid:
            continue

        # Never let superadmin drift: keep it equal to "all permissions".
        # For cashier/manager, only seed if empty unless force=True.
        if rname != "superadmin" and not force:
            existing = db.execute("SELECT 1 FROM role_permissions WHERE role_id=? LIMIT 1", (rid,)).fetchone()
            if existing:
                continue

        db.execute("DELETE FROM role_permissions WHERE role_id=?", (rid,))
        for code in sorted(perm_set):
            db.execute(
                "INSERT OR IGNORE INTO role_permissions(role_id,perm_code) VALUES(?,?)",
                (rid, code),
            )



def _ensure_employee_permissions(db) -> None:
    """
    Optional: per-employee overrides.
    If table doesn't exist, create it.
    """
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS employee_permissions (
          employee_id INTEGER NOT NULL,
          perm_code   TEXT NOT NULL,
          PRIMARY KEY (employee_id, perm_code)
        )
        """
    )


def _resolve_employee_for_purge(db, token: str):
    """
    Resolve employee row by:
      - exact employee_id (string field)
      - exact username
      - exact row id (integer)
    Returns sqlite row or None.
    """
    t = (token or "").strip()
    if not t:
        return None

    # Try row id
    if t.isdigit():
        row = db.execute("SELECT * FROM employees WHERE id=?", (int(t),)).fetchone()
        if row:
            return row

    # Try employee_id
    row = db.execute("SELECT * FROM employees WHERE employee_id=?", (t,)).fetchone()
    if row:
        return row

    # Try username
    row = db.execute("SELECT * FROM employees WHERE lower(username)=lower(?)", (t,)).fetchone()
    if row:
        return row

    return None


# ---------- Admin root ----------
@bp.get("/")
@login_required
def admin_root():
    return redirect(url_for("admin.employees"))


# ---------------- Branches ----------------
@bp.get("/branches")
@login_required
@require_perm("branches.manage")
def branches():
    db = get_db()
    rows = db.execute("SELECT * FROM branches ORDER BY name").fetchall()
    return render_template("admin/branches.html", branches=rows)


@bp.post("/branches/create")
@login_required
@require_perm("branches.manage")
def branches_create():
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Name required", "error")
        return redirect(url_for("admin.branches"))
    db = get_db()
    db.execute("INSERT INTO branches(name) VALUES(?)", (name,))
    db.commit()
    audit("branches.create", {"name": name})
    flash("Saved", "ok")
    return redirect(url_for("admin.branches"))


@bp.post("/branches/<int:branch_id>/update")
@login_required
@require_perm("branches.manage")
def branches_update(branch_id: int):
    name = (request.form.get("name") or "").strip()
    db = get_db()
    old = db.execute("SELECT * FROM branches WHERE id=?", (branch_id,)).fetchone()
    db.execute("UPDATE branches SET name=? WHERE id=?", (name, branch_id))
    db.commit()
    audit("branches.update", {"branch_id": branch_id, "old": dict(old) if old else None, "new": {"name": name}})
    flash("Saved", "ok")
    return redirect(url_for("admin.branches"))


@bp.post("/branches/<int:branch_id>/delete")
@login_required
@require_perm("branches.manage")
def branches_delete(branch_id: int):
    db = get_db()
    old = db.execute("SELECT * FROM branches WHERE id=?", (branch_id,)).fetchone()
    db.execute("DELETE FROM branches WHERE id=?", (branch_id,))
    db.commit()
    audit("branches.delete", {"branch_id": branch_id, "old": dict(old) if old else None})
    flash("Deleted", "ok")
    return redirect(url_for("admin.branches"))


# ---------------- Employees ----------------
@bp.get("/employees")
@login_required
@require_perm("employees.manage")
def employees():
    db = get_db()
    rows = db.execute(
        "SELECT e.*, group_concat(r.name, ', ') AS roles "
        "FROM employees e "
        "LEFT JOIN employee_roles er ON er.employee_id=e.id "
        "LEFT JOIN roles r ON r.id=er.role_id "
        "GROUP BY e.id "
        "ORDER BY CAST(e.employee_id AS INTEGER) ASC"
    ).fetchall()
    roles = db.execute("SELECT * FROM roles ORDER BY name").fetchall()
    s30_devices = db.execute("SELECT * FROM devices WHERE device_type='s30' AND is_active=1 ORDER BY name").fetchall()
    cashiers = db.execute(
        "SELECT * FROM devices WHERE device_type IN ('cashier','cashier_pc') AND is_active=1 ORDER BY name"
    ).fetchall()
    return render_template("admin/employees.html", employees=rows, roles=roles, s30_devices=s30_devices, cashiers=cashiers)


@bp.post("/employees/create")
@login_required
@require_perm("employees.manage")
def employees_create():
    import sqlite3

    employee_id = (request.form.get("employee_id") or "").strip()
    name = (request.form.get("name") or "").strip()
    username = (request.form.get("username") or "").strip()
    card_rfid = (request.form.get("card_rfid") or "").strip()
    password = (request.form.get("password") or "").strip()
    s30_password = (request.form.get("s30_password") or "").strip()
    role_ids = request.form.getlist("role_ids")

    if not employee_id or not name or not username:
        flash("Employee ID, name, username required", "error")
        return redirect(url_for("admin.employees"))

    if password and (not password.isdigit() or len(password) != 6):
        flash("Password must be 6 digits", "error")
        return redirect(url_for("admin.employees"))

    if s30_password and (not s30_password.isdigit() or len(s30_password) != 6):
        flash("S30 password must be 6 digits", "error")
        return redirect(url_for("admin.employees"))

    role_ints: List[int] = []
    for rid in role_ids:
        rid = (rid or "").strip()
        if rid.isdigit():
            role_ints.append(int(rid))

    db = get_db()

    try:
        if db.execute("SELECT 1 FROM employees WHERE username=?", (username,)).fetchone():
            flash("Username already exists.", "error")
            return redirect(url_for("admin.employees"))

        if db.execute("SELECT 1 FROM employees WHERE employee_id=?", (employee_id,)).fetchone():
            flash("Employee ID already exists.", "error")
            return redirect(url_for("admin.employees"))

        cur = db.execute(
            "INSERT INTO employees(employee_id,name,username,password_hash,s30_password,card_rfid,is_active,is_superadmin) "
            "VALUES(?,?,?,?,?,?,1,0)",
            (employee_id, name, username, hash_password(password or "000000"), s30_password, card_rfid),
        )
        emp_row_id = cur.lastrowid

        for rid in role_ints:
            db.execute("INSERT OR IGNORE INTO employee_roles(employee_id,role_id) VALUES(?,?)", (emp_row_id, rid))

        db.commit()
        audit("employees.create", {"employee_id": employee_id, "username": username})
        flash("Saved", "ok")
        return redirect(url_for("admin.employees"))

    except sqlite3.IntegrityError as e:
        db.rollback()
        flash(f"Save failed: {str(e)[:200]}", "error")
        audit("employees.create.fail", {"employee_id": employee_id, "username": username, "error": str(e)})
        return redirect(url_for("admin.employees"))

    except Exception as e:
        db.rollback()
        flash(f"Internal error: {str(e)[:200]}", "error")
        audit("employees.create.error", {"employee_id": employee_id, "username": username, "error": str(e)})
        return redirect(url_for("admin.employees"))


@bp.post("/employees/<int:emp_row_id>/update")
@login_required
@require_perm("employees.manage")
def employees_update(emp_row_id: int):
    import sqlite3

    db = get_db()
    old = db.execute("SELECT * FROM employees WHERE id=?", (emp_row_id,)).fetchone()
    if not old:
        flash("Employee not found", "error")
        return redirect(url_for("admin.employees"))

    employee_id = (request.form.get("employee_id") or "").strip()
    name = (request.form.get("name") or "").strip()
    username = (request.form.get("username") or "").strip()
    card_rfid = (request.form.get("card_rfid") or "").strip()
    password = (request.form.get("password") or "").strip()
    s30_password = (request.form.get("s30_password") or "").strip()
    is_active = 1 if request.form.get("is_active") == "1" else 0

    role_ids: List[int] = []
    for x in request.form.getlist("role_ids"):
        x = (x or "").strip()
        if x.isdigit():
            role_ids.append(int(x))

    if not employee_id or not name or not username:
        flash("Employee ID, name, username required", "error")
        return redirect(url_for("admin.employees"))

    if password and (not password.isdigit() or len(password) != 6):
        flash("Password must be 6 digits", "error")
        return redirect(url_for("admin.employees"))
    if s30_password and (not s30_password.isdigit() or len(s30_password) != 6):
        flash("S30 password must be 6 digits", "error")
        return redirect(url_for("admin.employees"))

    try:
        if int(old["is_superadmin"] or 0) == 1 and is_active == 0:
            flash("Cannot deactivate Super Admin.", "error")
            return redirect(url_for("admin.employees"))
    except Exception:
        pass

    try:
        if password:
            ph = hash_password(password)
            db.execute(
                "UPDATE employees SET employee_id=?, name=?, username=?, card_rfid=?, password_hash=?, s30_password=?, is_active=? WHERE id=?",
                (employee_id, name, username, card_rfid, ph, s30_password, is_active, emp_row_id),
            )
        else:
            db.execute(
                "UPDATE employees SET employee_id=?, name=?, username=?, card_rfid=?, s30_password=?, is_active=? WHERE id=?",
                (employee_id, name, username, card_rfid, s30_password, is_active, emp_row_id),
            )

        db.execute("DELETE FROM employee_roles WHERE employee_id=?", (emp_row_id,))
        for rid in role_ids:
            db.execute("INSERT OR IGNORE INTO employee_roles(employee_id,role_id) VALUES(?,?)", (emp_row_id, rid))

        db.commit()
        audit("employees.update", {"id": emp_row_id, "old": dict(old) if old else None})
        flash("Saved", "ok")
        return redirect(url_for("admin.employees"))

    except sqlite3.IntegrityError as e:
        db.rollback()
        flash(f"Save failed: {str(e)[:200]}", "error")
        audit("employees.update.fail", {"id": emp_row_id, "error": str(e)})
        return redirect(url_for("admin.employees"))
    except Exception as e:
        db.rollback()
        flash(f"Internal error: {str(e)[:200]}", "error")
        audit("employees.update.error", {"id": emp_row_id, "error": str(e)})
        return redirect(url_for("admin.employees"))


@bp.post("/employees/<int:emp_row_id>/delete")
@login_required
@require_perm("employees.manage")
def employees_delete(emp_row_id: int):
    """
    Soft-delete (deactivate) so sync can remove it from S30 (delete list).
    """
    db = get_db()
    old = db.execute("SELECT * FROM employees WHERE id=?", (emp_row_id,)).fetchone()
    if not old:
        flash("Not found", "error")
        return redirect(url_for("admin.employees"))

    try:
        if int(old["is_superadmin"] or 0) == 1:
            flash("Cannot delete Super Admin.", "error")
            return redirect(url_for("admin.employees"))
    except Exception:
        pass

    try:
        db.execute("UPDATE employees SET is_active=0 WHERE id=?", (emp_row_id,))
        db.execute("DELETE FROM employee_roles WHERE employee_id=?", (emp_row_id,))
        db.commit()
        audit("employees.soft_delete", {"id": emp_row_id, "old": dict(old) if old else None})
        flash("Employee deactivated. Now click 'Sync with devices' to remove from S30.", "ok")
    except Exception as e:
        db.rollback()
        audit("employees.soft_delete.error", {"id": emp_row_id, "error": str(e)})
        flash(f"Delete failed: {str(e)[:200]}", "error")

    return redirect(url_for("admin.employees"))


@bp.post("/employees/sync")
@login_required
@require_perm("devices.manage")
def employees_sync():
    import requests

    db = get_db()

    rows = db.execute(
        """
        SELECT
          e.id as row_id,
          e.employee_id,
          e.username,
          e.name,
          e.card_rfid,
          e.s30_password,
          e.is_active,
          e.is_superadmin
        FROM employees e
        ORDER BY CAST(e.employee_id AS INTEGER)
        """
    ).fetchall()

    role_rows = db.execute(
        """
        SELECT er.employee_id as emp_row_id, r.name as role_name
        FROM employee_roles er
        JOIN roles r ON r.id = er.role_id
        """
    ).fetchall()

    roles_map: Dict[int, Set[str]] = {}
    for rr in role_rows:
        roles_map.setdefault(int(rr["emp_row_id"]), set()).add((rr["role_name"] or "").strip().lower())

    def is_super_admin(emp_row) -> bool:
        try:
            if int(emp_row["is_superadmin"] or 0) == 1:
                return True
        except Exception:
            pass
        uname = (emp_row["username"] or "").strip().lower()
        rset = roles_map.get(int(emp_row["row_id"]), set())
        if uname in ("admin", "superadmin", "root"):
            return True
        if ("super_admin" in rset) or ("superadmin" in rset) or ("admin" in rset):
            return True
        return False

    def parse_card_int(card: str):
        c = (card or "").strip()
        if not c:
            return None
        if c.isdigit():
            try:
                return int(c)
            except Exception:
                return None
        try:
            return int(c, 16)
        except Exception:
            return None

    employees_payload = []
    delete_payload = []
    bad_rows = 0

    for r in rows:
        emp_id = (r["employee_id"] or "").strip()
        if not emp_id:
            bad_rows += 1
            continue

        try:
            uid = int(emp_id)
        except Exception:
            bad_rows += 1
            continue

        if int(r["is_active"] or 0) != 1:
            delete_payload.append(uid)
            continue

        priv = 14 if is_super_admin(r) else 0
        pwd = (r["s30_password"] or "").strip()
        card = (r["card_rfid"] or "").strip()
        card_int = parse_card_int(card)

        item = {
            "uid": uid,
            "user_id": emp_id,
            "name": (r["name"] or "").strip(),
            "privilege": priv,
            "password": pwd,
            # Card fields (compatibility with pyzk signature: card=int)
            "card_rfid": card,
            "card": card,
        }
        if card_int is not None:
            item["card_no"] = card_int

        employees_payload.append(item)

    agents = db.execute(
        """
        SELECT *
        FROM devices
        WHERE device_type IN ('cashier','cashier_pc')
          AND is_active=1
          AND agent_base_url IS NOT NULL
          AND agent_base_url<>'' 
        ORDER BY id
        """
    ).fetchall()

    if not agents:
        flash("No agents configured (set Agent URL in Devices page).", "error")
        return redirect(url_for("admin.employees"))

    secret = (current_app.config.get("AGENT_SHARED_SECRET", "") or "").strip()
    if not secret:
        flash("AGENT_SHARED_SECRET not set on server.", "error")
        return redirect(url_for("admin.employees"))

    results = []
    ok_count, fail_count = 0, 0

    for a in agents:
        base = (a["agent_base_url"] or "").strip().rstrip("/")
        url = base + "/sync/employees"
        dev_label = f'{a["name"]} ({base})'

        try:
            resp = requests.post(
                url,
                json={"employees": employees_payload, "delete": delete_payload},
                headers={"X-Agent-Secret": secret},
                timeout=25,
            )

            if resp.status_code == 200:
                ok_count += 1
                try:
                    j = resp.json()
                    results.append(
                        f"OK: {dev_label} -> inserted={j.get('inserted')} deleted={j.get('deleted')} errors={len(j.get('errors') or [])}"
                    )
                except Exception:
                    results.append(f"OK: {dev_label}")
            else:
                fail_count += 1
                body = (resp.text or "").strip()[:300]
                results.append(f"FAIL {resp.status_code}: {dev_label} -> {body}")

        except requests.exceptions.ConnectTimeout:
            fail_count += 1
            results.append(f"TIMEOUT: {dev_label}")
        except requests.exceptions.ConnectionError as e:
            fail_count += 1
            results.append(f"CONN ERROR: {dev_label} -> {str(e)[:200]}")
        except Exception as e:
            fail_count += 1
            results.append(f"ERROR: {dev_label} -> {str(e)[:200]}")

    audit("devices.sync_employees", {"ok": ok_count, "fail": fail_count, "bad_rows": bad_rows, "details": results[:50]})
    msg = "Sync result: ok=%d fail=%d bad_rows=%d\n%s" % (ok_count, fail_count, bad_rows, "\n".join(results))
    flash(msg, "ok" if fail_count == 0 else "error")
    return redirect(url_for("admin.employees"))


# ---------------- Devices ----------------
@bp.get("/devices")
@login_required
@require_perm("devices.manage")
def devices():
    db = get_db()
    rows = db.execute(
        "SELECT d.*, b.name AS branch_name FROM devices d LEFT JOIN branches b ON b.id=d.branch_id ORDER BY d.device_type, d.name"
    ).fetchall()
    branches = db.execute("SELECT * FROM branches ORDER BY name").fetchall()
    return render_template("admin/devices.html", devices=rows, branches=branches)


@bp.post("/devices/create")
@login_required
@require_perm("devices.manage")
def devices_create():
    name = (request.form.get("name") or "").strip()
    device_type = (request.form.get("device_type") or "").strip()
    ip = (request.form.get("ip") or "").strip()
    port = (request.form.get("port") or "").strip()
    port = int(port) if port else None
    agent_base_url = (request.form.get("agent_base_url") or "").strip()
    branch_id = request.form.get("branch_id")
    branch_id = int(branch_id) if branch_id else None
    notes = (request.form.get("notes") or "").strip()
    if not name or not device_type:
        flash("Name and type required", "error")
        return redirect(url_for("admin.devices"))
    db = get_db()
    db.execute(
        "INSERT INTO devices(name,device_type,ip,port,agent_base_url,branch_id,notes,is_active) VALUES(?,?,?,?,?,?,?,1)",
        (name, device_type, ip, port, agent_base_url, branch_id, notes),
    )
    db.commit()
    audit("devices.create", {"name": name, "type": device_type})
    flash("Saved", "ok")
    return redirect(url_for("admin.devices"))


@bp.post("/devices/<int:dev_id>/update")
@login_required
@require_perm("devices.manage")
def devices_update(dev_id: int):
    db = get_db()
    old = db.execute("SELECT * FROM devices WHERE id=?", (dev_id,)).fetchone()
    name = (request.form.get("name") or "").strip()
    ip = (request.form.get("ip") or "").strip()
    port = (request.form.get("port") or "").strip()
    port = int(port) if port else None
    agent_base_url = (request.form.get("agent_base_url") or "").strip()
    branch_id = request.form.get("branch_id")
    branch_id = int(branch_id) if branch_id else None
    is_active = 1 if request.form.get("is_active") == "1" else 0
    notes = (request.form.get("notes") or "").strip()
    db.execute(
        "UPDATE devices SET name=?, ip=?, port=?, agent_base_url=?, branch_id=?, is_active=?, notes=? WHERE id=?",
        (name, ip, port, agent_base_url, branch_id, is_active, notes, dev_id),
    )
    db.commit()
    audit("devices.update", {"id": dev_id, "old": dict(old) if old else None})
    flash("Saved", "ok")
    return redirect(url_for("admin.devices"))


@bp.post("/devices/<int:dev_id>/delete")
@login_required
@require_perm("devices.manage")
def devices_delete(dev_id: int):
    db = get_db()
    old = db.execute("SELECT * FROM devices WHERE id=?", (dev_id,)).fetchone()
    db.execute("DELETE FROM devices WHERE id=?", (dev_id,))
    db.commit()
    audit("devices.delete", {"id": dev_id, "old": dict(old) if old else None})
    flash("Deleted", "ok")
    return redirect(url_for("admin.devices"))


@bp.post("/devices/<int:dev_id>/fetch")
@login_required
@require_perm("devices.manage")
def devices_fetch_punches(dev_id: int):
    import datetime as _dt
    from zk import ZK

    db = get_db()
    d = db.execute("SELECT * FROM devices WHERE id=?", (dev_id,)).fetchone()
    if not d or d["device_type"] != "s30":
        flash("Invalid device", "error")
        return redirect(url_for("admin.devices"))

    since_min = int(request.form.get("since_min") or 60)
    limit = int(request.form.get("limit") or 200)
    since_min = max(1, min(since_min, 43200))
    limit = max(1, min(limit, 2000))

    now = _dt.datetime.now()
    since_ts = now - _dt.timedelta(minutes=since_min)

    ip = d["ip"]
    port = d["port"] or 4370
    device_id = d["id"]
    branch_id = d["branch_id"]

    zk = ZK(ip, port=port, timeout=5)
    inserted = 0
    skipped = 0

    try:
        conn = zk.connect()
        punches = conn.get_attendance() or []
        conn.disconnect()
    except Exception as e:
        flash(f"S30 error: {e}", "error")
        return redirect(url_for("admin.devices"))

    punches = sorted(punches, key=lambda p: p.timestamp, reverse=True)[:limit]

    for p in punches:
        ts = p.timestamp
        if ts < since_ts:
            continue

        ts_iso = ts.isoformat(timespec="seconds")
        emp_id = str(p.user_id)

        exists = db.execute(
            "SELECT 1 FROM attendance WHERE employee_id=? AND device_id=? AND ts=?",
            (emp_id, device_id, ts_iso),
        ).fetchone()
        if exists:
            skipped += 1
            continue

        punch_type = "in" if p.punch == 0 else "out"

        db.execute(
            """INSERT INTO attendance
               (employee_id, branch_id, device_id, punch_type, ts, status)
               VALUES (?, ?, ?, ?, ?, 'approved')""",
            (emp_id, branch_id, device_id, punch_type, ts_iso),
        )
        inserted += 1

    db.commit()
    flash(f"Fetched: inserted={inserted}, skipped={skipped}", "ok")
    return redirect(url_for("admin.devices"))


@bp.post("/devices/<int:dev_id>/test")
@login_required
@require_perm("devices.manage")
def devices_test(dev_id: int):
    import datetime as _dt
    import time
    import urllib.request

    db = get_db()
    d = db.execute("SELECT * FROM devices WHERE id=?", (dev_id,)).fetchone()
    if not d:
        flash("Not found", "error")
        return redirect(url_for("admin.devices"))

    host = (d["ip"] or "").strip()
    device_type = (d["device_type"] or "").strip().lower()
    agent_base_url = (d["agent_base_url"] or "").strip()
    port = d["port"]

    ok = False
    ms = None

    if agent_base_url:
        base = agent_base_url.rstrip("/")
        endpoints = ["/health", "/ping", "/"]
        for ep in endpoints:
            url = base + ep
            try:
                t0 = time.time()
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=2.5) as resp:
                    _ = resp.read(200)
                    if 200 <= resp.status < 500:
                        ok = True
                        ms = int((time.time() - t0) * 1000)
                        break
            except Exception:
                pass

    if not ok:
        if port:
            port_i = int(port)
        else:
            if device_type == "s30":
                port_i = 4370
            elif device_type == "server":
                port_i = 8001
            else:
                port_i = 80

        ok, ms = tcp_ping(host, int(port_i))

    grade = grade_latency(ms)

    db.execute(
        "UPDATE devices SET last_seen=?, last_latency_ms=? WHERE id=?",
        (_dt.datetime.now().isoformat(timespec="seconds") if ok else None, ms if ok else None, dev_id),
    )
    db.commit()

    audit("devices.test", {"id": dev_id, "ok": ok, "ms": ms, "grade": grade})

    if ok:
        flash(f"OK - {grade} - {ms}ms", "ok")
    else:
        flash("OFFLINE", "error")

    return redirect(url_for("admin.devices"))


# ---------------- Branding ----------------
@bp.get("/branding")
@login_required
@require_perm("branding.manage")
def branding():
    db = get_db()
    s = {r["key"]: r["value"] for r in db.execute("SELECT key,value FROM settings").fetchall()}

    s.setdefault("brand.primary", "#B48A2C")
    s.setdefault("brand.secondary", "#111111")
    s.setdefault("brand.font", "")
    s.setdefault("brand.title_ar", "")
    s.setdefault("brand.title_de", "")

    return render_template("admin/branding.html", s=s)


@bp.post("/branding/save")
@login_required
@require_perm("branding.manage")
def branding_save():
    db = get_db()

    existing = {r["key"]: r["value"] for r in db.execute("SELECT key,value FROM settings").fetchall()}

    primary = (request.form.get("primary") or "").strip()
    secondary = (request.form.get("secondary") or "").strip()
    font = (request.form.get("font") or "").strip()
    title_ar = (request.form.get("title_ar") or "").strip()
    title_de = (request.form.get("title_de") or "").strip()

    remove_logo = request.form.get("remove_logo") == "1"
    remove_bg = request.form.get("remove_bg") == "1"

    def set_setting(k, v):
        db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (k, v),
        )

    if primary:
        set_setting("brand.primary", primary)
    if secondary:
        set_setting("brand.secondary", secondary)
    if font or ("brand.font" not in existing):
        set_setting("brand.font", font)
    if title_ar or ("brand.title_ar" not in existing):
        set_setting("brand.title_ar", title_ar)
    if title_de or ("brand.title_de" not in existing):
        set_setting("brand.title_de", title_de)

    os.makedirs(current_app.config["BRANDING_DIR"], exist_ok=True)

    logo = request.files.get("logo")
    bg = request.files.get("bg")

    if logo and logo.filename:
        lp = os.path.join(current_app.config["BRANDING_DIR"], "logo.png")
        logo.save(lp)
        set_setting("brand.logo", "branding/logo.png")

    if bg and bg.filename:
        bp_ = os.path.join(current_app.config["BRANDING_DIR"], "background.jpg")
        bg.save(bp_)
        set_setting("brand.bg", "branding/background.jpg")

    if remove_logo:
        lp = os.path.join(current_app.config["BRANDING_DIR"], "logo.png")
        try:
            if os.path.exists(lp):
                os.remove(lp)
        except Exception:
            pass
        set_setting("brand.logo", "")

    if remove_bg:
        bp_ = os.path.join(current_app.config["BRANDING_DIR"], "background.jpg")
        try:
            if os.path.exists(bp_):
                os.remove(bp_)
        except Exception:
            pass
        set_setting("brand.bg", "")

    db.commit()
    audit("branding.save", {"primary": primary or None, "secondary": secondary or None, "remove_logo": remove_logo, "remove_bg": remove_bg})
    flash("Saved", "ok")
    return redirect(url_for("admin.branding"))


# ---------------- Exports (Profiles + Settings + Run) ----------------
@bp.get("/exports")
@login_required
@require_perm("exports.manage")
def exports():
    db = get_db()
    profiles = db.execute("SELECT * FROM export_profiles ORDER BY name").fetchall()
    settings_rows = db.execute("SELECT * FROM export_settings").fetchall()
    settings = {r["mode"]: r for r in settings_rows}
    return render_template("admin/exports.html", profiles=profiles, settings=settings)


@bp.post("/exports/settings/save")
@login_required
@require_perm("exports.manage")
def exports_settings_save():
    db = get_db()

    mode = (request.form.get("mode") or "").strip().lower()
    if mode not in ("daily", "weekly", "monthly"):
        flash("Ungültiger Modus", "error")
        return redirect(url_for("admin.exports"))

    profile_id = int(request.form.get("profile_id") or 0)
    if not profile_id:
        flash("Profil ist erforderlich", "error")
        return redirect(url_for("admin.exports"))

    fmts = []
    if request.form.get("fmt_excel") == "1":
        fmts.append("excel")
    if request.form.get("fmt_pdf") == "1":
        fmts.append("pdf")
    if not fmts:
        fmts = ["excel"]

    export_formats = ",".join(fmts)

    break_policy = (request.form.get("break_policy") or "manual").strip().lower()
    if break_policy not in ("none", "manual", "auto"):
        break_policy = "manual"

    try:
        break_minutes = int(request.form.get("break_minutes") or 30)
    except Exception:
        break_minutes = 30
    break_minutes = max(0, min(600, break_minutes))

    db.execute(
        """
        INSERT INTO export_settings(mode,profile_id,export_formats,break_policy,break_minutes,updated_ts)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(mode) DO UPDATE SET
          profile_id=excluded.profile_id,
          export_formats=excluded.export_formats,
          break_policy=excluded.break_policy,
          break_minutes=excluded.break_minutes,
          updated_ts=excluded.updated_ts
        """,
        (
            mode,
            profile_id,
            export_formats,
            break_policy,
            break_minutes,
            datetime.datetime.now().isoformat(timespec="seconds"),
        ),
    )
    db.commit()
    audit(
        "exports.settings.save",
        {"mode": mode, "profile_id": profile_id, "formats": export_formats, "break_policy": break_policy, "break_minutes": break_minutes},
    )
    flash("Gespeichert", "ok")
    return redirect(url_for("admin.exports"))


@bp.post("/exports/run")
@login_required
@require_perm("exports.manage")
def exports_run():
    db = get_db()

    mode = (request.form.get("mode") or "").strip().lower()
    if mode not in ("daily", "weekly", "monthly"):
        flash("Ungültiger Modus", "error")
        return redirect(url_for("admin.exports"))

    s = db.execute("SELECT * FROM export_settings WHERE mode=?", (mode,)).fetchone()
    if not s:
        flash("Keine Einstellungen gefunden", "error")
        return redirect(url_for("admin.exports"))

    prof = db.execute("SELECT * FROM export_profiles WHERE id=?", (s["profile_id"],)).fetchone()
    if not prof:
        flash("Profil nicht gefunden", "error")
        return redirect(url_for("admin.exports"))

    date_from = (request.form.get("date_from") or "").strip()
    date_to = (request.form.get("date_to") or "").strip()

    if (date_from and not date_to) or (date_to and not date_from):
        flash("Bitte 'Von' und 'Bis' zusammen angeben.", "error")
        return redirect(url_for("admin.exports"))

    args = [
        "/mnt/nvme/server/.venv/bin/python",
        "/mnt/nvme/server/tools/auto_export_hours.py",
        "--mode",
        mode,
    ]

    if date_from and date_to:
        try:
            df = datetime.date.fromisoformat(date_from)
            dt_ = datetime.date.fromisoformat(date_to)
            if df > dt_:
                flash("'Von' muss <= 'Bis' sein.", "error")
                return redirect(url_for("admin.exports"))
        except Exception:
            flash("Ungültiges Datum", "error")
            return redirect(url_for("admin.exports"))

        args += ["--date-from", date_from, "--date-to", date_to]

    export_formats = (s["export_formats"] or "excel,pdf").lower()
    if "pdf" not in export_formats:
        args.append("--no-pdf")
    if "excel" not in export_formats and "xlsx" not in export_formats:
        args.append("--no-xlsx")

    run_env = os.environ.copy()
    run_env["PYTHONPATH"] = "/mnt/nvme/server"
    run_env["ALJOUD_DB"] = current_app.config.get("ALJOUD_DB_PATH", "/mnt/nvme/data/aljoud.db")
    run_env["ALJOUD_EXPORT_DIR"] = current_app.config.get("EXPORT_DIR", "/mnt/nvme/exports")
    run_env["ALJOUD_BREAK_POLICY"] = (s["break_policy"] or "manual")
    run_env["ALJOUD_BREAK_MINUTES"] = str(int(s["break_minutes"] or 30))

    header_text = (prof["header_text"] or "").strip()
    run_env["ALJOUD_REPORT_TITLE"] = header_text if header_text else "ALJOUD – Stundenbericht (Arbeitszeiten)"

    try:
        cp = subprocess.run(
            args,
            cwd="/mnt/nvme/server",
            env=run_env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if cp.returncode != 0:
            msg = (cp.stderr or cp.stdout or "").strip()
            flash(f"Export fehlgeschlagen (Code {cp.returncode}). {msg[:400]}", "error")
            audit("exports.run.fail", {"mode": mode, "code": cp.returncode, "msg": msg[:1200]})
        else:
            flash("Export erstellt.", "ok")
            audit("exports.run.ok", {"mode": mode, "date_from": date_from or None, "date_to": date_to or None})
    except Exception as e:
        flash(f"Export fehlgeschlagen: {e}", "error")
        audit("exports.run.error", {"mode": mode, "error": str(e)})

    return redirect(url_for("admin.exports"))


@bp.post("/exports/create")
@login_required
@require_perm("exports.manage")
def exports_create():
    db = get_db()
    name = (request.form.get("name") or "").strip()
    header_text = (request.form.get("header_text") or "").strip()
    include_logo = 1 if request.form.get("include_logo") == "1" else 0
    columns = request.form.getlist("columns")
    break_rules = (request.form.get("break_rules_json") or "[]").strip()
    try:
        json.loads(break_rules)
    except Exception:
        flash("Invalid break rules JSON", "error")
        return redirect(url_for("admin.exports"))
    if not columns:
        columns = ["employee_id", "name", "in_time", "out_time", "work_minutes"]
    db.execute(
        "INSERT INTO export_profiles(name,header_text,include_logo,columns_json,break_rules_json,created_ts) VALUES(?,?,?,?,?,?)",
        (name, header_text, include_logo, json.dumps(columns), break_rules, datetime.datetime.now().isoformat(timespec="seconds")),
    )
    db.commit()
    audit("exports.create", {"name": name})
    flash("Saved", "ok")
    return redirect(url_for("admin.exports"))


@bp.post("/exports/<int:pid>/delete")
@login_required
@require_perm("exports.manage")
def exports_delete(pid: int):
    db = get_db()
    old = db.execute("SELECT * FROM export_profiles WHERE id=?", (pid,)).fetchone()
    db.execute("DELETE FROM export_profiles WHERE id=?", (pid,))
    db.commit()
    audit("exports.delete", {"id": pid, "old": dict(old) if old else None})
    flash("Deleted", "ok")
    return redirect(url_for("admin.exports"))


# ---------------- Backups ----------------
@bp.get("/backups")
@login_required
@require_perm("backups.manage")
def backups():
    db = get_db()
    rows = db.execute("SELECT * FROM backup_jobs ORDER BY created_ts DESC").fetchall()

    keep = 8
    try:
        keep = int(current_app.config.get("BACKUPS_KEEP", 8))
    except Exception:
        keep = 8

    return render_template("admin/backups.html", backups=rows, keep=keep)


@bp.post("/backups/create")
@login_required
@require_perm("backups.manage")
def backups_create():
    db = get_db()

    fname, size = create_backup(
        current_app.config["STORAGE_ROOT"],
        current_app.config["ALJOUD_DB_PATH"],
        current_app.config["BACKUPS_DIR"],
    )

    db.execute(
        "INSERT INTO backup_jobs(created_ts,filename,size_bytes) VALUES(?,?,?)",
        (datetime.datetime.now().isoformat(timespec="seconds"), fname, size),
    )
    db.commit()

    keep = 8
    try:
        keep = int(current_app.config.get("BACKUPS_KEEP", 8))
    except Exception:
        keep = 8
    keep = max(1, min(50, keep))

    rotate_backups(current_app.config["BACKUPS_DIR"], keep=keep)

    audit("backups.create", {"filename": fname, "size": size, "keep": keep})
    flash("Backup created", "ok")
    return redirect(url_for("admin.backups"))


@bp.get("/backups/download/<int:bid>")
@login_required
@require_perm("backups.manage")
def backups_download(bid: int):
    db = get_db()
    row = db.execute("SELECT * FROM backup_jobs WHERE id=?", (bid,)).fetchone()
    if not row:
        flash("Not found", "error")
        return redirect(url_for("admin.backups"))
    path = os.path.join(current_app.config["BACKUPS_DIR"], row["filename"])
    return send_file(path, as_attachment=True, download_name=row["filename"])


# ---------------- Roles ----------------
@bp.get("/roles")
@login_required
@require_perm("roles.manage")
def roles():
    db = get_db()

    # Seed RBAC defaults (idempotent)
    try:
        _ensure_default_rbac(db)
        _ensure_employee_permissions(db)
        db.commit()
    except Exception:
        db.rollback()

    roles_rows = db.execute("SELECT * FROM roles ORDER BY name").fetchall()

    mapping = db.execute("SELECT role_id, perm_code FROM role_permissions").fetchall()
    role_perms: Dict[int, Set[str]] = {}
    for r in mapping:
        role_perms.setdefault(int(r["role_id"]), set()).add(str(r["perm_code"]))

    employees = db.execute(
        "SELECT id, employee_id, name, username, is_active, is_superadmin "
        "FROM employees ORDER BY CAST(employee_id AS INTEGER)"
    ).fetchall()

    er = db.execute("SELECT employee_id, role_id FROM employee_roles").fetchall()
    er_map: Dict[int, Set[int]] = {}
    for r in er:
        er_map.setdefault(int(r["employee_id"]), set()).add(int(r["role_id"]))

    ep_map: Dict[int, Set[str]] = {}
    try:
        ep_rows = db.execute("SELECT employee_id, perm_code FROM employee_permissions").fetchall()
        for r in ep_rows:
            ep_map.setdefault(int(r["employee_id"]), set()).add(str(r["perm_code"]))
    except Exception:
        # table might not exist on older DBs (should be created by _ensure_employee_permissions)
        ep_map = {}

    perms_flat = sorted(PERM_TITLES.keys())

    return render_template(
        "admin/roles.html",
        roles=roles_rows,
        perm_groups=PERM_GROUPS,
        perm_titles=PERM_TITLES,
        perms_flat=perms_flat,
        role_perms=role_perms,
        employees=employees,
        er_map=er_map,
        ep_map=ep_map,
    )


@bp.post("/roles/<int:rid>/save_perms")
@login_required
@require_perm("roles.manage")
def roles_save_perms(rid: int):
    db = get_db()
    selected = set((x or "").strip() for x in request.form.getlist("perm_codes") if (x or "").strip())
    db.execute("DELETE FROM role_permissions WHERE role_id=?", (rid,))
    for code in selected:
        db.execute("INSERT OR IGNORE INTO role_permissions(role_id,perm_code) VALUES(?,?)", (rid, code))
    db.commit()
    audit("roles.save_perms", {"role_id": rid, "count": len(selected)})
    flash("Saved", "ok")
    return redirect(url_for("admin.roles"))


@bp.post("/roles/assign/<int:eid>")
@login_required
@require_perm("roles.manage")
def roles_assign(eid: int):
    db = get_db()
    selected = set(int(x) for x in request.form.getlist("role_ids") if str(x).strip().isdigit())
    db.execute("DELETE FROM employee_roles WHERE employee_id=?", (eid,))
    for rid in selected:
        db.execute("INSERT OR IGNORE INTO employee_roles(employee_id,role_id) VALUES(?,?)", (eid, rid))
    db.commit()
    audit("roles.assign", {"employee_id": eid, "roles": list(selected)})
    flash("Saved", "ok")
    return redirect(url_for("admin.roles"))


@bp.post("/roles/assign_bulk")
@login_required
@require_perm("roles.manage")
def roles_assign_bulk():
    """
    Assign roles to multiple employees at once.
    """
    db = get_db()
    emp_ids = [int(x) for x in request.form.getlist("employee_ids") if str(x).strip().isdigit()]
    role_ids = [int(x) for x in request.form.getlist("role_ids") if str(x).strip().isdigit()]

    if not emp_ids:
        flash("Select at least one employee.", "error")
        return redirect(url_for("admin.roles"))

    for eid in emp_ids:
        db.execute("DELETE FROM employee_roles WHERE employee_id=?", (eid,))
        for rid in role_ids:
            db.execute("INSERT OR IGNORE INTO employee_roles(employee_id,role_id) VALUES(?,?)", (eid, rid))

    db.commit()
    audit("roles.assign_bulk", {"employee_ids": emp_ids[:100], "role_ids": role_ids})
    flash("Bulk role assignment saved", "ok")
    return redirect(url_for("admin.roles"))


@bp.post("/roles/employee_perms/set")
@login_required
@require_perm("roles.manage")
def roles_employee_perms_set():
    """
    Set DIRECT (override/additional) permissions for one or more employees.
    Note: requires rbac.py to consider employee_permissions for enforcement.
    """
    db = get_db()
    emp_ids = [int(x) for x in request.form.getlist("employee_ids") if str(x).strip().isdigit()]
    perm_codes = [str(x).strip() for x in request.form.getlist("perm_codes") if str(x).strip()]

    if not emp_ids:
        flash("Select at least one employee.", "error")
        return redirect(url_for("admin.roles"))

    for eid in emp_ids:
        db.execute("DELETE FROM employee_permissions WHERE employee_id=?", (eid,))
        for code in perm_codes:
            db.execute("INSERT OR IGNORE INTO employee_permissions(employee_id,perm_code) VALUES(?,?)", (eid, code))

    db.commit()
    audit("employee_perms.set", {"employee_ids": emp_ids[:100], "perm_count": len(perm_codes)})
    flash("Employee permissions saved", "ok")
    return redirect(url_for("admin.roles"))


@bp.post("/roles/employee/purge")
@login_required
@require_perm("roles.manage")
def employee_purge():
    """
    Hard-delete employee from DB.
    Safety:
      - must exist
      - must NOT be superadmin
      - recommended: employee must already be inactive
    """
    db = get_db()
    token = (request.form.get("purge_token") or "").strip()
    row = _resolve_employee_for_purge(db, token)
    if not row:
        flash("Employee not found (use row id, Employee ID, or username).", "error")
        return redirect(url_for("admin.roles"))

    try:
        if int(row["is_superadmin"] or 0) == 1:
            flash("Cannot purge Super Admin.", "error")
            return redirect(url_for("admin.roles"))
    except Exception:
        pass

    try:
        if int(row["is_active"] or 0) == 1:
            flash("Deactivate employee first, then purge.", "error")
            return redirect(url_for("admin.roles"))
    except Exception:
        pass

    emp_row_id = int(row["id"])
    emp_id = str(row["employee_id"] or "")

    try:
        # remove relations
        db.execute("DELETE FROM employee_roles WHERE employee_id=?", (emp_row_id,))
        db.execute("DELETE FROM employee_permissions WHERE employee_id=?", (emp_row_id,))

        # optional cleanup punches related to that employee_id (string in attendance tables)
        if emp_id:
            try:
                db.execute("DELETE FROM pending_attendance WHERE employee_id=?", (emp_id,))
            except Exception:
                pass
            try:
                db.execute("DELETE FROM attendance WHERE employee_id=?", (emp_id,))
            except Exception:
                pass

        # delete employee row
        db.execute("DELETE FROM employees WHERE id=?", (emp_row_id,))
        db.commit()

        audit("employees.purge", {"id": emp_row_id, "employee_id": emp_id, "username": row["username"]})
        flash(f"Purged employee: {row['username']} (EmpID={emp_id})", "ok")
    except Exception as e:
        db.rollback()
        audit("employees.purge.error", {"id": emp_row_id, "error": str(e)})
        flash(f"Purge failed: {str(e)[:200]}", "error")

    return redirect(url_for("admin.roles"))
