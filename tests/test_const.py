"""Regression tests for device model labels."""

from custom_components.solplanet.const import METER_MODEL_NAMES


def test_meter_model_two_is_single_phase_sdm230():
    assert METER_MODEL_NAMES[2] == "EASTRON SDM230-Modbus V1"
