import sqlite3
from flask import current_app, g
from pathlib import Path

def get_db():
    if "db" in g:
        return g.db
    path = current_app.config["ALJOUD_DB_PATH"]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    g.db = conn
    return conn

def close_db(e=None):
    db = g.pop("db", None)
    if db:
        db.close()

SCHEMA_SQL = r'''
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  actor_user TEXT,
  action TEXT NOT NULL,
  details TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS branches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS roles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS permissions (
  code TEXT PRIMARY KEY,
  name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS employees (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  employee_id TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  s30_password TEXT,
  card_rfid TEXT,
  is_active INTEGER NOT NULL DEFAULT 1,
  is_superadmin INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS employee_roles (
  employee_id INTEGER NOT NULL,
  role_id INTEGER NOT NULL,
  PRIMARY KEY(employee_id, role_id),
  FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE,
  FOREIGN KEY(role_id) REFERENCES roles(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS role_permissions (
  role_id INTEGER NOT NULL,
  perm_code TEXT NOT NULL,
  PRIMARY KEY(role_id, perm_code),
  FOREIGN KEY(role_id) REFERENCES roles(id) ON DELETE CASCADE,
  FOREIGN KEY(perm_code) REFERENCES permissions(code) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS devices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  device_type TEXT NOT NULL,
  ip TEXT,
  port INTEGER,
  agent_base_url TEXT,
  branch_id INTEGER,
  is_active INTEGER NOT NULL DEFAULT 1,
  last_seen TEXT,
  last_latency_ms INTEGER,
  notes TEXT,
  FOREIGN KEY(branch_id) REFERENCES branches(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS attendance (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  employee_id TEXT NOT NULL,
  branch_id INTEGER,
  device_id INTEGER,
  punch_type TEXT NOT NULL,
  ts TEXT NOT NULL,
  approved_by TEXT,
  approved_ts TEXT,
  status TEXT NOT NULL DEFAULT 'approved',
  FOREIGN KEY(branch_id) REFERENCES branches(id) ON DELETE SET NULL,
  FOREIGN KEY(device_id) REFERENCES devices(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS pending_attendance (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  employee_id TEXT NOT NULL,
  branch_id INTEGER,
  device_id INTEGER,
  punch_type TEXT NOT NULL,
  ts TEXT NOT NULL,
  source TEXT NOT NULL,
  requested_edit INTEGER NOT NULL DEFAULT 0,
  requested_by TEXT,
  requested_ts TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  FOREIGN KEY(branch_id) REFERENCES branches(id) ON DELETE SET NULL,
  FOREIGN KEY(device_id) REFERENCES devices(id) ON DELETE SET NULL
);

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
);

CREATE TABLE IF NOT EXISTS export_profiles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  header_text TEXT,
  include_logo INTEGER NOT NULL DEFAULT 1,
  columns_json TEXT NOT NULL,
  break_rules_json TEXT NOT NULL,
  created_ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backup_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_ts TEXT NOT NULL,
  filename TEXT NOT NULL,
  size_bytes INTEGER NOT NULL
);
'''

def init_db(app):
    with app.app_context():
        db = get_db()
        db.executescript(SCHEMA_SQL)
        db.commit()

def seed_if_empty(app):
    from .security import hash_password
    import datetime, json
    with app.app_context():
        db = get_db()
        c = db.execute("SELECT COUNT(*) AS c FROM employees").fetchone()["c"]
        if c == 0:
            db.execute(
                "INSERT OR IGNORE INTO employees(employee_id,name,username,password_hash,s30_password,card_rfid,is_active,is_superadmin) "
                "VALUES(?,?,?,?,?,?,1,1)",
                ("1","Super Admin","admin",hash_password("262992"),"262992","")
            )
            db.executemany("INSERT OR IGNORE INTO roles(name) VALUES(?)", [("superadmin",),("cashier",),("manager",)])
            perms = [
                ("branches.manage","Manage branches"),
                ("employees.manage","Manage employees"),
                ("roles.manage","Manage roles & permissions"),
                ("devices.manage","Manage devices & network"),
                ("branding.manage","Manage branding & theme"),
                ("exports.manage","Manage exports & schedules"),
                ("backups.manage","Manage backups"),
                ("attendance.view","View attendance"),
                ("attendance.approve","Approve/reject attendance"),
                ("attendance.edit","Request/edit attendance times"),
                ("reports.view","View reports"),
                ("reports.export","Export reports"),
            ]
            db.executemany("INSERT OR IGNORE INTO permissions(code,name) VALUES(?,?)", perms)
            rid = db.execute("SELECT id FROM roles WHERE name='superadmin'").fetchone()["id"]
            db.execute("INSERT OR IGNORE INTO employee_roles(employee_id,role_id) VALUES(?,?)", (1, rid))
            for code,_ in perms:
                db.execute("INSERT OR IGNORE INTO role_permissions(role_id,perm_code) VALUES(?,?)", (rid, code))
            defaults = {
                "brand.primary":"#b88a2a",
                "brand.secondary":"#111111",
                "brand.font":"system-ui, -apple-system, Segoe UI, Roboto, Arial",
                "brand.logo":"",
                "brand.bg":"",
                "brand.title_ar":"الجود للحضور والانصراف",
                "brand.title_de":"Aljoud Anwesenheitssystem",
            }
            for k,v in defaults.items():
                db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k,v))
            db.execute(
                "INSERT OR IGNORE INTO export_profiles(name,header_text,include_logo,columns_json,break_rules_json,created_ts) VALUES(?,?,?,?,?,?)",
                ("Default","ALJOUD",1,json.dumps(["employee_id","name","in_time","in_branch","out_time","out_branch","break_minutes","work_minutes"]),
                 json.dumps([{"min_hours":8,"break_minutes":30}]),
                 datetime.datetime.now().isoformat(timespec="seconds"))
            )
            db.commit()
