from __future__ import annotations

import datetime
from typing import Any, Dict, Optional, Tuple

from flask import Blueprint, request, jsonify

from ..security import require_agent_secret
from ..db import get_db
from ..audit import audit

bp = Blueprint("api", __name__)


def _norm_str(x) -> str:
    return ("" if x is None else str(x)).strip()


def _norm_int(x) -> Optional[int]:
    try:
        if x is None or x == "":
            return None
        return int(x)
    except Exception:
        return None


def _norm_punch_type(v) -> str:
    """
    Normalize punch_type to 'in' or 'out'.
    Accepts: 'in'/'out', 0/1, '0'/'1', 'checkin'/'checkout', etc.
    Default: 'unknown'
    """
    s = _norm_str(v).lower()
    if s in ("in", "checkin", "check-in", "enter", "entry"):
        return "in"
    if s in ("out", "checkout", "check-out", "exit", "leave"):
        return "out"
    if s in ("0", "00"):
        return "in"
    if s in ("1", "01"):
        return "out"
    try:
        i = int(s)
        return "in" if i == 0 else "out"
    except Exception:
        return "unknown"


def _resolve_device_branch(db, punch: Dict[str, Any], payload: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    """
    Resolve (device_id, branch_id).
    Priority:
      1) punch.device_id / punch.branch_id if valid
      2) payload.device_id / payload.branch_id if valid
      3) lookup devices by s30_ip/device_ip in punch or payload -> devices.ip
    """
    # 1) punch explicit
    dev_id = _norm_int(punch.get("device_id"))
    br_id = _norm_int(punch.get("branch_id"))
    if dev_id is not None or br_id is not None:
        return dev_id, br_id

    # 2) payload defaults
    dev_id = _norm_int(payload.get("device_id"))
    br_id = _norm_int(payload.get("branch_id"))
    if dev_id is not None or br_id is not None:
        return dev_id, br_id

    # 3) lookup by ip
    ip = _norm_str(
        punch.get("s30_ip")
        or punch.get("device_ip")
        or payload.get("s30_ip")
        or payload.get("device_ip")
    )
    if not ip:
        return None, None

    row = db.execute(
        "SELECT id, branch_id FROM devices WHERE device_type='s30' AND ip=? AND is_active=1 ORDER BY id DESC LIMIT 1",
        (ip,),
    ).fetchone()
    if not row:
        return None, None

    return int(row["id"]), _norm_int(row["branch_id"])


def _latest_punch_type_before(db, employee_id: str, ts: str) -> Optional[str]:
    row = db.execute(
        """
        SELECT punch_type
        FROM attendance
        WHERE employee_id = ?
          AND status = 'approved'
          AND ts < ?
          AND punch_type IN ('in', 'out')
        ORDER BY ts DESC, id DESC
        LIMIT 1
        """,
        (employee_id, ts),
    ).fetchone()
    return row["punch_type"] if row else None


def _can_accept_real_punch(db, employee_id: str, punch_type: str, ts: str) -> bool:
    if punch_type not in ("in", "out"):
        return True
    latest = _latest_punch_type_before(db, employee_id, ts)
    if punch_type == "in":
        return latest is None or latest == "out"
    return latest == "in"


@bp.post("/agent/push_punches")
@require_agent_secret
def agent_push_punches():
    payload = request.get_json(force=True, silent=True) or {}
    punches = payload.get("punches") or []

    db = get_db()
    inserted = 0
    skipped = 0
    skipped_sequence = 0
    bad = 0

    punches_sorted = sorted(
        punches,
        key=lambda x: _norm_str(x.get("ts")) if isinstance(x, dict) else "",
    )

    for p in punches_sorted:
        if not isinstance(p, dict):
            bad += 1
            continue

        employee_id = _norm_str(p.get("employee_id"))
        ts = _norm_str(p.get("ts"))
        if not employee_id or not ts:
            bad += 1
            continue

        punch_type = _norm_punch_type(p.get("punch_type"))
        device_id, branch_id = _resolve_device_branch(db, p, payload)

        # لازم نعرف device_id حتى ما تدخل ضربات NULL
        if device_id is None:
            bad += 1
            continue

        if not _can_accept_real_punch(db, employee_id, punch_type, ts):
            skipped += 1
            skipped_sequence += 1
            continue

        try:
            cur = db.execute(
                """
                INSERT INTO attendance(employee_id, branch_id, device_id, punch_type, ts,
                                       approved_by, approved_ts, status)
                SELECT ?,?,?,?,?, 'auto', ?, 'approved'
                WHERE NOT EXISTS (
                    SELECT 1 FROM attendance
                    WHERE employee_id=? AND device_id=? AND ts=?
                )
                """,
                (employee_id, branch_id, device_id, punch_type, ts,
                 datetime.datetime.now().isoformat(timespec="seconds"),
                 employee_id, device_id, ts),
            )
            if cur.rowcount == 1:
                inserted += 1
            else:
                skipped += 1
        except Exception:
            bad += 1

    db.commit()
    audit(
        "agent.push_punches",
        {"received": len(punches), "inserted": inserted, "skipped": skipped, "skipped_sequence": skipped_sequence, "bad": bad},
    )
    return jsonify({"ok": True, "received": len(punches), "inserted": inserted, "skipped": skipped, "skipped_sequence": skipped_sequence, "bad": bad})


@bp.get("/health")
def health():
    return jsonify({"ok": True, "ts": datetime.datetime.now().isoformat(timespec="seconds")})
