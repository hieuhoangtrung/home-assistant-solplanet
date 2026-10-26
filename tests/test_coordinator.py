"""Regression tests for battery polling with the existing panel data shape."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.solplanet.const import (
    BATTERY_IDENTIFIER,
    DONGLE_IDENTIFIER,
    INVERTER_IDENTIFIER,
    METER_IDENTIFIER,
)
from custom_components.solplanet.coordinator import SolplanetDataUpdateCoordinator


def battery_data(energy=20, **overrides):
    values = {"cst": 1, "bst": 2, "vb": 5000, "tb": 250, "soc": 50, "soh": 100, "cli": 10, "clo": 10}
    values.update(etdpv=energy, ebi=energy, ebo=energy, **overrides)
    return SimpleNamespace(**values)


def api_for_batteries(*isns):
    inverters = [SimpleNamespace(isn=isn, isStorage=Mock(return_value=True)) for isn in isns]
    return SimpleNamespace(
        version="v2",
        client=SimpleNamespace(
            get=AsyncMock(return_value={"psn": "dongle"}),
            post=AsyncMock(return_value={"status": 404}),
        ),
        get_inverter_info=AsyncMock(return_value=SimpleNamespace(inv=inverters)),
        get_inverter_data=AsyncMock(return_value=SimpleNamespace(pac=100)),
        modbus_read_holding_registers=AsyncMock(return_value=[1]),
        get_schedule=AsyncMock(return_value={"slots": {}}),
        get_battery_data=AsyncMock(),
        get_battery_info=AsyncMock(return_value=SimpleNamespace(type=1, mod_r=2)),
        get_meter_data=AsyncMock(return_value=None),
        get_meter_info=AsyncMock(return_value=SimpleNamespace(sn=None)),
    )


async def poll(coordinator):
    coordinator.data = await coordinator._async_update_data()
    return coordinator.data[BATTERY_IDENTIFIER]


@pytest.mark.asyncio
async def test_zero_stub_retains_previous_data_and_panel_structure(hass):
    api = api_for_batteries("battery")
    good = battery_data()
    zero = battery_data(0, cst=0, bst=0, vb=0, tb=0, soc=0, soh=0, cli=0, clo=0)
    api.get_battery_data.side_effect = [good, zero, battery_data(21, soc=0)]
    coordinator = SolplanetDataUpdateCoordinator(hass, api, "entry", 30)
    await poll(coordinator)
    assert (await poll(coordinator))["battery"]["data"] is good
    assert set(coordinator.data) == {
        BATTERY_IDENTIFIER,
        DONGLE_IDENTIFIER,
        INVERTER_IDENTIFIER,
        METER_IDENTIFIER,
    }
    assert set(coordinator.data[BATTERY_IDENTIFIER]["battery"]) == {
        "data",
        "info",
        "work_modes",
        "schedule",
        "more_settings",
    }
    assert (await poll(coordinator))["battery"]["data"].soc == 0


@pytest.mark.asyncio
async def test_first_zero_stub_does_not_publish_zero_counters(hass):
    api = api_for_batteries("battery")
    api.get_battery_data.return_value = battery_data(0, cst=0, bst=0, vb=0, tb=0, soc=0, soh=0, cli=0, clo=0)
    coordinator = SolplanetDataUpdateCoordinator(hass, api, "entry", 30)
    assert (await poll(coordinator))["battery"]["data"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["zero", "data_error", "info_error"])
async def test_invalid_or_failed_poll_does_not_confirm_energy_reset(hass, interruption):
    api = api_for_batteries("battery")
    good = battery_data()
    api.get_battery_data.side_effect = [good, battery_data(0)]
    coordinator = SolplanetDataUpdateCoordinator(hass, api, "entry", 30)
    await poll(coordinator)
    assert (await poll(coordinator))["battery"]["data"].ebi == 20
    api.get_battery_data.side_effect = None
    api.get_battery_data.return_value = battery_data(0)
    if interruption == "zero":
        api.get_battery_data.return_value = battery_data(
            0, cst=0, bst=0, vb=0, tb=0, soc=0, soh=0, cli=0, clo=0
        )
    elif interruption == "data_error":
        api.get_battery_data.side_effect = TimeoutError("offline")
    else:
        api.get_battery_info.side_effect = TimeoutError("offline")
    assert (await poll(coordinator))["battery"]["data"].ebi == 20
    api.get_battery_info.side_effect = None
    api.get_battery_data.side_effect = [battery_data(1), battery_data(2), battery_data(3)]
    assert (await poll(coordinator))["battery"]["data"].ebi == 20
    assert (await poll(coordinator))["battery"]["data"].ebi == 2
    assert (await poll(coordinator))["battery"]["data"].ebi == 3
    assert coordinator._battery_energy_regression_candidates == {}
    assert good.ebi == 20


@pytest.mark.asyncio
async def test_energy_reset_candidates_are_per_battery(hass):
    api = api_for_batteries("one", "two")
    api.get_battery_data.side_effect = [
        battery_data(20),
        battery_data(40),
        battery_data(0),
        battery_data(41),
        battery_data(1),
        battery_data(42),
        battery_data(2),
        battery_data(43),
    ]
    coordinator = SolplanetDataUpdateCoordinator(hass, api, "entry", 30)
    await poll(coordinator)
    values = await poll(coordinator)
    assert (values["one"]["data"].ebi, values["two"]["data"].ebi) == (20, 41)
    assert set(coordinator._battery_energy_regression_candidates) == {"one"}
    await poll(coordinator)
    values = await poll(coordinator)
    assert (values["one"]["data"].ebi, values["two"]["data"].ebi) == (2, 43)
    assert coordinator._battery_energy_regression_candidates == {}
