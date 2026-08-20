from health_thermometer_daemon.storage import (
    ReadingStore,
    get_distinct_profiles,
    get_reading_recorded_at,
    set_reading_profile,
)


def test_record_and_read_back(tmp_path):
    db_path = str(tmp_path / "readings.db")
    store = ReadingStore(db_path)
    try:
        row_id = store.record(
            recorded_at="2026-01-01T00:00:00+00:00",
            measured_at=None,
            address="AA:BB:CC:DD:EE:FF",
            profile="Alice",
            value=37.0,
            unit="C",
            temperature_type="Ear (usually ear lobe)",
            battery_percent=90,
            error_code=None,
        )
        assert row_id == 1
    finally:
        store.close()


def test_get_distinct_profiles(tmp_path):
    db_path = str(tmp_path / "readings.db")
    store = ReadingStore(db_path)
    try:
        store.record(
            recorded_at="2026-01-01T00:00:00+00:00",
            measured_at=None,
            address="AA:BB:CC:DD:EE:FF",
            profile="Alice",
            value=37.0,
            unit="C",
            temperature_type=None,
            battery_percent=None,
            error_code=None,
        )
        store.record(
            recorded_at="2026-01-01T00:05:00+00:00",
            measured_at=None,
            address="AA:BB:CC:DD:EE:FF",
            profile=None,
            value=36.8,
            unit="C",
            temperature_type=None,
            battery_percent=None,
            error_code=None,
        )
    finally:
        store.close()

    assert get_distinct_profiles(db_path) == {"Alice"}


def test_get_distinct_profiles_no_table(tmp_path):
    db_path = str(tmp_path / "empty.db")
    assert get_distinct_profiles(db_path) == set()


def test_tag_reading_after_the_fact(tmp_path):
    db_path = str(tmp_path / "readings.db")
    store = ReadingStore(db_path)
    try:
        row_id = store.record(
            recorded_at="2026-01-01T00:00:00+00:00",
            measured_at=None,
            address="AA:BB:CC:DD:EE:FF",
            profile=None,
            value=37.0,
            unit="C",
            temperature_type=None,
            battery_percent=None,
            error_code=None,
        )
    finally:
        store.close()

    assert get_reading_recorded_at(db_path, row_id) == "2026-01-01T00:00:00+00:00"
    assert get_reading_recorded_at(db_path, 9999) is None

    assert set_reading_profile(db_path, row_id, "Alice") is True
    assert set_reading_profile(db_path, 9999, "Alice") is False
    assert get_distinct_profiles(db_path) == {"Alice"}
