# routes/product_machines.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from db import get_db
from .settings import _bump_pricing_cache_version


product_machines_bp = Blueprint(
    "product_machines", __name__, template_folder="../templates/product_machines"
)


def _load_product_machines_context(product_id):
    with get_db() as cur:
        # product info
        cur.execute(
            """
            SELECT id, code, micron, stretchability_percent, is_prestretch
            FROM products
            WHERE id = %s
            """,
            (product_id,),
        )
        product = cur.fetchone()
        if not product:
            return None, None

        # machines for dropdown
        cur.execute("SELECT id, name FROM machines ORDER BY name")
        machines = cur.fetchall()

        # existing mappings
        cur.execute(
            """
            SELECT pm.id,
                   m.name,
                   pm.kwh_per_kg,
                   pm.monthly_product_capacity_kg,
                   pm.preferred_machine,
                   pm.machine_id
            FROM product_machines pm
            JOIN machines m ON pm.machine_id = m.id
            WHERE pm.product_id = %s
            ORDER BY m.name
            """,
            (product_id,),
        )
        mappings = cur.fetchall()

    return product, machines, mappings


@product_machines_bp.route("/product/<int:product_id>", methods=["GET"])
def edit_product_machines(product_id):
    product, machines, mappings = _load_product_machines_context(product_id)
    if not product:
        flash("Product not found.", "danger")
        return redirect(url_for("products.index"))

    return render_template(
        "product_machines/edit.html",
        product=product,
        machines=machines,
        mappings=mappings,
    )


@product_machines_bp.route("/product/<int:product_id>/add", methods=["POST"])
def add_mapping(product_id):
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    machine_id = int(request.form.get("machine_id") or 0)
    kwh_per_kg = float(request.form.get("kwh_per_kg") or 0)
    monthly_capacity = int(request.form.get("monthly_product_capacity_kg") or 0)
    preferred = request.form.get("preferred_machine") == "on"

    if machine_id <= 0 or kwh_per_kg <= 0 or monthly_capacity <= 0:
        msg = "Machine, kWh/kg and monthly capacity are required and must be > 0."
        if is_ajax:
            return jsonify({"error": msg}), 400
        flash(msg, "danger")
        return redirect(url_for("product_machines.edit_product_machines", product_id=product_id))

    with get_db() as cur:
        if preferred:
            # only one preferred machine per product
            cur.execute(
                "UPDATE product_machines SET preferred_machine = false WHERE product_id = %s",
                (product_id,),
            )

        cur.execute(
            """
            INSERT INTO product_machines
            (product_id, machine_id, preferred_machine, kwh_per_kg, monthly_product_capacity_kg)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (product_id, machine_id) DO UPDATE
            SET preferred_machine = EXCLUDED.preferred_machine,
                kwh_per_kg = EXCLUDED.kwh_per_kg,
                monthly_product_capacity_kg = EXCLUDED.monthly_product_capacity_kg
            """,
            (product_id, machine_id, preferred, kwh_per_kg, monthly_capacity),
        )
        
    _bump_pricing_cache_version()

    if not is_ajax:
        flash("Machine mapping saved.", "success")
        return redirect(url_for("product_machines.edit_product_machines", product_id=product_id))

    # ردّ AJAX: رجّع قائمة mappings كاملة
    product, machines, mappings = _load_product_machines_context(product_id)
    mappings_json = []
    for pm in mappings:
        mappings_json.append(
            {
                "id": pm[0],
                "machine_name": pm[1],
                "kwh_per_kg": float(pm[2] or 0),
                "monthly_capacity": int(pm[3] or 0),
                "preferred": bool(pm[4]),
                "machine_id": pm[5],
            }
        )

    return jsonify(
        {
            "message": "Machine mapping saved.",
            "mappings": mappings_json,
        }
    )


@product_machines_bp.route("/mapping/<int:mapping_id>/delete/<int:product_id>", methods=["POST", "GET"])
def delete_mapping(mapping_id, product_id):
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    with get_db() as cur:
        cur.execute("DELETE FROM product_machines WHERE id = %s", (mapping_id,))
        
    _bump_pricing_cache_version()

    if not is_ajax:
        flash("Mapping deleted.", "success")
        return redirect(url_for("product_machines.edit_product_machines", product_id=product_id))

    product, machines, mappings = _load_product_machines_context(product_id)
    mappings_json = []
    for pm in mappings:
        mappings_json.append(
            {
                "id": pm[0],
                "machine_name": pm[1],
                "kwh_per_kg": float(pm[2] or 0),
                "monthly_capacity": int(pm[3] or 0),
                "preferred": bool(pm[4]),
                "machine_id": pm[5],
            }
        )

    return jsonify(
        {
            "message": "Machine mapping deleted.",
            "mappings": mappings_json,
        }
    )
