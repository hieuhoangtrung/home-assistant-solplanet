"""Regression tests for transient battery readings."""

from types import SimpleNamespace

import pytest

from custom_components.solplanet.validation import (
    is_zero_filled_battery_payload,
    retain_previous_battery_energy_values,
)


def battery_data(**values):
    """Return representative battery data with optional overrides."""
    return SimpleNamespace(**dict(cst=1, bst=2, vb=5000, tb=250, soc=50, soh=100, cli=10, clo=10, **values))


def test_zero_filled_payload():
    """Only the complete zero stub is rejected."""
    data = SimpleNamespace(cst=0, bst=0, vb=0, tb=0, soc=0, soh=0, cli=0, clo=0)
    assert is_zero_filled_battery_payload(data)
    data.soh = 100
    assert not is_zero_filled_battery_payload(data)


@pytest.mark.parametrize("data", [None, SimpleNamespace(), SimpleNamespace(soc=0)])
def test_incomplete_payload_is_not_zero_stub(data):
    assert not is_zero_filled_battery_payload(data)


def test_individual_zero_values_are_valid():
    data = SimpleNamespace(cst=1, bst=0, vb=5000, tb=0, soc=0, soh=100, cli=0, clo=0, pb=0)
    assert not is_zero_filled_battery_payload(data)


def test_transient_energy_regression_then_recovery():
    previous = battery_data(etdpv=100, ebi=20, ebo=30)
    current = battery_data(etdpv=99, ebi=0, ebo=31)
    candidates = {}
    assert retain_previous_battery_energy_values(current, previous, candidates) == (("etdpv", "ebi"), ())
    assert (current.etdpv, current.ebi, current.ebo) == (100, 20, 31)
    recovered = battery_data(etdpv=101, ebi=21, ebo=32)
    assert retain_previous_battery_energy_values(recovered, current, candidates) == ((), ())
    assert candidates == {}
    assert (previous.etdpv, previous.ebi, previous.ebo) == (100, 20, 30)


@pytest.mark.parametrize("readings", [(0, 0, 0), (0, 1, 2)])
def test_daily_reset_needs_two_later_confirmations(readings):
    previous = battery_data(ebi=20)
    candidates = {}
    for index, value in enumerate(readings):
        current = battery_data(ebi=value)
        retained, confirmed = retain_previous_battery_energy_values(current, previous, candidates)
        assert current.ebi == (20 if index < 2 else value)
        assert retained == (("ebi",) if index < 2 else ())
        assert confirmed == (("ebi",) if index == 2 else ())
        previous = current
    assert candidates == {}


def test_further_regression_restarts_confirmation():
    previous = battery_data(ebi=20)
    candidates = {}
    for value in (5, 6, 1, 2):
        current = battery_data(ebi=value)
        assert retain_previous_battery_energy_values(current, previous, candidates) == (("ebi",), ())
        assert current.ebi == 20
    current = battery_data(ebi=3)
    assert retain_previous_battery_energy_values(current, previous, candidates) == ((), ("ebi",))
    assert current.ebi == 3


@pytest.mark.parametrize("value", [None, "unknown"])
def test_missing_or_nonnumeric_energy_clears_candidate(value):
    candidates = {"ebi": (0, 1)}
    current = battery_data(ebi=value)
    assert retain_previous_battery_energy_values(current, battery_data(ebi=20), candidates) == ((), ())
    assert current.ebi == value
    assert candidates == {}


def test_first_reading_and_increasing_counters_pass_through():
    current = battery_data(etdpv=1, ebi=2, ebo=3)
    assert retain_previous_battery_energy_values(current, None, {}) == ((), ())
    assert retain_previous_battery_energy_values(current, battery_data(etdpv=0, ebi=1, ebo=2), {}) == ((), ())
