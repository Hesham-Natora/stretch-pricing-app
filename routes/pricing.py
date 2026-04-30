from collections import defaultdict
import math
import time
from decimal import Decimal, ROUND_HALF_UP


from flask import (
    Blueprint,
    render_template,
    request,
    flash,
    redirect,
    url_for,
    session,
    jsonify,
    current_app,
    make_response,
)

from db import get_db
from services.costing import (
    get_material_landed_price_per_kg,
    get_energy_rate_usd_per_kwh,
    get_materials_landed_price_per_kg_bulk,
    get_semi_total_cost_per_kg,
    get_semi_price_net_per_kg,
    get_semi_price_net_per_kg_with_width
)

#from xhtml2pdf import pisa
from io import BytesIO
from flask import make_response

from flask_login import login_required, current_user
from routes.auth import roles_required

pricing_bp = Blueprint(
    "pricing", __name__, template_folder="../templates/pricing"
)

_PRICING_STATIC_CACHE = {"version": None, "data": None}


def generate_next_quotation_number(cur):
    """Auto-generate quotation number"""
    cur.execute("SELECT nextval('quotation_number_seq')")
    seq_num = cur.fetchone()[0]
    return f"Quote No. {seq_num:04d}"


def invalidate_pricing_static_cache():
    global _PRICING_STATIC_CACHE
    _PRICING_STATIC_CACHE = {"version": None, "data": None}
    

def round_up_2(x: float) -> float:
    """Round up to 2 decimal places."""
    if x is None:
        return 0.0
    return math.ceil(x * 100) / 100.0

def round_3(x) -> float:
    """Round (half up) to 3 decimal places."""
    if x is None:
        return 0.0
    return float(
        Decimal(str(x)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    )
    
    
def select_packing_profile_id_for_item(
    *,
    product_id: int,
    packing_type_id: int | None,
    pallet_type_id: int | None,
    gross_kg_per_roll: float,
    packing_profile_overrides: dict,
    packing_profiles_by_id: dict,
    global_profile_by_key: dict,
) -> int | None:
    """
    يختار packing_profile_id المناسب:
    1) لو فيه override للمنتج في المدى (roll_weight_min/max) يرجّع البروفايل ده.
    2) غير كده يرجع global profile لنفس (packing_type_id, pallet_type_id) لو موجود.
    3) وإلا None.
    """
    if not packing_type_id or not pallet_type_id:
        return None

    # 1) override per product + وزن الرول
    overrides_for_product = packing_profile_overrides.get(product_id, [])
    for ov in overrides_for_product:
        w_min = ov["roll_weight_min"]
        w_max = ov["roll_weight_max"]
        if (w_min == 0 and w_max == 0) or (
            gross_kg_per_roll >= w_min
            and (w_max == 0 or gross_kg_per_roll <= w_max)
        ):
            pid = ov["packing_profile_id"]
            if pid in packing_profiles_by_id:
                prof = packing_profiles_by_id[pid]
                # تأكد أن نوع الباكنج + نوع البالته متطابقين
                if (
                    prof["packing_type_id"] == packing_type_id
                    and prof["pallet_type_id"] == pallet_type_id
                ):
                    return pid

    # 2) global fallback
    key = (packing_type_id, pallet_type_id)
    pid = global_profile_by_key.get(key)
    if pid and pid in packing_profiles_by_id:
        return pid

    return None

def calculate_line_price_bulk(
    *,
    product_id: int,
    is_colored: bool,
    selected_payment_term_id: int,
    discount_percent: float,    
    roll_weight_kg: float,
    core_weight_kg: float,
    pallets_per_container: float,
    rolls_per_pallet: float,
    pallet_type_id: int | None,
    packing_type_id: int | None,
    core_price_per_kg_usd: float,
    packing_profile_cost_map: dict,
    packing_profiles_by_id: dict,
    packing_profile_overrides: dict,
    global_profile_by_key: dict,
    # bulk data maps
    product_info_map: dict,
    product_roll_bom_map: dict,
    energy_rate: float,
    product_machine_map: dict,
    machine_costs_map: dict,
    egp_per_usd: float,
    margin_rules_map: dict,
    pricing_extras: dict,
    payment_terms_map: dict,
    fob_cost_per_kg: float,
    sea_freight_per_kg: float,
    material_price_map: dict,
    width_mm: float,
    is_foreign_pricing: bool = False,
    price_basis: str = "gross",
):
    """
    نسخة bulk من calculate_line_price:
    - لا تستخدم DB داخلها نهائيًا.
    - منطق التسعير:
      film_cost_per_kg = RM + Energy + Machine (فقط الفيلم)
      total_cost_unit = film_unit + pack_core_unit
      margin على total_cost_unit
      Extras:
        - Color extra محسوب per kg gross ويتحوّل إلى extra per roll.
        - Prestretch extra محسوب per kg gross ويتحوّل إلى extra per roll.
        - إجمالي extra يضاف بعد المارجن على مستوى الوحدة (لفة).
    """

    # ===== 1) Product basic info =====
    p_info = product_info_map.get(product_id)
    if not p_info:
        return None, "Product not found"

    micron = p_info["micron"]
    film_type = p_info["film_type"]
    is_manual = p_info["is_manual"]
    bom_scrap_percent = p_info.get("bom_scrap_percent", 0.0)

    # kg per roll من الشاشة
    gross_kg_per_roll = max(float(roll_weight_kg or 0), 0.0)
    core_kg = max(float(core_weight_kg or 0), 0.0)
    net_kg_per_roll = max(gross_kg_per_roll - core_kg, 0.0)
    unit_weight_net = net_kg_per_roll  # وزن الفيلم الصافي لكل لفة
    net_kg_per_roll_safe = net_kg_per_roll if net_kg_per_roll > 0 else 0.0

    # ===== 2) Total cost per kg (RM + energy + machine OH) =====
    total_cost_per_kg = 0.0

    # --- اختيار roll BOM المناسب حسب وزن الرول ---
    # الشكل المتوقع بعد التعديل:
    # product_roll_bom_map[product_id] = [
    #   {
    #       "weight_from_kg": ...,
    #       "weight_to_kg": ...,
    #       "items": [
    #           {"material_id": ..., "semi_product_id": ..., "pct": ...},
    #           ...
    #       ],
    #   },
    #   ...
    # ]
    roll_boms_for_product = product_roll_bom_map.get(product_id, [])

    selected_bom_items = []
    if roll_boms_for_product and gross_kg_per_roll > 0:
        for rb in roll_boms_for_product:
            w_from = float(rb.get("weight_from_kg", 0) or 0)
            w_to = float(rb.get("weight_to_kg", 0) or 0)
            if (w_from == 0 and w_to == 0) or (
                gross_kg_per_roll >= w_from
                and (w_to == 0 or gross_kg_per_roll <= w_to)
            ):
                selected_bom_items = rb.get("items", [])
                break

    # لو ما فيش رول BOM مناسب → نرجّع رسالة خطأ واضحة
    if not selected_bom_items:
        return None, "No roll BOM found for this product and roll weight"

    # RM cost using material_price_map + semi *price* (نفس الـ UI)
    for item in selected_bom_items:
        material_id = item.get("material_id")
        semi_product_id = item.get("semi_product_id")
        pct = float(item.get("pct") or 0.0)

        price = 0.0

        if semi_product_id:
            semi_id = int(semi_product_id)
            try:
                # لو المنتج النهائي بريسترتش، نطبّق عرض الكوتيشن على السيمي runtime فقط
                if film_type == "Prestretch" and width_mm and width_mm > 0:
                    # شرط العرض الخاص بالسيمي مطبّق داخل get_semi_total_cost_per_kg_with_width
                    price = float(
                        get_semi_price_net_per_kg_with_width(semi_id, float(width_mm)) or 0.0
                    )
                else:
                    # باقي الحالات (كل المنتجات الأخرى) تفضل على السلوك القديم
                    price = float(
                        get_semi_price_net_per_kg(semi_id) or 0.0
                    )
            except Exception:
                price = 0.0
        elif material_id:
            # مادة خام عادية من الـ landed price map
            price = float(material_price_map.get(int(material_id), 0.0) or 0.0)

        total_cost_per_kg += pct * price

    eff_factor = 1 + (bom_scrap_percent / 100.0)
    total_cost_per_kg *= eff_factor

    # Energy + machine OH
    energy_cost_per_kg = 0.0
    machine_oh_per_kg = 0.0

    mappings = product_machine_map.get(product_id, [])
    if mappings:
        # اختار preferred أو أول واحد
        preferred = [m for m in mappings if m["preferred_machine"]]
        mapping_row = preferred[0] if preferred else mappings[0]

        kwh_per_kg = float(mapping_row["kwh_per_kg"] or 0)
        machine_id = mapping_row["machine_id"]
        monthly_capacity_kg = float(mapping_row["monthly_product_capacity_kg"] or 0)

        # utilization من إعدادات المكنة (machines)
        utilization_rate = float(mapping_row.get("utilization_rate") or 1.0)
        # لو اليوزر كتبها 85 بدل 0.85
        if utilization_rate > 1:
            utilization_rate = utilization_rate / 100.0

        actual_capacity_kg = monthly_capacity_kg * utilization_rate

        # Energy cost /kg (بدون تقريب داخلي)
        energy_cost_per_kg = kwh_per_kg * energy_rate

        # machine costs
        cost_rows = machine_costs_map.get(machine_id, [])

        fixed_monthly_egp = 0.0
        variable_per_kg_egp = 0.0
        for ct, amount in cost_rows:
            amt = float(amount or 0)
            if ct == "fixed_monthly":
                fixed_monthly_egp += amt
            elif ct == "variable_per_kg":
                variable_per_kg_egp += amt

        fixed_per_kg_usd = 0.0
        variable_per_kg_usd = 0.0
        if egp_per_usd > 0 and actual_capacity_kg > 0:
            fixed_per_kg_usd = (fixed_monthly_egp / actual_capacity_kg) / egp_per_usd
            variable_per_kg_usd = variable_per_kg_egp / egp_per_usd

        machine_oh_per_kg = fixed_per_kg_usd + variable_per_kg_usd

        # تعديل Energy + Machine OH حسب الـ width لو أقل من 500mm
        if width_mm and width_mm < 500:
            factor = 500.0 / width_mm
            energy_cost_per_kg *= factor
            machine_oh_per_kg *= factor

        # مفيش أي round هنا

    total_cost_per_kg += energy_cost_per_kg + machine_oh_per_kg

    # RM منفصلة (بدون core/packing/extras)
    rm_cost_per_kg = total_cost_per_kg - energy_cost_per_kg - machine_oh_per_kg
    # برضه بدون round_3 هنا

    # ===== 3) Core + Packing cost =====
    core_cost_per_unit = 0.0
    core_cost_per_kg = 0.0

    core_weight = max(float(core_weight_kg or 0), 0.0)
    if core_weight > 0 and core_price_per_kg_usd > 0 and net_kg_per_roll_safe > 0:
        core_cost_per_unit = core_price_per_kg_usd * core_weight  # USD per roll
        core_cost_per_kg = core_cost_per_unit / net_kg_per_roll_safe

    packing_cost_per_unit = 0.0
    packing_cost_per_kg = 0.0
    if (
        packing_type_id
        and pallet_type_id
        and rolls_per_pallet
        and net_kg_per_roll_safe > 0
    ):
        profile_id = select_packing_profile_id_for_item(
            product_id=product_id,
            packing_type_id=packing_type_id,
            pallet_type_id=pallet_type_id,
            gross_kg_per_roll=gross_kg_per_roll,
            packing_profile_overrides=packing_profile_overrides,
            packing_profiles_by_id=packing_profiles_by_id,
            global_profile_by_key=global_profile_by_key,
        )
        if profile_id:
            cost_info = packing_profile_cost_map.get(profile_id)
            if cost_info:
                total_packing_usd_per_pallet = cost_info["usd"]
                if rolls_per_pallet > 0:
                    packing_cost_per_unit = (
                        total_packing_usd_per_pallet / rolls_per_pallet
                    )
                    packing_cost_per_kg = (
                        packing_cost_per_unit / net_kg_per_roll_safe
                    )
    # ===== شرط خاص بالبريسترتش مع البوكس (packing_type_id = 4, box material_id = 16) =====
    if (
        film_type == "Prestretch"
        and int(packing_type_id or 0) == 4
        and rolls_per_pallet
        and rolls_per_pallet > 0
    ):
        # عدد البوكسات في البالتة = عدد الرولات / 6
        # لو عايز تعملها ceil لآخر كرتونة كاملة، استبدل السطر اللي تحت بسطر math.ceil
        boxes_per_pallet = rolls_per_pallet / 6.0
        # مثال لو حبيت بعدين:
        # import math
        # boxes_per_pallet = math.ceil(rolls_per_pallet / 6.0)

        # سعر البوكس من material_id = 16 (من نفس الـ material_price_map المستخدم في الـ BOM)
        # سعر البوكس من materials.id = 16 مع تحويل العملة لـ USD
        box_price_usd = 0.0
        with get_db() as cur:
            cur.execute(
                """
                SELECT price_per_unit, currency
                FROM materials
                WHERE id = %s
                """,
                (16,),
            )
            row_box = cur.fetchone()

        if row_box:
            box_price_value = float(row_box[0] or 0)
            box_currency = (row_box[1] or "USD").strip().upper()

            if box_price_value > 0:
                if box_currency == "USD":
                    box_price_usd = box_price_value
                else:
                    # نفترض EGP → USD باستخدام نفس egp_per_usd الممرّر للدالة
                    if egp_per_usd > 0:
                        box_price_usd = box_price_value / egp_per_usd

        print(
            "DBG_BOX",
            "rolls_per_pallet=", rolls_per_pallet,
            "boxes_per_pallet=", boxes_per_pallet,
            "box_price_usd=", box_price_usd,
        )

        if box_price_usd > 0:
            # إجمالي تكلفة البوكسات للبالتة
            total_box_cost_per_pallet = boxes_per_pallet * box_price_usd

            # تكلفة البوكس لكل لفة
            extra_box_cost_per_unit = total_box_cost_per_pallet / rolls_per_pallet

            # أضفها على packing_cost_per_unit
            packing_cost_per_unit += extra_box_cost_per_unit

            # حدّث packing_cost_per_kg بناءً على الكيلو الصافي للرول
            if net_kg_per_roll_safe > 0:
                packing_cost_per_kg = packing_cost_per_unit / net_kg_per_roll_safe

    # ===== 4) Margin + extras + payment term =====

    # margin rule (micron / film_type / packing_type / roll_weight)
    if not packing_type_id:
        return None, "Packing type is required for margin"

    rules_for_key = margin_rules_map.get((film_type, int(packing_type_id)), [])
    margin_percent = None
    for r in rules_for_key:
        if r["micron_min"] <= micron <= r["micron_max"]:
            min_w = r["roll_weight_min"]
            max_w = r["roll_weight_max"]
            if (min_w == 0 and max_w == 0) or (
                gross_kg_per_roll >= min_w and gross_kg_per_roll <= max_w
            ):
                margin_percent = r["margin_percent"]
                break

    if margin_percent is None:
        return None, "No margin factor found"

    row_pt = payment_terms_map.get(selected_payment_term_id)
    if not row_pt:
        return None, "Payment term not found"

    # ===== Extras: color on gross, prestretch on net =====
    color_extra_gross = float(pricing_extras.get("color_extra_usd_per_kg", 0.0))
    prestretch_extra_gross = float(pricing_extras.get("prestretch_extra_usd_per_kg", 0.0))
    foreign_extra_mode = pricing_extras.get("foreign_extra_mode", "percent")
    foreign_extra_value = float(pricing_extras.get("foreign_extra_value", 0.0))

    # 1) Extra per roll من اللون (per kg gross)
    color_extra_roll = 0.0
    if is_colored and gross_kg_per_roll > 0:
        color_extra_roll = color_extra_gross * gross_kg_per_roll

    # 2) Extra per roll من prestretch (per kg net)
    prestretch_extra_roll = 0.0
    if film_type == "Prestretch" and gross_kg_per_roll > 0:
        prestretch_extra_roll = prestretch_extra_gross * gross_kg_per_roll

    # إجمالي Extra per roll
    extra_roll = color_extra_roll + prestretch_extra_roll

    # ===== منطق الكوست بالضبط =====
    # 1) تكلفة الفيلم الصافي /kg كبداية عامة (net-style)
    film_cost_per_kg = rm_cost_per_kg + energy_cost_per_kg + machine_oh_per_kg

    # 3) Pack+core per unit
    pack_core_unit = core_cost_per_unit + packing_cost_per_unit

    # ===== 3.5) لوجيك خاص للبريسترتش =====
    material_cost_per_kg_gross = None
    core_cost_per_kg_gross = None
    packing_cost_per_kg_gross = None
    total_cost_per_kg_gross_prestretch = None

    if film_type == "Prestretch" and gross_kg_per_roll > 0 and net_kg_per_roll > 0:
        # A: material على gross
        material_cost_per_kg_gross = (rm_cost_per_kg * net_kg_per_roll) / gross_kg_per_roll

        # B: core على gross
        core_cost_per_kg_gross = core_cost_per_unit / gross_kg_per_roll if core_cost_per_unit > 0 else 0.0

        # C: packing على gross
        packing_cost_per_kg_gross = packing_cost_per_unit / gross_kg_per_roll if packing_cost_per_unit > 0 else 0.0

        # D: إجمالي تكلفة البريسترتش per kg gross
        total_cost_per_kg_gross_prestretch = (
            material_cost_per_kg_gross
            + core_cost_per_kg_gross
            + packing_cost_per_kg_gross
        )

        # نستخدم تعريف gross للبريسترتش
        film_cost_per_kg = material_cost_per_kg_gross
        total_cost_per_kg = total_cost_per_kg_gross_prestretch

        # تكلفة الفيلم في الوحدة على أساس gross
        film_cost_unit = film_cost_per_kg * gross_kg_per_roll
    else:
        # باقي المنتجات: نفس اللوجيك القديم (net-style)
        film_cost_unit = film_cost_per_kg * unit_weight_net

    # 4) إجمالي تكلفة الوحدة قبل margin/extra
    total_cost_unit = film_cost_unit + pack_core_unit

    # 5) الخصم يقلل المارجن فقط
    raw_discount_pct = float(discount_percent or 0)
    effective_margin_pct = margin_percent - raw_discount_pct
    if effective_margin_pct < 0:
        effective_margin_pct = 0.0

    # 6) Margin value /unit
    margin_value_unit = total_cost_unit * (effective_margin_pct / 100.0)

    # 7) Extra /unit (per roll)
    extra_unit = extra_roll

    # 8) EXW /unit (cash)
    exw_unit_cash = total_cost_unit + margin_value_unit + extra_unit
    if exw_unit_cash < total_cost_unit:
        exw_unit_cash = total_cost_unit

    # 10) FOB /unit و CFR /unit (cash)
    fob_cost_unit = fob_cost_per_kg * gross_kg_per_roll if gross_kg_per_roll > 0 else 0.0
    sea_freight_cost_unit = (
        sea_freight_per_kg * gross_kg_per_roll if gross_kg_per_roll > 0 else 0.0
    )

    fob_unit_cash = exw_unit_cash + fob_cost_unit
    cfr_unit_cash = fob_unit_cash + sea_freight_cost_unit

    # ===== 11) payment term surcharge على سعر الفاتورة لكل إنكوترم =====
    credit_days = int(row_pt["credit_days"] or 0)
    annual_rate_percent = float(row_pt["annual_rate_percent"] or 0)
    daily_rate = annual_rate_percent / 365.0 if annual_rate_percent else 0.0
    credit_surcharge_percent = daily_rate * credit_days
    credit_factor = 1 + (credit_surcharge_percent / 100.0)

    # نطبّق الكريديت على EXW و FOB و CFR كل واحد لوحده (مش تراكمي)
    exw_unit_raw = exw_unit_cash * credit_factor
    fob_unit_raw = fob_unit_cash * credit_factor
    cfr_unit_raw = cfr_unit_cash * credit_factor

    # نبدأ بـ final = raw (لو مفيش foreign extra هيفضلوا هما هما)
    exw_unit_final = exw_unit_raw
    fob_unit_final = fob_unit_raw
    cfr_unit_final = cfr_unit_raw

    # ===== Foreign sellers extra (على الأجانب فقط) =====
    if is_foreign_pricing and foreign_extra_value > 0:
        if foreign_extra_mode == "percent":
            factor = 1.0 + (foreign_extra_value / 100.0)
            exw_unit_final *= factor
            fob_unit_final *= factor
            cfr_unit_final *= factor

        elif foreign_extra_mode == "per_unit":
            # القيمة في الإعدادات = مبلغ ثابت per kg gross
            extra_per_kg_gross = foreign_extra_value

            # الزيادة لكل لفة = per kg gross × gross_kg_per_roll
            if gross_kg_per_roll > 0:
                extra_per_unit = extra_per_kg_gross * gross_kg_per_roll
            else:
                extra_per_unit = 0.0

            exw_unit_final += extra_per_unit
            fob_unit_final += extra_per_unit
            cfr_unit_final += extra_per_unit

    # ===== اشتقاق base من unit_final (بدون أي ROUNDUP) =====
    # ===== اشتقاق base حسب نوع الفيلم =====
    gross = gross_kg_per_roll
    net = unit_weight_net

    def round_up_2(x: float) -> float:
        return math.ceil(x * 100) / 100.0

    if film_type == "Prestretch":
        # ===== منطق جديد للبريسترتش: ROUNDUP على /kg net =====

        # 1) base per kg gross من unit_final
        if gross > 0:
            exw_kg_gross_base = exw_unit_final / gross
            fob_kg_gross_base = fob_unit_final / gross
            cfr_kg_gross_base = cfr_unit_final / gross
        else:
            exw_kg_gross_base = fob_kg_gross_base = cfr_kg_gross_base = 0.0

        # 2) base per roll من unit_final
        exw_roll_base = exw_unit_final
        fob_roll_base = fob_unit_final
        cfr_roll_base = cfr_unit_final

        # 3) base per kg net من unit_final
        if net > 0:
            exw_kg_net_base = exw_roll_base / net
            fob_kg_net_base = fob_roll_base / net
            cfr_kg_net_base = cfr_roll_base / net
        else:
            exw_kg_net_base = fob_kg_net_base = cfr_kg_net_base = 0.0

        # EXW: نسيبه base
        exw_kg_net = exw_kg_net_base
        exw_kg_gross = exw_kg_gross_base

        # FOB/CFR: ROUNDUP على /kg net ثم نرجع للرول
        if net > 0:
            fob_kg_net = round_up_2(fob_kg_net_base)
            cfr_kg_net = round_up_2(cfr_kg_net_base)

            exw_roll = exw_kg_net * net
            fob_roll = fob_kg_net * net
            cfr_roll = cfr_kg_net * net
        else:
            fob_kg_net = cfr_kg_net = 0.0
            exw_roll = fob_roll = cfr_roll = 0.0

        # /kg gross للعرض فقط: نشتقها من أسعار الرول
        if gross > 0:
            fob_kg_gross = fob_roll / gross
            cfr_kg_gross = cfr_roll / gross
        else:
            fob_kg_gross = cfr_kg_gross = 0.0

    else:
        # ===== باقي المنتجات: المنطق القديم كما كان =====

        # 1) base per kg gross من unit_final
        if gross_kg_per_roll > 0:
            exw_kg_gross_base = exw_unit_final / gross_kg_per_roll
            fob_kg_gross_base = fob_unit_final / gross_kg_per_roll
            cfr_kg_gross_base = cfr_unit_final / gross_kg_per_roll
        else:
            exw_kg_gross_base = fob_kg_gross_base = cfr_kg_gross_base = 0.0

        # base per roll
        exw_roll_base = exw_unit_final
        fob_roll_base = fob_unit_final
        cfr_roll_base = cfr_unit_final

        # base per kg net
        if unit_weight_net > 0:
            exw_kg_net_base = exw_roll_base / unit_weight_net
            fob_kg_net_base = fob_roll_base / unit_weight_net
            cfr_kg_net_base = cfr_roll_base / unit_weight_net
        else:
            exw_kg_net_base = fob_kg_net_base = cfr_kg_net_base = 0.0

        # raw per kg gross
        if gross_kg_per_roll > 0:
            exw_gross_kg_raw = exw_unit_final / gross_kg_per_roll
            fob_gross_kg_raw = fob_unit_final / gross_kg_per_roll
            cfr_gross_kg_raw = cfr_unit_final / gross_kg_per_roll
        else:
            exw_gross_kg_raw = fob_gross_kg_raw = cfr_gross_kg_raw = 0.0

        # تقريب على kg gross (المنطق القديم)
        exw_kg_gross = exw_gross_kg_raw
        fob_kg_gross = round_up_2(fob_gross_kg_raw)
        cfr_kg_gross = round_up_2(cfr_gross_kg_raw)

        # roll من kg gross المقرّبة
        exw_roll = exw_kg_gross * gross_kg_per_roll
        fob_roll = fob_kg_gross * gross_kg_per_roll
        cfr_roll = cfr_kg_gross * gross_kg_per_roll

        # kg net من roll
        if unit_weight_net > 0:
            exw_kg_net = exw_roll / unit_weight_net
            fob_kg_net = fob_roll / unit_weight_net
            cfr_kg_net = cfr_roll / unit_weight_net
        else:
            exw_kg_net = fob_kg_net = cfr_kg_net = 0.0
    # ===== 12) cost_base_per_kg لأغراض العرض فقط =====
    if film_type == "Prestretch" and total_cost_per_kg is not None:
        # للبريسترتش: نخلي cost_base_per_kg = D (إجمالي الكوست per kg gross)
        cost_base_per_kg = total_cost_per_kg
    else:
        # لباقي المنتجات: نفس التعريف القديم (net-style)
        cost_base_per_kg = film_cost_per_kg + core_cost_per_kg + packing_cost_per_kg

    line_result = {
        "product_id": product_id,
        "film_type": film_type,
        "is_manual": is_manual,
        "gross_kg_per_roll": gross_kg_per_roll,
        "net_kg_per_roll": net_kg_per_roll,
        "rm_cost_per_kg": round_3(rm_cost_per_kg),
        "energy_cost_per_kg": round_3(energy_cost_per_kg),
        "machine_oh_per_kg": round_3(machine_oh_per_kg),
        "core_cost_per_kg": round(core_cost_per_kg, 2),
        "packing_cost_per_kg": round(packing_cost_per_kg, 2),
        "cost_base_per_kg": round(cost_base_per_kg, 2),
        "total_cost_per_kg": round(total_cost_per_kg, 2),
        "fob_cost_per_kg": round(fob_cost_per_kg, 2),
        "sea_freight_per_kg": round(sea_freight_per_kg, 2),
        "fob_cost_unit_roll": round(fob_cost_unit, 4),
        "sea_freight_cost_unit_roll": round(sea_freight_cost_unit, 4),
        "extra_roll": round(extra_roll, 4),
        "color_extra_roll": round(color_extra_roll, 4),
        "prestretch_extra_roll": round(prestretch_extra_roll, 4),
        "margin_percent": margin_percent,
        "payment_term_name": row_pt["name"],
        "credit_days": credit_days,
        "credit_surcharge_percent": credit_surcharge_percent,
        "exw": {
            "kg_net": exw_kg_net,
            "kg_gross": exw_kg_gross,
            "kg_gross_base": exw_kg_gross_base,
            "roll": exw_roll,
        },
        "fob": {
            "kg_net": fob_kg_net,
            "kg_gross": fob_kg_gross,
            "kg_gross_base": fob_kg_gross_base,
            "roll": fob_roll,
        },
        "cfr": {
            "kg_net": cfr_kg_net,
            "kg_gross": cfr_kg_gross,
            "kg_gross_base": cfr_kg_gross_base,
            "roll": cfr_roll,
        },
        "discount_percent": raw_discount_pct,
        "discounted": {
            "exw_kg_net": exw_kg_net,
            "exw_kg_gross": exw_kg_gross,
            "exw_roll": exw_roll,
            "fob_kg_net": fob_kg_net,
            "fob_kg_gross": fob_kg_gross,
            "fob_roll": fob_roll,
            "cfr_kg_net": cfr_kg_net,
            "cfr_kg_gross": cfr_kg_gross,
            "cfr_roll": cfr_roll,
            "exw_kg_gross_base": exw_kg_gross_base,
            "fob_kg_gross_base": fob_kg_gross_base,
            "cfr_kg_gross_base": cfr_kg_gross_base,
            "exw_kg_net_base": exw_kg_net_base,
            "fob_kg_net_base": fob_kg_net_base,
            "cfr_kg_net_base": cfr_kg_net_base,
            "exw_roll_base": exw_roll_base,
            "fob_roll_base": fob_roll_base,
            "cfr_roll_base": cfr_roll_base,
        },
        "foreign_extra_mode": foreign_extra_mode,
        "foreign_extra_value": foreign_extra_value,
    }

    return line_result, None

def load_pricing_static_data(cur, egp_per_usd: float):
    """
    يحمل البيانات الثابتة للتسعير (rules, extras, packing, energy, core price, payment terms)
    مع كاش داخلي مبني على cache_version من جدول pricing_cache_control.
    """
    global _PRICING_STATIC_CACHE

    # 1) نقرأ cache_version + updated_at من الجدول الصغير
    cur.execute(
        """
        SELECT cache_version, updated_at
        FROM pricing_cache_control
        WHERE id = 1
        """
    )
    row_cv = cur.fetchone()
    if not row_cv:
        return {
            "margin_rules_map": defaultdict(list),
            "payment_terms_map": {},
            "pricing_extras": {
                "color_extra_usd_per_kg": 0.0,
                "prestretch_extra_usd_per_kg": 0.0,
                "foreign_extra_mode": "percent",
                "foreign_extra_value": 0.0,
            },
            "packing_profiles_by_id": {},
            "global_profile_by_key": {},
            "packing_profile_overrides": defaultdict(list),
            "packing_profile_cost_map": defaultdict(lambda: {"usd": 0.0, "egp": 0.0}),
            "packing_cost_per_pallet_global": {},
            "energy_rate": 0.0,
            "core_price_standard_usd": 0.0,
            "core_price_prestretch_usd": 0.0,
            "product_machine_map": defaultdict(list),
            "machine_costs_map": defaultdict(list),
            "product_info_map": {},
            "product_roll_bom_map": defaultdict(list),
            "material_price_map": {},
            "shipping_rates_map": {},
            "cache_updated_at": None,
            "cache_version": None,
        }

    cache_version_db, cache_updated_at = row_cv[0], row_cv[1]

    # 2) لو النسخة نفس اللي في الكاش وفي data موجودة → استخدم الكاش
    if (
        _PRICING_STATIC_CACHE["data"] is not None
        and _PRICING_STATIC_CACHE["version"] == cache_version_db
    ):
        data = _PRICING_STATIC_CACHE["data"]
        data["cache_updated_at"] = cache_updated_at
        data["cache_version"] = cache_version_db
        return data

    # 3) لازم نعمل reload من الداتابيز لأن النسخة تغيّرت أو الكاش فاضي

    # --- pricing_rules bulk ---
    cur.execute(
        """
        SELECT micron_min,
               micron_max,
               film_type,
               packing_type_id,
               roll_weight_min,
               roll_weight_max,
               margin_percent
        FROM pricing_rules
        """
    )
    rows_rules = cur.fetchall()
    margin_rules_map = defaultdict(list)
    for (
        micron_min,
        micron_max,
        film_type,
        packing_type_id,
        rw_min,
        rw_max,
        margin_percent,
    ) in rows_rules:
        key = ((film_type or "standard").strip(), int(packing_type_id or 0))
        margin_rules_map[key].append(
            {
                "micron_min": int(micron_min or 0),
                "micron_max": int(micron_max or 0),
                "roll_weight_min": float(rw_min or 0),
                "roll_weight_max": float(rw_max or 0),
                "margin_percent": float(margin_percent or 0),
            }
        )

    for key in margin_rules_map:
        margin_rules_map[key].sort(
            key=lambda r: (r["roll_weight_min"], r["roll_weight_max"])
        )

    # --- payment_terms map ---
    cur.execute(
        """
        SELECT id, name, credit_days, annual_rate_percent
        FROM payment_terms
        WHERE is_active = true
        ORDER BY credit_days, id
        """
    )
    rows_pt = cur.fetchall()
    payment_terms_map = {}
    for pt_id, name, credit_days, annual_rate_percent in rows_pt:
        payment_terms_map[int(pt_id)] = {
            "name": name,
            "credit_days": int(credit_days or 0),
            "annual_rate_percent": float(annual_rate_percent or 0),
        }

    # --- pricing_extras ---
    cur.execute(
        """
        SELECT color_extra_usd_per_kg,
               prestretch_extra_usd_per_kg,
               foreign_extra_mode,
               foreign_extra_value
        FROM pricing_extras
        WHERE is_active = true
        ORDER BY id DESC
        LIMIT 1
        """
    )
    row_extras = cur.fetchone()
    if row_extras:
        mode = (row_extras[2] or "percent").strip()
        if mode not in ("percent", "per_unit"):
            mode = "percent"

        pricing_extras = {
            "color_extra_usd_per_kg": float(row_extras[0] or 0),
            "prestretch_extra_usd_per_kg": float(row_extras[1] or 0),
            "foreign_extra_mode": mode,
            "foreign_extra_value": float(row_extras[3] or 0),
        }
    else:
        pricing_extras = {
            "color_extra_usd_per_kg": 0.0,
            "prestretch_extra_usd_per_kg": 0.0,
            "foreign_extra_mode": "percent",
            "foreign_extra_value": 0.0,
        }

    # --- Energy rate ---
    energy_rate = get_energy_rate_usd_per_kwh()

    # --- CORE prices /kg (standard + prestretch) ---
    core_price_standard_usd = 0.0      # MAT-0010 Core
    core_price_prestretch_usd = 0.0    # MAT-0011 Core pre-stretch

    cur.execute(
        """
        SELECT id, price_per_unit, currency
        FROM materials
        WHERE category = 'CORE'
        AND id IN (10, 11)
        ORDER BY id
        """
    )
    rows_core = cur.fetchall()

    for mat_id, price_per_unit, currency in rows_core:
        price = float(price_per_unit or 0)
        curr = (currency or "USD").strip().upper()

        if price <= 0:
            continue

        # تحويل العملة لـ USD لو لزم الأمر
        if curr == "USD":
            price_usd = price
        else:
            if egp_per_usd > 0:
                price_usd = price / egp_per_usd
            else:
                price_usd = 0.0

        if mat_id == 10:
            core_price_standard_usd = price_usd
        elif mat_id == 11:
            core_price_prestretch_usd = price_usd

    # --- packing profiles + items + overrides ---

    # 1) load packing_profiles
    cur.execute(
        """
        SELECT
            id,
            packing_type_id,
            pallet_type_id,
            is_global,
            is_active
        FROM packing_profiles
        WHERE is_active = TRUE
        """
    )
    rows_profiles = cur.fetchall()
    profiles_by_id = {}
    global_profile_by_key = {}  # (packing_type_id, pallet_type_id) -> profile_id

    for pid, ptype_id, plt_id, is_global, is_active in rows_profiles:
        pid = int(pid)
        ptype_id = int(ptype_id)
        plt_id = int(plt_id)
        profiles_by_id[pid] = {
            "packing_type_id": ptype_id,
            "pallet_type_id": plt_id,
            "is_global": bool(is_global),
        }
        if is_global:
            global_profile_by_key[(ptype_id, plt_id)] = pid

    # 2) load packing_items by packing_profile_id
    cur.execute(
        """
        SELECT
            pi.packing_profile_id,
            pi.material_id,
            pi.quantity_per_pallet,
            m.price_per_unit,
            m.currency
        FROM packing_items pi
        JOIN materials m ON m.id = pi.material_id
        WHERE pi.packing_profile_id IS NOT NULL
        """
    )
    rows_packing_items = cur.fetchall()

    # تكلفة البالته لكل profile_id
    packing_profile_cost_map = defaultdict(lambda: {"usd": 0.0, "egp": 0.0})
    for (
        packing_profile_id,
        material_id,
        qty_per_pallet,
        price_per_unit,
        currency,
    ) in rows_packing_items:
        if not packing_profile_id:
            continue
        qty = float(qty_per_pallet or 0)
        price = float(price_per_unit or 0)
        curr = (currency or "USD").upper()
        pid = int(packing_profile_id)
        if curr == "USD":
            packing_profile_cost_map[pid]["usd"] += qty * price
        else:
            packing_profile_cost_map[pid]["egp"] += qty * price

    # حوّل أي EGP إلى USD
    for pid, val in packing_profile_cost_map.items():
        egp_val = val["egp"]
        if egp_val and egp_per_usd > 0:
            val["usd"] += egp_val / egp_per_usd
        val["egp"] = 0.0

    # 3) بنينا map نهائي للبحث أثناء التسعير:
    #    - global: لكل (packing_type_id, pallet_type_id)
    #    - profile_cost_map: لكل profile_id
    packing_cost_per_pallet_global = {}  # (packing_type_id, pallet_type_id) -> usd
    for (ptype_id, plt_id), profile_id in global_profile_by_key.items():
        cost_info = packing_profile_cost_map.get(profile_id)
        if cost_info:
            packing_cost_per_pallet_global[(ptype_id, plt_id)] = cost_info["usd"]

    # 4) overrides: product + roll_weight نطاق
    cur.execute(
        """
        SELECT
            id,
            packing_profile_id,
            product_id,
            roll_weight_min,
            roll_weight_max,
            is_active
        FROM packing_profile_overrides
        WHERE is_active = TRUE
        """
    )
    rows_overrides = cur.fetchall()

    packing_profile_overrides = defaultdict(list)
    for (
        oid,
        packing_profile_id,
        product_id,
        rw_min,
        rw_max,
        is_active,
    ) in rows_overrides:
        if not is_active:
            continue
        packing_profile_overrides[int(product_id)].append(
            {
                "override_id": int(oid),
                "packing_profile_id": int(packing_profile_id),
                "roll_weight_min": float(rw_min or 0.0),
                "roll_weight_max": float(rw_max or 0.0),
            }
        )

    # sort overrides by weight range
    for pid, lst in packing_profile_overrides.items():
        lst.sort(key=lambda r: (r["roll_weight_min"], r["roll_weight_max"]))

    # --- product_machines + machines (kwh, capacity, utilization) ---
    cur.execute(
        """
        SELECT
            pm.product_id,
            pm.kwh_per_kg,
            pm.preferred_machine,
            pm.machine_id,
            pm.monthly_product_capacity_kg,
            m.utilization_rate
        FROM product_machines pm
        JOIN machines m ON m.id = pm.machine_id
        """
    )
    rows_pm = cur.fetchall()

    product_machine_map = defaultdict(list)
    machine_ids = set()
    for (
        pid,
        kwh_per_kg,
        preferred_machine,
        machine_id,
        monthly_cap,
        utilization_rate,
    ) in rows_pm:
        product_machine_map[int(pid)].append(
            {
                "kwh_per_kg": float(kwh_per_kg or 0),
                "preferred_machine": bool(preferred_machine),
                "machine_id": int(machine_id),
                "monthly_product_capacity_kg": float(monthly_cap or 0),
                "utilization_rate": float(utilization_rate or 1.0),
            }
        )
        machine_ids.add(int(machine_id))

    # --- machine_costs bulk ---
    machine_costs_map = defaultdict(list)
    if machine_ids:
        cur.execute(
            """
            SELECT machine_id, cost_type, amount_egp
            FROM machine_costs
            WHERE machine_id = ANY(%s)
            """,
            (list(machine_ids),),
        )
        rows_mc = cur.fetchall()
        for mid, cost_type, amount in rows_mc:
            machine_costs_map[int(mid)].append(
                (cost_type, float(amount or 0))
            )

    # --- products: basic info map ---
    cur.execute(
        """
        SELECT
            id,
            code,
            micron,
            stretchability_percent,
            film_type,
            is_manual,
            kg_per_roll,
            bom_scrap_percent
        FROM products
        """
    )
    rows_products = cur.fetchall()
    product_info_map = {}
    for (
        pid,
        code,
        micron,
        stretchability_percent,
        film_type,
        is_manual,
        kg_per_roll,
        bom_scrap_percent,
    ) in rows_products:
        product_info_map[int(pid)] = {
            "code": code,
            "micron": int(micron or 0),
            "stretchability_percent": float(stretchability_percent or 0.0),
            "film_type": (film_type or "standard").strip(),
            "is_manual": bool(is_manual),
            "kg_per_roll": float(kg_per_roll or 0.0),
            "bom_scrap_percent": float(bom_scrap_percent or 0.0),
        }

    # --- BOM: product_roll_bom_map لكل المنتجات ---
    cur.execute(
        """
        SELECT
            prb.id,
            prb.product_id,
            prb.weight_from_kg,
            prb.weight_to_kg,
            prb.is_active
        FROM product_roll_boms prb
        WHERE prb.is_active = TRUE
        ORDER BY prb.product_id, prb.weight_from_kg, prb.weight_to_kg, prb.id
        """
    )
    rows_roll_boms = cur.fetchall()

    roll_bom_ids = [int(r[0]) for r in rows_roll_boms] if rows_roll_boms else []

    roll_bom_items_map = defaultdict(list)
    if roll_bom_ids:
        cur.execute(
            """
            SELECT
                pri.roll_bom_id,
                pri.material_id,
                pri.semi_product_id,
                pri.percentage
            FROM product_roll_bom_items pri
            WHERE pri.roll_bom_id = ANY(%s)
            """,
            (roll_bom_ids,),
        )
        rows_roll_items = cur.fetchall()
        for roll_bom_id, material_id, semi_product_id, pct in rows_roll_items:
            roll_bom_items_map[int(roll_bom_id)].append(
                {
                    "material_id": int(material_id) if material_id is not None else None,
                    "semi_product_id": int(semi_product_id) if semi_product_id is not None else None,
                    "pct": float(pct or 0.0),
                }
            )

    product_roll_bom_map = defaultdict(list)
    for rb_id, product_id, w_from, w_to, is_active in rows_roll_boms:
        items = roll_bom_items_map.get(int(rb_id), [])
        product_roll_bom_map[int(product_id)].append(
            {
                "weight_from_kg": float(w_from or 0.0),
                "weight_to_kg": float(w_to or 0.0),
                "items": items,  # list of dicts: {material_id, semi_product_id, pct}
            }
        )

    for pid in product_roll_bom_map:
        product_roll_bom_map[pid].sort(
            key=lambda rb: (rb["weight_from_kg"], rb["weight_to_kg"])
        )

    # --- material_price_map: landed price لكل المواد ---
    cur.execute(
        """
        SELECT id
        FROM materials
        """
    )
    rows_material_ids = cur.fetchall()
    all_material_ids = [int(r[0]) for r in rows_material_ids] if rows_material_ids else []

    material_price_map = {}
    if all_material_ids:
        material_price_map = get_materials_landed_price_per_kg_bulk(all_material_ids)

    data = {
        "margin_rules_map": margin_rules_map,
        "payment_terms_map": payment_terms_map,
        "pricing_extras": pricing_extras,
        "packing_profiles_by_id": profiles_by_id,
        "global_profile_by_key": global_profile_by_key,
        "packing_profile_overrides": packing_profile_overrides,
        "packing_profile_cost_map": packing_profile_cost_map,
        "packing_cost_per_pallet_global": packing_cost_per_pallet_global,
        "energy_rate": energy_rate,
        "core_price_standard_usd": core_price_standard_usd,
        "core_price_prestretch_usd": core_price_prestretch_usd,
        "product_machine_map": product_machine_map,
        "machine_costs_map": machine_costs_map,
        "product_info_map": product_info_map,
        "product_roll_bom_map": product_roll_bom_map,
        "material_price_map": material_price_map,
        "shipping_rates_map": {},
        "cache_updated_at": cache_updated_at,
        "cache_version": cache_version_db,
    }

    _PRICING_STATIC_CACHE["version"] = cache_version_db
    _PRICING_STATIC_CACHE["data"] = data

    return data

@pricing_bp.route("/pricing/sync", methods=["POST"])

@login_required
@roles_required("admin")

def pricing_sync():
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
    return jsonify({"ok": True})

@pricing_bp.route("/pricing", methods=["GET", "POST"])

@login_required
@roles_required("admin", "owner", "sales_manager", "sales")

def pricing_screen():
    # clear=1 من New quotation
    if request.method == "GET" and request.args.get("clear") == "1":
        session.pop("pricing_header", None)
        session.pop("pricing_lines_input", None)
        session.pop("pricing_lines_results", None)
        return redirect(url_for("pricing.pricing_screen"))

    # تحميل بيانات ثابتة للشاشة (products, ports, ... )
    with get_db() as cur:
        cur.execute(
            """
            SELECT id,
                   code,
                   micron,
                   stretchability_percent,
                   film_type,
                   is_manual,
                   kg_per_roll,
                   bom_scrap_percent
            FROM products
            ORDER BY code
            """
        )
        products_rows = cur.fetchall()

        products = products_rows  # للاستعمال في الـ template

        cur.execute("SELECT id, name, country FROM ports ORDER BY country, name")
        ports = cur.fetchall()

        cur.execute(
            """
            SELECT id, country, COALESCE(city,'') AS city
            FROM destinations
            ORDER BY country, city
            """
        )
        destinations = cur.fetchall()

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
        payment_terms_rows = cur.fetchall()

        cur.execute(
            """
            SELECT id, name
            FROM pallet_types
            ORDER BY id
            """
        )
        pallet_types = cur.fetchall()

        cur.execute(
            """
            SELECT id, name
            FROM packing_types
            ORDER BY id
            """
        )
        packing_types = cur.fetchall()

        # currency rate مرة واحدة
        cur.execute(
            """
            SELECT egp_per_usd
            FROM currency_rates
            WHERE is_active = true
            ORDER BY effective_date DESC, id DESC
            LIMIT 1
            """
        )
        row_fx = cur.fetchone()
        egp_per_usd = float(row_fx[0] or 0) if row_fx else 0.0

    # build product_info_map
    product_info_map = {}
    for row in products_rows:
        pid = row[0]
        product_info_map[pid] = {
            "micron": int(row[2] or 0),
            "stretchability_percent": row[3],
            "film_type": (row[4] or "standard").strip(),
            "is_manual": bool(row[5]),
            "kg_per_roll": float(row[6] or 0),
            "bom_scrap_percent": float(row[7] or 0),
        }

    # قيم ابتدائية
    selected_port_id = None
    selected_dest_id = None
    selected_payment_term_id = None
    discount_percent = 0.0

    lines_input = []
    lines_results = []
    
    cache_updated_at = None
    cache_version = None

    if request.method == "POST":
        t0 = time.perf_counter()

        mode = request.form.get("_mode") or "calculate"

        # تحديد نوع البائع (مصري / أجنبي)
        if current_user.role in ("admin", "owner", "sales_manager"):
            seller_type = (request.form.get("seller_type") or "egyptian").strip().lower()
            if seller_type not in ("egyptian", "foreign"):
                seller_type = "egyptian"
        else:
            sales_type = getattr(current_user, "sales_type", None) or ""
            sales_type = sales_type.strip().lower()
            if sales_type == "foreign_sellers":
                seller_type = "foreign"
            else:
                seller_type = "egyptian"

        is_foreign_pricing = (seller_type == "foreign")
        
        print("SELLER_TYPE:", seller_type, "IS_FOREIGN_PRICING:", is_foreign_pricing)
        
        # ===== حالة SAVE عبر AJAX: حفظ باستخدام آخر نتائج مخزّنة في session فقط =====
        if (
            request.headers.get("X-Requested-With") == "XMLHttpRequest"
            and mode == "save"
        ):
            
            t0 = time.perf_counter()
            
            # نقرأ آخر حالة من السيشن
            header_data = session.get("pricing_header") or {}
            lines_input = session.get("pricing_lines_input", []) or []
            lines_results = session.get("pricing_lines_results", []) or []

            if not lines_input or not lines_results:
                return jsonify(
                    {
                        "saved": False,
                        "need_calculate_first": True,
                        "message": "Please calculate pricing before saving.",
                    }
                )

            # قيم من الهيدر القديم
            selected_port_id = header_data.get("selected_port_id")
            selected_dest_id = header_data.get("selected_dest_id")
            selected_payment_term_id = header_data.get("selected_payment_term_id")
            discount_percent = float(header_data.get("discount_percent") or 0.0)

            # نسمح بتحديث بعض الحقول من الفورم وقت الحفظ (إن حابب تغيّر الاسم أو رقم الكوتيشن)
            customer_name = request.form.get("customer_name") or header_data.get("customer_name") or ""
            destination_text = request.form.get("destination_text") or header_data.get("customer_country") or ""

            with get_db() as cur:
                
                # توليد رقم الكوتيشن أوتوماتيك
                quotation_number = generate_next_quotation_number(cur)
              
                t_db_start = time.perf_counter()
                
                # snapshot لشروط الدفع
                cur.execute(
                    """
                    SELECT name, credit_days, annual_rate_percent
                    FROM payment_terms
                    WHERE id = %s
                    """,
                    (selected_payment_term_id,),
                )
                pt_row = cur.fetchone()
                if pt_row:
                    pt_name, pt_days, pt_annual_rate = pt_row
                    annual_rate = float(pt_annual_rate or 0.0)
                    days = int(pt_days or 0)
                    daily_rate = annual_rate / 365.0 if annual_rate else 0.0
                    credit_surcharge_percent_snapshot = (
                        daily_rate * days if days > 0 and annual_rate > 0 else 0.0
                    )
                else:
                    pt_name = None
                    pt_days = None
                    credit_surcharge_percent_snapshot = 0.0
                    
                t_header_start = time.perf_counter()

                # حفظ الهيدر
                cur.execute(
                    """
                    INSERT INTO quotations
                        (quotation_number,
                        customer_name,
                        customer_country,
                        port_id,
                        destination_id,
                        payment_term_id,
                        global_discount_percent,
                        fx_egp_per_usd,
                        payment_term_name_snapshot,
                        payment_term_days_snapshot,
                        credit_surcharge_percent_snapshot,
                        created_by_user_id,
                        created_at,
                        seller_type)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s)
                    RETURNING id
                    """,
                    (
                        quotation_number,
                        customer_name,
                        destination_text,
                        selected_port_id,
                        selected_dest_id,
                        selected_payment_term_id,
                        discount_percent,
                        egp_per_usd,  # نفس FX اللي اتستخدم في الحساب
                        pt_name,
                        pt_days,
                        credit_surcharge_percent_snapshot,
                        current_user.id,
                        seller_type,
                    ),
                )
                quotation_id = cur.fetchone()[0]
                
                t_items_start = time.perf_counter()

                # حفظ البنود + الـ snapshot من calc الجاهز
                for line, res in zip(lines_input, lines_results):
                    product_id = int(line.get("product_id") or 0)
                    price_basis = line.get("price_basis") or "gross"
                    pallets_per_container = float(line.get("pallets_per_container") or 0)
                    width_mm = float(line.get("width_mm") or 0)
                    rolls_per_pallet = float(line.get("rolls_per_pallet") or 0)
                    roll_weight_kg = float(line.get("roll_weight_kg") or 0)
                    core_weight_kg = float(line.get("core_weight_kg") or 0)
                    line_discount = float(line.get("discount_percent") or 0)
                    is_colored = bool(line.get("is_colored"))
                    pallet_type_id = int(line.get("pallet_type_id") or 0) or None
                    packing_type_id = int(line.get("packing_type_id") or 0) or None

                    exw_price = res["discounted"]["exw_display"]
                    fob_price = res["discounted"]["fob_display"]
                    cfr_price = res["discounted"]["cfr_display"]

                    cur.execute(
                        """
                        INSERT INTO quotation_items
                            (quotation_id,
                            product_id,
                            price_basis,
                            pallets_per_container,
                            width_mm,
                            rolls_per_pallet,
                            roll_weight_kg,
                            core_weight_kg,
                            discount_percent,
                            is_colored,
                            pallet_type_id,
                            packing_type_id,
                            exw_price,
                            fob_price,
                            cfr_price)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        RETURNING id
                        """,
                        (
                            quotation_id,
                            product_id,
                            price_basis,
                            pallets_per_container,
                            width_mm,
                            rolls_per_pallet,
                            roll_weight_kg,
                            core_weight_kg,
                            line_discount,
                            is_colored,
                            pallet_type_id,
                            packing_type_id,
                            exw_price,
                            fob_price,
                            cfr_price,
                        ),
                    )
                    quotation_item_id = cur.fetchone()[0]

                    calc = res.get("calc") or {}
                    disc_calc = calc.get("discounted") or {}
                    
                    foreign_extra_mode = calc.get("foreign_extra_mode")
                    foreign_extra_value = calc.get("foreign_extra_value")
                    
                    cur.execute(
                        """
                        INSERT INTO quotation_item_cost_snapshots (
                            quotation_item_id,
                            rm_cost_per_kg_net,
                            energy_cost_per_kg_net,
                            machine_oh_per_kg_net,
                            net_kg_per_roll,
                            gross_kg_per_roll,
                            extra_roll,
                            margin_percent,
                            fob_cost_unit_roll,
                            sea_freight_cost_unit_roll,
                            core_cost_unit_roll,
                            packing_cost_unit_roll,
                            packing_core_cost_unit_roll,
                            exw_kg_net,
                            exw_kg_gross,
                            exw_roll,
                            fob_kg_net,
                            fob_kg_gross,
                            fob_roll,
                            cfr_kg_net,
                            cfr_kg_gross,
                            cfr_roll,
                            foreign_extra_mode,
                            foreign_extra_value,
                            fob_kg_gross_base,
                            cfr_kg_gross_base,
                            exw_kg_net_base,
                            fob_kg_net_base,
                            cfr_kg_net_base,
                            exw_roll_base,
                            fob_roll_base,
                            cfr_roll_base
                        )
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            quotation_item_id,
                            float(calc.get("rm_cost_per_kg") or 0.0),
                            float(calc.get("energy_cost_per_kg") or 0.0),
                            float(calc.get("machine_oh_per_kg") or 0.0),
                            float(calc.get("net_kg_per_roll") or 0.0),
                            float(calc.get("gross_kg_per_roll") or 0.0),
                            float(calc.get("extra_roll") or 0.0),
                            float(calc.get("margin_percent") or 0.0),
                            float(calc.get("fob_cost_unit_roll") or 0.0),
                            float(calc.get("sea_freight_cost_unit_roll") or 0.0),
                            float(calc.get("core_cost_unit_roll") or 0.0),
                            float(calc.get("packing_cost_unit_roll") or 0.0),
                            float(calc.get("packing_core_cost_unit_roll") or 0.0),
                            float(disc_calc.get("exw_kg_net") or 0.0),
                            float(disc_calc.get("exw_kg_gross") or 0.0),
                            float(disc_calc.get("exw_roll") or 0.0),
                            float(disc_calc.get("fob_kg_net") or 0.0),
                            float(disc_calc.get("fob_kg_gross") or 0.0),
                            float(disc_calc.get("fob_roll") or 0.0),
                            float(disc_calc.get("cfr_kg_net") or 0.0),
                            float(disc_calc.get("cfr_kg_gross") or 0.0),
                            float(disc_calc.get("cfr_roll") or 0.0),
                            (foreign_extra_mode or "").strip(),
                            float(foreign_extra_value or 0.0),
                            float(disc_calc.get("fob_kg_gross_base") or 0.0),
                            float(disc_calc.get("cfr_kg_gross_base") or 0.0),
                            float(disc_calc.get("exw_kg_net_base") or 0.0),
                            float(disc_calc.get("fob_kg_net_base") or 0.0),
                            float(disc_calc.get("cfr_kg_net_base") or 0.0),
                            float(disc_calc.get("exw_roll_base") or 0.0),
                            float(disc_calc.get("fob_roll_base") or 0.0),
                            float(disc_calc.get("cfr_roll_base") or 0.0),
                        ),
                    )
                    
                t_end = time.perf_counter()

            print(
                "[save-prof] total = %.4fs | db_block = %.4fs | header_insert = %.4fs | items_loop = %.4fs"
                % (
                    t_end - t0,
                    t_db_start - t0,
                    t_items_start - t_header_start,
                    t_end - t_items_start,
                )
            )

            # نحدّث السيشن لو حابب
            session["pricing_header"] = {
                "selected_port_id": selected_port_id,
                "selected_dest_id": selected_dest_id,
                "selected_payment_term_id": selected_payment_term_id,
                "discount_percent": discount_percent,
                "customer_name": customer_name,
                "quotation_number": quotation_number,
            }
            session["pricing_lines_input"] = lines_input
            session["pricing_lines_results"] = lines_results

            return jsonify(
                {
                    "lines_results": lines_results,
                    "saved": True,
                    "quotation_id": quotation_id,
                    "quotation_number": quotation_number,
                }
            )

        # لو الطلب Export PDF/Excel: نستخدم POST عادي (مش AJAX) ونرجّع ملف
        if mode in ("export_pdf", "export_excel"):
            # هنا نفترض إن الحساب تم بالفعل (lines_results جاهزة في الـ session)
            header_data = session.get("pricing_header") or {}
            lines_input = session.get("pricing_lines_input", []) or []
            lines_results = session.get("pricing_lines_results", []) or []

            # لو مفيش بيانات، ما نصدرش
            if not lines_input or not lines_results:
                flash("Nothing to export. Please calculate first.", "warning")
                return redirect(url_for("pricing.pricing_screen"))

            # ======= lookups لأسماء الجداول (مشترك بين PDF و Excel) =======
            with get_db() as cur:
                # products
                cur.execute(
                    "SELECT id, code, micron, stretchability_percent, film_type FROM products WHERE id = ANY(%s)",
                    (
                        [
                            int(l.get("product_id"))
                            for l in lines_input
                            if l.get("product_id")
                        ],
                    ),
                )
                product_rows = cur.fetchall()
                products_lookup = {
                    int(r[0]): {
                        "code": r[1],
                        "micron": r[2],
                        "stretch": r[3],
                        "film_type": r[4],
                    }
                    for r in product_rows
                }

                # pallet_types
                cur.execute("SELECT id, name FROM pallet_types")
                pallet_rows = cur.fetchall()
                pallets_lookup = {int(r[0]): r[1] for r in pallet_rows}

                # packing_types
                cur.execute("SELECT id, name FROM packing_types")
                packing_rows = cur.fetchall()
                packing_lookup = {int(r[0]): r[1] for r in packing_rows}

                # ports
                cur.execute("SELECT id, name, country FROM ports")
                port_rows = cur.fetchall()
                ports_lookup = {int(r[0]): {"name": r[1], "country": r[2]} for r in port_rows}

                # destinations
                cur.execute("SELECT id, country, city FROM destinations")
                dest_rows = cur.fetchall()
                dest_lookup = {
                    int(r[0]): {"country": r[1], "city": r[2]} for r in dest_rows
                }

                # payment_terms
                cur.execute("SELECT id, name, credit_days FROM payment_terms")
                pay_rows = cur.fetchall()
                payment_terms_lookup = {
                    int(r[0]): {"name": r[1], "days": r[2]} for r in pay_rows
                }

            # ===== نبني rich_header مرة واحدة للـ PDF والـ Excel =====
            from datetime import datetime
            today = datetime.today()
            created_at = header_data.get("created_at")
            if isinstance(created_at, str):
                try:
                    created_at = datetime.fromisoformat(created_at)
                except ValueError:
                    created_at = today

            port_info = ports_lookup.get(header_data.get("selected_port_id") or 0)
            dest_info = dest_lookup.get(header_data.get("selected_dest_id") or 0)
            pay_info = payment_terms_lookup.get(header_data.get("selected_payment_term_id") or 0)

            rich_header = {
                "customer_name": header_data.get("customer_name"),
                "customer_country": header_data.get("customer_country"),
                "dest_country": dest_info["country"] if dest_info else None,
                "dest_city": dest_info["city"] if dest_info else None,
                "port_name": port_info["name"] if port_info else None,
                "port_country": port_info["country"] if port_info else None,
                "payment_term_name": pay_info["name"] if pay_info else None,
                "payment_term_days": pay_info["days"] if pay_info else None,
                "discount_percent": header_data.get("discount_percent") or 0.0,
                "selected_dest_id": header_data.get("selected_dest_id"),
                "selected_port_id": header_data.get("selected_port_id"),
                "selected_payment_term_id": header_data.get("selected_payment_term_id"),
                "pricing_ref": header_data.get("quotation_number"),
                "created_at": created_at or today,
            }
            print("RICH_HEADER:", rich_header)

            # ===== فرع Print-friendly بدلاً من PDF =====
            if mode == "export_pdf":
                logo_rel_path = url_for("static", filename="img/quotation_header.png")

                html = render_template(
                    "pricing/pricing_print.html",
                    header=rich_header,
                    lines_input=lines_input,
                    lines_results=lines_results,
                    products_lookup=products_lookup,
                    pallets_lookup=pallets_lookup,
                    packing_lookup=packing_lookup,
                    ports_lookup=ports_lookup,
                    dest_lookup=dest_lookup,
                    payment_terms_lookup=payment_terms_lookup,
                    logo_rel_path=logo_rel_path,
                )
                return html

            # ===== فرع Excel =====
            else:  # export_excel
                import xlsxwriter

                output = BytesIO()
                workbook = xlsxwriter.Workbook(output, {"in_memory": True})
                ws = workbook.add_worksheet("Quotation")

                # إعدادات صفحة الإكسل (Print Setup) بدون تكريش
                ws.set_portrait()
                ws.set_margins(left=0.5, right=0.5, top=0.7, bottom=0.7)
                ws.center_horizontally()
                ws.fit_to_pages(1, 0)


                # ===== صورة الهيدر =====
                header_image_path = "static/img/quotation_header.png"

                try:
                    ws.insert_image(
                        0, 0,  # A1
                        header_image_path,
                        {
                            "x_scale": 1.25,
                            "y_scale": 0.9,
                        },
                    )
                    ws.set_row(0, 90)   # صف الصورة
                    ws.set_row(1, 10)   # صف فاصل
                except Exception as e:
                    print("Excel header image error:", e)

                # ===== تنسيقات عامة =====
                title_fmt = workbook.add_format(
                    {
                        "bold": True,
                        "font_name": "Arial",
                        "font_size": 14,
                        "align": "left",
                        "valign": "vcenter",
                    }
                )
                label_fmt = workbook.add_format(
                    {
                        "bold": True,
                        "font_name": "Arial",
                        "font_size": 10,
                        "align": "left",
                    }
                )
                text_fmt = workbook.add_format(
                    {"font_name": "Arial", "font_size": 10, "align": "left"}
                )
                header_fmt = workbook.add_format(
                    {
                        "bold": True,
                        "font_name": "Arial",
                        "font_size": 10,
                        "bg_color": "#D9D9D9",
                        "border": 1,
                        "align": "center",
                        "valign": "vcenter",
                    }
                )
                num_fmt = workbook.add_format(
                    {
                        "num_format": "0.00",
                        "font_size": 9,
                        "align": "right",
                        "border": 1,
                    }
                )
                text_cell_fmt = workbook.add_format(
                    {
                        "font_name": "Arial",
                        "font_size": 9,
                        "align": "left",
                        "border": 1,
                    }
                )
                center_cell_fmt = workbook.add_format(
                    {
                        "font_name": "Arial",
                        "font_size": 9,
                        "align": "center",
                        "border": 1,
                    }
                )
                right_cell_fmt = workbook.add_format(
                    {
                        "font_name": "Arial",
                        "font_size": 9,
                        "align": "right",
                        "border": 1,
                    }
                )

                # ===== عرض الأعمدة بعد حذف عمود # =====
                ws.set_column("A:A", 26)    # Product
                ws.set_column("B:B", 12)    # Pallet
                ws.set_column("C:C", 12)    # Packing
                ws.set_column("D:D", 8)     # Basis
                ws.set_column("E:E", 8)     # Colored
                ws.set_column("F:F", 7)     # Width
                ws.set_column("G:G", 7)     # Rolls
                ws.set_column("H:H", 7)     # Roll kg
                ws.set_column("I:I", 7)     # Core kg
                ws.set_column("J:J", 7)     # Disc %
                ws.set_column("K:K", 9)     # EXW
                ws.set_column("L:L", 9)     # FOB
                ws.set_column("M:M", 9)     # CFR

                # ===== هيدر الكوتيشن =====
                # ننقل العنوان لتحت الصورة (صف 4 بدل 2)
                ws.merge_range(4, 0, 4, 6, "Quotation", title_fmt)

                # نخلي A للعناوين و B للقيم
                ws.set_column("A:A", 18)    # Header labels
                ws.set_column("B:B", 24)    # Header values + Product later

                # نزّح الهيدر صفّين لتحت
                ws.write(6, 0, "Customer:", label_fmt)
                ws.write(6, 1, rich_header.get("customer_name") or "", text_fmt)

                dest_text = ""
                if rich_header.get("dest_country"):
                    dest_text = rich_header["dest_country"]
                    if rich_header.get("dest_city"):
                        dest_text += " – " + str(rich_header["dest_city"])
                ws.write(7, 0, "Destination:", label_fmt)
                ws.write(7, 1, dest_text, text_fmt)

                port_text = ""
                if rich_header.get("port_name"):
                    port_text = rich_header["port_name"]
                    if rich_header.get("port_country"):
                        port_text += f" ({rich_header['port_country']})"
                ws.write(8, 0, "Loading port:", label_fmt)
                ws.write(8, 1, port_text, text_fmt)

                pay_text = ""
                if rich_header.get("payment_term_name"):
                    pay_text = rich_header["payment_term_name"]
                    if rich_header.get("payment_term_days") is not None:
                        pay_text += f" ({rich_header['payment_term_days']} days)"
                ws.write(9, 0, "Payment term:", label_fmt)
                ws.write(9, 1, pay_text, text_fmt)

                ws.write(6, 4, "Quotation no.:", label_fmt)
                ws.write(6, 5, rich_header.get("pricing_ref") or "", text_fmt)

                ws.write(7, 4, "Date:", label_fmt)
                date_value = ""
                if rich_header.get("created_at"):
                    ca = rich_header["created_at"]
                    try:
                        from datetime import datetime as _dt
                        if isinstance(ca, str):
                            ca = _dt.fromisoformat(ca)
                        date_value = ca.strftime("%Y-%m-%d")
                    except Exception:
                        date_value = str(ca)
                ws.write(7, 5, date_value, text_fmt)

                ws.write(8, 4, "Global discount %:", label_fmt)
                ws.write(
                    8,
                    5,
                    float(rich_header.get("discount_percent") or 0.0),
                    num_fmt,
                )

                # ===== جدول الآيتيمز =====
                start_row = 11
                headers = [
                    "Product",
                    "Pallet",
                    "Packing",
                    "Basis",
                    "Colored",
                    "Width",
                    "Rolls",
                    "Roll kg",
                    "Core kg",
                    "Disc %",
                    "EXW",
                    "FOB",
                    "CIF",
                ]
                for col, h in enumerate(headers):
                    ws.write(start_row, col, h, header_fmt)

                row_idx = start_row + 1
                for idx, (line, res) in enumerate(
                    zip(lines_input, lines_results), start=1
                ):
                    pid = int(line.get("product_id") or 0)
                    p = products_lookup.get(pid)
                    if p:
                        micron = p.get("micron")
                        stretch = p.get("stretch")
                        ft = p.get("film_type")
                        parts = []
                        if micron:
                            parts.append(f"{micron}µm")
                        if stretch:
                            parts.append(f"{stretch}%")
                        if ft:
                            parts.append(str(ft))
                        product_text = (
                            " / ".join(parts)
                            if parts
                            else (p.get("code") or str(pid))
                        )
                    else:
                        product_text = str(pid)

                    pal_id = (
                        int(line.get("pallet_type_id") or 0)
                        if line.get("pallet_type_id")
                        else None
                    )
                    pack_id = (
                        int(line.get("packing_type_id") or 0)
                        if line.get("packing_type_id")
                        else None
                    )

                    pallet_text = pallets_lookup.get(pal_id, "") if pal_id else ""
                    packing_text = packing_lookup.get(pack_id, "") if pack_id else ""

                    ws.write(row_idx, 0, product_text, text_cell_fmt)
                    ws.write(row_idx, 1, pallet_text, text_cell_fmt)
                    ws.write(row_idx, 2, packing_text, text_cell_fmt)
                    ws.write(
                        row_idx,
                        3,
                        line.get("price_basis") or "gross",
                        center_cell_fmt,
                    )
                    ws.write(
                        row_idx,
                        4,
                        "Color" if line.get("is_colored") else "Transparent",
                        center_cell_fmt,
                    )
                    ws.write_number(row_idx, 5, float(line.get("width_mm") or 0), num_fmt)
                    ws.write_number(row_idx, 6, float(line.get("rolls_per_pallet") or 0), num_fmt)
                    ws.write_number(row_idx, 7, float(line.get("roll_weight_kg") or 0), num_fmt)
                    ws.write_number(row_idx, 8, float(line.get("core_weight_kg") or 0), num_fmt)
                    ws.write_number(row_idx, 9, float(line.get("discount_percent") or 0), num_fmt)

                    exw_price = res["discounted"]["exw_display"]
                    fob_price = res["discounted"]["fob_display"]
                    cfr_price = res["discounted"]["cfr_display"]
                    ws.write_number(row_idx, 10, float(exw_price or 0), num_fmt)
                    ws.write_number(row_idx, 11, float(fob_price or 0), num_fmt)
                    ws.write_number(row_idx, 12, float(cfr_price or 0), num_fmt)

                    row_idx += 1

                workbook.close()
                output.seek(0)

                response = make_response(output.read())
                response.headers[
                    "Content-Type"
                ] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                response.headers[
                    "Content-Disposition"
                ] = 'attachment; filename="pricing_export.xlsx"'
                return response


        # ===== من هنا المكينة الأصلية للحساب/الحفظ كما هي =====
        selected_port_id = int(request.form.get("port_id") or 0) or None
        selected_dest_id = int(request.form.get("destination_id") or 0) or None
        selected_payment_term_id = (
            int(request.form.get("payment_term_id") or 0) or None
        )
        discount_percent = float(request.form.get("discount_percent") or 0)

        if not selected_port_id or not selected_dest_id:
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return (
                    jsonify(
                        {"error": "Please select loading port and destination."}
                    ),
                    400,
                )
            flash("Please select loading port and destination.", "danger")
        elif not selected_payment_term_id:
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"error": "Please select payment term."}), 400
            flash("Please select payment term.", "danger")
        else:
            raw = request.form
            line_map = defaultdict(dict)

            for full_key, value in raw.items():
                if not full_key.startswith("lines["):
                    continue
                try:
                    _, rest = full_key.split("[", 1)
                    idx_str, field_part = rest.split("]", 1)
                    idx = int(idx_str)
                    field_name = field_part.strip("[]")
                except ValueError:
                    continue

                line_map[idx][field_name] = value

            for idx in sorted(line_map.keys()):
                line_data = line_map[idx]
                if not line_data.get("product_id"):
                    continue
                lines_input.append(line_data)

            if not lines_input:
                if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    return (
                        jsonify(
                            {
                                "error": "Please add at least one line with a product."
                            }
                        ),
                        400,
                    )
                flash("Please add at least one line with a product.", "danger")
            else:
                # ========= bulk load لكل الداتا اللازمة لكـل الـ products في الكوتيشن =========
                product_ids = sorted(
                    {
                        int(l.get("product_id"))
                        for l in lines_input
                        if l.get("product_id")
                    }
                )
                if not product_ids:
                    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                        return (
                            jsonify(
                                {
                                    "error": "Please add at least one line with a product."
                                }
                            ),
                            400,
                        )
                    flash(
                        "Please add at least one line with a product.", "danger"
                    )
                else:
                    with get_db() as cur:
                        # 3) تحميل الداتا الثابتة من الكاش/الداتابيز مرة واحدة (تشمل المكن + products + roll BOM + materials)
                        static_data = load_pricing_static_data(cur, egp_per_usd)
                        margin_rules_map = static_data["margin_rules_map"]
                        payment_terms_map = static_data["payment_terms_map"]
                        pricing_extras = static_data["pricing_extras"]
                        packing_profiles_by_id = static_data["packing_profiles_by_id"]
                        global_profile_by_key = static_data["global_profile_by_key"]
                        packing_profile_overrides = static_data["packing_profile_overrides"]
                        packing_profile_cost_map = static_data["packing_profile_cost_map"]
                        energy_rate = static_data["energy_rate"]
                        core_price_standard_usd = static_data["core_price_standard_usd"]
                        core_price_prestretch_usd = static_data["core_price_prestretch_usd"]
                        product_machine_map = static_data["product_machine_map"]
                        machine_costs_map = static_data["machine_costs_map"]
                        product_info_map_cached = static_data["product_info_map"]
                        product_roll_bom_map = static_data["product_roll_bom_map"]
                        material_price_map = static_data["material_price_map"]
                        cache_updated_at = static_data["cache_updated_at"]
                        cache_version = static_data["cache_version"]

                        # 1) material_ids لكل المنتجات المطلوبة من الكاش roll BOM (للتتبع فقط لو حابب)
                        material_ids = set()
                        for pid in product_ids:
                            for rb in product_roll_bom_map.get(pid, []):
                                for item in rb.get("items", []):
                                    mid = item.get("material_id")
                                    if mid is not None:
                                        material_ids.add(int(mid))

                        print(
                            "[pricing-materials-bulk] using cached material_price_map for "
                            f"{len(material_ids)} material(s)"
                        )

                        # كاش الشحن داخل نفس static_data (مرتبطة بالـ cache_version)
                        shipping_rates_map = static_data.get("shipping_rates_map", {})

                        key = (selected_port_id or 0, selected_dest_id or 0)
                        cached_rates = shipping_rates_map.get(key)

                        if cached_rates is not None:
                            fob_per_container, sea_freight_per_container = cached_rates
                        else:
                            # 4) fob + freight من الـ DB مرة واحدة
                            cur.execute(
                                """
                                SELECT fob_cost_usd_per_container
                                FROM fob_costs
                                WHERE port_id = %s
                                """,
                                (selected_port_id,),
                            )
                            row_fob = cur.fetchone()
                            fob_per_container = float(row_fob[0]) if row_fob else 0.0

                            cur.execute(
                                """
                                SELECT shipping_rate_usd_per_container
                                FROM sea_freight_rates
                                WHERE loading_port_id = %s AND destination_id = %s
                                """,
                                (selected_port_id, selected_dest_id),
                            )
                            row_sf = cur.fetchone()
                            sea_freight_per_container = (
                                float(row_sf[0]) if row_sf else 0.0
                            )

                            # نخزن النتيجة في كاش الشحن للـ version الحالي
                            shipping_rates_map[key] = (
                                fob_per_container,
                                sea_freight_per_container,
                            )

                    # لحد هنا خلصنا DB + بناء الـ maps
                    t_after_db = time.perf_counter()
                    print("[prof] DB + maps took", t_after_db - t0, "seconds")

        # ========= loop على الـ lines باستخدام الداتا الـ bulk =========
        t_logic_start = time.perf_counter()

        for line_number, line in enumerate(lines_input, start=1):
            product_id = int(line.get("product_id") or 0)
            is_colored = bool(line.get("is_colored"))
            price_basis = line.get("price_basis") or "gross"

            # 1) خصم السطر
            line_discount_pct = float(line.get("discount_percent") or 0.0)
            # 2) الخصم الجلوبال من الهيدر
            global_discount_pct = float(discount_percent or 0.0)
            # 3) المجموع اللي فعلاً بيتطبق في الماكينة
            line_discount = line_discount_pct + global_discount_pct

            roll_weight_kg = float(line.get("roll_weight_kg") or 0)
            core_weight_kg = float(line.get("core_weight_kg") or 0)
            pallets_per_container = float(
                line.get("pallets_per_container") or 0
            )
            rolls_per_pallet = float(
                line.get("rolls_per_pallet") or 0
            )
            pallet_type_id = (
                int(line.get("pallet_type_id") or 0) or None
            )
            packing_type_id = (
                int(line.get("packing_type_id") or 0) or None
            )

            # هنا تضيف قراءة الـ width من الشاشة
            width_mm = float(line.get("width_mm") or 0)

            # فاليديشن بسيطة: أقل من 100 غالبًا cm
            if 0 < width_mm < 100:
                suggested = int(width_mm * 10)
                msg = (
                    f"Line {line_number}: Please enter a valid width in mm "
                    f"(e.g., {suggested} not {int(width_mm)})."
                )
                if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    return jsonify({"error": msg}), 400
                flash(msg, "warning")
                continue

            if not product_id:
                continue

            # ===== هنا المنطق الجديد: كل سطر = كونتينر مستقل =====
            kg_per_container_line = (
                pallets_per_container * rolls_per_pallet * roll_weight_kg
            )

            if kg_per_container_line > 0:
                fob_cost_per_kg_line = fob_per_container / kg_per_container_line
                sea_freight_per_kg_line = (
                    sea_freight_per_container / kg_per_container_line
                )
            else:
                fob_cost_per_kg_line = 0.0
                sea_freight_per_kg_line = 0.0

            # اختيار نوع الكور حسب نوع الفيلم للمنتج
            p_info_cached = product_info_map_cached.get(product_id)
            film_type_line = (p_info_cached.get("film_type") if p_info_cached else "standard").strip()

            if film_type_line == "Prestretch":
                core_price_per_kg_usd_line = core_price_prestretch_usd
            else:
                core_price_per_kg_usd_line = core_price_standard_usd

            line_result, err = calculate_line_price_bulk(
                product_id=product_id,
                is_colored=is_colored,
                selected_payment_term_id=selected_payment_term_id,
                discount_percent=line_discount,
                roll_weight_kg=roll_weight_kg,
                core_weight_kg=core_weight_kg,
                pallets_per_container=pallets_per_container,
                rolls_per_pallet=rolls_per_pallet,
                pallet_type_id=pallet_type_id,
                packing_type_id=packing_type_id,
                core_price_per_kg_usd=core_price_per_kg_usd_line,
                packing_profile_cost_map=packing_profile_cost_map,
                packing_profiles_by_id=packing_profiles_by_id,
                packing_profile_overrides=packing_profile_overrides,
                global_profile_by_key=global_profile_by_key,
                # bulk data maps
                product_info_map=product_info_map_cached,
                product_roll_bom_map=product_roll_bom_map,
                energy_rate=energy_rate,
                product_machine_map=product_machine_map,
                machine_costs_map=machine_costs_map,
                egp_per_usd=egp_per_usd,
                margin_rules_map=margin_rules_map,
                pricing_extras=pricing_extras,
                payment_terms_map=payment_terms_map,
                fob_cost_per_kg=fob_cost_per_kg_line,
                sea_freight_per_kg=sea_freight_per_kg_line,
                material_price_map=material_price_map,
                width_mm=width_mm,
                is_foreign_pricing=is_foreign_pricing,
                price_basis=price_basis,
            )
            if err:
                if (
                    request.headers.get("X-Requested-With")
                    == "XMLHttpRequest"
                ):
                    return (
                        jsonify(
                            {
                                "error": f"Line error (product {product_id}): {err}"
                            }
                        ),
                        400,
                    )
                flash(
                    f"Line error (product {product_id}): {err}",
                    "danger",
                )
                continue

            # نحسب core/packing per roll مرة واحدة هنا ونحفظها في line_result
            core_cost_per_unit_roll = 0.0
            core_weight = float(core_weight_kg or 0)
            if core_weight > 0 and core_price_per_kg_usd_line > 0:
                core_cost_per_unit_roll = core_price_per_kg_usd_line * core_weight

            packing_cost_per_unit_roll = 0.0
            try:
                rolls_per_pallet_val = float(rolls_per_pallet or 0)
                if (
                    packing_type_id
                    and pallet_type_id
                    and rolls_per_pallet_val > 0
                    and roll_weight_kg > 0
                ):
                    profile_id_for_snapshot = select_packing_profile_id_for_item(
                        product_id=product_id,
                        packing_type_id=packing_type_id,
                        pallet_type_id=pallet_type_id,
                        gross_kg_per_roll=roll_weight_kg,
                        packing_profile_overrides=packing_profile_overrides,
                        packing_profiles_by_id=packing_profiles_by_id,
                        global_profile_by_key=global_profile_by_key,
                    )
                    if profile_id_for_snapshot:
                        pack_info = packing_profile_cost_map.get(profile_id_for_snapshot)
                        if pack_info:
                            total_packing_usd_per_pallet = pack_info["usd"]
                            packing_cost_per_unit_roll = (
                                total_packing_usd_per_pallet / rolls_per_pallet_val
                            )
            except Exception:
                packing_cost_per_unit_roll = 0.0

            packing_core_cost_unit_roll = (
                core_cost_per_unit_roll + packing_cost_per_unit_roll
            )

            line_result["core_cost_unit_roll"] = core_cost_per_unit_roll
            line_result["packing_cost_unit_roll"] = packing_cost_per_unit_roll
            line_result["packing_core_cost_unit_roll"] = packing_core_cost_unit_roll

            disc = line_result["discounted"]
            if price_basis == "gross":
                exw_display = disc["exw_kg_gross"]
                fob_display = disc["fob_kg_gross"]
                cfr_display = disc["cfr_kg_gross"]
            elif price_basis == "net":
                exw_display = disc["exw_kg_net"]
                fob_display = disc["fob_kg_net"]
                cfr_display = disc["cfr_kg_net"]
            else:  # roll
                exw_display = disc["exw_roll"]
                fob_display = disc["fob_roll"]
                cfr_display = disc["cfr_roll"]

            lines_results.append(
                {
                    "calc": line_result,  # نحفظ كل نتيجة الكالكيوليشن
                    "discounted": {
                        "exw_display": exw_display,
                        "fob_display": fob_display,
                        "cfr_display": cfr_display,
                    },
                }
            )

        t_logic_end = time.perf_counter()
        print(
            f"[pricing-inner] Python logic (lines loop) took {t_logic_end - t_logic_start:.3f}s"
        )

        t1 = time.perf_counter()
        print(
            f"[pricing] Calculated {len(lines_input)} line(s) in {t1 - t0:.3f}s"
        )

        # تقسيم واضح: DB+maps vs loop
        print(
            "[prof] DB+maps =", t_after_db - t0,
            " | loop+calc =", t_logic_end - t_logic_start,
            " | total =", t1 - t0,
        )

        # ===== هنا نفصل بين AJAX وبين POST العادي =====
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            # طلب AJAX: إما calculate (والـ save اتعالج فوق)
            if mode == "calculate":
                session["pricing_header"] = {
                    "selected_port_id": selected_port_id,
                    "selected_dest_id": selected_dest_id,
                    "selected_payment_term_id": selected_payment_term_id,
                    "discount_percent": discount_percent,
                    "seller_type": seller_type,
                }
                session["pricing_lines_input"] = lines_input
                session["pricing_lines_results"] = lines_results

                return jsonify({"lines_results": lines_results})

            # أي mode AJAX آخر غير مدعوم هنا
            return jsonify({"error": "Unsupported AJAX mode."}), 400

        # ===== POST عادي (بدون AJAX): نحفظ في السيشن ثم نعمل redirect كما كان =====
        session["pricing_header"] = {
            "selected_port_id": selected_port_id,
            "selected_dest_id": selected_dest_id,
            "selected_payment_term_id": selected_payment_term_id,
            "discount_percent": discount_percent,
            "seller_type": seller_type,
        }
        session["pricing_lines_input"] = lines_input
        session["pricing_lines_results"] = lines_results

        return redirect(url_for("pricing.pricing_screen"))
    
    pricing_header = {}

    if request.method == "GET":
        header_data = session.get("pricing_header")
        if header_data:
            selected_port_id = header_data.get("selected_port_id")
            selected_dest_id = header_data.get("selected_dest_id")
            selected_payment_term_id = header_data.get(
                "selected_payment_term_id"
            )
            discount_percent = header_data.get("discount_percent", 0.0)
            
            pricing_header = header_data

        lines_input = session.get("pricing_lines_input", []) or []
        lines_results = session.get("pricing_lines_results", []) or []

    # reload payment terms for render
    with get_db() as cur:
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
        payment_terms_rows = cur.fetchall()

    return render_template(
        "pricing/index.html",
        products=products,
        ports=ports,
        destinations=destinations,
        payment_terms=payment_terms_rows,
        selected_port_id=selected_port_id,
        selected_dest_id=selected_dest_id,
        selected_payment_term_id=selected_payment_term_id,
        discount_percent=discount_percent,
        pallet_types=pallet_types,
        packing_types=packing_types,
        lines_input=lines_input,
        lines_results=lines_results,
        cache_updated_at=cache_updated_at,
        cache_version=cache_version,
        pricing_header=pricing_header,
    )


@pricing_bp.route("/quotations", methods=["GET"])
@login_required
@roles_required("admin", "owner", "sales_manager", "sales")
def quotations_list():
    """
    عرض الكوتيشن حسب صلاحيات اليوزر:
    - admin/owner/sales_manager: كل الكوتيشن
    - sales: فقط الكوتيشن اللي أنشأها هو
    """
    with get_db() as cur:
        if current_user.role in ("admin", "owner", "sales_manager"):
            cur.execute(
                """
                SELECT
                    q.id,                             -- 0
                    q.quotation_number,               -- 1
                    q.customer_name,                  -- 2
                    q.customer_country,               -- 3
                    q.port_id,                        -- 4
                    q.destination_id,                 -- 5
                    q.payment_term_id,                -- 6
                    q.global_discount_percent,        -- 7
                    q.created_at,                     -- 8
                    p.name AS port_name,              -- 9
                    p.country AS port_country,        -- 10
                    d.country AS dest_country,        -- 11
                    COALESCE(d.city, '') AS dest_city,-- 12
                    pt.name AS payment_term_name,     -- 13
                    u.username AS created_by_username -- 14
                FROM quotations q
                LEFT JOIN ports p
                    ON q.port_id = p.id
                LEFT JOIN destinations d
                    ON q.destination_id = d.id
                LEFT JOIN payment_terms pt
                    ON q.payment_term_id = pt.id
                LEFT JOIN users u
                    ON q.created_by_user_id = u.id
                ORDER BY q.created_at DESC, q.id DESC
                """
            )
        elif current_user.role == "sales":
            cur.execute(
                """
                SELECT
                    q.id,                             -- 0
                    q.quotation_number,               -- 1
                    q.customer_name,                  -- 2
                    q.customer_country,               -- 3
                    q.port_id,                        -- 4
                    q.destination_id,                 -- 5
                    q.payment_term_id,                -- 6
                    q.global_discount_percent,        -- 7
                    q.created_at,                     -- 8
                    p.name AS port_name,              -- 9
                    p.country AS port_country,        -- 10
                    d.country AS dest_country,        -- 11
                    COALESCE(d.city, '') AS dest_city,-- 12
                    pt.name AS payment_term_name,     -- 13
                    u.username AS created_by_username -- 14
                FROM quotations q
                LEFT JOIN ports p
                    ON q.port_id = p.id
                LEFT JOIN destinations d
                    ON q.destination_id = d.id
                LEFT JOIN payment_terms pt
                    ON q.payment_term_id = pt.id
                LEFT JOIN users u
                    ON q.created_by_user_id = u.id
                WHERE q.created_by_user_id = %s
                ORDER BY q.created_at DESC, q.id DESC
                """,
                (current_user.id,)
            )

        quotations = cur.fetchall()

    return render_template(
        "pricing/quotations_list.html",
        quotations=quotations,
    )

@pricing_bp.route("/quotation/<int:quotation_id>/print", methods=["GET"])
@login_required
@roles_required("admin", "owner", "sales_manager", "sales")
def quotation_print(quotation_id: int):
    """
    عرض الكوتيشن في صفحة HTML قابلة للطباعة.
    """
    with get_db() as cur:
        # header
        cur.execute(
            """
            SELECT
                q.id,
                q.quotation_number,
                q.customer_name,
                q.customer_country,
                q.port_id,
                q.destination_id,
                q.payment_term_id,
                q.global_discount_percent,
                q.created_at,
                q.created_by_user_id,
                p.name AS port_name,
                p.country AS port_country,
                d.country AS dest_country,
                COALESCE(d.city, '') AS dest_city,
                pt.name AS payment_term_name,
                pt.credit_days
            FROM quotations q
            LEFT JOIN ports p
                ON q.port_id = p.id
            LEFT JOIN destinations d
                ON q.destination_id = d.id
            LEFT JOIN payment_terms pt
                ON q.payment_term_id = pt.id
            WHERE q.id = %s
            """,
            (quotation_id,),
        )
        header = cur.fetchone()

        if not header:
            flash("Quotation not found.", "warning")
            return redirect(url_for("pricing.quotations_list"))

        q_created_by_user_id = header[9]

        if current_user.role == "sales" and q_created_by_user_id != current_user.id:
            flash("You are not authorized to view this quotation.", "warning")
            return redirect(url_for("pricing.quotations_list"))

        (
            q_id,
            quotation_number,
            customer_name,
            customer_country,
            port_id,
            destination_id,
            payment_term_id,
            global_discount_percent,
            created_at,
            _created_by_user_id,
            port_name,
            port_country,
            dest_country,
            dest_city,
            payment_term_name,
            credit_days,
        ) = header

        # items
        cur.execute(
            """
            SELECT
                qi.product_id,
                pr.code,
                pr.micron,
                pr.stretchability_percent,
                pr.film_type,
                qi.price_basis,
                qi.pallets_per_container,
                qi.width_mm,
                qi.rolls_per_pallet,
                qi.roll_weight_kg,
                qi.core_weight_kg,
                qi.discount_percent,
                qi.is_colored,
                pt.name AS pallet_type_name,
                pkt.name AS packing_type_name,
                qi.exw_price,
                qi.fob_price,
                qi.cfr_price
            FROM quotation_items qi
            LEFT JOIN products pr
                ON qi.product_id = pr.id
            LEFT JOIN pallet_types pt
                ON qi.pallet_type_id = pt.id
            LEFT JOIN packing_types pkt
                ON qi.packing_type_id = pkt.id
            WHERE qi.quotation_id = %s
            ORDER BY qi.id
            """,
            (quotation_id,),
        )
        items = cur.fetchall()

    logo_rel_path = url_for("static", filename="img/quotation_header.png")

    return render_template(
        "pricing/quotation_print.html",
        header=header,
        items=items,
        logo_rel_path=logo_rel_path,
    )

@pricing_bp.route("/quotation/<int:quotation_id>/cost", methods=["GET"])

@login_required
@roles_required("admin", "owner", "sales_manager", "sales")

def quotation_cost(quotation_id: int):
    """
    استعراض cost breakdown لكوتيشن محفوظة (لكل بند)،
    معتمد على snapshot محفوظة وقت حفظ الكوتيشن.
    لا يغيّر أي بيانات، عرض فقط.
    """
    # ===== 1) تحميل هيدر الكوتيشن المحفوظ =====
    with get_db() as cur:
        cur.execute(
            """
            SELECT
                q.id,
                q.quotation_number,
                q.customer_name,
                q.customer_country,
                q.port_id,
                q.destination_id,
                q.payment_term_id,
                q.global_discount_percent,
                q.created_at,
                p.name AS port_name,
                p.country AS port_country,
                d.country AS dest_country,
                COALESCE(d.city, '') AS dest_city,
                COALESCE(q.payment_term_name_snapshot, pt.name) AS payment_term_name,
                COALESCE(q.payment_term_days_snapshot, pt.credit_days) AS payment_term_days,
                COALESCE(q.credit_surcharge_percent_snapshot, 0) AS credit_surcharge_percent_snapshot,
                q.fx_egp_per_usd,
                q.created_by_user_id,
                q.seller_type
            FROM quotations q
            LEFT JOIN ports p
                ON q.port_id = p.id
            LEFT JOIN destinations d
                ON q.destination_id = d.id
            LEFT JOIN payment_terms pt
                ON q.payment_term_id = pt.id
            WHERE q.id = %s
            """,
            (quotation_id,),
        )
        header = cur.fetchone()

    if not header:
        flash("Quotation not found.", "warning")
        return redirect(url_for("pricing.quotations_list"))

    (
        q_id,
        quotation_number,
        customer_name,
        customer_country,
        selected_port_id,
        selected_dest_id,
        selected_payment_term_id,
        global_discount_percent,
        created_at,
        port_name,
        port_country,
        dest_country,
        dest_city,
        payment_term_name,
        payment_term_days,
        credit_surcharge_percent_snapshot,
        fx_egp_per_usd,
        created_by_user_id,
        seller_type,
    ) = header
    
    is_foreign_pricing = (seller_type or "").lower() == "foreign"

    if current_user.role == "sales" and created_by_user_id != current_user.id:
        flash("You are not authorized to view cost breakdown for this quotation.", "warning")
        return redirect(url_for("pricing.quotations_list"))

    # نقرأ الـ Credit% من الـ snapshot مباشرة
    credit_surcharge_percent_header = float(credit_surcharge_percent_snapshot or 0.0)

    with get_db() as cur:
        # ===== 2) تحميل بنود الكوتيشن (quotation_items) مع الـ id =====
        cur.execute(
            """
            SELECT
                qi.id,                 -- quotation_item_id
                qi.product_id,
                qi.price_basis,
                qi.pallets_per_container,
                qi.width_mm,
                qi.rolls_per_pallet,
                qi.roll_weight_kg,
                qi.core_weight_kg,
                qi.discount_percent,
                qi.is_colored,
                qi.pallet_type_id,
                qi.packing_type_id,
                qi.exw_price,
                qi.fob_price,
                qi.cfr_price
            FROM quotation_items qi
            WHERE qi.quotation_id = %s
            ORDER BY qi.id
            """,
            (quotation_id,),
        )
        q_items = cur.fetchall()
        if not q_items:
            return "No items in quotation", 404

        # ===== تحميل snapshots لكل البنود =====
        quotation_item_ids = [int(r[0]) for r in q_items]
        snapshots_map = {}
        if quotation_item_ids:
            cur.execute(
                """
                SELECT
                    quotation_item_id,
                    rm_cost_per_kg_net,
                    energy_cost_per_kg_net,
                    machine_oh_per_kg_net,
                    net_kg_per_roll,
                    gross_kg_per_roll,
                    extra_roll,
                    margin_percent,
                    fob_cost_unit_roll,
                    sea_freight_cost_unit_roll,
                    core_cost_unit_roll,
                    packing_cost_unit_roll,
                    packing_core_cost_unit_roll,
                    exw_kg_net,
                    exw_kg_gross,
                    exw_roll,
                    fob_kg_net,
                    fob_kg_gross,
                    fob_roll,
                    cfr_kg_net,
                    cfr_kg_gross,
                    cfr_roll,
                    foreign_extra_mode,
                    foreign_extra_value,
                    fob_kg_gross_base,
                    cfr_kg_gross_base,
                    exw_kg_net_base,
                    fob_kg_net_base,
                    cfr_kg_net_base,
                    exw_roll_base,
                    fob_roll_base,
                    cfr_roll_base
                FROM quotation_item_cost_snapshots
                WHERE quotation_item_id = ANY(%s)
                """,
                (quotation_item_ids,),
            )
            rows_snap = cur.fetchall()
            for r in rows_snap:
                (
                    qi_id,
                    rm_cost_per_kg_net,
                    energy_cost_per_kg_net,
                    machine_oh_per_kg_net,
                    net_kg_per_roll,
                    gross_kg_per_roll,
                    extra_roll,
                    margin_percent,
                    fob_cost_unit_roll,
                    sea_freight_cost_unit_roll,
                    core_cost_unit_roll,
                    packing_cost_unit_roll,
                    packing_core_cost_unit_roll,
                    exw_kg_net,
                    exw_kg_gross,
                    exw_roll,
                    fob_kg_net,
                    fob_kg_gross,
                    fob_roll,
                    cfr_kg_net,
                    cfr_kg_gross,
                    cfr_roll,
                    foreign_extra_mode,
                    foreign_extra_value,
                    fob_kg_gross_base,
                    cfr_kg_gross_base,
                    exw_kg_net_base,
                    fob_kg_net_base,
                    cfr_kg_net_base,
                    exw_roll_base,
                    fob_roll_base,
                    cfr_roll_base,
                ) = r
                snapshots_map[int(qi_id)] = {
                    "rm_cost_per_kg_net": float(rm_cost_per_kg_net or 0.0),
                    "energy_cost_per_kg_net": float(energy_cost_per_kg_net or 0.0),
                    "machine_oh_per_kg_net": float(machine_oh_per_kg_net or 0.0),
                    "net_kg_per_roll": float(net_kg_per_roll or 0.0),
                    "gross_kg_per_roll": float(gross_kg_per_roll or 0.0),
                    "extra_roll": float(extra_roll or 0.0),
                    "margin_percent": float(margin_percent or 0.0),
                    "fob_cost_unit_roll": float(fob_cost_unit_roll or 0.0),
                    "sea_freight_cost_unit_roll": float(sea_freight_cost_unit_roll or 0.0),
                    "core_cost_unit_roll": float(core_cost_unit_roll or 0.0),
                    "packing_cost_unit_roll": float(packing_cost_unit_roll or 0.0),
                    "packing_core_cost_unit_roll": float(packing_core_cost_unit_roll or 0.0),
                    "exw_kg_net": float(exw_kg_net or 0.0),
                    "exw_kg_gross": float(exw_kg_gross or 0.0),
                    "exw_roll": float(exw_roll or 0.0),
                    "fob_kg_net": float(fob_kg_net or 0.0),
                    "fob_kg_gross": float(fob_kg_gross or 0.0),
                    "fob_roll": float(fob_roll or 0.0),
                    "cfr_kg_net": float(cfr_kg_net or 0.0),
                    "cfr_kg_gross": float(cfr_kg_gross or 0.0),
                    "cfr_roll": float(cfr_roll or 0.0),
                    "foreign_extra_mode": (foreign_extra_mode or "").strip(),
                    "foreign_extra_value": float(foreign_extra_value or 0.0),
                    "fob_kg_gross_base": float(fob_kg_gross_base or 0.0),
                    "cfr_kg_gross_base": float(cfr_kg_gross_base or 0.0),
                    "exw_kg_net_base": float(exw_kg_net_base or 0.0),
                    "fob_kg_net_base": float(fob_kg_net_base or 0.0),
                    "cfr_kg_net_base": float(cfr_kg_net_base or 0.0),
                    "exw_roll_base": float(exw_roll_base or 0.0),
                    "fob_roll_base": float(fob_roll_base or 0.0),
                    "cfr_roll_base": float(cfr_roll_base or 0.0),
                }

        # ===== 3) تحميل بيانات تساعد العرض (spec + core + packing) =====

        # منتجات (نستخدم product_id من q_items: index 1)
        cur.execute(
            """
            SELECT id,
                   code,
                   micron,
                   stretchability_percent,
                   film_type,
                   is_manual,
                   kg_per_roll,
                   bom_scrap_percent
            FROM products
            WHERE id = ANY(%s)
            """,
            ([int(row[1]) for row in q_items],),
        )
        products_rows = cur.fetchall()
        product_info_map = {}
        for row in products_rows:
            pid = row[0]
            product_info_map[pid] = {
                "code": row[1],
                "micron": int(row[2] or 0),
                "stretchability_percent": row[3],
                "film_type": (row[4] or "standard").strip(),
                "is_manual": bool(row[5]),
                "kg_per_roll": float(row[6] or 0),
                "bom_scrap_percent": float(row[7] or 0),
            }

        # سعر الصرف USD/EGP (للهيدر و CORE و packing)
        cur.execute(
            """
            SELECT egp_per_usd
            FROM currency_rates
            WHERE is_active = true
            ORDER BY effective_date DESC, id DESC
            LIMIT 1
            """
        )
        row_fx = cur.fetchone()
        egp_per_usd = float(row_fx[0] or 0) if row_fx else 0.0

        # ===== سعر الكور /kg (materials.category = 'CORE') =====
        cur.execute(
            """
            SELECT price_per_unit, currency
            FROM materials
            WHERE category = 'CORE'
            ORDER BY id
            LIMIT 1
            """
        )
        row_core = cur.fetchone()
        core_price_per_kg_usd = 0.0
        if row_core:
            core_price, core_curr = row_core[0], (row_core[1] or "USD").upper()
            core_price = float(core_price or 0)
            if core_curr != "USD" and egp_per_usd > 0:
                core_price_per_kg_usd = core_price / egp_per_usd
            else:
                core_price_per_kg_usd = core_price

        # ===== تجهيز خريطة تكلفة التعبئة لكل كومبو pallet_type + packing_type =====
        cur.execute(
            """
            SELECT
                pp.packing_type_id,
                pp.pallet_type_id,
                pi.material_id,
                pi.quantity_per_pallet,
                m.price_per_unit,
                m.currency
            FROM packing_items pi
            JOIN packing_profiles pp ON pp.id = pi.packing_profile_id
            JOIN materials m        ON m.id = pi.material_id
            """
        )
        packing_rows = cur.fetchall()
        packing_cost_per_pallet_map = defaultdict(lambda: {"usd": 0.0, "egp": 0.0})
        for (
            packing_type_id,
            pallet_type_id,
            material_id,
            qty_per_pallet,
            price_per_unit,
            currency,
        ) in packing_rows:
            # حماية احتياطية، بس نظريًا مش محتاجها لأن packing_profiles.* NOT NULL
            if packing_type_id is None or pallet_type_id is None:
                continue

            qty = float(qty_per_pallet or 0)
            price = float(price_per_unit or 0)
            curr = (currency or "USD").upper()
            key_pp = (int(packing_type_id), int(pallet_type_id))
            if curr == "USD":
                packing_cost_per_pallet_map[key_pp]["usd"] += qty * price
            else:
                packing_cost_per_pallet_map[key_pp]["egp"] += qty * price

        # تحويل EGP إلى USD
        for key, val in packing_cost_per_pallet_map.items():
            egp_val = val["egp"]
            if egp_val and egp_per_usd > 0:
                val["usd"] += egp_val / egp_per_usd
            val["egp"] = 0.0

    # ===== 4) استخدام snapshots لكل بند لإخراج breakdown =====
    breakdown_rows = []
    for row in q_items:
        (
            quotation_item_id,
            product_id,
            price_basis,
            pallets_per_container,
            width_mm,
            rolls_per_pallet,
            roll_weight_kg,
            core_weight_kg,
            line_discount,
            is_colored,
            pallet_type_id,
            packing_type_id,
            exw_price_saved,
            fob_price_saved,
            cfr_price_saved,
        ) = row

        snap = snapshots_map.get(int(quotation_item_id))
        if not snap:
            continue

        # معلومات المنتج لعرض الـ spec
        p_info = product_info_map.get(int(product_id)) or {}
        product_micron = p_info.get("micron")
        product_stretch = p_info.get("stretchability_percent")
        product_film_type = p_info.get("film_type")

        # ===== قيم الفيلم /kg net من snapshot =====
        rm_cost_per_kg_net = snap["rm_cost_per_kg_net"]
        energy_cost_per_kg_net = snap["energy_cost_per_kg_net"]
        machine_oh_per_kg_net = snap["machine_oh_per_kg_net"]
        film_cost_per_kg_net = (
            rm_cost_per_kg_net + energy_cost_per_kg_net + machine_oh_per_kg_net
        )

        unit_weight_net = snap["net_kg_per_roll"]
        roll_gross_kg = snap["gross_kg_per_roll"]

        # ===== Core + Packing per roll =====
        core_cost_per_unit_roll = snap["core_cost_unit_roll"]
        packing_cost_per_unit_roll = snap["packing_cost_unit_roll"]
        packing_core_cost_unit_roll = snap["packing_core_cost_unit_roll"]

        # Film cost /roll و Total cost /roll (before margin & extra)
        film_cost_unit_roll = film_cost_per_kg_net * unit_weight_net
        total_cost_unit_roll = film_cost_unit_roll + packing_core_cost_unit_roll

        # Extra per roll من snapshot
        extra_roll = snap["extra_roll"]

        # FOB/Sea cost per roll من snapshot
        fob_cost_unit_roll = snap["fob_cost_unit_roll"]
        sea_freight_cost_unit_roll = snap["sea_freight_cost_unit_roll"]

        # ===== أسعار البيع من snapshot حسب الـ basis =====
        if price_basis == "gross":
            exw_unit = snap["exw_kg_gross"]
            fob_unit = snap["fob_kg_gross"]
            cfr_unit = snap["cfr_kg_gross"]

            # EXW مفيهوش round up → الخام = النهائي
            exw_unit_base = exw_unit
            fob_unit_base = snap["fob_kg_gross_base"]
            cfr_unit_base = snap["cfr_kg_gross_base"]

            unit_divisor_for_roll = roll_gross_kg if roll_gross_kg > 0 else 1.0

            if roll_gross_kg > 0:
                fob_cost_unit = fob_cost_unit_roll / roll_gross_kg
                sea_freight_cost_unit = sea_freight_cost_unit_roll / roll_gross_kg
            else:
                fob_cost_unit = 0.0
                sea_freight_cost_unit = 0.0

        elif price_basis == "net":
            exw_unit = snap["exw_kg_net"]
            fob_unit = snap["fob_kg_net"]
            cfr_unit = snap["cfr_kg_net"]
            exw_unit_base = snap["exw_kg_net_base"]
            fob_unit_base = snap["fob_kg_net_base"]
            cfr_unit_base = snap["cfr_kg_net_base"]
            unit_divisor_for_roll = unit_weight_net if unit_weight_net > 0 else 1.0

            if unit_weight_net > 0:
                fob_cost_unit = fob_cost_unit_roll / unit_weight_net
                sea_freight_cost_unit = sea_freight_cost_unit_roll / unit_weight_net
            else:
                fob_cost_unit = 0.0
                sea_freight_cost_unit = 0.0

        else:  # roll
            exw_unit = snap["exw_roll"]
            fob_unit = snap["fob_roll"]
            cfr_unit = snap["cfr_roll"]
            exw_unit_base = snap["exw_roll_base"]
            fob_unit_base = snap["fob_roll_base"]
            cfr_unit_base = snap["cfr_roll_base"]
            unit_divisor_for_roll = 1.0

            fob_cost_unit = fob_cost_unit_roll
            sea_freight_cost_unit = sea_freight_cost_unit_roll

        # costs per unit basis
        if price_basis == "gross" and roll_gross_kg > 0 and unit_weight_net > 0:
            ratio_net_to_gross = unit_weight_net / roll_gross_kg
            rm_cost_per_unit_basis = rm_cost_per_kg_net * ratio_net_to_gross
            energy_cost_per_unit_basis = energy_cost_per_kg_net * ratio_net_to_gross
            machine_oh_per_unit_basis = machine_oh_per_kg_net * ratio_net_to_gross
            film_cost_per_unit_basis = film_cost_per_kg_net * ratio_net_to_gross
        elif price_basis == "net":
            rm_cost_per_unit_basis = rm_cost_per_kg_net
            energy_cost_per_unit_basis = energy_cost_per_kg_net
            machine_oh_per_unit_basis = machine_oh_per_kg_net
            film_cost_per_unit_basis = film_cost_per_kg_net
        else:
            rm_cost_per_unit_basis = rm_cost_per_kg_net * unit_weight_net
            energy_cost_per_unit_basis = energy_cost_per_kg_net * unit_weight_net
            machine_oh_per_unit_basis = machine_oh_per_kg_net * unit_weight_net
            film_cost_per_unit_basis = film_cost_per_kg_net * unit_weight_net

        film_cost_unit = film_cost_unit_roll / unit_divisor_for_roll
        packing_core_cost_unit = packing_core_cost_unit_roll / unit_divisor_for_roll
        total_cost_unit = total_cost_unit_roll / unit_divisor_for_roll

        # Extra /unit حسب الـ basis
        if price_basis == "gross":
            extra_unit = extra_roll / roll_gross_kg if roll_gross_kg > 0 else 0.0
        elif price_basis == "net":
            extra_unit = extra_roll / unit_weight_net if unit_weight_net > 0 else 0.0
        else:
            extra_unit = extra_roll

        # ===== المارجن: base و net (net = base - discount) =====
        base_margin_pct = snap["margin_percent"]

        line_discount_pct = float(line_discount or 0.0)
        global_discount_pct = float(global_discount_percent or 0.0)

        # الخصم الكلي اللي فعلاً مطبق على السعر
        total_discount_pct = line_discount_pct + global_discount_pct

        effective_margin_pct = base_margin_pct - total_discount_pct
        if effective_margin_pct < 0:
            effective_margin_pct = 0.0

        margin_value_unit = total_cost_unit * (effective_margin_pct / 100.0)
        
                # ===== Foreign extra من الـ snapshot (للعرض فقط) =====
        foreign_extra_mode = (snap.get("foreign_extra_mode") or "").strip()
        foreign_extra_value = float(snap.get("foreign_extra_value") or 0.0)

        if foreign_extra_mode == "percent":
            # يعرض كما أدخله اليوزر: نسبة
            foreign_extra_display = f"{foreign_extra_value:.2f} %"
        elif foreign_extra_mode == "per_unit":
            # يعرض كقيمة per unit
            foreign_extra_display = f"{foreign_extra_value:.3f}"
        else:
            foreign_extra_display = ""

        breakdown_rows.append(
                {
                    "product_id": product_id,
                    "product_micron": product_micron,
                    "product_stretch": product_stretch,
                    "product_film_type": product_film_type,
                    "price_basis": price_basis,
                    "pallets_per_container": pallets_per_container,
                    "width_mm": width_mm,
                    "rolls_per_pallet": rolls_per_pallet,
                    "roll_weight_kg": roll_weight_kg,
                    "core_weight_kg": core_weight_kg,
                    "discount_percent": total_discount_pct,
                    "line_discount_percent": line_discount_pct,
                    "global_discount_percent": global_discount_pct,
                    "is_colored": is_colored,
                    "pallet_type_id": pallet_type_id,
                    "packing_type_id": packing_type_id,
                    "exw_price_saved": exw_price_saved,
                    "fob_price_saved": fob_price_saved,
                    "cfr_price_saved": cfr_price_saved,
                    "rm_cost_per_unit_basis": rm_cost_per_unit_basis,
                    "energy_cost_per_unit_basis": energy_cost_per_unit_basis,
                    "machine_oh_per_unit_basis": machine_oh_per_unit_basis,
                    "film_cost_per_unit_basis": film_cost_per_unit_basis,
                    "rm_cost_per_kg_net": rm_cost_per_kg_net,
                    "energy_cost_per_kg_net": energy_cost_per_kg_net,
                    "machine_oh_per_kg_net": machine_oh_per_kg_net,
                    "film_cost_per_kg_net": film_cost_per_kg_net,
                    "unit_weight_net": unit_weight_net,
                    "film_cost_unit": film_cost_unit,
                    "packing_core_cost_unit": packing_core_cost_unit,
                    "total_cost_unit": total_cost_unit,
                    "margin_percent_base": base_margin_pct,
                    "margin_percent_net": effective_margin_pct,
                    "margin_value_unit": margin_value_unit,
                    "extra_roll": extra_roll,
                    "extra_unit": extra_unit,
                    "foreign_extra_display": foreign_extra_display,
                    "exw_unit": exw_unit,
                    "exw_unit_base": exw_unit_base,
                    "fob_cost_unit": fob_cost_unit,
                    "fob_unit": fob_unit,
                    "fob_unit_base": fob_unit_base,
                    "sea_freight_cost_unit": sea_freight_cost_unit,
                    "cfr_unit": cfr_unit,
                    "cfr_unit_base": cfr_unit_base,
                    "calc": None,
                }
            )
        
    return render_template(
        "pricing/quotation_cost.html",
        header=header,
        breakdown_rows=breakdown_rows,
        credit_surcharge_percent=credit_surcharge_percent_header,
        is_foreign_pricing=is_foreign_pricing,
    )