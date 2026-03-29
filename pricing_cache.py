"""
Centralized pricing cache module.

- In-memory, process-local cache.
- Explicit invalidate_* APIs to be called from views/services
  whenever underlying DB data changes.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple
import time

# ---------
# Settings cache (rules, extras, energy, packing, payment terms, ...).
# ---------

# مثال: نخزن كل الإعدادات في dict واحدة كبيرة
_SETTINGS_CACHE: Dict[str, Any] = {}
_SETTINGS_CACHE_VERSION: int = 0  # ممكن تستخدمه لو حبيت قدام في logging/debug


def get_settings_cache() -> Dict[str, Any]:
    """Return current settings cache dict (read-only usage in code)."""
    return _SETTINGS_CACHE


def update_settings_cache(new_data: Dict[str, Any]) -> None:
    """
    Replace settings cache content atomically with new_data.
    Expected to be called from a loader function after reading from DB.
    """
    global _SETTINGS_CACHE, _SETTINGS_CACHE_VERSION
    _SETTINGS_CACHE = dict(new_data or {})
    _SETTINGS_CACHE_VERSION += 1


def invalidate_settings_cache() -> None:
    """
    Clear all pricing settings cache.
    Call this after any UPDATE/INSERT/DELETE on:
    - pricing_rules
    - pricing_extras
    - energy_rates
    - currency_rates
    - packing_settings / shipping_settings / costing_settings ...
    """
    global _SETTINGS_CACHE, _SETTINGS_CACHE_VERSION
    _SETTINGS_CACHE.clear()
    _SETTINGS_CACHE_VERSION += 1


# ---------
# Materials landed price cache.
# ---------

# material_id -> (landed_price_per_kg, timestamp)
_MATERIAL_LANDED_CACHE: Dict[int, Tuple[float, float]] = {}
# TTL بالثواني (مثال: 10 دقايق). تقدر تخليها 0 لو عايز تعتمد فقط على ال invalidation اليدوي.
MATERIAL_LANDED_TTL_SECONDS: int = 600


def get_cached_material_landed_price(material_id: int) -> float | None:
    """
    Get cached landed price per kg for a material if valid, else None.
    """
    if material_id <= 0:
        return None

    entry = _MATERIAL_LANDED_CACHE.get(material_id)
    if not entry:
        return None

    value, ts = entry
    if MATERIAL_LANDED_TTL_SECONDS > 0:
        now = time.time()
        if now - ts > MATERIAL_LANDED_TTL_SECONDS:
            # expired
            _MATERIAL_LANDED_CACHE.pop(material_id, None)
            return None

    return value


def set_cached_material_landed_price(material_id: int, value: float) -> None:
    """
    Set / refresh cached landed price for a single material.
    """
    if material_id <= 0:
        return
    _MATERIAL_LANDED_CACHE[material_id] = (float(value or 0.0), time.time())


def get_cached_materials_landed_bulk(material_ids: list[int]) -> Dict[int, float]:
    """
    Return dict of material_id -> cached_value for valid cache entries.
    Non-cached or expired ids are simply not included.
    """
    now = time.time()
    result: Dict[int, float] = {}

    if not material_ids:
        return result

    for mid in material_ids:
        if mid <= 0:
            continue
        entry = _MATERIAL_LANDED_CACHE.get(mid)
        if not entry:
            continue
        value, ts = entry
        if MATERIAL_LANDED_TTL_SECONDS > 0 and now - ts > MATERIAL_LANDED_TTL_SECONDS:
            # expired
            _MATERIAL_LANDED_CACHE.pop(mid, None)
            continue
        result[mid] = value

    return result


def set_cached_materials_landed_bulk(data: Dict[int, float]) -> None:
    """
    Bulk set landed prices for multiple materials.
    """
    if not data:
        return
    now = time.time()
    for mid, val in data.items():
        if mid <= 0:
            continue
        _MATERIAL_LANDED_CACHE[mid] = (float(val or 0.0), now)


def invalidate_material(material_id: int) -> None:
    """
    Invalidate cache for a single material.
    Call after updating/deleting that material or its import_cost_profiles material-scope rows.
    """
    if material_id <= 0:
        return
    _MATERIAL_LANDED_CACHE.pop(material_id, None)


def invalidate_all_materials() -> None:
    """
    Invalidate all materials landed price cache.
    Call after:
    - Updating any global import_cost_profiles row (scope='global')
    - Bulk update on materials
    - Any operation where it's safer to drop all material cached prices.
    """
    _MATERIAL_LANDED_CACHE.clear()


# ---------
# Helper to clear everything at once (e.g. from a Sync endpoint).
# ---------

def invalidate_all_pricing_caches() -> None:
    """
    Clear all pricing-related caches (settings + materials).
    Useful for a global Sync / admin maintenance.
    """
    invalidate_settings_cache()
    invalidate_all_materials()