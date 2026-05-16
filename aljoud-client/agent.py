from __future__ import annotations
import os, json, time, traceback
from datetime import datetime
from flask import Flask, request, jsonify
import requests

try:
    from zk import ZK
except Exception:
    ZK = None

app = Flask(__name__)

def now_iso():
    return datetime.now().isoformat(timespec="seconds")

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def log_line(log_path: str, msg: str):
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{now_iso()}] {msg}\n")

def require_secret(cfg, req):
    secret = (cfg.get("agent_shared_secret") or "").strip()
    if not secret:
        return True  # allow if not configured
    got = (req.headers.get("X-Aljoud-Agent-Secret") or "").strip()
    return got == secret

def s30_connect(cfg):
    if ZK is None:
        raise RuntimeError("Python package 'zk' not available")
    ip = (cfg.get("s30_ip") or "").strip()
    if not ip:
        raise RuntimeError("Missing s30_ip in config")
    port = int(cfg.get("s30_port") or 4370)
    timeout = float(cfg.get("s30_timeout") or 6)
    zk = ZK(ip, port=port, timeout=timeout)
    conn = zk.connect()
    return conn

def s30_upsert_users(cfg, employees: list[dict], log_path: str):
    """
    Upsert users without deleting fingerprints:
    - Use set_user for active employees.
    - Do NOT delete absent users unless explicitly requested by 'deleted_employee_ids'.
    """
    conn = None
    try:
        conn = s30_connect(cfg)
        ok = 0
        fail = 0
        for e in employees:
            if not e.get("is_active", 1):
                continue

            emp_id = str(e.get("employee_id") or "").strip()
            if not emp_id:
                continue

            # ZK user_id must be int on many devices
            try:
                uid = int(emp_id)
            except Exception:
                # skip non-numeric
                continue

            name = (e.get("name") or "").strip()[:24]
            pin = (e.get("s30_password") or "").strip()
            card = (e.get("card_rfid") or "").strip()

            # IMPORTANT:
            # set_user updates user data but typically does NOT wipe fingerprints/templates.
            # Deletion is what removes templates.
            try:
                # signature varies by library versions; try common forms
                # 1) set_user(uid, name, password, group_id, user_id)
                try:
                    conn.set_user(uid=uid, name=name, password=pin or "", group_id=0, user_id=str(uid))
                except TypeError:
                    # 2) set_user(uid, name, privilege, password, group_id, user_id, card)
                    try:
                        conn.set_user(uid, name, 0, pin or "", 0, str(uid), card or "0")
                    except TypeError:
                        # 3) minimal
                        conn.set_user(uid, name=name, password=pin or "")

                # card assignment if supported
                if card:
                    try:
                        conn.set_user(uid=uid, name=name, password=pin or "", card=card)
                    except Exception:
                        pass

                ok += 1
            except Exception as ex:
                fail += 1
                log_line(log_path, f"S30 UPSERT FAIL emp_id={emp_id} err={str(ex)[:180]}")
        return {"ok": ok, "fail": fail}
    finally:
        try:
            if conn:
                conn.disconnect()
        except Exception:
            pass

def s30_delete_users(cfg, employee_ids: list[str], log_path: str):
    """
    Delete user => fingerprints/templates removed (expected).
    """
    if not employee_ids:
        return {"deleted": 0, "fail": 0}
    conn = None
    deleted = 0
    fail = 0
    try:
        conn = s30_connect(cfg)
        for emp_id in employee_ids:
            try:
                uid = int(str(emp_id).strip())
            except Exception:
                continue
            try:
                # common delete calls
                try:
                    conn.delete_user(uid=uid)
                except TypeError:
                    conn.delete_user(uid)
                deleted += 1
            except Exception as ex:
                fail += 1
                log_line(log_path, f"S30 DELETE FAIL emp_id={emp_id} err={str(ex)[:180]}")
        return {"deleted": deleted, "fail": fail}
    finally:
        try:
            if conn:
                conn.disconnect()
        except Exception:
            pass

@app.get("/health")
def health():
    return jsonify({"ok": True, "ts": now_iso()})

@app.post("/sync/users")
def sync_users():
    cfg_path = os.environ.get("ALJOUD_CLIENT_CONFIG") or ""
    if not cfg_path:
        return jsonify({"ok": False, "error": "Missing env ALJOUD_CLIENT_CONFIG"}), 500

    cfg = load_json(cfg_path, {})
    log_path = cfg.get("log_path") or "/mnt/nvme/aljoud-client/logs/agent.log"
    state_path = cfg.get("state_path") or "/mnt/nvme/aljoud-client/state/state.json"

    if not require_secret(cfg, request):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    employees = payload.get("employees") or []
    deleted_ids = payload.get("deleted_employee_ids") or []

    # persist last payload
    state = load_json(state_path, {})
    state["last_sync_ts"] = now_iso()
    state["last_sync_count"] = len(employees)
    save_json(state_path, state)

    # apply to S30
    try:
        res_upsert = s30_upsert_users(cfg, employees, log_path)
        res_del = s30_delete_users(cfg, deleted_ids, log_path)
        log_line(log_path, f"SYNC DONE upsert_ok={res_upsert['ok']} upsert_fail={res_upsert['fail']} del={res_del['deleted']} del_fail={res_del['fail']}")
        return jsonify({"ok": True, "upsert": res_upsert, "delete": res_del})
    except Exception as ex:
        log_line(log_path, "SYNC ERROR: " + str(ex))
        log_line(log_path, traceback.format_exc()[:1200])
        return jsonify({"ok": False, "error": str(ex)}), 500

def run():
    cfg_path = os.environ.get("ALJOUD_CLIENT_CONFIG") or ""
    cfg = load_json(cfg_path, {})
    host = cfg.get("bind_host") or "0.0.0.0"
    port = int(cfg.get("bind_port") or 8002)
    app.run(host=host, port=port)

if __name__ == "__main__":
    run()
