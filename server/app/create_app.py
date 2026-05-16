import os
from flask import Flask
from .config import Config
from .db import init_db, seed_if_empty, close_db, get_db
from .security import load_current_user
from .i18n import t, get_locale

def create_app():
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config.from_object(Config)

    for d in [
        app.config["STORAGE_ROOT"],
        app.config["UPLOADS_DIR"],
        app.config["BRANDING_DIR"],
        app.config["EXPORTS_DIR"],
        app.config["BACKUPS_DIR"],
        app.config["LOGS_DIR"],
        os.path.dirname(app.config["ALJOUD_DB_PATH"]),
    ]:
        os.makedirs(d, exist_ok=True)

    init_db(app)
    seed_if_empty(app)
    app.teardown_appcontext(close_db)

    @app.before_request
    def _load():
        load_current_user()

    @app.context_processor
    def _ctx():
        def setting(key: str, default: str = ""):
            db = get_db()
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default
        return {"t": t, "get_locale": get_locale, "setting": setting}

    from .blueprints import auth, admin, attendance, reports, api
    app.register_blueprint(auth.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(attendance.bp)
    app.register_blueprint(reports.bp)
    app.register_blueprint(api.bp, url_prefix="/api")

    @app.route("/")
    def index():
        from flask import redirect, url_for, g
        if getattr(g, "user", None):
            return redirect(url_for("attendance.pending"))
        return redirect(url_for("auth.login"))

    return app
