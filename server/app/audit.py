import json, datetime
from flask import g
from .db import get_db

def audit(action: str, details: dict):
    db = get_db()
    actor = None
    if getattr(g, "user", None):
        actor = g.user.get("username")
    db.execute(
        "INSERT INTO audit_log(ts, actor_user, action, details) VALUES(?,?,?,?)",
        (datetime.datetime.now().isoformat(timespec="seconds"), actor, action, json.dumps(details, ensure_ascii=False)),
    )
    db.commit()
