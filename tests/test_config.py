from pathlib import Path

import pytest

from health_thermometer_daemon.config import (
    ConfigError,
    load_alert_config,
    load_api_config,
    load_config,
    load_mqtt_config,
    load_profile_details,
    load_profiles_config,
    load_report_config,
    persist_discovered_address,
)

_BASE_CONFIG = """
[daemon]
address = AA:BB:CC:DD:EE:FF
log_level = INFO
retry_seconds = 5

[storage]
db_path = /tmp/readings.db
"""


def _write(tmp_path: Path, contents: str) -> str:
    path = tmp_path / "config.ini"
    path.write_text(contents)
    return str(path)


def test_load_config_basic(tmp_path):
    config_path = _write(tmp_path, _BASE_CONFIG)
    config = load_config(config_path)
    assert config.address == "AA:BB:CC:DD:EE:FF"
    assert config.retry_seconds == 5
    assert config.db_path == "/tmp/readings.db"
    assert config.log_level == "INFO"


def test_load_config_missing_file():
    with pytest.raises(ConfigError):
        load_config("/nonexistent/config.ini")


def test_load_config_requires_db_path(tmp_path):
    config_path = _write(tmp_path, "[storage]\ndb_path =\n")
    with pytest.raises(ConfigError):
        load_config(config_path)


def test_load_config_invalid_retry_seconds(tmp_path):
    config_path = _write(
        tmp_path, "[storage]\ndb_path = /tmp/x.db\n\n[daemon]\nretry_seconds = 0\n"
    )
    with pytest.raises(ConfigError):
        load_config(config_path)


def test_load_report_config_defaults(tmp_path):
    config_path = _write(tmp_path, _BASE_CONFIG)
    report = load_report_config(config_path)
    assert report.unit == "c"
    assert report.include_summary is True
    assert report.include_chart is True
    assert report.include_table is True
    assert report.table_layout == "full"
    assert report.rollup_period == "week"


def test_load_report_config_invalid_unit(tmp_path):
    config_path = _write(tmp_path, _BASE_CONFIG + "\n[report]\nunit = kelvin\n")
    with pytest.raises(ConfigError):
        load_report_config(config_path)


def test_load_report_config_table_layout(tmp_path):
    config_path = _write(
        tmp_path,
        _BASE_CONFIG + "\n[report]\ntable_layout = rollup\nrollup_period = month\n",
    )
    report = load_report_config(config_path)
    assert report.table_layout == "rollup"
    assert report.rollup_period == "month"


def test_load_report_config_invalid_table_layout(tmp_path):
    config_path = _write(tmp_path, _BASE_CONFIG + "\n[report]\ntable_layout = fancy\n")
    with pytest.raises(ConfigError):
        load_report_config(config_path)


def test_load_report_config_invalid_rollup_period(tmp_path):
    config_path = _write(tmp_path, _BASE_CONFIG + "\n[report]\nrollup_period = year\n")
    with pytest.raises(ConfigError):
        load_report_config(config_path)


def test_load_report_config_include_chart_and_table_toggles(tmp_path):
    config_path = _write(
        tmp_path,
        _BASE_CONFIG + "\n[report]\ninclude_chart = no\ninclude_table = no\n",
    )
    report = load_report_config(config_path)
    assert report.include_chart is False
    assert report.include_table is False


def test_load_mqtt_config_requires_host_when_enabled(tmp_path):
    config_path = _write(tmp_path, _BASE_CONFIG + "\n[mqtt]\nenabled = yes\n")
    with pytest.raises(ConfigError):
        load_mqtt_config(config_path)


def test_load_mqtt_config_disabled_default(tmp_path):
    config_path = _write(tmp_path, _BASE_CONFIG)
    mqtt = load_mqtt_config(config_path)
    assert mqtt.enabled is False


def test_load_alert_config_requires_urls_when_enabled(tmp_path):
    config_path = _write(
        tmp_path, _BASE_CONFIG + "\n[alerting]\nenabled = yes\nstale_after_days = 2\n"
    )
    with pytest.raises(ConfigError):
        load_alert_config(config_path)


def test_load_alert_config_requires_something_to_check(tmp_path):
    config_path = _write(
        tmp_path,
        _BASE_CONFIG
        + "\n[alerting]\nenabled = yes\napprise_urls = tgram://x/y\n"
        "high_temp_alert_celsius = 0\n",
    )
    with pytest.raises(ConfigError):
        load_alert_config(config_path)


def test_load_alert_config_valid(tmp_path):
    config_path = _write(
        tmp_path,
        _BASE_CONFIG
        + "\n[alerting]\nenabled = yes\napprise_urls = tgram://x/y\n"
        "high_temp_alert_celsius = 39.0\n",
    )
    alert = load_alert_config(config_path)
    assert alert.enabled is True
    assert alert.high_temp_alert_celsius == 39.0
    assert alert.apprise_urls == ["tgram://x/y"]


def test_load_alert_config_low_temp_optional(tmp_path):
    config_path = _write(tmp_path, _BASE_CONFIG)
    alert = load_alert_config(config_path)
    assert alert.low_temp_alert_celsius is None


def test_load_alert_config_invalid_low_temp(tmp_path):
    config_path = _write(
        tmp_path, _BASE_CONFIG + "\n[alerting]\nlow_temp_alert_celsius = cold\n"
    )
    with pytest.raises(ConfigError):
        load_alert_config(config_path)


def test_load_api_config_defaults(tmp_path):
    config_path = _write(tmp_path, _BASE_CONFIG)
    api = load_api_config(config_path)
    assert api.enabled is False
    assert api.host == "127.0.0.1"
    assert api.port == 8080


def test_load_profiles_config_more_than_two_names_allowed(tmp_path):
    config_path = _write(
        tmp_path, _BASE_CONFIG + "\n[profiles]\nenabled = yes\nnames = A, B, C, D\n"
    )
    profiles = load_profiles_config(config_path)
    assert profiles.names == ["A", "B", "C", "D"]


def test_load_profiles_config_valid(tmp_path):
    config_path = _write(
        tmp_path,
        _BASE_CONFIG
        + "\n[profiles]\nenabled = yes\nnames = Alice, Bob\n"
        "ntfy_url = https://ntfy.sh/topic\nassign_window_seconds = 120\n",
    )
    profiles = load_profiles_config(config_path)
    assert profiles.enabled is True
    assert profiles.names == ["Alice", "Bob"]
    assert profiles.ntfy_url == "https://ntfy.sh/topic"
    assert profiles.assign_window_seconds == 120


def test_load_profiles_config_requires_names_when_enabled(tmp_path):
    config_path = _write(tmp_path, _BASE_CONFIG + "\n[profiles]\nenabled = yes\n")
    with pytest.raises(ConfigError):
        load_profiles_config(config_path)


def test_load_profiles_config_invalid_assign_window(tmp_path):
    config_path = _write(
        tmp_path,
        _BASE_CONFIG + "\n[profiles]\nenabled = yes\nnames = Alice\n"
        "assign_window_seconds = -5\n",
    )
    with pytest.raises(ConfigError):
        load_profiles_config(config_path)


def test_load_profile_details_missing_section_defaults_to_profile_name(tmp_path):
    config_path = _write(tmp_path, _BASE_CONFIG)
    patient = load_profile_details(config_path, "Alice")
    assert patient.name == "Alice"
    assert patient.email == ""
    assert patient.notes == ""
    assert patient.unit == ""
    assert patient.date_format == ""
    assert patient.page_size == ""
    assert patient.high_temp_alert_celsius is None
    assert patient.apprise_urls == []
    assert patient.stale_after_days is None


def test_load_profile_details_full_section(tmp_path):
    config_path = _write(
        tmp_path,
        _BASE_CONFIG
        + "\n[profile.Alice]\n"
        "name = Alice Smith\n"
        "email = alice@example.com\n"
        "notes = Recovering from flu\n"
        "unit = f\n"
        "date_format = us\n"
        "page_size = a4\n"
        "high_temp_alert_celsius = 38.5\n"
        "apprise_urls = json://alice-phone\n"
        "stale_after_days = 1\n",
    )
    patient = load_profile_details(config_path, "Alice")
    assert patient.name == "Alice Smith"
    assert patient.email == "alice@example.com"
    assert patient.notes == "Recovering from flu"
    assert patient.unit == "f"
    assert patient.date_format == "us"
    assert patient.page_size == "a4"
    assert patient.high_temp_alert_celsius == 38.5
    assert patient.apprise_urls == ["json://alice-phone"]
    assert patient.stale_after_days == 1


def test_load_profile_details_invalid_unit(tmp_path):
    config_path = _write(tmp_path, _BASE_CONFIG + "\n[profile.Alice]\nunit = kelvin\n")
    with pytest.raises(ConfigError):
        load_profile_details(config_path, "Alice")


def test_load_profile_details_invalid_high_temp_alert(tmp_path):
    config_path = _write(
        tmp_path, _BASE_CONFIG + "\n[profile.Alice]\nhigh_temp_alert_celsius = -5\n"
    )
    with pytest.raises(ConfigError):
        load_profile_details(config_path, "Alice")


def test_load_profile_details_invalid_date_format(tmp_path):
    config_path = _write(
        tmp_path, _BASE_CONFIG + "\n[profile.Alice]\ndate_format = european\n"
    )
    with pytest.raises(ConfigError):
        load_profile_details(config_path, "Alice")


def test_load_profile_details_invalid_stale_after_days(tmp_path):
    config_path = _write(
        tmp_path, _BASE_CONFIG + "\n[profile.Alice]\nstale_after_days = -1\n"
    )
    with pytest.raises(ConfigError):
        load_profile_details(config_path, "Alice")


def test_load_profile_details_stale_after_days_zero_is_valid(tmp_path):
    config_path = _write(
        tmp_path, _BASE_CONFIG + "\n[profile.Alice]\nstale_after_days = 0\n"
    )
    patient = load_profile_details(config_path, "Alice")
    assert patient.stale_after_days == 0


def test_persist_discovered_address(tmp_path):
    config_path = tmp_path / "config.ini"
    config_path.write_text("[daemon]\naddress =\n\n[storage]\ndb_path = /tmp/x.db\n")
    persist_discovered_address(config_path, "11:22:33:44:55:66")
    updated = config_path.read_text()
    assert "address = 11:22:33:44:55:66" in updated

    config = load_config(str(config_path))
    assert config.address == "11:22:33:44:55:66"
