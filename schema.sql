-- schema.sql - Stretch Pricing App Database

-- Users & Authentication
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,

    -- الأدوار النهائية
    role VARCHAR(20) NOT NULL DEFAULT 'sales'
        CHECK (role IN ('admin', 'owner', 'sales_manager', 'sales')),

    -- نوع السيلز (اختياري، ويستخدم فقط لما يكون role = 'sales')
    sales_type VARCHAR(30),
    
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE users
    DROP CONSTRAINT IF EXISTS users_sales_type_check;

ALTER TABLE users
    ADD CONSTRAINT users_sales_type_check
    CHECK (
        sales_type IS NULL
        OR sales_type IN ('egyptian_sellers', 'foreign_sellers')
    );

-- Machines
CREATE TABLE IF NOT EXISTS machines (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    utilization_rate DECIMAL(5,2) DEFAULT 0.80, -- default utilization
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Fixed & Variable Costs per Machine
CREATE TABLE IF NOT EXISTS machine_costs (
    id SERIAL PRIMARY KEY,
    machine_id INTEGER REFERENCES machines(id),
    cost_type VARCHAR(20) CHECK (cost_type IN ('fixed_monthly', 'variable_per_kg')),
    amount_egp DECIMAL(12,2) NOT NULL,
    description TEXT
);

-- Products
CREATE TABLE IF NOT EXISTS products (
    id SERIAL PRIMARY KEY,
    code VARCHAR(20) UNIQUE NOT NULL,
    micron INTEGER NOT NULL,
    stretchability_percent INTEGER NOT NULL,
    is_prestretch BOOLEAN DEFAULT false,
    bom_scrap_percent NUMERIC(5,2) NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE products
    ADD COLUMN IF NOT EXISTS is_manual   BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS is_colored  BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS kg_per_roll NUMERIC(10,4) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS film_type   VARCHAR(20) NOT NULL DEFAULT 'standard';

-- pricing extras
CREATE TABLE IF NOT EXISTS pricing_extras (
    id SERIAL PRIMARY KEY,
    color_extra_usd_per_kg DECIMAL(8,4) NOT NULL DEFAULT 0,
    prestretch_extra_usd_per_kg DECIMAL(8,4) NOT NULL DEFAULT 0,
    -- NEW: extra for foreign sellers
    foreign_extra_mode VARCHAR(20) NOT NULL DEFAULT 'percent', -- 'percent' or 'per_kg'
    foreign_extra_value DECIMAL(8,4) NOT NULL DEFAULT 0,
    is_active BOOLEAN DEFAULT true
);

-- Product-Machine mapping + electricity consumption
CREATE TABLE IF NOT EXISTS product_machines (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    machine_id INTEGER NOT NULL REFERENCES machines(id),
    preferred_machine BOOLEAN DEFAULT false,
    kwh_per_kg DECIMAL(6,4) NOT NULL,              -- electricity consumption per kg
    monthly_product_capacity_kg INTEGER NOT NULL,  -- max kg/month for this product on this machine
    UNIQUE(product_id, machine_id)
);


-- Materials master (raw materials, cores, packing, etc.)
CREATE TABLE IF NOT EXISTS materials (
    id SERIAL PRIMARY KEY,
    code VARCHAR(50) NOT NULL UNIQUE,             -- generated in app (e.g. MAT-0001)
    name VARCHAR(255) NOT NULL,
    category VARCHAR(50) NOT NULL,                -- RAW, PACKING, CORE ...
    unit VARCHAR(20) NOT NULL DEFAULT 'kg',       -- Ton / kg / pc / carton ...
    unit_type VARCHAR(20) NOT NULL DEFAULT 'weight', -- weight / count / length ...
    currency VARCHAR(10) NOT NULL DEFAULT 'USD',  -- USD / EGP / ...
    -- ملاحظة: المعنى يعتمد على category + unit_type + currency:
    -- RAW      = Price (USD per kg)   غالبًا weight/USD
    -- PACKING  = Price (EGP per unit) ممكن weight أو count لكن currency EGP
    price_per_unit NUMERIC(12,4) NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);


-- Bill of Materials (BOM) for products
-- رابط بين المنتج والمادة فقط + نسبة واستهلاك/تالف
CREATE TABLE IF NOT EXISTS product_bom (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    material_id INTEGER NOT NULL REFERENCES materials(id),
    percentage NUMERIC(5,4) NOT NULL,           -- نسبة من 1 (0.7 = 70%)
    scrap_percent NUMERIC(5,2) NOT NULL DEFAULT 0, -- نسبة تالف (%)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (product_id, material_id)
);

-- Packing Types (مستوى عام: Automatic / Manual ... تستخدم في quotation_items)
CREATE TABLE IF NOT EXISTS packing_types (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) NOT NULL, -- Automatic / Manual
    description TEXT
);


CREATE TABLE IF NOT EXISTS pallet_types (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) NOT NULL,       -- Standard / Euro / Half ...
    description TEXT
);


-- Packing Items per pallet
-- تعريف الباكنج الفعلي للبالتة: مادة PACKING + كميتها على البالتة
CREATE TABLE IF NOT EXISTS packing_items (
    id SERIAL PRIMARY KEY,
    packing_type_id INTEGER REFERENCES packing_types(id),
    material_id INTEGER REFERENCES materials(id),              -- المادة من جدول materials (CATEGORY = PACKING)
    item_name VARCHAR(100),                                    -- اختياري لو حابب اسم مخصص
    quantity_per_pallet DECIMAL(8,3) NOT NULL                  -- كام وحدة من المادة على البالتة
    -- السعر لا يُخزن هنا، بيُقرأ من materials.price_per_unit
);

ALTER TABLE packing_items
    ADD COLUMN IF NOT EXISTS pallet_type_id INTEGER REFERENCES pallet_types(id);

-- NEW: Packing profiles (global + product-specific)
CREATE TABLE IF NOT EXISTS packing_profiles (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(100) NOT NULL,
    packing_type_id INTEGER NOT NULL REFERENCES packing_types(id),
    pallet_type_id  INTEGER NOT NULL REFERENCES pallet_types(id),
    is_global       BOOLEAN NOT NULL DEFAULT FALSE,  -- TRUE = عام لكل المنتجات
    is_active       BOOLEAN NOT NULL DEFAULT TRUE
);

-- ربط packing_items بالبروفايل (بدل ما تكون عامة فقط)
ALTER TABLE packing_items
    ADD COLUMN IF NOT EXISTS packing_profile_id INTEGER REFERENCES packing_profiles(id);

-- NEW: product-specific packing profile overrides by roll weight
CREATE TABLE IF NOT EXISTS packing_profile_overrides (
    id                 SERIAL PRIMARY KEY,
    packing_profile_id INTEGER NOT NULL REFERENCES packing_profiles(id) ON DELETE CASCADE,
    product_id         INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    roll_weight_min    NUMERIC(10,4) NOT NULL,
    roll_weight_max    NUMERIC(10,4) NOT NULL,
    is_active          BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (packing_profile_id, product_id, roll_weight_min, roll_weight_max)
);


-- Currency Rate (EGP to USD)
CREATE TABLE IF NOT EXISTS currency_rates (
    id SERIAL PRIMARY KEY,
    egp_per_usd DECIMAL(10,4) NOT NULL,
    effective_date DATE NOT NULL,
    is_active BOOLEAN DEFAULT true
);

-- Energy (electricity) rate in EGP per kWh, converted to USD via currency_rates
CREATE TABLE IF NOT EXISTS energy_rates (
    id SERIAL PRIMARY KEY,
    egp_per_kwh NUMERIC(10,6) NOT NULL,
    effective_date DATE NOT NULL DEFAULT CURRENT_DATE,
    is_active BOOLEAN DEFAULT true
);

-- Payment Terms Settings
CREATE TABLE IF NOT EXISTS payment_terms (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) NOT NULL,
    credit_days INTEGER NOT NULL DEFAULT 0,
    is_active BOOLEAN DEFAULT true
);

ALTER TABLE payment_terms
ADD COLUMN IF NOT EXISTS annual_rate_percent NUMERIC(6,2) NOT NULL DEFAULT 0;

-- Ports & Shipping
CREATE TABLE IF NOT EXISTS ports (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL, -- Alexandria, Damietta
    country VARCHAR(50) NOT NULL
);

CREATE TABLE IF NOT EXISTS destinations (
    id SERIAL PRIMARY KEY,
    country VARCHAR(50) NOT NULL,
    city VARCHAR(50)
);

CREATE TABLE IF NOT EXISTS fob_costs (
    id SERIAL PRIMARY KEY,
    port_id INTEGER REFERENCES ports(id),
    fob_cost_usd_per_container DECIMAL(12,2) NOT NULL,
    UNIQUE (port_id)
);

CREATE TABLE IF NOT EXISTS sea_freight_rates (
    id SERIAL PRIMARY KEY,
    loading_port_id INTEGER REFERENCES ports(id),
    destination_id INTEGER REFERENCES destinations(id),
    shipping_rate_usd_per_container DECIMAL(12,2) NOT NULL,
    carrier_name VARCHAR(100),
    UNIQUE (loading_port_id, destination_id)
);

CREATE TABLE IF NOT EXISTS quotations (
    id SERIAL PRIMARY KEY,
    quotation_number VARCHAR(20) UNIQUE NOT NULL,
    customer_name VARCHAR(200),

    -- الكود بيستخدم الحقل ده في INSERT (حالياً بنحط فيه الـ destination_text)
    customer_country VARCHAR(100),

    -- ربط بجدول الموانئ
    port_id INTEGER REFERENCES ports(id),

    -- ربط بجدول الـ destinations (selected_dest_id من الفورم)
    destination_id INTEGER REFERENCES destinations(id),

    -- ربط بجدول شروط الدفع (selected_payment_term_id)
    payment_term_id INTEGER REFERENCES payment_terms(id),

    -- الخصم الجلوبال اللي في شاشة التسعير (discount_percent من الهيدر)
    global_discount_percent DECIMAL(5,2) DEFAULT 0,

    -- سعر الصرف وقت حفظ الكوتيشن (snapshot)
    fx_egp_per_usd NUMERIC(10,4),

    -- SNAPSHOT للحالة الفعلية لشروط الدفع وقت حفظ الكوتيشن
    payment_term_name_snapshot VARCHAR(200),
    payment_term_days_snapshot INTEGER,
    credit_surcharge_percent_snapshot NUMERIC(6,3),

    created_by_user_id INTEGER REFERENCES users(id),

    -- نوع البائع (مصري / أجنبي) وقت الحفظ
    seller_type TEXT,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Quotation Items (Products in quotation)
CREATE TABLE IF NOT EXISTS quotation_items (
    id SERIAL PRIMARY KEY,
    quotation_id INTEGER NOT NULL REFERENCES quotations(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id),

    -- نفس العمود اللي ظاهر في الكود
    price_basis VARCHAR(10) CHECK (price_basis IN ('gross', 'net', 'roll')),

    pallets_per_container INTEGER,
    width_mm INTEGER,
    rolls_per_pallet INTEGER,
    roll_weight_kg DECIMAL(10,3),
    core_weight_kg DECIMAL(10,3),

    discount_percent DECIMAL(5,2) DEFAULT 0,
    is_colored BOOLEAN DEFAULT FALSE,

    pallet_type_id INTEGER REFERENCES pallet_types(id),
    packing_type_id INTEGER REFERENCES packing_types(id),

    exw_price DECIMAL(10,2),
    fob_price DECIMAL(10,2),
    cfr_price DECIMAL(10,2)
);


-- جدول إعدادات تكلفة الاستيراد
CREATE TABLE IF NOT EXISTS import_cost_profiles (
    id          SERIAL PRIMARY KEY,
    material_id INTEGER REFERENCES materials(id) ON DELETE CASCADE,
    scope       VARCHAR(20) NOT NULL CHECK (scope IN ('global', 'material')),
    mode        VARCHAR(20) NOT NULL CHECK (mode IN ('per_ton', 'percent')),
    value       NUMERIC(12,4) NOT NULL
);

-- جدول pricing_rules
CREATE TABLE IF NOT EXISTS pricing_rules (
    id                 SERIAL PRIMARY KEY,
    micron_min         INTEGER NOT NULL,
    micron_max         INTEGER NOT NULL,
    film_type          VARCHAR(20) NOT NULL,  -- standard / regid / uvi_6m / uvi_12m / prestretch
    is_manual          BOOLEAN NOT NULL,      -- True = manual, False = automatic
    roll_weight_min    NUMERIC(10,4) DEFAULT 0,
    roll_weight_max    NUMERIC(10,4) DEFAULT 0,
    margin_percent     NUMERIC(6,2) NOT NULL,
    UNIQUE (micron_min, micron_max, film_type, is_manual, roll_weight_min, roll_weight_max)
);

CREATE TABLE IF NOT EXISTS quotation_item_cost_snapshots (
    id SERIAL PRIMARY KEY,
    quotation_item_id INTEGER NOT NULL REFERENCES quotation_items(id) ON DELETE CASCADE,

    rm_cost_per_kg_net NUMERIC(12,6),
    energy_cost_per_kg_net NUMERIC(12,6),
    machine_oh_per_kg_net NUMERIC(12,6),
    net_kg_per_roll NUMERIC(12,6),
    gross_kg_per_roll NUMERIC(12,6),

    extra_roll NUMERIC(12,6),
    margin_percent NUMERIC(6,2),

    fob_cost_unit_roll NUMERIC(12,6),
    sea_freight_cost_unit_roll NUMERIC(12,6),

    core_cost_unit_roll NUMERIC(12,6),
    packing_cost_unit_roll NUMERIC(12,6),
    packing_core_cost_unit_roll NUMERIC(12,6),

    exw_kg_net NUMERIC(12,6),
    exw_kg_gross NUMERIC(12,6),
    exw_roll NUMERIC(12,6),
    fob_kg_net NUMERIC(12,6),
    fob_kg_gross NUMERIC(12,6),
    fob_roll NUMERIC(12,6),
    cfr_kg_net NUMERIC(12,6),
    cfr_kg_gross NUMERIC(12,6),
    cfr_roll NUMERIC(12,6),

    -- base قبل الراوند أب
    fob_kg_gross_base NUMERIC(12,6),
    cfr_kg_gross_base NUMERIC(12,6),

    exw_kg_net_base NUMERIC(12,6),
    fob_kg_net_base NUMERIC(12,6),
    cfr_kg_net_base NUMERIC(12,6),

    exw_roll_base NUMERIC(12,6),
    fob_roll_base NUMERIC(12,6),
    cfr_roll_base NUMERIC(12,6),

    foreign_extra_mode TEXT,
    foreign_extra_value NUMERIC(12,4),

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pricing_cache_control (
    id           SERIAL PRIMARY KEY,
    cache_version INTEGER NOT NULL DEFAULT 1,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- NEW: BOMs per product per roll weight

CREATE TABLE IF NOT EXISTS product_roll_boms (
    id              SERIAL PRIMARY KEY,
    product_id      INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    label           VARCHAR(100),
    weight_from_kg  NUMERIC(10,4) NOT NULL,
    weight_to_kg    NUMERIC(10,4) NOT NULL,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (product_id, weight_from_kg, weight_to_kg)
);

CREATE TABLE IF NOT EXISTS product_roll_bom_items (
    id            SERIAL PRIMARY KEY,
    roll_bom_id   INTEGER NOT NULL REFERENCES product_roll_boms(id) ON DELETE CASCADE,
    material_id   INTEGER NOT NULL REFERENCES materials(id),
    percentage    NUMERIC(5,4) NOT NULL,
    scrap_percent NUMERIC(5,2) NOT NULL DEFAULT 0,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (roll_bom_id, material_id)
);