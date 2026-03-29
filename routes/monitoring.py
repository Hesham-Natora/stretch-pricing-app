from flask import Blueprint, render_template, abort
from flask_login import login_required, current_user
from db import get_db

monitoring_bp = Blueprint("monitoring", __name__, template_folder="../templates")

@monitoring_bp.route("/monitoring-report")
@login_required
def monitoring_report():
    if current_user.role not in ["admin", "owner", "sales_manager"]:
        abort(403)

    with get_db() as cur:
        # Materials
        cur.execute("""
            SELECT id, code, name, category, unit, unit_type, currency, price_per_unit
            FROM materials
            ORDER BY category, name
        """)
        materials = cur.fetchall()

        # FX (active only)
        cur.execute("""
            SELECT id, egp_per_usd, effective_date
            FROM currency_rates
            WHERE is_active = true
            ORDER BY id DESC
        """)
        fx_rates = cur.fetchall()

        # Margin factors
        cur.execute("""
            SELECT id,
                   micron_min,
                   micron_max,
                   film_type,
                   is_manual,
                   roll_weight_min,
                   roll_weight_max,
                   margin_percent
            FROM pricing_rules
            ORDER BY film_type, is_manual DESC, micron_min
        """)
        pricing_rules = cur.fetchall()

        # Extra price settings
        cur.execute("""
            SELECT id,
                   color_extra_usd_per_kg,
                   prestretch_extra_usd_per_kg,
                   foreign_extra_mode,
                   foreign_extra_value
            FROM pricing_extras
            ORDER BY id DESC
        """)
        pricing_extras = cur.fetchall()

        # FOB costs
        cur.execute("""
            SELECT fc.id,
                   p.name AS port_name,
                   p.country AS port_country,
                   fc.fob_cost_usd_per_container
            FROM fob_costs fc
            JOIN ports p ON fc.port_id = p.id
            ORDER BY p.country, p.name
        """)
        fob_costs = cur.fetchall()

        # Sea freight rates
        cur.execute("""
            SELECT sfr.id,
                   lp.name  AS loading_port_name,
                   lp.country AS loading_port_country,
                   d.country AS dest_country,
                   d.city    AS dest_city,
                   sfr.shipping_rate_usd_per_container,
                   sfr.carrier_name
            FROM sea_freight_rates sfr
            JOIN ports lp        ON sfr.loading_port_id = lp.id
            JOIN destinations d  ON sfr.destination_id = d.id
            ORDER BY lp.country, lp.name, d.country, COALESCE(d.city, '')
        """)
        sea_freight_rates = cur.fetchall()

    return render_template(
        "monitoring_report.html",
        materials=materials,
        fx_rates=fx_rates,
        pricing_rules=pricing_rules,
        pricing_extras=pricing_extras,
        fob_costs=fob_costs,
        sea_freight_rates=sea_freight_rates,
    )