from flask import Blueprint, render_template, request, redirect, url_for, flash
from db import get_db
from pricing_cache import (
    invalidate_settings_cache,
    invalidate_all_materials,
)
from .pricing import invalidate_pricing_static_cache


settings_bp = Blueprint(
    "settings", __name__, template_folder="../templates/settings"
)

def _bump_pricing_cache_version():
    with get_db() as cur:
        cur.execute(
            """
            UPDATE pricing_cache_control
            SET cache_version = cache_version + 1,
                updated_at = NOW()
            WHERE id = 1
            """
        )
    invalidate_pricing_static_cache()


# -----------------------------
# Shipping Settings
# -----------------------------


def _load_shipping_context(cur):
    cur.execute("SELECT id, name, country FROM ports ORDER BY country, name")
    ports = cur.fetchall()

    cur.execute(
        "SELECT id, country, COALESCE(city,'') FROM destinations ORDER BY country, city"
    )
    destinations = cur.fetchall()

    cur.execute(
        """
        SELECT f.id, p.name, p.country, f.fob_cost_usd_per_container
        FROM fob_costs f
        JOIN ports p ON f.port_id = p.id
        ORDER BY p.country, p.name
        """
    )
    fob_costs = cur.fetchall()

    cur.execute(
        """
        SELECT s.id,
               lp.name AS loading_port,
               d.country,
               COALESCE(d.city,'') AS city,
               s.shipping_rate_usd_per_container,
               COALESCE(s.carrier_name,'')
        FROM sea_freight_rates s
        JOIN ports lp ON s.loading_port_id = lp.id
        JOIN destinations d ON s.destination_id = d.id
        ORDER BY lp.name, d.country, d.city
        """
    )
    sea_freight = cur.fetchall()

    return ports, destinations, fob_costs, sea_freight


@settings_bp.route("/shipping", methods=["GET", "POST"])
def shipping_settings():
    if request.method == "POST":
        form_action = request.form.get("_action")

        with get_db() as cur:
            if form_action == "add_port":
                name = (request.form.get("port_name") or "").strip()
                country = (request.form.get("port_country") or "").strip()
                if not name or not country:
                    flash("Port name and country are required.", "danger")
                else:
                    cur.execute(
                        "INSERT INTO ports (name, country) VALUES (%s, %s)",
                        (name, country),
                    )
                    flash("Port saved.", "success")
                    
                    _bump_pricing_cache_version()

            elif form_action == "add_destination":
                country = (request.form.get("dest_country") or "").strip()
                city = (request.form.get("dest_city") or "").strip()
                if not country:
                    flash("Destination country is required.", "danger")
                else:
                    cur.execute(
                        "INSERT INTO destinations (country, city) VALUES (%s, %s)",
                        (country, city or None),
                    )
                    flash("Destination saved.", "success")
                    
                    _bump_pricing_cache_version()

            elif form_action == "add_fob":
                port_id = int(request.form.get("fob_port_id") or 0)
                try:
                    cost = float(request.form.get("fob_cost_usd_per_container") or 0)
                except ValueError:
                    cost = 0
                if port_id <= 0 or cost <= 0:
                    flash("FOB port and positive cost are required.", "danger")
                else:
                    cur.execute(
                        """
                        INSERT INTO fob_costs (port_id, fob_cost_usd_per_container)
                        VALUES (%s, %s)
                        ON CONFLICT (port_id) DO UPDATE
                        SET fob_cost_usd_per_container = EXCLUDED.fob_cost_usd_per_container
                        """,
                        (port_id, cost),
                    )
                    flash("FOB cost saved.", "success")
                    
                    _bump_pricing_cache_version()

            elif form_action == "add_sea_freight":
                loading_port_id = int(request.form.get("sf_port_id") or 0)
                destination_id = int(request.form.get("sf_dest_id") or 0)
                try:
                    rate = float(request.form.get("sf_rate_usd_per_container") or 0)
                except ValueError:
                    rate = 0
                carrier = (request.form.get("sf_carrier_name") or "").strip()

                if loading_port_id <= 0 or destination_id <= 0 or rate <= 0:
                    flash(
                        "Loading port, destination, and positive rate are required.",
                        "danger",
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO sea_freight_rates (
                            loading_port_id,
                            destination_id,
                            shipping_rate_usd_per_container,
                            carrier_name
                        )
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (loading_port_id, destination_id) DO UPDATE
                        SET shipping_rate_usd_per_container = EXCLUDED.shipping_rate_usd_per_container,
                            carrier_name = EXCLUDED.carrier_name
                        """
                        ,
                        (loading_port_id, destination_id, rate, carrier or None),
                    )
                    flash("Sea freight rate saved.", "success")
                    
                    _bump_pricing_cache_version()

        # بعد الـ POST نرجّع نفس الكونتكست ونحدد التاب النشط
        with get_db() as cur:
            ports, destinations, fob_costs, sea_freight = _load_shipping_context(cur)

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            active_tab = "ports"
            if form_action == "add_fob":
                active_tab = "fob"
            elif form_action == "add_sea_freight":
                active_tab = "sea"

            return render_template(
                "settings/shipping.html",
                ports=ports,
                destinations=destinations,
                fob_costs=fob_costs,
                sea_freight=sea_freight,
                active_tab=active_tab,
            )

        return redirect(url_for("settings.shipping_settings"))

    # GET
    with get_db() as cur:
        ports, destinations, fob_costs, sea_freight = _load_shipping_context(cur)

    return render_template(
        "settings/shipping.html",
        ports=ports,
        destinations=destinations,
        fob_costs=fob_costs,
        sea_freight=sea_freight,
        active_tab=request.args.get("tab") or "ports",
    )


@settings_bp.route("/shipping/ports/<int:port_id>/delete", methods=["POST"])
def delete_port(port_id):
    with get_db() as cur:
        cur.execute("DELETE FROM ports WHERE id = %s", (port_id,))
        ports, destinations, fob_costs, sea_freight = _load_shipping_context(cur)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template(
            "settings/shipping.html",
            ports=ports,
            destinations=destinations,
            fob_costs=fob_costs,
            sea_freight=sea_freight,
            active_tab="ports",
        )
        
    _bump_pricing_cache_version()

    flash("Port deleted.", "success")
    return redirect(url_for("settings.shipping_settings", tab="ports"))


@settings_bp.route("/shipping/destinations/<int:dest_id>/delete", methods=["POST"])
def delete_destination(dest_id):
    with get_db() as cur:
        cur.execute("DELETE FROM destinations WHERE id = %s", (dest_id,))
        ports, destinations, fob_costs, sea_freight = _load_shipping_context(cur)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template(
            "settings/shipping.html",
            ports=ports,
            destinations=destinations,
            fob_costs=fob_costs,
            sea_freight=sea_freight,
            active_tab="ports",
        )
        
    _bump_pricing_cache_version()

    flash("Destination deleted.", "success")
    return redirect(url_for("settings.shipping_settings", tab="ports"))


@settings_bp.route("/shipping/fob/<int:fob_id>/delete", methods=["POST"])
def delete_fob_cost(fob_id):
    with get_db() as cur:
        cur.execute("DELETE FROM fob_costs WHERE id = %s", (fob_id,))
        ports, destinations, fob_costs, sea_freight = _load_shipping_context(cur)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template(
            "settings/shipping.html",
            ports=ports,
            destinations=destinations,
            fob_costs=fob_costs,
            sea_freight=sea_freight,
            active_tab="fob",
        )
        
    _bump_pricing_cache_version()

    flash("FOB cost deleted.", "success")
    return redirect(url_for("settings.shipping_settings", tab="fob"))


@settings_bp.route("/shipping/sea-freight/<int:rate_id>/delete", methods=["POST"])
def delete_sea_freight(rate_id):
    with get_db() as cur:
        cur.execute("DELETE FROM sea_freight_rates WHERE id = %s", (rate_id,))
        ports, destinations, fob_costs, sea_freight = _load_shipping_context(cur)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template(
            "settings/shipping.html",
            ports=ports,
            destinations=destinations,
            fob_costs=fob_costs,
            sea_freight=sea_freight,
            active_tab="sea",
        )
        
    _bump_pricing_cache_version()

    flash("Sea freight rate deleted.", "success")
    return redirect(url_for("settings.shipping_settings", tab="sea"))


# -----------------------------
# Pricing Settings
# -----------------------------
def get_roll_size_label(roll_weight_min, roll_weight_max, packing_type_name: str) -> str:
    """
    يحوّل مدى وزن الرول + نوع الباكنج إلى label جاهز للـ UI.
    """
    name = (packing_type_name or "").strip()

    # Pre-stretch بأنواعه
    if name in ("Pre-stretch (No Box)", "Pre-stretch (Box)"):
        return "Prestretch Roll size"

    # Manual من 0 إلى 9.99
    if name == "Manual":
        if roll_weight_min is not None and roll_weight_max is not None:
            if roll_weight_min >= 0 and roll_weight_max <= 9.99:
                return "Manual Roll size"

    # Standard: 10–24.99
    if roll_weight_min is not None and roll_weight_max is not None:
        if roll_weight_min >= 10 and roll_weight_max <= 24.99:
            return "Standard Roll size"

    # Jumbo: 25–100
    if roll_weight_min is not None and roll_weight_max is not None:
        if roll_weight_min >= 25 and roll_weight_max <= 100:
            return "Jumbo Roll size"

    return ""


@settings_bp.route("/pricing", methods=["GET", "POST"])
def pricing_settings():
    editing_rule = None
    editing_term = None
    active_tab = "rules"

    if request.method == "POST":
        form_action = request.form.get("_action")

        with get_db() as cur:
            if form_action == "add_rule":
                micron_min = int(request.form.get("micron_min") or 0)
                micron_max = int(request.form.get("micron_max") or 0)
                film_type = (request.form.get("film_type") or "").strip()
                packing_type_id = int(request.form.get("packing_type_id") or 0)
                roll_weight_min = float(request.form.get("roll_weight_min") or 0)
                roll_weight_max = float(request.form.get("roll_weight_max") or 0)
                margin_percent = float(request.form.get("margin_percent") or 0)

                active_tab = "rules"

                if micron_min <= 0 or micron_max <= 0 or micron_max < micron_min:
                    flash("Micron range is invalid.", "danger")
                elif not film_type:
                    flash("Film type is required.", "danger")
                elif packing_type_id <= 0:
                    flash("Packing type is required.", "danger")
                elif roll_weight_max and roll_weight_max < roll_weight_min:
                    flash("Roll weight range is invalid.", "danger")
                else:
                    cur.execute(
                        """
                        INSERT INTO pricing_rules (
                            micron_min,
                            micron_max,
                            film_type,
                            packing_type_id,
                            roll_weight_min,
                            roll_weight_max,
                            margin_percent
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            micron_min,
                            micron_max,
                            film_type,
                            packing_type_id,
                            roll_weight_min,
                            roll_weight_max,
                            margin_percent,
                        ),
                    )
                    flash("Margin factor saved.", "success")
                    _bump_pricing_cache_version()

            elif form_action == "edit_rule_load":
                rule_id = int(request.form.get("rule_id") or 0)
                active_tab = "rules"
                if rule_id > 0:
                    cur.execute(
                        """
                        SELECT id,
                               micron_min,
                               micron_max,
                               film_type,
                               packing_type_id,
                               roll_weight_min,
                               roll_weight_max,
                               margin_percent
                        FROM pricing_rules
                        WHERE id = %s
                        """,
                        (rule_id,),
                    )
                    editing_rule = cur.fetchone()
                    if not editing_rule:
                        flash("Margin factor not found.", "danger")

            elif form_action == "edit_rule_save":
                rule_id = int(request.form.get("rule_id") or 0)
                micron_min = int(request.form.get("micron_min") or 0)
                micron_max = int(request.form.get("micron_max") or 0)
                film_type = (request.form.get("film_type") or "").strip()
                packing_type_id = int(request.form.get("packing_type_id") or 0)
                roll_weight_min = float(request.form.get("roll_weight_min") or 0)
                roll_weight_max = float(request.form.get("roll_weight_max") or 0)
                margin_percent = float(request.form.get("margin_percent") or 0)

                active_tab = "rules"

                if rule_id <= 0:
                    flash("Invalid margin factor.", "danger")
                elif micron_min <= 0 or micron_max <= 0 or micron_max < micron_min:
                    flash("Micron range is invalid.", "danger")
                elif not film_type:
                    flash("Film type is required.", "danger")
                elif packing_type_id <= 0:
                    flash("Packing type is required.", "danger")
                elif roll_weight_max and roll_weight_max < roll_weight_min:
                    flash("Roll weight range is invalid.", "danger")
                else:
                    cur.execute(
                        """
                        UPDATE pricing_rules
                        SET micron_min = %s,
                            micron_max = %s,
                            film_type = %s,
                            packing_type_id = %s,
                            roll_weight_min = %s,
                            roll_weight_max = %s,
                            margin_percent = %s
                        WHERE id = %s
                        """,
                        (
                            micron_min,
                            micron_max,
                            film_type,
                            packing_type_id,
                            roll_weight_min,
                            roll_weight_max,
                            margin_percent,
                            rule_id,
                        ),
                    )
                    flash("Margin factor updated.", "success")
                    _bump_pricing_cache_version()

            elif form_action == "delete_rule":
                rule_id = int(request.form.get("rule_id") or 0)
                active_tab = "rules"
                if rule_id > 0:
                    cur.execute("DELETE FROM pricing_rules WHERE id = %s", (rule_id,))
                    flash("Margin factor deleted.", "success")
                    _bump_pricing_cache_version()

            elif form_action == "save_extras":
                color_extra = float(
                    request.form.get("color_extra_usd_per_kg") or 0
                )
                prestretch_extra = float(
                    request.form.get("prestretch_extra_usd_per_kg") or 0
                )

                foreign_extra_mode = (request.form.get("foreign_extra_mode") or "percent").strip()
                if foreign_extra_mode not in ("percent", "per_unit"):
                    foreign_extra_mode = "percent"

                foreign_extra_value = float(
                    request.form.get("foreign_extra_value") or 0
                )

                active_tab = "extras"

                cur.execute(
                    """
                    SELECT id
                    FROM pricing_extras
                    WHERE is_active = true
                    ORDER BY id
                    LIMIT 1
                    """
                )
                row_extra = cur.fetchone()

                if row_extra:
                    cur.execute(
                        """
                        UPDATE pricing_extras
                        SET color_extra_usd_per_kg = %s,
                            prestretch_extra_usd_per_kg = %s,
                            foreign_extra_mode = %s,
                            foreign_extra_value = %s
                        WHERE id = %s
                        """,
                        (color_extra, prestretch_extra, foreign_extra_mode, foreign_extra_value, row_extra[0]),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO pricing_extras (
                            color_extra_usd_per_kg,
                            prestretch_extra_usd_per_kg,
                            foreign_extra_mode,
                            foreign_extra_value,
                            is_active
                        )
                        VALUES (%s, %s, %s, %s, true)
                        """,
                        (color_extra, prestretch_extra, foreign_extra_mode, foreign_extra_value),
                    )
                flash("Extras saved.", "success")
                _bump_pricing_cache_version()

            elif form_action == "edit_payment_term_load":
                pt_id = int(request.form.get("pt_id") or 0)
                active_tab = "terms"
                if pt_id > 0:
                    cur.execute(
                        """
                        SELECT id, name, credit_days, annual_rate_percent
                        FROM payment_terms
                        WHERE id = %s
                        """,
                        (pt_id,),
                    )
                    editing_term = cur.fetchone()
                    if not editing_term:
                        flash("Payment term not found.", "danger")

            elif form_action == "add_payment_term":
                name = (request.form.get("pt_name") or "").strip()
                credit_days = int(request.form.get("credit_days") or 0)
                annual_rate_percent = float(
                    request.form.get("annual_rate_percent") or 0
                )

                active_tab = "terms"

                if not name:
                    flash("Payment term name is required.", "danger")
                elif credit_days < 0:
                    flash("Credit days cannot be negative.", "danger")
                else:
                    cur.execute(
                        """
                        INSERT INTO payment_terms (
                            name,
                            credit_days,
                            annual_rate_percent,
                            is_active
                        )
                        VALUES (%s, %s, %s, true)
                        """,
                        (name, credit_days, annual_rate_percent),
                    )
                    flash("Payment term added.", "success")
                    _bump_pricing_cache_version()

            elif form_action == "edit_payment_term_save":
                pt_id = int(request.form.get("pt_id") or 0)
                name = (request.form.get("pt_name") or "").strip()
                credit_days = int(request.form.get("credit_days") or 0)
                annual_rate_percent = float(
                    request.form.get("annual_rate_percent") or 0
                )

                active_tab = "terms"

                if pt_id <= 0:
                    flash("Invalid payment term.", "danger")
                elif not name:
                    flash("Payment term name is required.", "danger")
                elif credit_days < 0:
                    flash("Credit days cannot be negative.", "danger")
                else:
                    cur.execute(
                        """
                        UPDATE payment_terms
                        SET name = %s,
                            credit_days = %s,
                            annual_rate_percent = %s
                        WHERE id = %s
                        """,
                        (name, credit_days, annual_rate_percent, pt_id),
                    )
                    flash("Payment term updated.", "success")
                    _bump_pricing_cache_version()

            elif form_action == "delete_payment_term":
                pt_id = int(request.form.get("pt_id") or 0)
                active_tab = "terms"
                if pt_id > 0:
                    cur.execute("DELETE FROM payment_terms WHERE id = %s", (pt_id,))
                    flash("Payment term deleted.", "success")
                    _bump_pricing_cache_version()

        # بعد POST نحمّل البيانات من جديد
        with get_db() as cur:
            cur.execute(
                """
                SELECT id,
                       micron_min,
                       micron_max,
                       film_type,
                       packing_type_id,
                       roll_weight_min,
                       roll_weight_max,
                       margin_percent
                FROM pricing_rules
                ORDER BY film_type, packing_type_id, micron_min, roll_weight_min
                """
            )
            pricing_rules = cur.fetchall()

            cur.execute(
                """
                SELECT id,
                    color_extra_usd_per_kg,
                    prestretch_extra_usd_per_kg,
                    foreign_extra_mode,
                    foreign_extra_value
                FROM pricing_extras
                WHERE is_active = true
                ORDER BY id
                """
            )
            extras_rows = cur.fetchall()

            cur.execute(
                """
                SELECT id,
                       name,
                       credit_days,
                       annual_rate_percent
                FROM payment_terms
                WHERE is_active = true
                ORDER BY credit_days, id
                """
            )
            payment_terms = cur.fetchall()

            cur.execute(
                """
                SELECT id, name
                FROM packing_types
                ORDER BY id
                """
            )
            packing_types = cur.fetchall()

        extras = extras_rows[0] if extras_rows else None

        # بناء labels
        packing_type_name_by_id = {pt[0]: pt[1] for pt in packing_types}
        pricing_rules_with_labels = []
        for r in pricing_rules:
            (
                rule_id,
                micron_min,
                micron_max,
                film_type,
                packing_type_id,
                roll_weight_min,
                roll_weight_max,
                margin_percent,
            ) = r
            pt_name = packing_type_name_by_id.get(packing_type_id, "")
            roll_size_label = get_roll_size_label(roll_weight_min, roll_weight_max, pt_name)
            pricing_rules_with_labels.append(
                (
                    rule_id,
                    micron_min,
                    micron_max,
                    film_type,
                    packing_type_id,
                    roll_weight_min,
                    roll_weight_max,
                    margin_percent,
                    roll_size_label,
                )
            )

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return render_template(
                "settings/pricing.html",
                pricing_rules=pricing_rules_with_labels,
                extras=extras,
                payment_terms=payment_terms,
                editing_rule=editing_rule,
                editing_term=editing_term,
                active_tab=active_tab,
                packing_types=packing_types,
            )

        return redirect(url_for("settings.pricing_settings"))

    # GET
    with get_db() as cur:
        cur.execute(
            """
            SELECT id,
                micron_min,
                micron_max,
                film_type,
                packing_type_id,
                roll_weight_min,
                roll_weight_max,
                margin_percent
            FROM pricing_rules
            ORDER BY film_type, packing_type_id, micron_min, roll_weight_min
            """
        )
        pricing_rules = cur.fetchall()

        cur.execute(
            """
            SELECT id,
                color_extra_usd_per_kg,
                prestretch_extra_usd_per_kg,
                foreign_extra_mode,
                foreign_extra_value
            FROM pricing_extras
            WHERE is_active = true
            ORDER BY id
            """
        )
        extras_rows = cur.fetchall()

        cur.execute(
            """
            SELECT id,
                name,
                credit_days,
                annual_rate_percent
            FROM payment_terms
            WHERE is_active = true
            ORDER BY credit_days, id
            """
        )
        payment_terms = cur.fetchall()

        cur.execute(
            """
            SELECT id, name
            FROM packing_types
            ORDER BY id
            """
        )
        packing_types = cur.fetchall()

    extras = extras_rows[0] if extras_rows else None

    # بناء labels للـ GET
    packing_type_name_by_id = {pt[0]: pt[1] for pt in packing_types}
    pricing_rules_with_labels = []
    for r in pricing_rules:
        (
            rule_id,
            micron_min,
            micron_max,
            film_type,
            packing_type_id,
            roll_weight_min,
            roll_weight_max,
            margin_percent,
        ) = r
        pt_name = packing_type_name_by_id.get(packing_type_id, "")
        roll_size_label = get_roll_size_label(roll_weight_min, roll_weight_max, pt_name)
        pricing_rules_with_labels.append(
            (
                rule_id,
                micron_min,
                micron_max,
                film_type,
                packing_type_id,
                roll_weight_min,
                roll_weight_max,
                margin_percent,
                roll_size_label,
            )
        )

    return render_template(
        "settings/pricing.html",
        pricing_rules=pricing_rules_with_labels,
        extras=extras,
        payment_terms=payment_terms,
        editing_rule=editing_rule,
        editing_term=editing_term,
        active_tab=request.args.get("tab") or "rules",
        packing_types=packing_types,
    )


# -----------------------------
# Costing Settings
# -----------------------------
def _load_machine_costs(cur):
    cur.execute(
        """
        SELECT mc.id,
               mc.machine_id,
               m.name,
               mc.cost_type,
               mc.amount_egp,
               mc.description
        FROM machine_costs mc
        JOIN machines m ON mc.machine_id = m.id
        ORDER BY m.name, mc.cost_type, mc.id
        """
    )
    return cur.fetchall()


def _load_import_profiles(cur):
    cur.execute(
        """
        SELECT icp.id,
               icp.material_id,
               icp.scope,
               icp.mode,
               icp.value,
               m.code,
               m.name
        FROM import_cost_profiles icp
        LEFT JOIN materials m ON icp.material_id = m.id
        WHERE icp.scope = 'global'
        ORDER BY icp.id
        """
    )
    return cur.fetchall()


@settings_bp.route("/settings/costing", methods=["GET"])
def costing_settings():
    active_tab = request.args.get("tab", "energy")

    with get_db() as cur:
        cur.execute(
            """
            SELECT id, egp_per_kwh, effective_date, is_active
            FROM energy_rates
            ORDER BY effective_date DESC, id DESC
            """
        )
        energy_rates = cur.fetchall()

        cur.execute(
            """
            SELECT id, egp_per_usd, effective_date, is_active
            FROM currency_rates
            ORDER BY effective_date DESC, id DESC
            """
        )
        currency_rates = cur.fetchall()

        cur.execute(
            """
            SELECT id, name
            FROM machines
            ORDER BY name
            """
        )
        machines = cur.fetchall()

        machine_costs = _load_machine_costs(cur)

        cur.execute(
            """
            SELECT id, code, name
            FROM materials
            ORDER BY code
            """
        )
        materials = cur.fetchall()

        import_profiles = _load_import_profiles(cur)

    return render_template(
        "settings/costing.html",
        active_tab=active_tab,
        energy_rates=energy_rates,
        currency_rates=currency_rates,
        machines=machines,
        machine_costs=machine_costs,
        materials=materials,
        import_profiles=import_profiles,
        editing_machine_cost=None,
    )


# -------- Actions for Energy --------
@settings_bp.route("/settings/costing/energy/save", methods=["POST"])
def save_energy():
    egp_per_kwh = request.form.get("egp_per_kwh", "").strip()

    if not egp_per_kwh:
        flash("Please enter EGP/kWh.", "danger")
        return redirect(url_for("settings.costing_settings", tab="energy"))

    try:
        egp_per_kwh_val = float(egp_per_kwh)
    except ValueError:
        flash("Invalid numeric value.", "danger")
        return redirect(url_for("settings.costing_settings", tab="energy"))

    with get_db() as cur:
        cur.execute("UPDATE energy_rates SET is_active = false")
        cur.execute(
            """
            INSERT INTO energy_rates (egp_per_kwh, effective_date, is_active)
            VALUES (%s, CURRENT_DATE, true)
            """,
            (egp_per_kwh_val,),
        )

    flash("Energy rate saved.", "success")
    
    invalidate_settings_cache()
    _bump_pricing_cache_version()
    
    return redirect(url_for("settings.costing_settings", tab="energy"))


# -------- Actions for FX --------
@settings_bp.route("/settings/costing/fx/save", methods=["POST"])
def save_fx():
    egp_per_usd = request.form.get("egp_per_usd", "").strip()

    if not egp_per_usd:
        flash("Please enter EGP per USD.", "danger")
        return redirect(url_for("settings.costing_settings", tab="fx"))

    try:
        egp_per_usd_val = float(egp_per_usd)
    except ValueError:
        flash("Invalid numeric value.", "danger")
        return redirect(url_for("settings.costing_settings", tab="fx"))

    with get_db() as cur:
        cur.execute("UPDATE currency_rates SET is_active = false")
        cur.execute(
            """
            INSERT INTO currency_rates (egp_per_usd, effective_date, is_active)
            VALUES (%s, CURRENT_DATE, true)
            """,
            (egp_per_usd_val,),
        )

    flash("FX rate saved.", "success")
    
    invalidate_settings_cache()
    _bump_pricing_cache_version()
    
    return redirect(url_for("settings.costing_settings", tab="fx"))

# -------- Actions for Machine Costs --------
@settings_bp.route("/settings/costing/machine_costs/add", methods=["POST"])
def add_machine_cost():
    machine_id = int(request.form.get("machine_id") or 0)
    cost_type = request.form.get("cost_type", "").strip()
    amount_egp = request.form.get("amount_egp", "").strip()
    description = (request.form.get("description") or "").strip()

    if machine_id <= 0 or cost_type not in ("fixed_monthly", "variable_per_kg"):
        flash("Please select machine and valid cost type.", "danger")
        return redirect(url_for("settings.costing_settings", tab="machine_costs"))

    try:
        amount_val = float(amount_egp or 0)
    except ValueError:
        flash("Invalid amount.", "danger")
        return redirect(url_for("settings.costing_settings", tab="machine_costs"))

    with get_db() as cur:
        cur.execute(
            """
            INSERT INTO machine_costs (machine_id, cost_type, amount_egp, description)
            VALUES (%s, %s, %s, %s)
            """,
            (machine_id, cost_type, amount_val, description),
        )
        machine_costs = _load_machine_costs(cur)
        cur.execute(
            """
            SELECT id, name
            FROM machines
            ORDER BY name
            """
        )
        machines = cur.fetchall()
        
    _bump_pricing_cache_version()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        # نرجّع البلوك الكامل (فورم + جدول) والفورم في وضع Add
        return render_template(
            "settings/_machine_costs_block.html",
            machines=machines,
            machine_costs=machine_costs,
            editing_machine_cost=None,
        )

    flash("Machine cost added.", "success")
    
    
    return redirect(url_for("settings.costing_settings", tab="machine_costs"))


@settings_bp.route("/settings/costing/machine_costs/<int:cost_id>/edit", methods=["GET"])
def edit_machine_cost(cost_id):
    with get_db() as cur:
        cur.execute(
            """
            SELECT mc.id,
                   mc.machine_id,
                   m.name,
                   mc.cost_type,
                   mc.amount_egp,
                   mc.description
            FROM machine_costs mc
            JOIN machines m ON mc.machine_id = m.id
            WHERE mc.id = %s
            """,
            (cost_id,),
        )
        cost_row = cur.fetchone()

        if not cost_row:
            flash("Machine cost not found.", "danger")
            return redirect(url_for("settings.costing_settings", tab="machine_costs"))

        cur.execute(
            """
            SELECT id, egp_per_kwh, effective_date, is_active
            FROM energy_rates
            ORDER BY effective_date DESC, id DESC
            """
        )
        energy_rates = cur.fetchall()

        cur.execute(
            """
            SELECT id, egp_per_usd, effective_date, is_active
            FROM currency_rates
            ORDER BY effective_date DESC, id DESC
            """
        )
        currency_rates = cur.fetchall()

        cur.execute(
            """
            SELECT id, name
            FROM machines
            ORDER BY name
            """
        )
        machines = cur.fetchall()

        machine_costs = _load_machine_costs(cur)

        cur.execute(
            """
            SELECT id, code, name
            FROM materials
            ORDER BY code
            """
        )
        materials = cur.fetchall()

        import_profiles = _load_import_profiles(cur)

    return render_template(
        "settings/costing.html",
        active_tab="machine_costs",
        energy_rates=energy_rates,
        currency_rates=currency_rates,
        machines=machines,
        machine_costs=machine_costs,
        materials=materials,
        import_profiles=import_profiles,
        editing_machine_cost=cost_row,
    )


@settings_bp.route("/settings/costing/machine_costs/<int:cost_id>/update", methods=["POST"])
def update_machine_cost(cost_id):
    machine_id = int(request.form.get("machine_id") or 0)
    cost_type = request.form.get("cost_type", "").strip()
    amount_egp = request.form.get("amount_egp", "").strip()
    description = (request.form.get("description") or "").strip()

    if machine_id <= 0 or cost_type not in ("fixed_monthly", "variable_per_kg"):
        flash("Please select machine and valid cost type.", "danger")
        return redirect(url_for("settings.costing_settings", tab="machine_costs"))

    try:
        amount_val = float(amount_egp or 0)
    except ValueError:
        flash("Invalid amount.", "danger")
        return redirect(url_for("settings.costing_settings", tab="machine_costs"))

    with get_db() as cur:
        cur.execute(
            """
            UPDATE machine_costs
            SET machine_id = %s,
                cost_type = %s,
                amount_egp = %s,
                description = %s
            WHERE id = %s
            """,
            (machine_id, cost_type, amount_val, description, cost_id),
        )
        machine_costs = _load_machine_costs(cur)
        cur.execute(
            """
            SELECT id, name
            FROM machines
            ORDER BY name
            """
        )
        machines = cur.fetchall()
        
    _bump_pricing_cache_version()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        # بعد الحفظ نرجّع الفورم لوضع Add
        return render_template(
            "settings/_machine_costs_block.html",
            machines=machines,
            machine_costs=machine_costs,
            editing_machine_cost=None,
        )

    flash("Machine cost updated.", "success")
        
    return redirect(url_for("settings.costing_settings", tab="machine_costs"))


@settings_bp.route("/settings/costing/machine_costs/<int:cost_id>/delete", methods=["POST"])
def delete_machine_cost(cost_id):
    with get_db() as cur:
        cur.execute("DELETE FROM machine_costs WHERE id = %s", (cost_id,))
        machine_costs = _load_machine_costs(cur)
        cur.execute(
            """
            SELECT id, name
            FROM machines
            ORDER BY name
            """
        )
        machines = cur.fetchall()
        
    _bump_pricing_cache_version()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template(
            "settings/_machine_costs_block.html",
            machines=machines,
            machine_costs=machine_costs,
            editing_machine_cost=None,
        )

    flash("Machine cost deleted.", "success")
        
    return redirect(url_for("settings.costing_settings", tab="machine_costs"))

# -------- Actions for Landed Cost Overhead --------
@settings_bp.route("/settings/costing/import_profiles/save", methods=["POST"])
def save_import_profile():
    mode = request.form.get("mode", "").strip()
    value = request.form.get("value", "").strip()

    if mode not in ("per_ton", "percent"):
        flash("Please select a valid mode.", "danger")
        return redirect(url_for("settings.costing_settings", tab="import_profiles"))

    try:
        value_val = float(value or 0)
    except ValueError:
        flash("Invalid value.", "danger")
        return redirect(url_for("settings.costing_settings", tab="import_profiles"))

    with get_db() as cur:
        cur.execute("DELETE FROM import_cost_profiles WHERE scope = 'global'")
        cur.execute(
            """
            INSERT INTO import_cost_profiles (material_id, scope, mode, value)
            VALUES (NULL, 'global', %s, %s)
            """,
            (mode, value_val),
        )
        import_profiles = _load_import_profiles(cur)
        
    invalidate_all_materials()
    _bump_pricing_cache_version()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template(
            "settings/_import_profiles_table.html",
            import_profiles=import_profiles,
        )

    flash("Import overhead saved.", "success")
    return redirect(url_for("settings.costing_settings", tab="import_profiles"))


@settings_bp.route("/settings/costing/import_profiles/<int:profile_id>/delete", methods=["POST"])
def delete_import_profile(profile_id):
    with get_db() as cur:
        cur.execute("DELETE FROM import_cost_profiles WHERE id = %s", (profile_id,))
        import_profiles = _load_import_profiles(cur)
        
    invalidate_all_materials()
    _bump_pricing_cache_version()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template(
            "settings/_import_profiles_table.html",
            import_profiles=import_profiles,
        )

    flash("Import cost profile deleted.", "success")
    return redirect(url_for("settings.costing_settings", tab="import_profiles"))

# -----------------------------
# Packing Settings
# -----------------------------
def _load_packing_context(cur):
    """
    يحمل كل الداتا الخاصة بالباكنج لإستخدامها في شاشة الإعدادات:
    - packing_types, pallet_types, packing_materials
    - packing_profiles
    - packing_items (مرتبطة بالـ profiles)
    - profile_costs: إجمالي تكلفة البالتة لكل profile
    - packing_profile_overrides: أوفررايد per product + roll weight
    """

    # ===== 1) Packing & pallet types =====
    cur.execute(
        """
        SELECT id, name, COALESCE(description, '')
        FROM packing_types
        ORDER BY id
        """
    )
    packing_types = cur.fetchall()

    cur.execute(
        """
        SELECT id, name, COALESCE(description, '')
        FROM pallet_types
        ORDER BY id
        """
    )
    pallet_types = cur.fetchall()

    # ===== 2) PACKING materials =====
    cur.execute(
        """
        SELECT id, code, name, unit, price_per_unit
        FROM materials
        WHERE UPPER(category) = 'PACKING'
        ORDER BY code
        """
    )
    packing_materials = cur.fetchall()

    # ===== 3) Packing profiles (الرئيسية) =====
    cur.execute(
        """
        SELECT
            pp.id,
            pp.name,
            pp.packing_type_id,
            pt.name AS packing_type_name,
            pp.pallet_type_id,
            plt.name AS pallet_type_name,
            pp.is_global,
            pp.is_active
        FROM packing_profiles pp
        JOIN packing_types pt ON pt.id = pp.packing_type_id
        JOIN pallet_types  plt ON plt.id = pp.pallet_type_id
        ORDER BY pt.id, plt.id, pp.id
        """
    )
    packing_profiles = cur.fetchall()

    # ===== 4) Items داخل كل profile =====
    cur.execute(
        """
        SELECT
            pi.id,
            pi.packing_profile_id,
            pp.name AS profile_name,
            pt.id  AS packing_type_id,
            pt.name AS packing_type_name,
            plt.id AS pallet_type_id,
            plt.name AS pallet_type_name,
            pi.material_id,
            COALESCE(pi.item_name, m.name) AS item_name,
            m.code AS material_code,
            m.unit,
            pi.quantity_per_pallet,
            m.price_per_unit
        FROM packing_items pi
        JOIN packing_profiles pp ON pp.id = pi.packing_profile_id
        JOIN packing_types   pt ON pt.id = pp.packing_type_id
        JOIN pallet_types    plt ON plt.id = pp.pallet_type_id
        JOIN materials       m  ON m.id = pi.material_id
        ORDER BY pt.id, plt.id, pp.id, pi.id
        """
    )
    packing_items = cur.fetchall()

    # إجمالي تكلفة البالتة لكل profile
    profile_costs: dict[int, float] = {}
    for (
        item_id,
        packing_profile_id,
        profile_name,
        packing_type_id,
        packing_type_name,
        pallet_type_id,
        pallet_type_name,
        material_id,
        item_name,
        material_code,
        unit,
        quantity_per_pallet,
        price_per_unit,
    ) in packing_items:
        qty = float(quantity_per_pallet or 0)
        price = float(price_per_unit or 0)
        line_cost = qty * price

        if packing_profile_id not in profile_costs:
            profile_costs[packing_profile_id] = 0.0
        profile_costs[packing_profile_id] += line_cost

    # ===== 5) Overrides per product + roll weight =====
    cur.execute(
        """
        SELECT
            o.id,
            o.packing_profile_id,
            pp.name AS profile_name,
            o.product_id,
            p.code AS product_code,
            p.micron,
            p.film_type,
            p.stretchability_percent,
            o.roll_weight_min,
            o.roll_weight_max,
            o.is_active
        FROM packing_profile_overrides o
        JOIN packing_profiles pp ON pp.id = o.packing_profile_id
        JOIN products        p  ON p.id = o.product_id
        ORDER BY p.code, o.roll_weight_min, o.roll_weight_max, o.id
        """
    )
    packing_profile_overrides = cur.fetchall()

    # ===== 6) Products list for UI =====
    cur.execute(
        """
        SELECT id, code, micron, film_type, stretchability_percent
        FROM products
        ORDER BY code
        """
    )
    products = cur.fetchall()

    return (
        packing_types,
        pallet_types,
        packing_materials,
        packing_profiles,
        packing_items,
        profile_costs,
        packing_profile_overrides,
        products,
    )


@settings_bp.route("/packing", methods=["GET"])
def packing_settings():
    with get_db() as cur:
        (
            packing_types,
            pallet_types,
            packing_materials,
            packing_profiles,
            packing_items,
            profile_costs,
            packing_profile_overrides,
            products,
        ) = _load_packing_context(cur)

    return render_template(
        "settings/packing.html",
        packing_types=packing_types,
        pallet_types=pallet_types,
        packing_materials=packing_materials,
        packing_profiles=packing_profiles,
        packing_items=packing_items,
        profile_costs=profile_costs,
        packing_profile_overrides=packing_profile_overrides,
        products=products,
        editing_packing_profile=None,
        editing_packing_item=None,
    )


@settings_bp.route("/settings/packing/types/add", methods=["POST"])
def add_packing_type():
    name = (request.form.get("name") or "").strip()
    description = (request.form.get("description") or "").strip()

    if not name:
        flash("Packing type name is required.", "danger")
    else:
        with get_db() as cur:
            cur.execute(
                "INSERT INTO packing_types (name, description) VALUES (%s, %s)",
                (name, description or None),
            )

    # بعد أي إضافة/فشل فاليديشين نرجّع نفس الصفحة
    with get_db() as cur:
        (
            packing_types,
            pallet_types,
            packing_materials,
            packing_profiles,
            packing_items,
            profile_costs,
            packing_profile_overrides,
            products,
        ) = _load_packing_context(cur)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template(
            "settings/packing.html",
            packing_types=packing_types,
            pallet_types=pallet_types,
            packing_materials=packing_materials,
            packing_profiles=packing_profiles,
            packing_items=packing_items,
            profile_costs=profile_costs,
            packing_profile_overrides=packing_profile_overrides,
            products=products,
            editing_packing_profile=None,
            editing_packing_item=None,
        )

    if not name:
        return redirect(url_for("settings.packing_settings"))
    
    _bump_pricing_cache_version()

    flash("Packing type added.", "success")
    return redirect(url_for("settings.packing_settings"))


@settings_bp.route("/settings/packing/types/<int:type_id>/delete", methods=["POST"])
def delete_packing_type(type_id):
    with get_db() as cur:
        # أولاً نحذف كل الـ profiles لهذا الـ packing_type
        cur.execute(
            "SELECT id FROM packing_profiles WHERE packing_type_id = %s",
            (type_id,),
        )
        rows_profiles = cur.fetchall()
        profile_ids = [r[0] for r in rows_profiles] if rows_profiles else []

        if profile_ids:
            # نحذف items المرتبطة بهذه الـ profiles
            cur.execute(
                "DELETE FROM packing_items WHERE packing_profile_id = ANY(%s)",
                (profile_ids,),
            )
            # نحذف الـ profiles نفسها
            cur.execute(
                "DELETE FROM packing_profiles WHERE id = ANY(%s)",
                (profile_ids,),
            )

        # أخيرًا نحذف الـ packing_type نفسه
        cur.execute("DELETE FROM packing_types WHERE id = %s", (type_id,))

        (
            packing_types,
            pallet_types,
            packing_materials,
            packing_profiles,
            packing_items,
            profile_costs,
            packing_profile_overrides,
            products,
        ) = _load_packing_context(cur)

    _bump_pricing_cache_version()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template(
            "settings/packing.html",
            packing_types=packing_types,
            pallet_types=pallet_types,
            packing_materials=packing_materials,
            packing_profiles=packing_profiles,
            packing_items=packing_items,
            profile_costs=profile_costs,
            packing_profile_overrides=packing_profile_overrides,
            products=products,
            editing_packing_profile=None,
            editing_packing_item=None,
        )

    flash("Packing type deleted.", "success")
    return redirect(url_for("settings.packing_settings"))


@settings_bp.route("/settings/packing/items/add", methods=["POST"])
def add_packing_item():
    packing_profile_id = int(request.form.get("packing_profile_id") or 0)
    material_id = int(request.form.get("material_id") or 0)
    item_name = (request.form.get("item_name") or "").strip()
    quantity_per_pallet = (request.form.get("quantity_per_pallet") or "").strip()

    error = None

    if packing_profile_id <= 0:
        error = "Please select packing profile."
    elif material_id <= 0:
        error = "Please select packing material."
    else:
        try:
            qty_val = float(quantity_per_pallet)
            if qty_val <= 0:
                raise ValueError()
        except ValueError:
            error = "Quantity per pallet must be positive number."

    if error:
        flash(error, "danger")
    else:
        with get_db() as cur:
            cur.execute(
                """
                INSERT INTO packing_items (
                    packing_profile_id,
                    material_id,
                    item_name,
                    quantity_per_pallet
                )
                VALUES (%s, %s, %s, %s)
                """,
                (packing_profile_id, material_id, item_name or None, qty_val),
            )
        flash("Packing item added.", "success")
        _bump_pricing_cache_version()

    with get_db() as cur:
        (
            packing_types,
            pallet_types,
            packing_materials,
            packing_profiles,
            packing_items,
            profile_costs,
            packing_profile_overrides,
            products,
        ) = _load_packing_context(cur)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template(
            "settings/packing.html",
            packing_types=packing_types,
            pallet_types=pallet_types,
            packing_materials=packing_materials,
            packing_profiles=packing_profiles,
            packing_items=packing_items,
            profile_costs=profile_costs,
            packing_profile_overrides=packing_profile_overrides,
            products=products,
            editing_packing_profile=None,
            editing_packing_item=None,
        )

    return redirect(url_for("settings.packing_settings"))


@settings_bp.route("/settings/packing/items/<int:item_id>/delete", methods=["POST"])
def delete_packing_item(item_id):
    with get_db() as cur:
        cur.execute("DELETE FROM packing_items WHERE id = %s", (item_id,))

        (
            packing_types,
            pallet_types,
            packing_materials,
            packing_profiles,
            packing_items,
            profile_costs,
            packing_profile_overrides,
            products,
        ) = _load_packing_context(cur)
        
    _bump_pricing_cache_version()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template(
            "settings/packing.html",
            packing_types=packing_types,
            pallet_types=pallet_types,
            packing_materials=packing_materials,
            packing_profiles=packing_profiles,
            packing_items=packing_items,
            profile_costs=profile_costs,
            packing_profile_overrides=packing_profile_overrides,
            products=products,
            editing_packing_profile=None,
            editing_packing_item=None,
        )
        

    flash("Packing item deleted.", "success")
    return redirect(url_for("settings.packing_settings"))


@settings_bp.route("/settings/packing/items/<int:item_id>/edit", methods=["POST"])
def edit_packing_item_load(item_id):
    with get_db() as cur:
        (
            packing_types,
            pallet_types,
            packing_materials,
            packing_profiles,
            packing_items,
            profile_costs,
            packing_profile_overrides,
            products,
        ) = _load_packing_context(cur)

        cur.execute(
            """
            SELECT
                pi.id,
                pi.packing_profile_id,
                pi.material_id,
                COALESCE(pi.item_name, ''),
                pi.quantity_per_pallet
            FROM packing_items pi
            WHERE pi.id = %s
            """,
            (item_id,),
        )
        editing_packing_item = cur.fetchone()

    if not editing_packing_item:
        flash("Packing item not found.", "danger")

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template(
            "settings/packing.html",
            packing_types=packing_types,
            pallet_types=pallet_types,
            packing_materials=packing_materials,
            packing_profiles=packing_profiles,
            packing_items=packing_items,
            profile_costs=profile_costs,
            packing_profile_overrides=packing_profile_overrides,
            products=products,
            editing_packing_profile=None,
            editing_packing_item=editing_packing_item,
        )

    return redirect(url_for("settings.packing_settings"))


@settings_bp.route("/settings/packing/items/<int:item_id>/update", methods=["POST"])
def update_packing_item(item_id):
    packing_profile_id = int(request.form.get("packing_profile_id") or 0)
    material_id = int(request.form.get("material_id") or 0)
    item_name = (request.form.get("item_name") or "").strip()
    quantity_per_pallet = (request.form.get("quantity_per_pallet") or "").strip()

    error = None

    if packing_profile_id <= 0:
        error = "Please select packing profile."
    elif material_id <= 0:
        error = "Please select packing material."
    else:
        try:
            qty_val = float(quantity_per_pallet)
            if qty_val <= 0:
                raise ValueError()
        except ValueError:
            error = "Quantity per pallet must be positive number."

    if error:
        flash(error, "danger")
    else:
        with get_db() as cur:
            cur.execute(
                """
                UPDATE packing_items
                SET packing_profile_id   = %s,
                    material_id          = %s,
                    item_name            = %s,
                    quantity_per_pallet  = %s
                WHERE id = %s
                """,
                (
                    packing_profile_id,
                    material_id,
                    item_name or None,
                    qty_val,
                    item_id,
                ),
            )
        flash("Packing item updated.", "success")
        _bump_pricing_cache_version()

    with get_db() as cur:
        (
            packing_types,
            pallet_types,
            packing_materials,
            packing_profiles,
            packing_items,
            profile_costs,
            packing_profile_overrides,
            products,
        ) = _load_packing_context(cur)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        # بعد التحديث نرجّع الفورم لوضع Add
        return render_template(
            "settings/packing.html",
            packing_types=packing_types,
            pallet_types=pallet_types,
            packing_materials=packing_materials,
            packing_profiles=packing_profiles,
            packing_items=packing_items,
            profile_costs=profile_costs,
            packing_profile_overrides=packing_profile_overrides,
            products=products,
            editing_packing_profile=None,
            editing_packing_item=None,
        )

    return redirect(url_for("settings.packing_settings"))

@settings_bp.route("/settings/packing/profiles/add", methods=["POST"])
def add_packing_profile():
    name = (request.form.get("profile_name") or "").strip()
    packing_type_id = int(request.form.get("packing_type_id") or 0)
    pallet_type_id = int(request.form.get("pallet_type_id") or 0)
    is_global = bool(request.form.get("is_global"))

    error = None
    if not name:
        error = "Profile name is required."
    elif packing_type_id <= 0:
        error = "Please select packing type."
    elif pallet_type_id <= 0:
        error = "Please select pallet type."

    if error:
        flash(error, "danger")
    else:
        with get_db() as cur:
            cur.execute(
                """
                INSERT INTO packing_profiles (name, packing_type_id, pallet_type_id, is_global)
                VALUES (%s, %s, %s, %s)
                """,
                (name, packing_type_id, pallet_type_id, is_global),
            )
        flash("Packing profile added.", "success")
        _bump_pricing_cache_version()

    with get_db() as cur:
        (
            packing_types,
            pallet_types,
            packing_materials,
            packing_profiles,
            packing_items,
            profile_costs,
            packing_profile_overrides,
            products,
        ) = _load_packing_context(cur)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template(
            "settings/packing.html",
            packing_types=packing_types,
            pallet_types=pallet_types,
            packing_materials=packing_materials,
            packing_profiles=packing_profiles,
            packing_items=packing_items,
            profile_costs=profile_costs,
            packing_profile_overrides=packing_profile_overrides,
            products=products,
            editing_packing_profile=None,
            editing_packing_item=None,
        )

    return redirect(url_for("settings.packing_settings"))

@settings_bp.route("/settings/packing/profiles/<int:profile_id>/delete", methods=["POST"])
def delete_packing_profile(profile_id):
    with get_db() as cur:
        # نحذف items المرتبطة بالبروفايل
        cur.execute("DELETE FROM packing_items WHERE packing_profile_id = %s", (profile_id,))
        # ثم البروفايل نفسه
        cur.execute("DELETE FROM packing_profiles WHERE id = %s", (profile_id,))

        (
            packing_types,
            pallet_types,
            packing_materials,
            packing_profiles,
            packing_items,
            profile_costs,
            packing_profile_overrides,
            products,
        ) = _load_packing_context(cur)

    _bump_pricing_cache_version()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template(
            "settings/packing.html",
            packing_types=packing_types,
            pallet_types=pallet_types,
            packing_materials=packing_materials,
            packing_profiles=packing_profiles,
            packing_items=packing_items,
            profile_costs=profile_costs,
            packing_profile_overrides=packing_profile_overrides,
            products=products,
            editing_packing_profile=None,
            editing_packing_item=None,
        )

    flash("Packing profile deleted.", "success")
    return redirect(url_for("settings.packing_settings"))

@settings_bp.route("/settings/packing/overrides/<int:override_id>/edit", methods=["POST"])
def edit_packing_profile_override_load(override_id):
    with get_db() as cur:
        (
            packing_types,
            pallet_types,
            packing_materials,
            packing_profiles,
            packing_items,
            profile_costs,
            packing_profile_overrides,
            products,
        ) = _load_packing_context(cur)

        cur.execute(
            """
            SELECT
                o.id,
                o.packing_profile_id,
                o.product_id,
                o.roll_weight_min,
                o.roll_weight_max,
                o.is_active
            FROM packing_profile_overrides o
            WHERE o.id = %s
            """,
            (override_id,),
        )
        editing_override = cur.fetchone()

    if not editing_override:
        flash("Override not found.", "danger")

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template(
            "settings/packing.html",
            packing_types=packing_types,
            pallet_types=pallet_types,
            packing_materials=packing_materials,
            packing_profiles=packing_profiles,
            packing_items=packing_items,
            profile_costs=profile_costs,
            packing_profile_overrides=packing_profile_overrides,
            products=products,
            editing_packing_profile=None,
            editing_packing_item=None,
            editing_override=editing_override,
        )

    return redirect(url_for("settings.packing_settings"))

@settings_bp.route("/settings/packing/overrides/<int:override_id>/update", methods=["POST"])
def update_packing_profile_override(override_id):
    product_id = int(request.form.get("product_id") or 0)
    packing_profile_id = int(request.form.get("profile_id") or 0)
    roll_weight_min = (request.form.get("roll_weight_min") or "").strip()
    roll_weight_max = (request.form.get("roll_weight_max") or "").strip()
    is_active = bool(request.form.get("is_active"))

    print("OVERRIDE UPDATE FORM:", dict(request.form))

    error = None

    if product_id <= 0:
        error = "Please select product."
    elif packing_profile_id <= 0:
        error = "Please select packing profile."
    else:
        try:
            w_min = float(roll_weight_min)
            w_max = float(roll_weight_max)
            if w_min < 0 or w_max <= 0 or w_min > w_max:
                raise ValueError()
        except ValueError:
            error = "Roll weight range is invalid."

    if error:
        flash(error, "danger")
    else:
        with get_db() as cur:
            cur.execute(
                """
                UPDATE packing_profile_overrides
                SET packing_profile_id = %s,
                    product_id         = %s,
                    roll_weight_min    = %s,
                    roll_weight_max    = %s,
                    is_active          = %s
                WHERE id = %s
                """,
                (packing_profile_id, product_id, w_min, w_max, is_active, override_id),
            )
        flash("Packing profile override updated.", "success")
        _bump_pricing_cache_version()

    # إعادة تحميل الداتا
    with get_db() as cur:
        (
            packing_types,
            pallet_types,
            packing_materials,
            packing_profiles,
            packing_items,
            profile_costs,
            packing_profile_overrides,
            products,
        ) = _load_packing_context(cur)

    # لو الطلب جاي AJAX (من المودال) → رجّع tbody بس
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        filtered_overrides = [o for o in packing_profile_overrides if o[1] == packing_profile_id]
        return render_template(
            "settings/_packing_overrides_tbody.html",
            packing_profile_overrides=filtered_overrides,
        )

    # غير كده → redirect عادي
    return redirect(url_for("settings.packing_settings"))

@settings_bp.route("/settings/packing/overrides/<int:override_id>/delete", methods=["POST"])
def delete_packing_profile_override(override_id):
    # نجيب profile_id للأوفررايد قبل الحذف عشان نفلتر عليه بعد كده
    profile_id = None
    with get_db() as cur:
        cur.execute(
            "SELECT packing_profile_id FROM packing_profile_overrides WHERE id = %s",
            (override_id,),
        )
        row = cur.fetchone()
        if row:
            profile_id = row[0]

        cur.execute(
            "DELETE FROM packing_profile_overrides WHERE id = %s",
            (override_id,),
        )

        (
            packing_types,
            pallet_types,
            packing_materials,
            packing_profiles,
            packing_items,
            profile_costs,
            packing_profile_overrides,
            products,
        ) = _load_packing_context(cur)

    _bump_pricing_cache_version()

    # لو الطلب جاي AJAX (من المودال) → رجّع tbody بس
    if request.headers.get("X-Requested-With") == "XMLHttpRequest" and profile_id:
        filtered_overrides = [o for o in packing_profile_overrides if o[1] == profile_id]
        return render_template(
            "settings/_packing_overrides_tbody.html",
            packing_profile_overrides=filtered_overrides,
        )

    # غير كده → redirect عادي
    flash("Packing profile override deleted.", "success")
    return redirect(url_for("settings.packing_settings"))

@settings_bp.route("/settings/packing/profiles/<int:profile_id>/set-default", methods=["POST"])
def set_packing_profile_default(profile_id):
    with get_db() as cur:
        # هات packing_type_id, pallet_type_id للبروفايل ده
        cur.execute(
            "SELECT packing_type_id, pallet_type_id FROM packing_profiles WHERE id = %s",
            (profile_id,),
        )
        row = cur.fetchone()
        if not row:
            flash("Packing profile not found.", "danger")
        else:
            packing_type_id, pallet_type_id = row

            # خلى كل البروفايلات لنفس النوع/البالتة مش global
            cur.execute(
                """
                UPDATE packing_profiles
                SET is_global = FALSE
                WHERE packing_type_id = %s AND pallet_type_id = %s
                """,
                (packing_type_id, pallet_type_id),
            )

            # خلى ده هو الـ global
            cur.execute(
                """
                UPDATE packing_profiles
                SET is_global = TRUE
                WHERE id = %s
                """,
                (profile_id,),
            )

            flash("Default packing profile updated.", "success")
            _bump_pricing_cache_version()

        (
            packing_types,
            pallet_types,
            packing_materials,
            packing_profiles,
            packing_items,
            profile_costs,
            packing_profile_overrides,
            products,
        ) = _load_packing_context(cur)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template(
            "settings/packing.html",
            packing_types=packing_types,
            pallet_types=pallet_types,
            packing_materials=packing_materials,
            packing_profiles=packing_profiles,
            packing_items=packing_items,
            profile_costs=profile_costs,
            packing_profile_overrides=packing_profile_overrides,
            products=products,
            editing_packing_profile=None,
            editing_packing_item=None,
            editing_override=None,
        )

    return redirect(url_for("settings.packing_settings"))

@settings_bp.route("/packing/profiles/<int:profile_id>/overrides/add", methods=["POST"])
def add_profile_overrides(profile_id):
    product_ids = request.form.getlist("product_id")  # multi-select
    roll_weight_min = (request.form.get("roll_weight_min") or "").strip()
    roll_weight_max = (request.form.get("roll_weight_max") or "").strip()
    is_active = bool(request.form.get("is_active"))

    error = None

    if not product_ids:
        error = "Please select at least one product."
    else:
        try:
            w_min = float(roll_weight_min)
            w_max = float(roll_weight_max)
            if w_min < 0 or w_max <= 0 or w_min > w_max:
                raise ValueError()
        except ValueError:
            error = "Roll weight range is invalid."

    if error:
        flash(error, "danger")
    else:
        with get_db() as cur:
            for pid in product_ids:
                pid_int = int(pid)
                if pid_int <= 0:
                    continue
                cur.execute(
                    """
                    INSERT INTO packing_profile_overrides (
                        packing_profile_id,
                        product_id,
                        roll_weight_min,
                        roll_weight_max,
                        is_active
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (profile_id, pid_int, w_min, w_max, is_active),
                )
        flash("Packing profile overrides added.", "success")
        _bump_pricing_cache_version()

    # إعادة تحميل الداتا
    with get_db() as cur:
        (
            packing_types,
            pallet_types,
            packing_materials,
            packing_profiles,
            packing_items,
            profile_costs,
            packing_profile_overrides,
            products,
        ) = _load_packing_context(cur)

    # لو جاي AJAX (من المودال) → رجّع tbody بس
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        filtered_overrides = [o for o in packing_profile_overrides if o[1] == profile_id]
        return render_template(
            "settings/_packing_overrides_tbody.html",
            packing_profile_overrides=filtered_overrides,
        )

    # غير كده → redirect عادي
    return redirect(url_for("settings.packing_settings"))