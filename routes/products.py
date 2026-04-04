# routes/products.py
from flask import Blueprint, render_template, request, redirect, url_for, flash
from db import get_db
from .settings import _bump_pricing_cache_version


products_bp = Blueprint("products", __name__, template_folder="../templates/products")


FILM_TYPES = [
    ("Standard", "Standard"),
    ("Super_Rigid", "Super RIGID"),
    ("Regular_Rigid", "Regular RIGID"),
    ("Power", "Power"),
    ("Power_Plus", "Power Plus"),
    ("UVI_6m", "UVI (6 month)"),
    ("UVI_12m", "UVI (12 month)"),
    ("UV_Rigid", "UV&Rigid"),
    ("Prestretch", "Prestretch"),
]


def generate_product_code(cur) -> str:
    """
    Generate next product code like P0001, P0002...
    """
    cur.execute(
        """
        SELECT code FROM products
        WHERE code LIKE 'P%%'
        ORDER BY CAST(SUBSTRING(code FROM 2) AS INTEGER) DESC
        LIMIT 1
        """
    )
    row = cur.fetchone()
    if not row:
        next_number = 1
    else:
        last_code = row[0]
        try:
            last_num = int(last_code[1:])
        except ValueError:
            last_num = 0
        next_number = last_num + 1

    return f"P{next_number:04d}"


@products_bp.route("/", methods=["GET"])
def index():
    with get_db() as cur:
        cur.execute(
            """
            SELECT id,
                   code,
                   micron,
                   stretchability_percent,
                   is_prestretch,
                   bom_scrap_percent,
                   film_type,
                   is_manual,
                   is_colored,
                   kg_per_roll
            FROM products
            ORDER BY code
            """
        )
        products = cur.fetchall()
    return render_template("products/index.html", products=products)


@products_bp.route("/edit/<int:product_id>", methods=["GET", "POST"])
def edit(product_id):
    if request.method == "POST":
        micron = request.form["micron"].strip()
        stretchability_raw = request.form.get("stretchability", "").strip()
        is_prestretch = request.form.get("is_prestretch") == "on"
        bom_scrap_input = request.form.get("bom_scrap_percent", "").strip() or "0"
        film_type = (request.form.get("film_type") or "standard").strip()
        is_manual = request.form.get("is_manual") == "on"
        is_colored = request.form.get("is_colored") == "on"
        kg_per_roll_input = request.form.get("kg_per_roll", "").strip() or "0"

        if not micron:
            flash("Micron is required.", "danger")
            return redirect(request.url)

        micron_int = int(micron)

        # Stretchability اختيارية
        if stretchability_raw:
            stretch_int = int(stretchability_raw)
        else:
            stretch_int = 0  # أو أي default يناسبك

        try:
            bom_scrap_percent = float(bom_scrap_input)
        except ValueError:
            bom_scrap_percent = 0.0

        if bom_scrap_percent < 0:
            bom_scrap_percent = 0.0

        try:
            kg_per_roll = float(kg_per_roll_input)
        except ValueError:
            kg_per_roll = 0.0

        with get_db() as cur:
            if product_id == 0:
                code = generate_product_code(cur)
                cur.execute(
                    """
                    INSERT INTO products (
                        code,
                        micron,
                        stretchability_percent,
                        is_prestretch,
                        bom_scrap_percent,
                        is_manual,
                        is_colored,
                        kg_per_roll,
                        film_type
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        code,
                        micron_int,
                        stretch_int,
                        is_prestretch,
                        bom_scrap_percent,
                        is_manual,
                        is_colored,
                        kg_per_roll,
                        film_type,
                    ),
                )
                flash(f"Product created with code {code}.", "success")
            else:
                code = request.form["code"].strip()
                cur.execute(
                    """
                    UPDATE products
                    SET code = %s,
                        micron = %s,
                        stretchability_percent = %s,
                        is_prestretch = %s,
                        bom_scrap_percent = %s,
                        is_manual = %s,
                        is_colored = %s,
                        kg_per_roll = %s,
                        film_type = %s
                    WHERE id = %s
                    """,
                    (
                        code,
                        micron_int,
                        stretch_int,
                        is_prestretch,
                        bom_scrap_percent,
                        is_manual,
                        is_colored,
                        kg_per_roll,
                        film_type,
                        product_id,
                    ),
                )
                flash("Product updated.", "success")
                
        _bump_pricing_cache_version()

        return redirect(url_for("products.index"))

    # GET
    product = None
    if product_id != 0:
        with get_db() as cur:
            cur.execute(
                """
                SELECT id,
                       code,
                       micron,
                       stretchability_percent,
                       is_prestretch,
                       bom_scrap_percent,
                       film_type,
                       is_manual,
                       is_colored,
                       kg_per_roll
                FROM products
                WHERE id = %s
                """,
                (product_id,),
            )
            product = cur.fetchone()

    return render_template(
        "products/form.html",
        product=product,
        film_types=FILM_TYPES,
    )


@products_bp.route("/delete/<int:product_id>")
def delete(product_id):
    with get_db() as cur:
        cur.execute("SELECT code FROM products WHERE id = %s", (product_id,))
        row = cur.fetchone()
        if row:
            cur.execute("DELETE FROM products WHERE id = %s", (product_id,))
            flash(f'Product "{row[0]}" deleted.', "success")
            
            _bump_pricing_cache_version()
            
        else:
            flash("Product not found.", "danger")
    return redirect(url_for("products.index"))
