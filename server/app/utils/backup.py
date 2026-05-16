import os, zipfile, datetime
from pathlib import Path

def create_backup(storage_root: str, db_path: str, backups_dir: str):
    Path(backups_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"aljoud_backup_{ts}.zip"
    out = os.path.join(backups_dir, fname)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        if os.path.exists(db_path):
            z.write(db_path, arcname="data/aljoud.db")
        for rel in ["uploads", "branding", "exports"]:
            p = os.path.join(storage_root, rel)
            if os.path.isdir(p):
                for root, _, files in os.walk(p):
                    for f in files:
                        full = os.path.join(root, f)
                        arc = os.path.relpath(full, storage_root)
                        z.write(full, arcname=arc)
    return fname, os.path.getsize(out)

def rotate_backups(backups_dir: str, keep: int = 8):
    files = sorted([f for f in os.listdir(backups_dir) if f.endswith(".zip")])
    if len(files) <= keep:
        return
    for f in files[:-keep]:
        try:
            os.remove(os.path.join(backups_dir, f))
        except Exception:
            pass
