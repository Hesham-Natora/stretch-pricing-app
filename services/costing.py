from db import get_db
from pricing_cache import (
    get_cached_materials_landed_bulk,
    set_cached_materials_landed_bulk,
)
from typing import Optional

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


CORE_SEMI_AND_STANDARD_ID = 10  # MAT-0010 Core (للسيميز + المنتجات التامة العادية)


def _get_core_price_per_kg_for_semi_usd() -> float:
    """
    يرجّع سعر الكور لكل كجم بالدولار للسيميز والمنتجات العادية.
    يستخدم material id = CORE_SEMI_AND_STANDARD_ID (MAT-0010 Core).
    """
    with get_db() as cur:
        cur.execute(
            """
            SELECT price_per_unit, currency
            FROM materials
            WHERE id = %s
            """,
            (CORE_SEMI_AND_STANDARD_ID,),
        )
        row = cur.fetchone()

    if not row:
        return 0.0

    price_per_unit = float(row[0] or 0)
    currency = (row[1] or "USD").strip().upper()

    if price_per_unit <= 0:
        return 0.0

    if currency == "USD":
        return price_per_unit

    # نفترض أن العملة EGP ونحوّلها لـ USD باستخدام آخر currency_rates
    with get_db() as cur:
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

    if not row_fx:
        return 0.0

    egp_per_usd = float(row_fx[0] or 0)
    if egp_per_usd <= 0:
        return 0.0

    # السعر بالجنيه / (جنيه لكل دولار) = دولار لكل كجم
    return price_per_unit / egp_per_usd

def get_semi_core_cost_per_kg(product_id: int) -> float:
    """
    يحسب تكلفة الكور للسيمي لكل كجم صافي:
    - يقرأ gross_kg_per_roll, core_kg_per_roll من product_semis
    - يستخدم _get_core_price_per_kg_for_semi_usd لسعر الكور بالدولار لكل كجم
    - يوزع تكلفة الكور على الكيلو الصافي في اللفة
    """
    with get_db() as cur:
        cur.execute(
            """
            SELECT gross_kg_per_roll, core_kg_per_roll
            FROM product_semis
            WHERE product_id = %s
            """,
            (product_id,),
        )
        row = cur.fetchone()

    if not row:
        return 0.0

    gross_kg_per_roll = float(row[0] or 0)
    core_kg_per_roll = float(row[1] or 0)

    if (
        gross_kg_per_roll <= 0
        or core_kg_per_roll <= 0
        or core_kg_per_roll >= gross_kg_per_roll
    ):
        return 0.0

    net_kg_per_roll = gross_kg_per_roll - core_kg_per_roll
    core_price_per_kg = _get_core_price_per_kg_for_semi_usd()
    if core_price_per_kg <= 0 or net_kg_per_roll <= 0:
        return 0.0

    core_cost_per_roll = core_price_per_kg * core_kg_per_roll
    return core_cost_per_roll / net_kg_per_roll    

def get_ref_product_id_for_semi(product_id: int) -> Optional[int]:
    """
    يرجّع product_id المرجعي للسيمي:
    - يقرأ roll_bom_id من product_semis للـ product_id (السيمي)
    - ومنه يجيب product_roll_boms.product_id (المنتج المرجعي اللي أخدنا منه الـ Roll BOM)
    """
    with get_db() as cur:
        cur.execute(
            """
            SELECT roll_bom_id
            FROM product_semis
            WHERE product_id = %s
            """,
            (product_id,),
        )
        row = cur.fetchone()
        if not row:
            return None

        roll_bom_id = row[0]
        if not roll_bom_id:
            return None

        cur.execute(
            """
            SELECT product_id
            FROM product_roll_boms
            WHERE id = %s
            """,
            (roll_bom_id,),
        )
        row2 = cur.fetchone()
        if not row2:
            return None

        ref_product_id = int(row2[0])
        return ref_product_id
    

def get_semi_energy_and_capacity(product_id: int) -> tuple[float, float]:
    ref_product_id = get_ref_product_id_for_semi(product_id)
    if not ref_product_id:
        return 0.0, 0.0

    with get_db() as cur:
        cur.execute(
            """
            SELECT kwh_per_kg, monthly_product_capacity_kg
            FROM product_machines
            WHERE product_id = %s
              AND preferred_machine = TRUE
            ORDER BY id
            LIMIT 1
            """,
            (ref_product_id,),
        )
        row = cur.fetchone()

        if not row:
            cur.execute(
                """
                SELECT kwh_per_kg, monthly_product_capacity_kg
                FROM product_machines
                WHERE product_id = %s
                ORDER BY id
                LIMIT 1
                """,
                (ref_product_id,),
            )
            row = cur.fetchone()

        if not row:
            return 0.0, 0.0

        kwh_per_kg = float(row[0] or 0)
        monthly_capacity_kg = float(row[1] or 0)
        return kwh_per_kg, monthly_capacity_kg

    
def get_semi_energy_cost_per_kg(product_id: int) -> float:
    """
    يحسب تكلفة الطاقة للسيمي لكل كجم:
    semi_energy_cost_per_kg = kwh_per_kg * energy_rate_usd_per_kwh
    """
    kwh_per_kg, monthly_capacity_kg = get_semi_energy_and_capacity(product_id)
    if kwh_per_kg <= 0:
        return 0.0

    energy_rate = get_energy_rate_usd_per_kwh()
    if energy_rate <= 0:
        return 0.0

    semi_energy_cost_per_kg = kwh_per_kg * energy_rate
    return semi_energy_cost_per_kg


def _normalize_width_for_semi(width_mm: float | None) -> float:
    """
    يطبّق شرط الـ width للسيمي:
    - لو width فاضي أو <= 0 → 500 مم
    - لو width > 390 → 500 مم
    - لو width <= 390 → 450 مم
    """
    if not width_mm or width_mm <= 0:
        return 500.0
    if width_mm > 390:
        return 500.0
    # width <= 390
    return 450.0


def get_roll_bom_id_for_semi(product_id: int) -> Optional[int]:
    """
    يرجّع roll_bom_id المرتبط بسيمي منتج معيّن من product_semis.
    """
    with get_db() as cur:
        cur.execute(
            """
            SELECT roll_bom_id
            FROM product_semis
            WHERE product_id = %s
            """,
            (product_id,),
        )
        row = cur.fetchone()
        if not row:
            return None

        roll_bom_id = row[0]
        if not roll_bom_id:
            return None

        return int(roll_bom_id)


def get_semi_material_cost_per_kg(product_id: int) -> float:
    """
    يحسب تكلفة المواد للسيمي لكل كجم من الـ Roll BOM المرتبط بالسيمي:
    - يجيب roll_bom_id من product_semis
    - يجمع نسب المواد × landed_price_per_kg لكل مادة
    - يطبق bom_scrap_percent من جدول products
    """
    roll_bom_id = get_roll_bom_id_for_semi(product_id)
    if not roll_bom_id:
        return 0.0

    with get_db() as cur:
        # 1) نقرأ BOM scrap percent من المنتج الريفرنس اللي مرتبط بالـ roll_bom
        cur.execute(
            """
            SELECT p.bom_scrap_percent
            FROM product_roll_boms prb
            JOIN products p ON p.id = prb.product_id
            WHERE prb.id = %s
            """,
            (roll_bom_id,),
        )
        row_p = cur.fetchone()
        bom_scrap_percent = float(row_p[0] or 0) if row_p else 0.0

        # 2) نجيب items بتاعة الـ Roll BOM
        cur.execute(
            """
            SELECT
                pri.material_id,   -- 0
                pri.percentage     -- 1 (stored as 0.xx)
            FROM product_roll_bom_items pri
            WHERE pri.roll_bom_id = %s
            """,
            (roll_bom_id,),
        )
        items = cur.fetchall()

    if not items:
        return 0.0

    # نحضّر IDs للـ landed cost bulk
    material_ids = [int(row[0]) for row in items if row[0]]
    if not material_ids:
        return 0.0

    # نجيب landed price per kg لكل مادة (من الدالة اللي عندك)
    landed_map = get_materials_landed_price_per_kg_bulk(material_ids)

    base_cost_per_kg = 0.0
    for material_id, pct in items:
        pct_val = float(pct or 0)      # نسبة 0.xx
        price_per_kg = float(landed_map.get(int(material_id), 0.0))
        base_cost_per_kg += pct_val * price_per_kg

    # نطبّق scrap factor زي _load_bom_tab
    eff_factor = 1 + (bom_scrap_percent / 100.0)
    total_cost_per_kg = base_cost_per_kg * eff_factor

    return total_cost_per_kg

def _get_semi_cost_breakdown(product_id: int) -> tuple[
    float,  # material_cost_per_kg
    float,  # energy_cost_per_kg
    float,  # packing_cost_per_kg
    float,  # core_cost_per_kg
    float,  # fixed_oh_per_kg
    float,  # variable_oh_per_kg
]:
    material_cost = get_semi_material_cost_per_kg(product_id)
    energy_cost = get_semi_energy_cost_per_kg(product_id)
    packing_cost = get_semi_packing_cost_per_kg(product_id)
    fixed_oh, variable_oh = get_machine_overheads_per_kg_for_semi(product_id)

    # نفس لوجيك الكور اللي عندك في get_semi_total_cost_per_kg
    core_cost_per_kg = 0.0
    with get_db() as cur:
        cur.execute(
            """
            SELECT gross_kg_per_roll, core_kg_per_roll
            FROM product_semis
            WHERE product_id = %s
            """,
            (product_id,),
        )
        row = cur.fetchone()

    if row:
        gross_kg_per_roll = float(row[0] or 0)
        core_kg_per_roll = float(row[1] or 0)

        if (
            gross_kg_per_roll > 0
            and core_kg_per_roll > 0
            and core_kg_per_roll < gross_kg_per_roll
        ):
            net_kg_per_roll = gross_kg_per_roll - core_kg_per_roll
            core_price_per_kg = _get_core_price_per_kg_for_semi_usd()
            if core_price_per_kg > 0 and net_kg_per_roll > 0:
                core_cost_per_roll = core_price_per_kg * core_kg_per_roll
                core_cost_per_kg = core_cost_per_roll / net_kg_per_roll

    return (
        material_cost,
        energy_cost,
        packing_cost,
        core_cost_per_kg,
        fixed_oh,
        variable_oh,
    )

def get_semi_total_cost_per_kg(product_id: int) -> float:
    """
    total semi cost per kg = materials + energy + packing + core + fixed_OH + variable_OH
    """
    (
        material_cost,
        energy_cost,
        packing_cost,
        core_cost_per_kg,
        fixed_oh,
        variable_oh,
    ) = _get_semi_cost_breakdown(product_id)

    return (
        material_cost
        + energy_cost
        + packing_cost
        + core_cost_per_kg
        + fixed_oh
        + variable_oh
    )


def get_semi_total_cost_per_kg_with_width(
    product_id: int,
    width_mm: float | None,
) -> float:
    """
    يحسب total semi cost per kg مع تأثير الـ width (لمنتجات البريسترتش فقط):
    - المواد + الباكنج + الكور: بدون أي تغيير.
    - الطاقة + الـ machine OH (fixed + variable) يتم تعديلهم بعامل width:
        base_width = 500 مم
        width_effective = 500 أو 450 حسب _normalize_width_for_semi
        factor = base_width / width_effective
    - عند width_effective = 500 → نفس نتيجة get_semi_total_cost_per_kg.
    """
    (
        material_cost,
        energy_cost_base,
        packing_cost,
        core_cost_per_kg,
        fixed_oh_base,
        variable_oh_base,
    ) = _get_semi_cost_breakdown(product_id)

    # لو كل حاجة صفر، نرجّع 0 حماية
    if (
        material_cost <= 0
        and energy_cost_base <= 0
        and packing_cost <= 0
        and core_cost_per_kg <= 0
        and fixed_oh_base <= 0
        and variable_oh_base <= 0
    ):
        return 0.0

    width_effective = _normalize_width_for_semi(width_mm)  # 500 أو 450 حسب شرطك
    base_width = 500.0
    factor = base_width / width_effective if width_effective > 0 else 1.0

    # نعدّل الطاقة + الـ OH فقط
    energy_cost = energy_cost_base * factor
    fixed_oh = fixed_oh_base * factor
    variable_oh = variable_oh_base * factor

    total_cost_per_kg = (
        material_cost
        + packing_cost
        + core_cost_per_kg
        + energy_cost
        + fixed_oh
        + variable_oh
    )
    return total_cost_per_kg


def get_semi_price_net_per_kg_with_width(
    product_id: int,
    width_mm: float | None,
) -> float:
    """
    سعر السيمي net/kg مع تأثير عرض الكوتيشن (للبريسترتش):
    - C_n_base  = total semi cost per kg (بدون عرض).
    - P_n_base  = get_semi_price_net_per_kg (بدون عرض).
    - نحسب margin_ratio = (P_n_base - C_n_base) / C_n_base.
    - C_n_width = get_semi_total_cost_per_kg_with_width.
    - P_n_width = C_n_width * (1 + margin_ratio).
    بالتالي:
    - عند width_effective = 500 → P_n_width = P_n_base بالملّي.
    - عند 450 → تزيد التكلفة والسعر بنفس منطق الطاقة + الـ OH.
    """
    # تكلفة وسعر السيمي بدون عرض
    C_n_base = get_semi_total_cost_per_kg(product_id)
    P_n_base = get_semi_price_net_per_kg(product_id)

    if C_n_base <= 0:
        return 0.0

    # نسبة المارجن الحقيقية المستخدمة للسيمي
    margin_ratio = 0.0
    if P_n_base > 0:
        margin_ratio = (P_n_base - C_n_base) / C_n_base

    # تكلفة السيمي مع العرض (500 أو 450 حسب الشرط)
    C_n_width = get_semi_total_cost_per_kg_with_width(product_id, width_mm)
    if C_n_width <= 0:
        return 0.0

    # نطبّق نفس نسبة المارجن
    P_n_width = C_n_width * (1.0 + margin_ratio)
    return P_n_width

def get_semi_price_net_per_kg(product_id: int) -> float:
    """
    يحسب سعر السيمي لكل كجم *صافي*:
    - C_n = total semi cost per kg net (من get_semi_total_cost_per_kg)
    - نقرأ gross/net + pricing_rule_id من product_semis
    - نحسب cost per kg gross
    - نطبّق هامش الربح على الجروس
    - نعيد توزيع الهامش على النت ونضيفه على C_n
    """
    C_n = get_semi_total_cost_per_kg(product_id)
    if C_n <= 0:
        return 0.0

    with get_db() as cur:
        cur.execute(
            """
            SELECT
                gross_kg_per_roll,
                core_kg_per_roll,
                pricing_rule_id
            FROM product_semis
            WHERE product_id = %s
            """,
            (product_id,),
        )
        row = cur.fetchone()

    if not row:
        return C_n

    gross_kg_per_roll = float(row[0] or 0)
    core_kg_per_roll = float(row[1] or 0)
    pricing_rule_id = row[2]

    if gross_kg_per_roll <= 0 or core_kg_per_roll < 0 or core_kg_per_roll >= gross_kg_per_roll:
        return C_n

    net_kg_per_roll = gross_kg_per_roll - core_kg_per_roll

    # نجيب margin_percent من جدول pricing_rules باستخدام pricing_rule_id
    margin_percent = 0.0
    if pricing_rule_id:
        with get_db() as cur:
            cur.execute(
                "SELECT margin_percent FROM pricing_rules WHERE id = %s",
                (pricing_rule_id,),
            )
            row_rule = cur.fetchone()
            if row_rule:
                margin_percent = float(row_rule[0] or 0)

    m = margin_percent / 100.0

    # 1) cost per kg gross من cost per kg net
    C_g = C_n * (net_kg_per_roll / gross_kg_per_roll)

    # 2) هامش الربح per kg gross
    margin_per_kg_gross = m * C_g

    # 3) إعادة توزيع الهامش على الكيلو الصافي
    margin_per_kg_net = margin_per_kg_gross * (gross_kg_per_roll / net_kg_per_roll)

    # 4) السعر النهائي للصافي
    P_n = C_n + margin_per_kg_net
    return P_n


def get_roll_bom_cost_per_kg_with_semi(roll_bom_id: int) -> float:
    """
    يحسب تكلفة الـ Roll BOM لكل كجم (net) مع دعم السيمي:
    - لو السطر فيه semi_product_id → يستخدم get_semi_price_net_per_kg للسيمي.
    - لو السطر خام عادي → يستخدم get_material_landed_price_per_kg للماتريال.
    - يطبّق bom_scrap_percent من جدول products المرتبط بالـ roll_bom.
    """
    if not roll_bom_id:
        return 0.0

    with get_db() as cur:
        cur.execute(
            """
            SELECT prb.product_id, p.bom_scrap_percent
            FROM product_roll_boms prb
            JOIN products p ON p.id = prb.product_id
            WHERE prb.id = %s
            """,
            (roll_bom_id,),
        )
        row_prod = cur.fetchone()
        if not row_prod:
            return 0.0

        product_id, bom_scrap_percent = row_prod
        bom_scrap_percent = float(bom_scrap_percent or 0)

        cur.execute(
            """
            SELECT
                pri.material_id,
                pri.semi_product_id,
                pri.percentage
            FROM product_roll_bom_items pri
            WHERE pri.roll_bom_id = %s
            """,
            (roll_bom_id,),
        )
        items = cur.fetchall()

    if not items:
        return 0.0

    material_ids = [int(row[0]) for row in items if row[0] is not None]
    landed_map: dict[int, float] = {}
    if material_ids:
        landed_map = get_materials_landed_price_per_kg_bulk(material_ids)

    base_cost_per_kg = 0.0

    for material_id, semi_product_id, pct in items:
        pct_val = float(pct or 0)

        if semi_product_id:
            price_per_kg = get_semi_price_net_per_kg(int(semi_product_id))
        else:
            price_per_kg = float(landed_map.get(int(material_id), 0.0))

        base_cost_per_kg += pct_val * price_per_kg

    eff_factor = 1 + (bom_scrap_percent / 100.0)
    total_cost_per_kg = base_cost_per_kg * eff_factor
    return total_cost_per_kg


def get_semi_packing_cost_per_kg(product_id: int) -> float:
    """
    يحسب تكلفة الباكنج للسيمي لكل كجم *صافي*:
    - يقرأ gross_kg_per_roll, core_kg_per_roll, rolls_per_pallet, packing_profile_id من product_semis
    - يحسب إجمالي تكلفة البالتة للبروفايل من packing_items + materials.price_per_unit
    - يقسم تكلفة البالتة على إجمالي الكيلو جرام الصافي في البالتة (net)
    """
    with get_db() as cur:
        # 1) بيانات السيمي
        cur.execute(
            """
            SELECT gross_kg_per_roll, core_kg_per_roll, rolls_per_pallet, packing_profile_id
            FROM product_semis
            WHERE product_id = %s
            """,
            (product_id,),
        )
        row = cur.fetchone()
        if not row:
            return 0.0

        gross_kg_per_roll = float(row[0] or 0)
        core_kg_per_roll = float(row[1] or 0)
        rolls_per_pallet = float(row[2] or 0)
        packing_profile_id = int(row[3] or 0)

        if (
            gross_kg_per_roll <= 0
            or rolls_per_pallet <= 0
            or packing_profile_id <= 0
            or core_kg_per_roll < 0
            or core_kg_per_roll >= gross_kg_per_roll
        ):
            return 0.0

        net_kg_per_roll = gross_kg_per_roll - core_kg_per_roll
        net_kg_per_pallet = net_kg_per_roll * rolls_per_pallet

        # 2) إجمالي تكلفة البالتة للبروفايل ده
        cur.execute(
            """
            SELECT
                pi.quantity_per_pallet,
                m.price_per_unit,
                m.currency
            FROM packing_items pi
            JOIN materials m ON m.id = pi.material_id
            WHERE pi.packing_profile_id = %s
            """,
            (packing_profile_id,),
        )
        items = cur.fetchall()
        
        # آخر FX
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

    if not items or net_kg_per_pallet <= 0:
        return 0.0

    total_pallet_cost_usd = 0.0
    for qty, price, currency in items:
        q = float(qty or 0)
        p = float(price or 0)
        curr = (currency or "USD").strip().upper()

        if curr == "USD":
            total_pallet_cost_usd += q * p
        else:
            # نفترض EGP → USD
            if egp_per_usd > 0:
                total_pallet_cost_usd += q * (p / egp_per_usd)

    # تكلفة الباكنج لكل كجم *صافي* بالدولار
    return total_pallet_cost_usd / net_kg_per_pallet


def get_machine_overheads_per_kg_for_semi(product_id: int) -> tuple[float, float]:
    """
    يرجّع (fixed_oh_per_kg_usd, variable_oh_per_kg_usd) للسيمي:
    - يجيب ref_product_id
    - يحدد المكنة (preferred أو أول واحدة) ويقرأ monthly_capacity_kg + utilization_rate
    - يقرأ machine_costs للمكنة (fixed_monthly, variable_per_kg) بالـ EGP
    - يحول لـ USD باستخدام آخر currency_rates
    - يوزع الفكسد على actual_capacity_kg = monthly_capacity_kg * utilization_rate
    """
    ref_product_id = get_ref_product_id_for_semi(product_id)
    if not ref_product_id:
        return 0.0, 0.0

    with get_db() as cur:
        # نجيب المكنة المفضلة + utilization من machines
        cur.execute(
            """
            SELECT
                pm.machine_id,
                pm.monthly_product_capacity_kg,
                m.utilization_rate
            FROM product_machines pm
            JOIN machines m ON m.id = pm.machine_id
            WHERE pm.product_id = %s
              AND pm.preferred_machine = TRUE
            ORDER BY pm.id
            LIMIT 1
            """,
            (ref_product_id,),
        )
        row = cur.fetchone()

        if not row:
            # fallback: أول مكنة لأي utilization
            cur.execute(
                """
                SELECT
                    pm.machine_id,
                    pm.monthly_product_capacity_kg,
                    m.utilization_rate
                FROM product_machines pm
                JOIN machines m ON m.id = pm.machine_id
                WHERE pm.product_id = %s
                ORDER BY pm.id
                LIMIT 1
                """,
                (ref_product_id,),
            )
            row = cur.fetchone()

        if not row:
            return 0.0, 0.0

        machine_id = int(row[0])
        monthly_capacity_kg = float(row[1] or 0)
        utilization_rate = float(row[2] or 1.0)

        if utilization_rate > 1:
            utilization_rate = utilization_rate / 100.0

        if monthly_capacity_kg <= 0 or utilization_rate <= 0:
            return 0.0, 0.0

        actual_capacity_kg = monthly_capacity_kg * utilization_rate

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
        row_fx = cur.fetchone()
        if not row_fx:
            return 0.0, 0.0

        egp_per_usd = float(row_fx[0] or 0)
        if egp_per_usd <= 0:
            return 0.0, 0.0

        # machine_costs للمكنة
        cur.execute(
            """
            SELECT cost_type, amount_egp
            FROM machine_costs
            WHERE machine_id = %s
            """,
            (machine_id,),
        )
        rows_costs = cur.fetchall()

    if not rows_costs:
        return 0.0, 0.0

    fixed_monthly_egp = 0.0
    variable_per_kg_egp = 0.0

    for cost_type, amount_egp in rows_costs:
        amt = float(amount_egp or 0)
        if cost_type == "fixed_monthly":
            fixed_monthly_egp += amt
        elif cost_type == "variable_per_kg":
            variable_per_kg_egp += amt

    fixed_monthly_usd = fixed_monthly_egp / egp_per_usd if egp_per_usd > 0 else 0.0
    variable_per_kg_usd = variable_per_kg_egp / egp_per_usd if egp_per_usd > 0 else 0.0

    fixed_per_kg_usd = (
        fixed_monthly_usd / actual_capacity_kg if actual_capacity_kg > 0 else 0.0
    )

    return fixed_per_kg_usd, variable_per_kg_usd


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

            if film_type == "Prestretch":
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
    
