"""
app.py
──────
Flask application factory.
Import and call create_app() to get a configured Flask instance.
"""

from __future__ import annotations

import os

from flask import Flask, render_template


def create_app() -> Flask:
    """Create and configure the Flask app with all blueprints registered."""
    # Point Flask at the templates/ folder inside this package
    app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(__file__), "templates"),
    )

    # ── Register blueprints ────────────────────────────────────────────────────
    from .routes.config_routes      import bp as config_bp
    from .routes.sync_routes        import bp as sync_bp
    from .routes.task_routes        import bp as task_bp
    from .routes.dashboard_routes   import bp as dashboard_bp
    from .routes.student_routes     import bp as student_bp
    from .routes.orphan_routes      import bp as orphan_bp
    from .routes.work_type_routes   import bp as work_type_bp
    from .routes.health_check_routes import bp as health_check_bp
    from .routes.recode_routes      import bp as recode_bp

    app.register_blueprint(config_bp)
    app.register_blueprint(sync_bp)
    app.register_blueprint(task_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(student_bp)
    app.register_blueprint(orphan_bp)
    app.register_blueprint(work_type_bp)
    app.register_blueprint(health_check_bp)
    app.register_blueprint(recode_bp)

    # ── Main UI route ──────────────────────────────────────────────────────────
    @app.route("/")
    def index():
        return render_template("index.html")

    return app
