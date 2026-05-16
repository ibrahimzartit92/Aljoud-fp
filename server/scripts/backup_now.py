import datetime
from app.create_app import create_app
from app.utils.backup import create_backup, rotate_backups
from app.db import get_db

app = create_app()
with app.app_context():
    db = get_db()
    fname, size = create_backup(app.config["STORAGE_ROOT"], app.config["ALJOUD_DB_PATH"], app.config["BACKUPS_DIR"])
    db.execute("INSERT INTO backup_jobs(created_ts,filename,size_bytes) VALUES(?,?,?)",
               (datetime.datetime.now().isoformat(timespec='seconds'), fname, size))
    db.commit()
    rotate_backups(app.config["BACKUPS_DIR"], keep=8)
print("OK", fname, size)
