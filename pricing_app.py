# pricing_app.py
import os
from flask import Flask, redirect, url_for, jsonify
from dotenv import load_dotenv
from db import get_db
from routes.pricing import load_pricing_static_data

from routes.machines import machines_bp
from routes.products import products_bp
from routes.product_machines import product_machines_bp
from routes.materials import materials_bp
from routes.product_bom import product_bom_bp
from routes.product_settings import product_settings_bp
from routes.pricing import pricing_bp
from routes.settings import settings_bp
from routes.auth import auth_bp, login_manager  # <<<<<< إضافة

from routes.monitoring import monitoring_bp

load_dotenv()


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev")

    # تهيئة Flask-Login
    login_manager.init_app(app)

    # Blueprints (الإعدادات)
    app.register_blueprint(machines_bp, url_prefix="/machines")
    app.register_blueprint(products_bp, url_prefix="/products")
    app.register_blueprint(product_machines_bp, url_prefix="/product-machines")
    app.register_blueprint(materials_bp, url_prefix="/materials")
    app.register_blueprint(product_bom_bp, url_prefix="/product-bom")
    app.register_blueprint(product_settings_bp, url_prefix="/product-settings")

    # Settings
    app.register_blueprint(settings_bp, url_prefix="/settings")

    # شاشة التسعير
    app.register_blueprint(pricing_bp)  # /pricing

    # Auth (login/logout)
    app.register_blueprint(auth_bp, url_prefix="/auth")
    
    # Monitoring report
    app.register_blueprint(monitoring_bp)

    # ===== pre-warm =====
    with app.app_context():
        with get_db() as cur:
            cur.execute(
                """
                SELECT egp_per_usd
                FROM currency_rates
                WHERE is_active = true
                ORDER BY id DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
            egp_per_usd = float(row[0]) if row else 0.0
            load_pricing_static_data(cur, egp_per_usd)
    # ===== نهاية pre-warm =====

    @app.route("/")
    def index():
        return redirect(url_for("pricing.pricing_screen"))
    
    @app.route("/health", methods=["GET"])
    def health_check():
        return jsonify(status="ok"), 200

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True, port=5001)