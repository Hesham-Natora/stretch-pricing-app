from flask import Blueprint, render_template, request, redirect, url_for, flash
from db import get_db
from .settings import _bump_pricing_cache_version

machines_bp = Blueprint("machines", __name__, template_folder="../templates/machines")


@machines_bp.route("/", methods=["GET"])
def index():
    with get_db() as cur:
        cur.execute(
            "SELECT id, name, utilization_rate FROM machines ORDER BY name"
        )
        machines = cur.fetchall()
    return render_template("machines/index.html", machines=machines)


@machines_bp.route("/edit/<int:machine_id>", methods=["GET", "POST"])
def edit(machine_id):
    if request.method == "POST":
        name = request.form["name"].strip()
        utilization_rate = float(request.form["utilization_rate"] or 0) / 100.0

        if not name:
            flash("Machine name is required.", "danger")
            return redirect(request.url)

        with get_db() as cur:
            if machine_id == 0:
                cur.execute(
                    """
                    INSERT INTO machines (name, utilization_rate)
                    VALUES (%s, %s)
                    """,
                    (name, utilization_rate),
                )
                flash(f'Machine "{name}" created.', "success")
            else:
                cur.execute(
                    """
                    UPDATE machines
                    SET name = %s,
                        utilization_rate = %s
                    WHERE id = %s
                    """,
                    (name, utilization_rate, machine_id),
                )
                flash("Machine updated.", "success")
                
        _bump_pricing_cache_version()

        return redirect(url_for("machines.index"))

    machine = None
    if machine_id != 0:
        with get_db() as cur:
            cur.execute(
                "SELECT id, name, utilization_rate FROM machines WHERE id = %s",
                (machine_id,),
            )
            machine = cur.fetchone()

    return render_template("machines/form.html", machine=machine)


@machines_bp.route("/delete/<int:machine_id>")
def delete(machine_id):
    with get_db() as cur:
        cur.execute("SELECT name FROM machines WHERE id = %s", (machine_id,))
        row = cur.fetchone()
        if row:
            name = row[0]
            cur.execute("DELETE FROM machines WHERE id = %s", (machine_id,))
            flash(f'Machine "{name}" deleted.', "success")
            
            _bump_pricing_cache_version()
            
        else:
            flash("Machine not found.", "danger")
    return redirect(url_for("machines.index"))
