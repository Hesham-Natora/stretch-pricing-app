# routes/materials.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from db import get_db
from pricing_cache import invalidate_material
from routes.auth import roles_required
from .settings import _bump_pricing_cache_version

materials_bp = Blueprint(
    "materials", __name__, template_folder="../templates/materials"
)

def generate_material_code(cur):
    """
    يولّد كود جديد بالشكل MAT-0001 بناء على آخر كود موجود.
    اليوزر ما بيدخلش أي كود.
    """
    cur.execute(
        "SELECT code FROM materials WHERE code LIKE 'MAT-%' ORDER BY code DESC LIMIT 1"
    )
    row = cur.fetchone()
    if not row:
        next_num = 1
    else:
        last_code = row[0]          # MAT-0012
        try:
            last_num = int(last_code.split("-")[1])
        except (IndexError, ValueError):
            last_num = 0
        next_num = last_num + 1
    return f"MAT-{next_num:04d}"

def _load_all_materials():
    with get_db() as cur:
        cur.execute(
            """
            SELECT id, code, name, category, unit, unit_type, currency, price_per_unit
            FROM materials
            ORDER BY category, code
            """
        )
        materials = cur.fetchall()
    return materials

def _material_row_to_dict(m):
    """m: 0-id,1-code,2-name,3-category,4-unit,5-unit_type,6-currency,7-price_per_unit"""
    unit_type = m[5]
    currency = m[6]
    price = float(m[7] or 0)
    display_unit = "kg" if unit_type == "weight" else m[4]
    return {
        "id": m[0],
        "code": m[1],
        "name": m[2],
        "category": m[3],
        "unit": display_unit,
        "unit_type": unit_type,
        "raw_unit": m[4],
        "currency": currency,
        "price": price,
    }

@materials_bp.route("/", methods=["GET"])
@login_required
@roles_required("admin", "owner", "sales_manager")
def index():
    materials = _load_all_materials()
    return render_template("materials/index.html", materials=materials)

@materials_bp.route("/delete/<int:material_id>", methods=["POST", "GET"])
@login_required
@roles_required("admin", "owner")
def delete(material_id):
    """حذف كلاسيك مع redirect (احتياطي لو حد استخدم لينك قديم)."""
    with get_db() as cur:
        cur.execute("DELETE FROM materials WHERE id = %s", (material_id,))
    invalidate_material(material_id)
    _bump_pricing_cache_version()
    flash("Material deleted.", "success")
    return redirect(url_for("materials.index"))

@materials_bp.route("/delete_ajax/<int:material_id>", methods=["POST"])
@login_required
@roles_required("admin", "owner")
def delete_ajax(material_id):
    """حذف عن طريق AJAX يرجّع جدول المواد كـ JSON."""
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if not is_ajax:
        return redirect(url_for("materials.index"))

    with get_db() as cur:
        cur.execute("DELETE FROM materials WHERE id = %s", (material_id,))
    invalidate_material(material_id)
    _bump_pricing_cache_version()

    materials = _load_all_materials()
    materials_json = [_material_row_to_dict(m) for m in materials]

    return jsonify(
        {
            "message": "Material deleted.",
            "materials": materials_json,
        }
    )

@materials_bp.route("/get/<int:material_id>", methods=["GET"])
@login_required
@roles_required("admin", "owner", "sales_manager")
def get_material(material_id):
    """جلب بيانات مادة واحدة للـ modal (AJAX GET)."""
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if not is_ajax:
        return redirect(url_for("materials.index"))

    with get_db() as cur:
        cur.execute(
            """
            SELECT id, code, name, category, unit, unit_type, currency, price_per_unit
            FROM materials
            WHERE id = %s
            """,
            (material_id,),
        )
        m = cur.fetchone()

    if not m:
        return jsonify({"error": "Material not found"}), 404

    data = _material_row_to_dict(m)

    # display_price زي logic القديم (لو unit_type وزن ووحدة Ton رجّع السعر *1000)
    unit_type = m[5]
    unit = m[4]
    stored_price = float(m[7] or 0)
    if unit_type == "weight" and unit.lower() in ("ton", "tonne", "t"):
        display_price = stored_price * 1000.0
        display_unit = "Ton"
    else:
        display_price = stored_price
        display_unit = unit

    data["display_price"] = display_price
    data["input_unit"] = display_unit

    return jsonify(data)

@materials_bp.route("/save_ajax", methods=["POST"])
@login_required
@roles_required("admin", "owner", "sales_manager")
def save_ajax():
    """حفظ مادة من خلال الـ modal بـ AJAX بنفس منطق التخزين القديم."""
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if not is_ajax:
        return redirect(url_for("materials.index"))

    material_id = int(request.form.get("id") or 0)
    name = (request.form.get("name") or "").strip()
    category = (request.form.get("category") or "").strip()
    unit = (request.form.get("unit") or "").strip() or "kg"
    unit_type = (request.form.get("unit_type") or "").strip() or "weight"
    currency = (request.form.get("currency") or "").strip() or "USD"
    price_input = float(request.form.get("price_input") or 0)

    # sales_manager: ممنوع create/delete، وممنوع يغير name/category/unit_type/currency
    # مسموح يغيّر unit + price_input فقط
    if current_user.role == "sales_manager":
        if material_id == 0:
            return jsonify({"error": "You are not allowed to create materials."}), 403

        with get_db() as cur:
            cur.execute(
                """
                SELECT name, category, unit, unit_type, currency
                FROM materials
                WHERE id = %s
                """,
                (material_id,),
            )
            row = cur.fetchone()

        if not row:
            return jsonify({"error": "Material not found."}), 404

        name_db, category_db, unit_db, unit_type_db, currency_db = row
        name = name_db
        category = category_db
        unit_type = unit_type_db
        currency = currency_db
        # unit يظل من الفورم
        # price_input يظل من الفورم

    if not name or not category:
        return jsonify({"error": "Name and category are required."}), 400
    if unit_type not in ("weight", "count", "length"):
        return jsonify({"error": "Invalid unit type."}), 400
    if not currency:
        return jsonify({"error": "Currency is required."}), 400

    if unit_type == "weight":
        if unit.lower() in ("ton", "tonne", "t"):
            price_per_unit = price_input / 1000.0
            store_unit = "Ton"
        elif unit.lower() in ("kg", "kilogram"):
            price_per_unit = price_input
            store_unit = "kg"
        else:
            price_per_unit = price_input
            store_unit = unit
    else:
        price_per_unit = price_input
        store_unit = unit

    with get_db() as cur:
        if material_id == 0:
            code = generate_material_code(cur)
            cur.execute(
                """
                INSERT INTO materials (
                    code,
                    name,
                    category,
                    unit,
                    unit_type,
                    currency,
                    price_per_unit
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id, code, name, category, unit, unit_type, currency, price_per_unit
                """,
                (
                    code,
                    name,
                    category,
                    store_unit,
                    unit_type,
                    currency,
                    price_per_unit,
                ),
            )
            m = cur.fetchone()
            msg = f"Material created with code {m[1]}."
        else:
            cur.execute(
                """
                UPDATE materials
                SET name = %s,
                    category = %s,
                    unit = %s,
                    unit_type = %s,
                    currency = %s,
                    price_per_unit = %s
                WHERE id = %s
                RETURNING id, code, name, category, unit, unit_type, currency, price_per_unit
                """,
                (
                    name,
                    category,
                    store_unit,
                    unit_type,
                    currency,
                    price_per_unit,
                    material_id,
                ),
            )
            m = cur.fetchone()
            msg = "Material updated."

    if not m:
        return jsonify({"error": "DB error while saving material."}), 500

    saved_material_id = int(m[0])
    invalidate_material(saved_material_id)
    _bump_pricing_cache_version()

    data = _material_row_to_dict(m)
    return jsonify({"message": msg, "material": data})