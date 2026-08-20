"""Fever/hypothermia band classification, for report rendering and alerting.

Unlike the AHA blood-pressure categories the etekcity-bp-daemon sibling
uses, there's no single authoritative clinical body defining fever bands
this precisely -- the cutoffs below are a common, widely cited rule of
thumb (CDC/Mayo Clinic style guidance), not a fixed medical standard.
Treat this module the same way as the rest of this project: informational
labeling for a report, not a diagnostic tool.

Every reading is stored in whatever unit the device itself reported (see
storage.py), so classification and any cross-unit comparison always
converts to Celsius first via `celsius()` -- Fahrenheit thresholds would
just be the same cutoffs restated, not a second independent definition.
"""

from __future__ import annotations

HYPOTHERMIA = "Hypothermia"
NORMAL = "Normal"
LOW_GRADE_FEVER = "Low-Grade Fever"
FEVER = "Fever"
HIGH_FEVER = "High Fever"

# Ordered low-to-high; used by report.py to find the "worst" band in a
# rollup period the same way etekcity-bp-daemon ranks AHA categories.
SEVERITY_ORDER = [NORMAL, LOW_GRADE_FEVER, FEVER, HIGH_FEVER, HYPOTHERMIA]


def celsius(value: float, unit: str) -> float:
    """Convert a temperature value to Celsius.

    Args:
        value: The temperature value, in ``unit``.
        unit: "C" or "F" (case-insensitive), matching
            ``health_thermometer_ble.Reading.unit``.

    Returns:
        The value in Celsius. Returned unchanged if ``unit`` is already
        "C".
    """
    if unit.upper() == "F":
        return (value - 32.0) * 5.0 / 9.0
    return value


def fahrenheit(value: float, unit: str) -> float:
    """Convert a temperature value to Fahrenheit.

    Args:
        value: The temperature value, in ``unit``.
        unit: "C" or "F" (case-insensitive).

    Returns:
        The value in Fahrenheit. Returned unchanged if ``unit`` is
        already "F".
    """
    if unit.upper() == "C":
        return value * 9.0 / 5.0 + 32.0
    return value


def convert(value: float, from_unit: str, to_unit: str) -> float:
    """Convert a temperature value between "C" and "F" (case-insensitive).

    Args:
        value: The temperature value, in ``from_unit``.
        from_unit: The unit ``value`` is currently in.
        to_unit: The unit to convert to.

    Returns:
        The converted value, unchanged if the units already match.
    """
    if from_unit.upper() == to_unit.upper():
        return value
    return fahrenheit(value, from_unit) if to_unit.upper() == "F" else celsius(value, from_unit)


def classify(value: float, unit: str) -> str:
    """Classify a temperature reading into a fever/hypothermia band.

    Args:
        value: The temperature value, in ``unit``.
        unit: "C" or "F" (case-insensitive).

    Returns:
        One of HYPOTHERMIA, NORMAL, LOW_GRADE_FEVER, FEVER, or HIGH_FEVER.
    """
    value_c = celsius(value, unit)
    if value_c < 35.0:
        return HYPOTHERMIA
    if value_c < 37.3:
        return NORMAL
    if value_c < 38.1:
        return LOW_GRADE_FEVER
    if value_c < 39.5:
        return FEVER
    return HIGH_FEVER
