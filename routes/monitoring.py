from flask import Blueprint, render_template, abort
from flask_login import login_required, current_user
from db import get_db

monitoring_bp = Blueprint("monitoring", __name__, template_folder="../templates")

@monitoring_bp.route("/monitoring-report")
@login_required
def monitoring_report():
    print("AUTH?", current_user.is_authenticated, current_user.id, current_user.role)
    return "TEST MONITORING"