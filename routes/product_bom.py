# routes/product_bom.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from db import get_db
from services.costing import get_material_landed_price_per_kg
from .settings import _bump_pricing_cache_version

product_bom_bp = Blueprint(
    "product_bom", __name__, template_folder="../templates/product_bom"
)


@product_bom_bp.route("/", methods=["GET"])
def overview():
    with get_db() as cur:
        cur.execute(
            """
            SELECT
                id,                     -- 0
                code,                   -- 1
                micron,                 -- 2
                stretchability_percent, -- 3
                bom_scrap_percent,      -- 4
                film_type               -- 5
            FROM products
            ORDER BY code
            """
        )
        products = cur.fetchall()

        cur.execute(
            """
            SELECT
                pb.product_id,
                COALESCE(SUM(pb.percentage), 0) AS total_pct,
                COALESCE(
                    SUM(pb.percentage * m.price_per_unit), 0
                ) AS base_cost_per_kg
            FROM product_bom pb
            JOIN materials m ON pb.material_id = m.id
            GROUP BY pb.product_id
            """
        )
        bom_summary_rows = cur.fetchall()

    base_cost_map = {
        row[0]: (float(row[1] or 0), float(row[2] or 0))
        for row in bom_summary_rows
    }

    bom_summary = {}
    for p in products:
        product_id = p[0]
        bom_scrap_percent = float(p[4] or 0)
        total_pct, base_cost = base_cost_map.get(product_id, (0.0, 0.0))

        eff_factor = 1 + (bom_scrap_percent / 100.0)
        total_cost_per_kg = base_cost * eff_factor

        bom_summary[product_id] = {
            "total_pct": total_pct,
            "total_cost": total_cost_per_kg,
        }

    return render_template(
        "product_bom/overview.html",
        products=products,
        bom_summary=bom_summary,
    )


def _load_bom_context(product_id):
    """Helper: load product, materials, bom_items and cost summary."""
    with get_db() as cur:
        cur.execute(
            """
            SELECT id, code, micron, stretchability_percent, bom_scrap_percent
            FROM products
            WHERE id = %s
            """,
            (product_id,),
        )
        product = cur.fetchone()
        if not product:
            return None, None, None, 0.0, 0.0, 0.0, 0.0

        bom_scrap_percent = float(product[4] or 0)

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
                pb.id,             -- 0
                pb.material_id,    -- 1
                m.code,            -- 2
                m.name,            -- 3
                m.category,        -- 4
                pb.percentage,     -- 5
                pb.scrap_percent,  -- 6
                m.unit,            -- 7
                m.price_per_unit   -- 8 (per kg)
            FROM product_bom pb
            JOIN materials m ON pb.material_id = m.id
            WHERE pb.product_id = %s
            ORDER BY m.category, m.code
            """,
            (product_id,),
        )
        bom_items = cur.fetchall()

    # نفس منطق _load_bom_tab في product_settings:
    total_pct = 0.0
    base_cost = 0.0
    for item in bom_items:
        pct = float(item[5] or 0)
        price = float(item[8] or 0)  # سعر المادة العادي
        base_cost += pct * price
        total_pct += pct

    eff_factor = 1 + (bom_scrap_percent / 100.0)
    total_cost = base_cost * eff_factor  # cost مواد + سكرب فقط

    total_cost_per_kg = 0.0
    for item in bom_items:
        pct = float(item[5] or 0)
        material_id = int(item[1])
        landed_price = get_material_landed_price_per_kg(material_id)
        total_cost_per_kg += pct * landed_price
    total_cost_per_kg *= eff_factor  # landed cost (زي شاشة البرودكت)

    return product, materials, bom_items, bom_scrap_percent, total_pct, total_cost, total_cost_per_kg

@product_bom_bp.route("/product/<int:product_id>", methods=["GET", "POST"])
def edit_bom(product_id):
    # GET
    if request.method == "GET":
        (
            product,
            materials,
            bom_items,
            bom_scrap_percent,
            total_pct,
            total_cost,
            total_cost_per_kg,
        ) = _load_bom_context(product_id)
        if not product:
            flash("Product not found.", "danger")
            return redirect(url_for("products.index"))

        return render_template(
            "product_bom/edit.html",
            product=product,
            materials=materials,
            bom_items=bom_items,
            bom_scrap_percent=bom_scrap_percent,
            total_pct=total_pct,
            total_cost=total_cost,
            total_cost_per_kg=total_cost_per_kg,
        )

    # POST: إضافة/تعديل سطر BOM (يدعم AJAX و non-AJAX)
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    material_id = int(request.form.get("material_id") or 0)
    percentage_input = float(request.form.get("percentage") or 0)

    if material_id <= 0 or percentage_input <= 0:
        msg = "Material and percentage are required."
        if is_ajax:
            return jsonify({"error": msg}), 400
        flash(msg, "danger")
        return redirect(url_for("product_bom.edit_bom", product_id=product_id))

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

    if not is_ajax:
        flash("BOM item saved.", "success")
        return redirect(url_for("product_bom.edit_bom", product_id=product_id))

    # ردّ AJAX: نرجع البيانات الجديدة للجدول
    (
        product,
        materials,
        bom_items,
        bom_scrap_percent,
        total_pct,
        total_cost,
        total_cost_per_kg,
    ) = _load_bom_context(product_id)

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
                "percentage": pct * 100.0,  # %
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

@product_bom_bp.route("/item/<int:item_id>/delete/<int:product_id>", methods=["POST", "GET"])
def delete_bom_item(item_id, product_id):
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    with get_db() as cur:
        cur.execute("DELETE FROM product_bom WHERE id = %s", (item_id,))

    _bump_pricing_cache_version()

    if not is_ajax:
        flash("BOM item deleted.", "success")
        return redirect(url_for("product_bom.edit_bom", product_id=product_id))

    # ردّ AJAX بعد الحذف
    (
        product,
        materials,
        bom_items,
        bom_scrap_percent,
        total_pct,
        total_cost,
        total_cost_per_kg,
    ) = _load_bom_context(product_id)

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