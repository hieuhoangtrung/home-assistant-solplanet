"""Validation helpers for Solplanet device payloads."""

from __future__ import annotations

_BATTERY_ZERO_STUB_FIELDS = ("cst", "bst", "vb", "tb", "soc", "soh", "cli", "clo")
_BATTERY_DAILY_ENERGY_FIELDS = ("etdpv", "ebi", "ebo")
_BATTERY_ENERGY_RESET_CONFIRMATIONS = 2


def is_zero_filled_battery_payload(data: object) -> bool:
    """Identify the dongle's transient zero stub, not individual zero values."""
    return all(getattr(data, field, None) == 0 for field in _BATTERY_ZERO_STUB_FIELDS)


def retain_previous_battery_energy_values(
    data: object,
    previous_data: object,
    candidates: dict[str, tuple[float | int, int]],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Retain counters that fall until later readings confirm a daily reset."""
    retained = []
    confirmed = []
    for field in _BATTERY_DAILY_ENERGY_FIELDS:
        previous_value = getattr(previous_data, field, None)
        current_value = getattr(data, field, None)
        if not isinstance(previous_value, (int, float)) or not isinstance(current_value, (int, float)):
            candidates.pop(field, None)
            continue
        if current_value >= previous_value:
            candidates.pop(field, None)
            continue

        candidate = candidates.get(field)
        if candidate is not None and current_value >= candidate[0]:
            confirmation_count = candidate[1] + 1
            if confirmation_count >= _BATTERY_ENERGY_RESET_CONFIRMATIONS:
                candidates.pop(field)
                confirmed.append(field)
                continue
        else:
            confirmation_count = 0

        candidates[field] = (current_value, confirmation_count)
        setattr(data, field, previous_value)
        retained.append(field)

    return tuple(retained), tuple(confirmed)
