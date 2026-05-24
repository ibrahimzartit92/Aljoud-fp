#!/usr/bin/env python3
import datetime
import sqlite3

DB_PATH = "/mnt/nvme/data/aljoud.db"


def map_punch(p):
    """
    ZK punch codes vary by device/firmware.
    Common mapping:
      0 -> in
      1 -> out
    Anything else -> unknown (still goes to pending).
    """
    try:
        p = int(p)
    except Exception:
        return "unknown"
    if p == 0:
        return "in"
    if p == 1:
        return "out"
    return "unknown"


def iso(ts: datetime.datetime) -> str:
    return ts.replace(microsecond=0).isoformat()


def latest_punch_type_before(cur, employee_id: str, ts: str):
    row = cur.execute("""
        SELECT punch_type
        FROM attendance
        WHERE employee_id = ?
          AND status = 'approved'
          AND ts < ?
          AND punch_type IN ('in', 'out')
        ORDER BY ts DESC, id DESC
        LIMIT 1
    """, (employee_id, ts)).fetchone()
    return row["punch_type"] if row else None


def can_accept_real_punch(cur, employee_id: str, punch_type: str, ts: str) -> bool:
    if punch_type not in ("in", "out"):
        return True
    latest = latest_punch_type_before(cur, employee_id, ts)
    if punch_type == "in":
        return latest is None or latest == "out"
    return latest == "in"


def main():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # Watermark table (per device)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS device_pull_state (
          device_id INTEGER PRIMARY KEY,
          last_ts TEXT
        )
    """)

    # Helpful indexes (safe; will no-op if already exist in most sqlite versions? no, it errors.
    # We'll wrap in try.
    try:
        cur.execute("""
            CREATE INDEX IF NOT EXISTS ix_pending_device_ts
            ON pending_attendance(device_id, ts)
        """)
    except Exception:
        pass
    try:
        cur.execute("""
            CREATE INDEX IF NOT EXISTS ix_attendance_device_ts
            ON attendance(device_id, ts)
        """)
    except Exception:
        pass

    # Active S30 devices
    devs = cur.execute("""
        SELECT id, ip, port, branch_id
        FROM devices
        WHERE device_type='s30'
          AND is_active=1
          AND ip IS NOT NULL AND ip<>''
        ORDER BY id
    """).fetchall()

    if not devs:
        return

    # Import ZK here so missing dependency fails loudly
    from zk import ZK  # pyzk

    for d in devs:
        dev_id = int(d["id"])
        ip = (d["ip"] or "").strip()
        port = int(d["port"] or 4370)
        branch_id = d["branch_id"]

        # Watermark for this device
        row = cur.execute(
            "SELECT last_ts FROM device_pull_state WHERE device_id=?",
            (dev_id,)
        ).fetchone()
        last_ts = row["last_ts"] if row else None

        # Pull from device
        zk = ZK(ip, port=port, timeout=4)  # keep timeout short to avoid load
        conn = None
        try:
            conn = zk.connect()
            att = conn.get_attendance() or []
        except Exception:
            # device offline/unreachable -> skip this cycle
            continue
        finally:
            try:
                if conn:
                    conn.disconnect()
            except Exception:
                pass

        if not att:
            continue

        # Sort ascending so watermark skip works
        att_sorted = sorted(att, key=lambda x: x.timestamp)

        inserted = 0
        skipped_sequence = 0
        newest_ts = last_ts

        for a in att_sorted:
            ts_iso = iso(a.timestamp)

            # skip already processed
            if last_ts and ts_iso <= last_ts:
                continue

            employee_id = str(getattr(a, "user_id", "") or "")
            if not employee_id:
                continue

            punch_type = map_punch(getattr(a, "punch", None))

            # De-dupe against BOTH pending_attendance and attendance
            # (so approving won't cause re-queue, and old imported rows won't reappear)
            exists = cur.execute("""
                SELECT 1 FROM pending_attendance
                WHERE employee_id=? AND device_id=? AND ts=?
                UNION ALL
                SELECT 1 FROM attendance
                WHERE employee_id=? AND device_id=? AND ts=?
                LIMIT 1
            """, (employee_id, dev_id, ts_iso, employee_id, dev_id, ts_iso)).fetchone()

            if exists:
                # still advance watermark
                if (newest_ts is None) or (ts_iso > newest_ts):
                    newest_ts = ts_iso
                continue

            if not can_accept_real_punch(cur, employee_id, punch_type, ts_iso):
                skipped_sequence += 1
                if (newest_ts is None) or (ts_iso > newest_ts):
                    newest_ts = ts_iso
                continue

            try:
                cur.execute("""
                    INSERT INTO attendance(employee_id, branch_id, device_id, punch_type, ts,
                                           approved_by, approved_ts, status)
                    SELECT ?,?,?,?,?, 'auto', ?, 'approved'
                    WHERE NOT EXISTS (
                        SELECT 1 FROM attendance
                        WHERE employee_id=? AND device_id=? AND ts=?
                    )
                """, (
                    employee_id, branch_id, dev_id, punch_type, ts_iso,
                    datetime.datetime.now().isoformat(timespec="seconds"),
                    employee_id, dev_id, ts_iso,
                ))
                if cur.rowcount:
                    inserted += 1
            except Exception:
                # don't kill the whole device on one row
                pass

            if (newest_ts is None) or (ts_iso > newest_ts):
                newest_ts = ts_iso

        # Update watermark only if progressed
        if newest_ts and newest_ts != last_ts:
            cur.execute("""
                INSERT INTO device_pull_state(device_id, last_ts)
                VALUES(?, ?)
                ON CONFLICT(device_id) DO UPDATE SET last_ts=excluded.last_ts
            """, (dev_id, newest_ts))

        if inserted or (newest_ts and newest_ts != last_ts):
            con.commit()


if __name__ == "__main__":
    main()
