# routes/product_settings.py
from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from db import get_db
from services.costing import get_material_landed_price_per_kg
from .settings import _bump_pricing_cache_version

product_settings_bp = Blueprint(
    "product_settings", __name__, template_folder="../templates/product_settings"
)


def _load_product_context(product_id):
    with get_db() as cur:
        cur.execute(
            """
            SELECT
                id,                      -- 0
                code,                    -- 1
                micron,                  -- 2
                stretchability_percent,  -- 3
                is_prestretch,           -- 4
                bom_scrap_percent        -- 5
            FROM products
            WHERE id = %s
            """,
            (product_id,),
        )
        product = cur.fetchone()
    return product


def _load_machines_tab(product_id):
    with get_db() as cur:
        cur.execute("SELECT id, name FROM machines ORDER BY name")
        machines = cur.fetchall()

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
    return machines, mappings


def _load_bom_tab(product_id, product):
    with get_db() as cur:
        cur.execute(
            """
            SELECT id, code, name, category, unit, price_per_unit
            FROM materials
            ORDER BY category, code
            """
        )
        materials = cur.fetchall()

        cur.execute(
            """
            SELECT
                pb.id,              -- 0
                pb.material_id,     -- 1
                m.code,             -- 2
                m.name,             -- 3
                m.category,         -- 4
                pb.percentage,      -- 5 (0.70)
                pb.scrap_percent,   -- 6
                m.unit,             -- 7
                m.price_per_unit    -- 8 (per kg)
            FROM product_bom pb
            JOIN materials m ON pb.material_id = m.id
            WHERE pb.product_id = %s
            ORDER BY m.category, m.code
            """,
            (product_id,),
        )
        bom_items = cur.fetchall()

    bom_scrap_percent = float(product[5] or 0)

    total_pct = 0.0
    base_cost = 0.0
    for item in bom_items:
        pct = float(item[5] or 0)
        price = float(item[8] or 0)
        base_cost += pct * price
        total_pct += pct

    eff_factor = 1 + (bom_scrap_percent / 100.0)
    total_cost = base_cost * eff_factor

    total_cost_per_kg = 0.0
    for item in bom_items:
        pct = float(item[5] or 0)
        material_id = int(item[1])
        price = get_material_landed_price_per_kg(material_id)
        total_cost_per_kg += pct * price
    total_cost_per_kg *= eff_factor

    return materials, bom_items, bom_scrap_percent, total_pct, total_cost, total_cost_per_kg


@product_settings_bp.route("/product/<int:product_id>/settings", methods=["GET"])
def index(product_id):
    product = _load_product_context(product_id)
    if not product:
        flash("Product not found.", "danger")
        return redirect(url_for("products.index"))

    machines, mappings = _load_machines_tab(product_id)
    materials, bom_items, bom_scrap_percent, total_pct, total_cost, total_cost_per_kg = _load_bom_tab(
        product_id, product
    )

    return render_template(
        "product_settings/index.html",
        product=product,
        machines=machines,
        mappings=mappings,
        materials=materials,
        bom_items=bom_items,
        total_pct=total_pct,
        total_cost=total_cost,
        bom_scrap_percent=bom_scrap_percent,
        total_cost_per_kg=total_cost_per_kg,
    )


@product_settings_bp.route("/product/<int:product_id>/settings/machines", methods=["POST"])
def machines_save(product_id):
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if not is_ajax:
        return redirect(url_for("product_settings.index", product_id=product_id))

    machine_id = int(request.form.get("machine_id") or 0)
    kwh_per_kg = float(request.form.get("kwh_per_kg") or 0)
    monthly_capacity = int(request.form.get("monthly_product_capacity_kg") or 0)
    preferred = request.form.get("preferred_machine") == "on"

    if machine_id <= 0 or kwh_per_kg <= 0 or monthly_capacity <= 0:
        return jsonify({"error": "Machine, kWh/kg and monthly capacity must be > 0."}), 400

    with get_db() as cur:
        if preferred:
            cur.execute(
                """
                UPDATE product_machines
                SET preferred_machine = false
                WHERE product_id = %s
                """,
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

    machines, mappings = _load_machines_tab(product_id)
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

    return jsonify({"message": "Machine mapping saved.", "mappings": mappings_json})


@product_settings_bp.route(
    "/product/<int:product_id>/settings/machines/<int:mapping_id>/delete",
    methods=["POST"],
)
def machines_delete(product_id, mapping_id):
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if not is_ajax:
        return redirect(url_for("product_settings.index", product_id=product_id))

    with get_db() as cur:
        cur.execute("DELETE FROM product_machines WHERE id = %s", (mapping_id,))
        
    _bump_pricing_cache_version()

    machines, mappings = _load_machines_tab(product_id)
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

    return jsonify({"message": "Machine mapping deleted.", "mappings": mappings_json})


@product_settings_bp.route("/product/<int:product_id>/settings/bom", methods=["POST"])
def bom_save(product_id):
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if not is_ajax:
        return redirect(url_for("product_settings.index", product_id=product_id))

    material_id = int(request.form.get("material_id") or 0)
    percentage_input = float(request.form.get("percentage") or 0)

    if material_id <= 0 or percentage_input <= 0:
        return jsonify({"error": "Material and percentage are required."}), 400

    percentage = percentage_input / 100.0
    scrap_percent = 0.0

    with get_db() as cur:
        cur.execute(
            """
            INSERT INTO product_bom (product_id, material_id, percentage, scrap_percent)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (product_id, material_id) DO UPDATE
            SET percentage = EXCLUDED.percentage,
                scrap_percent = EXCLUDED.scrap_percent
            """,
            (product_id, material_id, percentage, scrap_percent),
        )
        
    _bump_pricing_cache_version()

    product = _load_product_context(product_id)
    if not product:
        return jsonify({"error": "Product not found"}), 404

    materials, bom_items, bom_scrap_percent, total_pct, total_cost, total_cost_per_kg = _load_bom_tab(
        product_id, product
    )

    items_json = []
    for item in bom_items:
        pct = float(item[5] or 0)
        price = float(item[8] or 0)
        contrib = pct * price
        items_json.append(
            {
                "id": item[0],
                "material_id": item[1],
                "material_code": item[2],
                "material_name": item[3],
                "category": item[4],
                "percentage": pct * 100.0,
                "price_per_kg": price,
                "contribution": contrib,
            }
        )

    return jsonify(
        {
            "message": "BOM item saved.",
            "bom_scrap_percent": bom_scrap_percent,
            "total_pct": total_pct * 100.0,
            "total_cost": total_cost,
            "total_cost_per_kg": total_cost_per_kg,
            "items": items_json,
        }
    )


@product_settings_bp.route(
    "/product/<int:product_id>/settings/bom/<int:item_id>/delete",
    methods=["POST"],
)
def bom_delete(product_id, item_id):
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if not is_ajax:
        return redirect(url_for("product_settings.index", product_id=product_id))

    with get_db() as cur:
        cur.execute("DELETE FROM product_bom WHERE id = %s", (item_id,))
        
    _bump_pricing_cache_version()

    product = _load_product_context(product_id)
    if not product:
        return jsonify({"error": "Product not found"}), 404

    materials, bom_items, bom_scrap_percent, total_pct, total_cost, total_cost_per_kg = _load_bom_tab(
        product_id, product
    )

    items_json = []
    for item in bom_items:
        pct = float(item[5] or 0)
        price = float(item[8] or 0)
        contrib = pct * price
        items_json.append(
            {
                "id": item[0],
                "material_id": item[1],
                "material_code": item[2],
                "material_name": item[3],
                "category": item[4],
                "percentage": pct * 100.0,
                "price_per_kg": price,
                "contribution": contrib,
            }
        )

    return jsonify(
        {
            "message": "BOM item deleted.",
            "bom_scrap_percent": bom_scrap_percent,
            "total_pct": total_pct * 100.0,
            "total_cost": total_cost,
            "total_cost_per_kg": total_cost_per_kg,
            "items": items_json,
        }
    )
