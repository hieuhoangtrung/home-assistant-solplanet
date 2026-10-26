"""Regression tests for schedule service cache isolation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol

from custom_components.solplanet.client import ScheduleSlot
from custom_components.solplanet.const import BATTERY_IDENTIFIER, DOMAIN
from custom_components.solplanet.services import async_setup_services


@pytest.mark.asyncio
@pytest.mark.parametrize("start_hour,duration,error", [(1, 1, "overlaps"), (23, 2, "midnight")])
async def test_rejected_slot_does_not_mutate_cached_schedule(hass, start_hour, duration, error):
    await async_setup_services(hass)
    slots = {"Mon": [ScheduleSlot.from_time("01:00", 2, "charge")]}
    coordinator = SimpleNamespace(
        data={BATTERY_IDENTIFIER: {"battery": {"schedule": {"slots": slots}}}},
        set_battery_schedule_slots=AsyncMock(),
    )
    hass.data[DOMAIN] = {"entry": {"coordinator": coordinator}}
    with (
        patch(
            "custom_components.solplanet.services.get_isn_from_target", AsyncMock(return_value=["battery"])
        ),
        pytest.raises(vol.Invalid, match=error),
    ):
        await hass.services.async_call(
            DOMAIN,
            "set_schedule_slots",
            {
                "device_id": "device",
                "day": "Mon",
                "start_hour": start_hour,
                "start_minute": 0,
                "duration": duration,
                "mode": "charge",
            },
            blocking=True,
        )
    assert slots == {"Mon": [ScheduleSlot.from_time("01:00", 2, "charge")]}
    coordinator.set_battery_schedule_slots.assert_not_awaited()


@pytest.mark.asyncio
async def test_valid_slot_sends_new_list_without_changing_cache(hass):
    await async_setup_services(hass)
    monday = [ScheduleSlot.from_time("01:00", 2, "charge")]
    tuesday = [ScheduleSlot.from_time("05:00", 1, "discharge")]
    slots = {"Mon": monday, "Tus": tuesday}
    coordinator = SimpleNamespace(
        data={BATTERY_IDENTIFIER: {"battery": {"schedule": {"slots": slots}}}},
        set_battery_schedule_slots=AsyncMock(),
    )
    hass.data[DOMAIN] = {"entry": {"coordinator": coordinator}}
    with patch(
        "custom_components.solplanet.services.get_isn_from_target", AsyncMock(return_value=["battery"])
    ):
        await hass.services.async_call(
            DOMAIN,
            "set_schedule_slots",
            {
                "device_id": "device",
                "day": "Mon",
                "start_hour": 4,
                "start_minute": 30,
                "duration": 1,
                "mode": "discharge",
            },
            blocking=True,
        )
    sent = coordinator.set_battery_schedule_slots.await_args.args
    assert sent == (
        "battery",
        {"Mon": [monday[0], ScheduleSlot.from_time("04:30", 1, "discharge")], "Tus": tuesday},
    )
    assert sent[1]["Mon"] is not monday
    assert slots == {"Mon": [ScheduleSlot.from_time("01:00", 2, "charge")], "Tus": tuesday}


@pytest.mark.asyncio
async def test_failed_schedule_write_does_not_mutate_cache(hass):
    await async_setup_services(hass)
    slots = {"Mon": [ScheduleSlot.from_time("01:00", 2, "charge")]}
    coordinator = SimpleNamespace(
        data={BATTERY_IDENTIFIER: {"battery": {"schedule": {"slots": slots}}}},
        set_battery_schedule_slots=AsyncMock(side_effect=TimeoutError("offline")),
    )
    hass.data[DOMAIN] = {"entry": {"coordinator": coordinator}}
    with (
        patch(
            "custom_components.solplanet.services.get_isn_from_target", AsyncMock(return_value=["battery"])
        ),
        pytest.raises(vol.Invalid, match="Communication error"),
    ):
        await hass.services.async_call(
            DOMAIN,
            "set_schedule_slots",
            {
                "device_id": "device",
                "day": "Mon",
                "start_hour": 4,
                "start_minute": 0,
                "duration": 1,
                "mode": "charge",
            },
            blocking=True,
        )
    assert slots == {"Mon": [ScheduleSlot.from_time("01:00", 2, "charge")]}
