# routes/product_settings.py
from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from db import get_db
from services.costing import get_material_landed_price_per_kg
from .settings import _bump_pricing_cache_version
from flask_login import current_user

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


def _load_bom_tab(product_id, product, roll_bom_id=None):
    with get_db() as cur:
        # المواد
        cur.execute(
            """
            SELECT id, code, name, category, unit, price_per_unit
            FROM materials
            ORDER BY category, code
            """
        )
        materials = cur.fetchall()

        # رول BOMs
        cur.execute(
            """
            SELECT
                id,
                label,
                weight_from_kg,
                weight_to_kg,
                is_active
            FROM product_roll_boms
            WHERE product_id = %s
            ORDER BY weight_from_kg, weight_to_kg, id
            """,
            (product_id,),
        )
        roll_boms = cur.fetchall()

        selected_roll_bom = None
        roll_bom_items = []
        total_pct = 0.0
        total_cost = 0.0
        total_cost_per_kg = 0.0
        bom_scrap_percent = float(product[5] or 0)

        if roll_boms:
            # لو roll_bom_id متبعت، حاول تختاره؛ لو مش موجود اختار أول واحد
            if roll_bom_id:
                for rb in roll_boms:
                    if rb[0] == roll_bom_id:
                        selected_roll_bom = rb
                        break
            if selected_roll_bom is None:
                selected_roll_bom = roll_boms[0]

            selected_roll_bom_id = selected_roll_bom[0]

            cur.execute(
                """
                SELECT
                    pri.id,             -- 0
                    pri.material_id,    -- 1
                    m.code,             -- 2
                    m.name,             -- 3
                    m.category,         -- 4
                    pri.percentage,     -- 5
                    pri.scrap_percent,  -- 6
                    m.unit,             -- 7
                    m.price_per_unit    -- 8
                FROM product_roll_bom_items pri
                JOIN materials m ON pri.material_id = m.id
                WHERE pri.roll_bom_id = %s
                ORDER BY m.category, m.code
                """,
                (selected_roll_bom_id,),
            )
            roll_bom_items = cur.fetchall()

            base_cost = 0.0
            total_pct = 0.0
            for item in roll_bom_items:
                pct = float(item[5] or 0)
                price = float(item[8] or 0)
                base_cost += pct * price
                total_pct += pct

            eff_factor = 1 + (bom_scrap_percent / 100.0)
            total_cost = base_cost * eff_factor

            total_cost_per_kg = 0.0
            for item in roll_bom_items:
                pct = float(item[5] or 0)
                material_id = int(item[1])
                price = get_material_landed_price_per_kg(material_id)
                total_cost_per_kg += pct * price
            total_cost_per_kg *= eff_factor

    return (
        materials,
        roll_boms,
        selected_roll_bom,
        roll_bom_items,
        bom_scrap_percent,
        total_pct,
        total_cost,
        total_cost_per_kg,
    )


@product_settings_bp.route("/product/<int:product_id>/settings", methods=["GET"])
def index(product_id):
    product = _load_product_context(product_id)
    if not product:
        flash("Product not found.", "danger")
        return redirect(url_for("products.index"))

    roll_bom_id = request.args.get("roll_bom_id", type=int)
    show_bom_only = bool(request.args.get("show_bom_only", type=int))

    machines, mappings = _load_machines_tab(product_id)
    (
        materials,
        roll_boms,
        selected_roll_bom,
        roll_bom_items,
        bom_scrap_percent,
        total_pct,
        total_cost,
        total_cost_per_kg,
    ) = _load_bom_tab(product_id, product, roll_bom_id=roll_bom_id)

    # تحميل بيانات السيمي + البروفايلات + الرولز
    with get_db() as cur:
        # 1) سجل السيمي (لو موجود)
        cur.execute(
            """
            SELECT
                id,
                product_id,
                gross_kg_per_roll,
                core_kg_per_roll,
                rolls_per_pallet,
                packing_profile_id,
                pricing_rule_id,
                is_active,
                COALESCE(notes, '')
            FROM product_semis
            WHERE product_id = %s
            """,
            (product_id,),
        )
        product_semi = cur.fetchone()

        # 2) كل البروفايلات الفعالة للباكنج
        cur.execute(
            """
            SELECT
                id,
                name,
                packing_type_id,
                pallet_type_id,
                is_global,
                is_active
            FROM packing_profiles
            WHERE is_active = TRUE
            ORDER BY name
            """
        )
        packing_profiles_for_semi = cur.fetchall()

        # 3) كل الـ pricing_rules للفيلم type = 'prestretch'
        cur.execute(
            """
            SELECT
                id,
                micron_min,
                micron_max,
                film_type,
                packing_type_id,
                roll_weight_min,
                roll_weight_max,
                margin_percent
            FROM pricing_rules
            ORDER BY film_type, micron_min, roll_weight_min, id
            """
        )
        pricing_rules_for_semi = cur.fetchall()

    return render_template(
        "product_settings/index.html",
        product=product,
        machines=machines,
        mappings=mappings,
        materials=materials,
        roll_boms=roll_boms,
        selected_roll_bom=selected_roll_bom,
        bom_items=roll_bom_items,
        total_pct=total_pct,
        total_cost=total_cost,
        bom_scrap_percent=bom_scrap_percent,
        total_cost_per_kg=total_cost_per_kg,
        show_bom_only=show_bom_only,
        product_semi=product_semi,
        packing_profiles_for_semi=packing_profiles_for_semi,
        pricing_rules_for_semi=pricing_rules_for_semi,
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

@product_settings_bp.route("/product/<int:product_id>/settings/roll-bom", methods=["POST"])
def roll_bom_create(product_id):
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if not is_ajax:
        return redirect(url_for("product_settings.index", product_id=product_id))

    label = (request.form.get("label") or "").strip()
    weight_from_kg = float(request.form.get("weight_from_kg") or 0)
    weight_to_kg = float(request.form.get("weight_to_kg") or 0)

    if weight_from_kg < 0 or weight_to_kg < 0:
        return jsonify({"error": "Weights must be >= 0."}), 400
    if weight_to_kg and weight_to_kg < weight_from_kg:
        return jsonify({"error": "To kg must be >= From kg."}), 400

    with get_db() as cur:
        cur.execute(
            """
            INSERT INTO product_roll_boms (product_id, label, weight_from_kg, weight_to_kg, is_active)
            VALUES (%s, %s, %s, %s, TRUE)
            RETURNING id
            """,
            (product_id, label or None, weight_from_kg, weight_to_kg),
        )
        new_id = cur.fetchone()[0]

    _bump_pricing_cache_version()

    # رجّع قائمة roll_boms المحدثة
    product = _load_product_context(product_id)
    (
        materials,
        roll_boms,
        selected_roll_bom,
        roll_bom_items,
        bom_scrap_percent,
        total_pct,
        total_cost,
        total_cost_per_kg,
    ) = _load_bom_tab(product_id, product)

    roll_boms_json = []
    for rb in roll_boms:
        rb_id, rb_label, w_from, w_to, is_active = rb
        roll_boms_json.append(
            {
                "id": rb_id,
                "label": rb_label or "",
                "weight_from_kg": float(w_from or 0),
                "weight_to_kg": float(w_to or 0),
                "is_active": bool(is_active),
            }
        )

    return jsonify(
        {
            "message": "Roll BOM created.",
            "roll_boms": roll_boms_json,
            "selected_roll_bom_id": new_id,
        }
    )
    
@product_settings_bp.route(
    "/product/<int:product_id>/settings/roll-bom/<int:roll_bom_id>/details",
    methods=["GET"],
)
def roll_bom_details(product_id, roll_bom_id):
    """
    يرجع تفاصيل الـ BOM (items + totals + cost) لرول بوم معينة كـ JSON،
    بنفس الـ structure اللي renderBomTable في الواجهة يتوقعه.
    """
    product = _load_product_context(product_id)
    if not product:
        return jsonify({"error": "Product not found"}), 404

    (
        materials,
        roll_boms,
        selected_roll_bom,
        roll_bom_items,
        bom_scrap_percent,
        total_pct,
        total_cost,
        total_cost_per_kg,
    ) = _load_bom_tab(product_id, product, roll_bom_id=roll_bom_id)

    # لو roll_bom_id مش بتاع المنتج ده أو مش موجود
    if not selected_roll_bom or int(selected_roll_bom[0]) != int(roll_bom_id):
        return jsonify({"error": "Roll BOM not found for this product."}), 404

    items_json = []
    for item in roll_bom_items:
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
            "items": items_json,
            "bom_scrap_percent": float(bom_scrap_percent or 0),
            "total_pct": float(total_pct or 0) * 100.0,
            "total_cost": float(total_cost or 0),
            "total_cost_per_kg": float(total_cost_per_kg or 0),
        }
    )


@product_settings_bp.route("/product/<int:product_id>/settings/bom", methods=["POST"])
def bom_save(product_id):
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if not is_ajax:
        return redirect(url_for("product_settings.index", product_id=product_id))

    roll_bom_id = int(request.form.get("roll_bom_id") or 0)
    material_id = int(request.form.get("material_id") or 0)
    percentage_input = float(request.form.get("percentage") or 0)

    if roll_bom_id <= 0:
        return jsonify({"error": "Please select or create a roll BOM first."}), 400

    if material_id <= 0 or percentage_input <= 0:
        return jsonify({"error": "Material and percentage are required."}), 400

    percentage = percentage_input / 100.0
    scrap_percent = 0.0

    with get_db() as cur:
        # تأكد إن الـ roll_bom فعلاً للـ product ده
        cur.execute(
            "SELECT product_id FROM product_roll_boms WHERE id = %s",
            (roll_bom_id,),
        )
        row_rb = cur.fetchone()
        if not row_rb or int(row_rb[0]) != int(product_id):
            return jsonify({"error": "Invalid roll BOM for this product."}), 400

        cur.execute(
            """
            INSERT INTO product_roll_bom_items (roll_bom_id, material_id, percentage, scrap_percent)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (roll_bom_id, material_id) DO UPDATE
            SET percentage = EXCLUDED.percentage,
                scrap_percent = EXCLUDED.scrap_percent
            """,
            (roll_bom_id, material_id, percentage, scrap_percent),
        )

    _bump_pricing_cache_version()

    product = _load_product_context(product_id)
    if not product:
        return jsonify({"error": "Product not found"}), 404

    (
        materials,
        roll_boms,
        selected_roll_bom,
        roll_bom_items,
        bom_scrap_percent,
        total_pct,
        total_cost,
        total_cost_per_kg,
    ) = _load_bom_tab(product_id, product, roll_bom_id=roll_bom_id)

    # نضمن إن _load_bom_tab رجّع نفس roll_bom_id كمختار (هنضبطها في UI لاحقًا)
    items_json = []
    for item in roll_bom_items:
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
        # نجيب roll_bom_id للتأكد وربطه بالمنتج
        cur.execute(
            """
            SELECT pri.roll_bom_id, prb.product_id
            FROM product_roll_bom_items pri
            JOIN product_roll_boms prb ON pri.roll_bom_id = prb.id
            WHERE pri.id = %s
            """,
            (item_id,),
        )
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "BOM item not found."}), 404

        roll_bom_id, rb_product_id = int(row[0]), int(row[1])
        if rb_product_id != int(product_id):
            return jsonify({"error": "Invalid BOM item for this product."}), 400

        cur.execute(
            "DELETE FROM product_roll_bom_items WHERE id = %s",
            (item_id,),
        )

    _bump_pricing_cache_version()

    product = _load_product_context(product_id)
    if not product:
        return jsonify({"error": "Product not found"}), 404

    (
        materials,
        roll_boms,
        selected_roll_bom,
        roll_bom_items,
        bom_scrap_percent,
        total_pct,
        total_cost,
        total_cost_per_kg,
    ) = _load_bom_tab(product_id, product, roll_bom_id=roll_bom_id)

    items_json = []
    for item in roll_bom_items:
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
    
@product_settings_bp.route("/product/<int:product_id>/settings/semi/save", methods=["POST"])
def semi_save(product_id):
    # نسمح فقط للـ admin و owner
    if current_user.role not in ("admin", "owner"):
        flash("You are not allowed to edit semi settings.", "danger")
        return redirect(url_for("product_settings.index", product_id=product_id))

    gross_kg_per_roll = (request.form.get("gross_kg_per_roll") or "").strip()
    core_kg_per_roll  = (request.form.get("core_kg_per_roll") or "").strip()
    rolls_per_pallet  = (request.form.get("rolls_per_pallet") or "").strip()
    packing_profile_id = int(request.form.get("packing_profile_id") or 0)
    pricing_rule_id    = int(request.form.get("pricing_rule_id") or 0)
    is_active          = bool(request.form.get("is_active"))
    notes              = (request.form.get("notes") or "").strip()

    error = None

    # فاليديشن للأوزان
    try:
        gross_val = float(gross_kg_per_roll)
        core_val  = float(core_kg_per_roll)
        if gross_val <= 0 or core_val < 0 or core_val >= gross_val:
            raise ValueError()
    except ValueError:
        error = "Invalid gross/core roll weights."

    # فاليديشن لعدد الرولات
    if not error:
        try:
            rolls_val = int(rolls_per_pallet)
            if rolls_val <= 0:
                raise ValueError()
        except ValueError:
            error = "Rolls per pallet must be a positive integer."

    # اختيار البروفايل
    if not error and packing_profile_id <= 0:
        error = "Please select packing profile."

    # اختيار قاعدة المارجن
    if not error and pricing_rule_id <= 0:
        error = "Please select pricing rule (margin)."

    if error:
        flash(error, "danger")
        return redirect(url_for("product_settings.index", product_id=product_id))

    with get_db() as cur:
        # هل يوجد سيمي مسبقًا؟
        cur.execute(
            "SELECT id FROM product_semis WHERE product_id = %s",
            (product_id,),
        )
        row = cur.fetchone()

        if row:
            # تحديث
            cur.execute(
                """
                UPDATE product_semis
                SET gross_kg_per_roll = %s,
                    core_kg_per_roll  = %s,
                    rolls_per_pallet  = %s,
                    packing_profile_id = %s,
                    pricing_rule_id    = %s,
                    is_active          = %s,
                    notes              = %s
                WHERE product_id = %s
                """,
                (
                    gross_val,
                    core_val,
                    rolls_val,
                    packing_profile_id,
                    pricing_rule_id,
                    is_active,
                    notes or None,
                    product_id,
                ),
            )
        else:
            # إدراج جديد
            cur.execute(
                """
                INSERT INTO product_semis (
                    product_id,
                    gross_kg_per_roll,
                    core_kg_per_roll,
                    rolls_per_pallet,
                    packing_profile_id,
                    pricing_rule_id,
                    is_active,
                    notes
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    product_id,
                    gross_val,
                    core_val,
                    rolls_val,
                    packing_profile_id,
                    pricing_rule_id,
                    is_active,
                    notes or None,
                ),
            )

    _bump_pricing_cache_version()

    flash("Semi settings saved.", "success")
    return redirect(url_for("product_settings.index", product_id=product_id))