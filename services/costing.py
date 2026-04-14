from db import get_db
from pricing_cache import (
    get_cached_materials_landed_bulk,
    set_cached_materials_landed_bulk,
)


def get_material_landed_price_per_kg(material_id: int) -> float:
    """
    يرجّع landed cost per kg لمادة معينة.
    - base price بيتقرأ من materials.price_per_unit (دايمًا per kg داخليًا).
    - بعدين نطبّق import_cost_profiles:
        - global scope (ينطبق على الكل)
        - material scope (لو موجود، يتطبق بالإضافة للـ global)
    """
    with get_db() as cur:
        # 1) Base price per kg من materials
        cur.execute(
            """
            SELECT price_per_unit
            FROM materials
            WHERE id = %s
            """,
            (material_id,),
        )
        row = cur.fetchone()
        if not row:
            return 0.0
        base_price = float(row[0] or 0)

        # 2) Import cost profiles
        cur.execute(
            """
            SELECT scope, mode, value
            FROM import_cost_profiles
            WHERE scope = 'global'
               OR (scope = 'material' AND material_id = %s)
            """,
            (material_id,),
        )
        rows = cur.fetchall()

    if not rows:
        return base_price

    import_cost_per_kg = 0.0

    for scope, mode, value in rows:
        val = float(value or 0)
        if mode == "per_ton":
            import_cost_per_kg += val / 1000.0  # نحول من /ton إلى /kg
        elif mode == "percent":
            import_cost_per_kg += base_price * (val / 100.0)

    return base_price + import_cost_per_kg


def get_materials_landed_price_per_kg_bulk(material_ids: list[int]) -> dict[int, float]:
    if not material_ids:
        return {}

    # 1) حاول تجيب من الكاش
    material_ids = list({int(m) for m in material_ids if m})  # unique & clean
    cached_map = get_cached_materials_landed_bulk(material_ids)
    result: dict[int, float] = dict(cached_map)

    # ids اللي محتاجة حساب فعلي من DB
    missing_ids = [mid for mid in material_ids if mid not in cached_map]
    if not missing_ids:
        return result

    with get_db() as cur:
        # 2) Base price per kg من materials
        cur.execute(
            """
            SELECT id, price_per_unit
            FROM materials
            WHERE id = ANY(%s)
            """,
            (missing_ids,),
        )
        rows_base = cur.fetchall()
        base_price_map: dict[int, float] = {
            int(mid): float(price or 0)
            for mid, price in rows_base
        }

        if not base_price_map:
            return result

        # 3) Import cost profiles (global + material) للـ missing_ids
        cur.execute(
            """
            SELECT material_id, scope, mode, value
            FROM import_cost_profiles
            WHERE scope = 'global'
               OR (scope = 'material' AND material_id = ANY(%s))
            """,
            (missing_ids,),
        )
        rows_profiles = cur.fetchall()

    # لو ما فيش أي profiles، نرجّع base_price مباشرة
    if not rows_profiles:
        result.update(base_price_map)
        set_cached_materials_landed_bulk(base_price_map)
        return result

    # حضّر global rows + material-specific rows
    global_rows: list[tuple[str, float]] = []
    material_rows_map: dict[int, list[tuple[str, float]]] = {}

    for mat_id, scope, mode, value in rows_profiles:
        val = float(value or 0)
        if scope == "global":
            global_rows.append((mode, val))
        else:
            mid = int(mat_id or 0)
            material_rows_map.setdefault(mid, []).append((mode, val))

    computed_missing: dict[int, float] = {}

    for mid, base_price in base_price_map.items():
        import_cost_per_kg = 0.0

        # global rows
        for mode, val in global_rows:
            if mode == "per_ton":
                import_cost_per_kg += val / 1000.0
            elif mode == "percent":
                import_cost_per_kg += base_price * (val / 100.0)

        # material-specific rows
        for mode, val in material_rows_map.get(mid, []):
            if mode == "per_ton":
                import_cost_per_kg += val / 1000.0
            elif mode == "percent":
                import_cost_per_kg += base_price * (val / 100.0)

        computed_missing[mid] = base_price + import_cost_per_kg

    # 4) حدّث الكاش وارجع النتيجة
    set_cached_materials_landed_bulk(computed_missing)
    result.update(computed_missing)
    return result

def get_energy_rate_usd_per_kwh() -> float:
    """Return active electricity rate in USD/kWh, based on EGP tariff and active FX."""
    with get_db() as cur:
        # آخر تعريفة كهرباء بالجنيه
        cur.execute(
            """
            SELECT egp_per_kwh
            FROM energy_rates
            WHERE is_active = true
            ORDER BY effective_date DESC, id DESC
            LIMIT 1
            """
        )
        row_e = cur.fetchone()
        if not row_e:
            return 0.0
        egp_per_kwh = float(row_e[0] or 0)

        # آخر سعر صرف (EGP per USD)
        cur.execute(
            """
            SELECT egp_per_usd
            FROM currency_rates
            WHERE is_active = true
            ORDER BY effective_date DESC, id DESC
            LIMIT 1
            """
        )
        row_c = cur.fetchone()
        if not row_c:
            return 0.0
        egp_per_usd = float(row_c[0] or 0)

    if egp_per_usd <= 0:
        return 0.0

    usd_per_kwh = egp_per_kwh / egp_per_usd
    return usd_per_kwh


def get_pricing_rule_for_product(
    product_id: int,
    packing_type_id: int,
    roll_weight_kg: float,
) -> float:
    """
    يرجّع margin_percent المناسب للمنتج من جدول pricing_rules
    بناءً على:
      - micron, film_type من products
      - packing_type_id من quotation_items
      - roll_weight_kg من quotation_items
    """
    with get_db() as cur:
        # بيانات المنتج
        cur.execute(
            """
            SELECT micron,
                   film_type
            FROM products
            WHERE id = %s
            """,
            (product_id,),
        )
        row = cur.fetchone()
        if not row:
            return 0.0

        micron = int(row[0] or 0)
        film_type = (row[1] or "standard").strip()
        rw = float(roll_weight_kg or 0) or 1.0

        # margin factor من pricing_rules
        cur.execute(
            """
            SELECT margin_percent
            FROM pricing_rules
            WHERE micron_min <= %s AND micron_max >= %s
              AND film_type = %s
              AND packing_type_id = %s
              AND (
                    (roll_weight_min = 0 AND roll_weight_max = 0)
                 OR (%s >= roll_weight_min AND %s <= roll_weight_max)
                  )
            ORDER BY roll_weight_min, roll_weight_max
            LIMIT 1
            """,
            (micron, micron, film_type, packing_type_id, rw, rw),
        )
        rule = cur.fetchone()
        if not rule:
            return 0.0
        return float(rule[0] or 0)


def get_pricing_extras() -> tuple[float, float]:
    """
    Returns (color_extra_usd_per_kg, prestretch_extra_usd_per_kg)
    from the active pricing_extras record (or zeros).
    """
    with get_db() as cur:
        cur.execute(
            """
            SELECT color_extra_usd_per_kg, prestretch_extra_usd_per_kg
            FROM pricing_extras
            WHERE is_active = true
            ORDER BY id DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
        if not row:
            return 0.0, 0.0
        return float(row[0] or 0), float(row[1] or 0)


def calculate_base_price_per_kg(
    product_id: int,
    packing_type_id: int,
    roll_weight_kg: float,
    total_cost_per_kg: float,
) -> float:
    """
    total_cost_per_kg: كل التكاليف قبل الربحية (خامات + كهرباء + ثابت + باكينج + ...)

    السعر النهائي للكيلو = تكلفة × (1 + margin%)
    ثم نضيف extras للبريسترتش واللون كقيمة ثابتة لكل كجم.
    """
    if total_cost_per_kg <= 0:
        return 0.0

    margin = get_pricing_rule_for_product(product_id, packing_type_id, roll_weight_kg)
    margin_factor = 1 + (margin / 100.0)
    base_price = total_cost_per_kg * margin_factor

    color_extra, prestretch_extra = get_pricing_extras()

    with get_db() as cur:
        cur.execute(
            """
            SELECT film_type, is_colored
            FROM products
            WHERE id = %s
            """,
            (product_id,),
        )
        row = cur.fetchone()
        if row:
            film_type = (row[0] or "standard").strip()
            is_colored = bool(row[1])

            if film_type == "prestretch":
                base_price += prestretch_extra
            if is_colored:
                base_price += color_extra

    return base_price


def calculate_export_prices(
    product_id: int,
    port_id: int,
    destination_id: int,
    payment_term: str,
    total_cost_per_kg: float,
    packing_type_id: int,
    roll_weight_kg: float,
) -> dict:
    """
    يحسب:
    - FOB/CFR per kg
    - FOB/CFR per roll
    - نسخ cash/credit بسيطة بناءً على payment_term
    """
    base_price_per_kg = calculate_base_price_per_kg(
        product_id,
        packing_type_id,
        roll_weight_kg,
        total_cost_per_kg,
    )

    with get_db() as cur:
        # kg per roll
        cur.execute(
            "SELECT kg_per_roll FROM products WHERE id = %s",
            (product_id,),
        )
        row = cur.fetchone()
        kg_per_roll = float(row[0] or 0) if row else 0.0

        # FOB + freight per container (لسه مش متوزعة على الكيلو)
        cur.execute(
            "SELECT fob_cost_usd_per_container FROM fob_costs WHERE port_id = %s LIMIT 1",
            (port_id,),
        )
        row_fob = cur.fetchone()
        fob_per_container = float(row_fob[0] or 0) if row_fob else 0.0

        cur.execute(
            """
            SELECT shipping_rate_usd_per_container
            FROM sea_freight_rates
            WHERE loading_port_id = %s
              AND destination_id = %s
            LIMIT 1
            """,
            (port_id, destination_id),
        )
        row_sf = cur.fetchone()
        freight_per_container = float(row_sf[0] or 0) if row_sf else 0.0

        # Payment terms (نستخدم أكبر credit_days نشطة كمثال)
        cur.execute(
            """
            SELECT credit_days, annual_rate_percent
            FROM payment_terms
            WHERE is_active = true
            ORDER BY credit_days DESC, id DESC
            LIMIT 1
            """
        )
        row_pt = cur.fetchone()
        if row_pt:
            credit_days = int(row_pt[0] or 0)
            annual_rate_percent = float(row_pt[1] or 0)
        else:
            credit_days = 0
            annual_rate_percent = 0.0

    # لحد ما نحدد kg_per_container هنخلي توزيع FOB & freight = 0
    fob_extra_per_kg = 0.0
    freight_extra_per_kg = 0.0

    fob_kg = base_price_per_kg + fob_extra_per_kg
    cfr_kg = base_price_per_kg + fob_extra_per_kg + freight_extra_per_kg

    fob_roll = fob_kg * kg_per_roll if kg_per_roll > 0 else 0.0
    cfr_roll = cfr_kg * kg_per_roll if kg_per_roll > 0 else 0.0

    # Cash vs credit
    if payment_term == "cash" or credit_days <= 0 or annual_rate_percent <= 0:
        cash_factor = 1.0
        credit_factor = 1.0
    else:
        daily_rate = annual_rate_percent / 365.0
        credit_surcharge_percent = daily_rate * credit_days
        cash_factor = 1.0
        credit_factor = 1 + (credit_surcharge_percent / 100.0)

    cash_fob_kg = fob_kg * cash_factor
    cash_cfr_kg = cfr_kg * cash_factor
    cash_fob_roll = fob_roll * cash_factor
    cash_cfr_roll = cfr_roll * cash_factor

    credit_fob_kg = fob_kg * credit_factor
    credit_cfr_kg = cfr_kg * credit_factor
    credit_fob_roll = fob_roll * credit_factor
    credit_cfr_roll = cfr_roll * credit_factor

    return {
        "base_price_per_kg": base_price_per_kg,
        "fob_kg": fob_kg,
        "cfr_kg": cfr_kg,
        "fob_roll": fob_roll,
        "cfr_roll": cfr_roll,
        "cash_fob_kg": cash_fob_kg,
        "cash_cfr_kg": cash_cfr_kg,
        "cash_fob_roll": cash_fob_roll,
        "cash_cfr_roll": cash_cfr_roll,
        "credit_fob_kg": credit_fob_kg,
        "credit_cfr_kg": credit_cfr_kg,
        "credit_fob_roll": credit_fob_roll,
        "credit_cfr_roll": credit_cfr_roll,
    }