from health_thermometer_daemon.categories import (
    FEVER,
    HIGH_FEVER,
    HYPOTHERMIA,
    LOW_GRADE_FEVER,
    NORMAL,
    celsius,
    classify,
    convert,
    fahrenheit,
)


def test_celsius_passthrough():
    assert celsius(37.0, "C") == 37.0


def test_celsius_from_fahrenheit():
    assert round(celsius(98.6, "F"), 1) == 37.0


def test_celsius_is_case_insensitive():
    assert round(celsius(98.6, "f"), 1) == 37.0


def test_fahrenheit_passthrough():
    assert fahrenheit(98.6, "F") == 98.6


def test_fahrenheit_from_celsius():
    assert round(fahrenheit(37.0, "C"), 1) == 98.6


def test_convert_same_unit_is_noop():
    assert convert(37.0, "C", "c") == 37.0


def test_convert_c_to_f():
    assert round(convert(0.0, "C", "F"), 1) == 32.0


def test_convert_f_to_c():
    assert round(convert(212.0, "F", "C"), 1) == 100.0


def test_classify_hypothermia():
    assert classify(34.9, "C") == HYPOTHERMIA


def test_classify_normal():
    assert classify(36.5, "C") == NORMAL


def test_classify_low_grade_fever():
    assert classify(37.5, "C") == LOW_GRADE_FEVER


def test_classify_fever():
    assert classify(38.5, "C") == FEVER


def test_classify_high_fever():
    assert classify(39.5, "C") == HIGH_FEVER


def test_classify_respects_unit():
    assert classify(104.0, "F") == HIGH_FEVER  # 40.0 degrees C
