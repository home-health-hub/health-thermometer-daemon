"""Configuration loading and persistence for the daemon."""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    """Raised when the configuration file is missing or invalid."""


@dataclass
class DaemonConfig:
    """Parsed ``[daemon]``/``[storage]`` sections."""

    config_path: Path
    address: str
    log_level: str
    retry_seconds: int
    db_path: str


@dataclass
class ReportConfig:
    """Parsed [report] section controlling PDF/CSV report rendering."""

    include_address: bool
    include_battery: bool
    include_profile: bool
    include_summary: bool
    include_chart: bool  # PDF only
    include_table: bool  # PDF only; CSV always exports the full row set
    table_layout: str  # "full", "compact", or "rollup" -- PDF only
    rollup_period: str  # "week" or "month" -- only used when table_layout = rollup
    unit: str  # "c" or "f" -- display unit; stored value is never converted
    date_format: str  # "us" or "world"
    page_size: str  # "letter" or "a4"


DEFAULT_REPORT_CONFIG = ReportConfig(
    include_address=True,
    include_battery=True,
    include_profile=False,
    include_summary=True,
    include_chart=True,
    include_table=True,
    table_layout="full",
    rollup_period="week",
    unit="c",
    date_format="world",
    page_size="letter",
)

_UNITS = ("c", "f")
_DATE_FORMATS = ("us", "world")
_PAGE_SIZES = ("letter", "a4")
_TABLE_LAYOUTS = ("full", "compact", "rollup")
_ROLLUP_PERIODS = ("week", "month")


@dataclass
class PatientConfig:
    """A profile's identifying info, report preferences, and alert overrides.

    Loaded from a ``[profile.<name>]`` section (see ``load_profile_details``)
    or left at these blanks/unset when no profile is selected. Report
    fields (``unit``, ``date_format``, ``page_size``) are consumed by
    ``report.py``; alert fields (``high_temp_alert_celsius``,
    ``apprise_urls``, ``stale_after_days``) are consumed by ``alerting.py``.
    Every field is optional -- a profile with no section at all still tags
    and reports normally, just without personalization.
    """

    name: str
    email: str
    notes: str
    unit: str  # "" (unset, use report.unit), "c", or "f"
    date_format: str  # "" (unset, use report.date_format), "us", or "world"
    page_size: str  # "" (unset, use report.page_size), "letter", or "a4"
    high_temp_alert_celsius: float | None  # None means "use [alerting]'s value"
    apprise_urls: list[str]  # empty means "use [alerting] apprise_urls"
    stale_after_days: int | None  # None means "use [alerting] stale_after_days"


DEFAULT_PATIENT_CONFIG = PatientConfig(
    name="",
    email="",
    notes="",
    unit="",
    date_format="",
    page_size="",
    high_temp_alert_celsius=None,
    apprise_urls=[],
    stale_after_days=None,
)


@dataclass
class MqttConfig:
    """Parsed [mqtt] section: optional MQTT publishing of live readings."""

    enabled: bool
    host: str
    port: int
    username: str
    password: str
    use_tls: bool
    topic_prefix: str
    qos: int
    retain: bool


DEFAULT_MQTT_CONFIG = MqttConfig(
    enabled=False,
    host="",
    port=1883,
    username="",
    password="",
    use_tls=False,
    topic_prefix="health_thermometer_daemon",
    qos=0,
    retain=True,
)

_QOS_LEVELS = (0, 1, 2)


@dataclass
class AlertConfig:
    """Parsed [alerting] section: optional Apprise-based notifications."""

    enabled: bool
    apprise_urls: list[str]
    stale_after_days: int  # 0 disables the staleness check
    high_temp_alert_celsius: float  # 0 disables the fever check
    low_temp_alert_celsius: float | None  # None disables the hypothermia check
    state_path: str


DEFAULT_ALERT_CONFIG = AlertConfig(
    enabled=False,
    apprise_urls=[],
    stale_after_days=0,
    high_temp_alert_celsius=38.0,
    low_temp_alert_celsius=None,
    state_path="/var/lib/health-thermometer-daemon/alert-state.json",
)


@dataclass
class ApiConfig:
    """Parsed [api] section: optional local HTTP API for reading data on demand."""

    enabled: bool
    host: str
    port: int
    token: str  # "" means no authentication required


DEFAULT_API_CONFIG = ApiConfig(enabled=False, host="127.0.0.1", port=8080, token="")


@dataclass
class ProfilesConfig:
    """Parsed [profiles] section: who-was-this tagging for a shared device.

    There's no device-side "user slot" for this device class the way a BP
    monitor or scale has -- a thermometer reading has no identity of its
    own at all, so tagging asks a human, the same reasoning
    etekcity-bp-daemon and etekcity-scale-daemon use. When the HTTP API is
    enabled, a new reading is announced via an ntfy notification with one
    HTTP action button per profile, each calling back into the API to tag
    the reading. When the API is disabled, there's nothing for ntfy's
    action buttons to call back to, so a local dunstify prompt is used
    instead, which resolves synchronously in-process.
    """

    enabled: bool
    names: list[str]
    ntfy_url: str
    ntfy_token: str
    api_base_url: str
    dunstify_timeout_seconds: int
    assign_window_seconds: int  # 0 disables rejecting a late /api/v1/assign-profile tag


DEFAULT_PROFILES_CONFIG = ProfilesConfig(
    enabled=False,
    names=[],
    ntfy_url="",
    ntfy_token="",
    api_base_url="http://127.0.0.1:8080",
    dunstify_timeout_seconds=30,
    assign_window_seconds=0,
)


def _parse_bool(value: str, key: str) -> bool:
    """Parse a yes/no-style config value.

    Args:
        value: Raw string from the config file.
        key: Dotted key name, used in the error message.

    Returns:
        The parsed boolean.

    Raises:
        ConfigError: If ``value`` isn't a recognized yes/no spelling.
    """
    normalized = value.strip().lower()
    if normalized in ("yes", "true", "1", "on"):
        return True
    if normalized in ("no", "false", "0", "off"):
        return False
    raise ConfigError(f"{key} must be yes/no, got {value!r}")


def load_config(config_path: str) -> DaemonConfig:
    """Load and validate the daemon configuration file.

    Args:
        config_path: Path to the INI configuration file.

    Returns:
        The parsed configuration.

    Raises:
        ConfigError: If the file is missing or a required value is invalid.
    """
    path = Path(config_path)
    if not path.is_file():
        raise ConfigError(
            f"Config file not found: {path}. Copy "
            "config/health-thermometer-daemon.ini.example to this path and edit it."
        )

    parser = configparser.ConfigParser()
    parser.read(path)

    daemon = parser["daemon"] if parser.has_section("daemon") else {}
    storage = parser["storage"] if parser.has_section("storage") else {}

    try:
        retry_seconds = int(daemon.get("retry_seconds", "30"))
    except ValueError as exc:
        raise ConfigError("daemon.retry_seconds must be an integer") from exc
    if retry_seconds <= 0:
        raise ConfigError("daemon.retry_seconds must be a positive number")

    db_path = storage.get("db_path", "").strip()
    if not db_path:
        raise ConfigError("storage.db_path must be set")

    return DaemonConfig(
        config_path=path,
        address=daemon.get("address", "").strip(),
        log_level=daemon.get("log_level", "INFO").strip().upper(),
        retry_seconds=retry_seconds,
        db_path=db_path,
    )


def load_report_config(config_path: str) -> ReportConfig:
    """Load the ``[report]`` section of the daemon config file, if present.

    Args:
        config_path: Path to the INI configuration file.

    Returns:
        The parsed report configuration, or ``DEFAULT_REPORT_CONFIG`` if the
        file has no ``[report]`` section.

    Raises:
        ConfigError: If the file is missing or a ``[report]`` value is invalid.
    """
    path = Path(config_path)
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")

    parser = configparser.ConfigParser()
    parser.read(path)

    if not parser.has_section("report"):
        return DEFAULT_REPORT_CONFIG

    report = parser["report"]

    unit = report.get("unit", DEFAULT_REPORT_CONFIG.unit).strip().lower()
    if unit not in _UNITS:
        raise ConfigError(f"report.unit must be one of {_UNITS}, got {unit!r}")

    date_format = report.get("date_format", DEFAULT_REPORT_CONFIG.date_format).strip().lower()
    if date_format not in _DATE_FORMATS:
        raise ConfigError(
            f"report.date_format must be one of {_DATE_FORMATS}, got {date_format!r}"
        )

    page_size = report.get("page_size", DEFAULT_REPORT_CONFIG.page_size).strip().lower()
    if page_size not in _PAGE_SIZES:
        raise ConfigError(f"report.page_size must be one of {_PAGE_SIZES}, got {page_size!r}")

    table_layout = report.get("table_layout", DEFAULT_REPORT_CONFIG.table_layout).strip().lower()
    if table_layout not in _TABLE_LAYOUTS:
        raise ConfigError(
            f"report.table_layout must be one of {_TABLE_LAYOUTS}, got {table_layout!r}"
        )

    rollup_period = report.get(
        "rollup_period", DEFAULT_REPORT_CONFIG.rollup_period
    ).strip().lower()
    if rollup_period not in _ROLLUP_PERIODS:
        raise ConfigError(
            f"report.rollup_period must be one of {_ROLLUP_PERIODS}, got {rollup_period!r}"
        )

    return ReportConfig(
        include_address=_parse_bool(
            report.get("include_address", "yes"), "report.include_address"
        ),
        include_battery=_parse_bool(
            report.get("include_battery", "yes"), "report.include_battery"
        ),
        include_profile=_parse_bool(
            report.get("include_profile", "no"), "report.include_profile"
        ),
        include_summary=_parse_bool(
            report.get("include_summary", "yes"), "report.include_summary"
        ),
        include_chart=_parse_bool(report.get("include_chart", "yes"), "report.include_chart"),
        include_table=_parse_bool(report.get("include_table", "yes"), "report.include_table"),
        table_layout=table_layout,
        rollup_period=rollup_period,
        unit=unit,
        date_format=date_format,
        page_size=page_size,
    )


def load_profile_details(config_path: str, profile: str) -> PatientConfig:
    """Load one ``[profile.<name>]`` section: identity, report prefs, alert overrides.

    Each profile is self-contained -- a missing section just falls back to
    blanks/unset, since none of these fields are required for the daemon to
    function; they only personalize reports/alerts if provided.

    Args:
        config_path: Path to the INI configuration file.
        profile: The profile name, expected to match one of the names in
            ``[profiles] names``.

    Returns:
        A ``PatientConfig`` for this profile (``name`` defaults to the
        profile name itself if left blank). All fields are "unset" defaults
        if the section doesn't exist at all.

    Raises:
        ConfigError: If the file is missing or a value is invalid.
    """
    path = Path(config_path)
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")

    parser = configparser.ConfigParser()
    parser.read(path)

    section_name = f"profile.{profile}"
    if not parser.has_section(section_name):
        return PatientConfig(
            name=profile,
            email="",
            notes="",
            unit="",
            date_format="",
            page_size="",
            high_temp_alert_celsius=None,
            apprise_urls=[],
            stale_after_days=None,
        )

    section = parser[section_name]

    unit = section.get("unit", "").strip().lower()
    if unit and unit not in _UNITS:
        raise ConfigError(f"{section_name}.unit must be one of {_UNITS}, got {unit!r}")

    date_format = section.get("date_format", "").strip().lower()
    if date_format and date_format not in _DATE_FORMATS:
        raise ConfigError(
            f"{section_name}.date_format must be one of {_DATE_FORMATS}, got {date_format!r}"
        )

    page_size = section.get("page_size", "").strip().lower()
    if page_size and page_size not in _PAGE_SIZES:
        raise ConfigError(
            f"{section_name}.page_size must be one of {_PAGE_SIZES}, got {page_size!r}"
        )

    high_temp_alert_celsius = None
    high_temp_raw = section.get("high_temp_alert_celsius", "").strip()
    if high_temp_raw:
        try:
            high_temp_alert_celsius = float(high_temp_raw)
        except ValueError as exc:
            raise ConfigError(
                f"{section_name}.high_temp_alert_celsius must be a number"
            ) from exc
        if high_temp_alert_celsius < 0:
            raise ConfigError(f"{section_name}.high_temp_alert_celsius must be zero or positive")

    urls_raw = section.get("apprise_urls", "").strip()
    apprise_urls = [url.strip() for url in urls_raw.split(",") if url.strip()]

    stale_after_days = None
    stale_after_days_str = section.get("stale_after_days", "").strip()
    if stale_after_days_str:
        try:
            stale_after_days = int(stale_after_days_str)
        except ValueError as exc:
            raise ConfigError(f"{section_name}.stale_after_days must be an integer") from exc
        if stale_after_days < 0:
            raise ConfigError(f"{section_name}.stale_after_days must be zero or positive")

    return PatientConfig(
        name=section.get("name", "").strip() or profile,
        email=section.get("email", "").strip(),
        notes=section.get("notes", "").strip(),
        unit=unit,
        date_format=date_format,
        page_size=page_size,
        high_temp_alert_celsius=high_temp_alert_celsius,
        apprise_urls=apprise_urls,
        stale_after_days=stale_after_days,
    )


def load_mqtt_config(config_path: str) -> MqttConfig:
    """Load the ``[mqtt]`` section of the daemon config file, if present.

    Args:
        config_path: Path to the INI configuration file.

    Returns:
        The parsed MQTT configuration, or ``DEFAULT_MQTT_CONFIG`` (disabled)
        if the file has no ``[mqtt]`` section.

    Raises:
        ConfigError: If the file is missing or a ``[mqtt]`` value is invalid.
    """
    path = Path(config_path)
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")

    parser = configparser.ConfigParser()
    parser.read(path)

    if not parser.has_section("mqtt"):
        return DEFAULT_MQTT_CONFIG

    mqtt = parser["mqtt"]
    enabled = _parse_bool(mqtt.get("enabled", "no"), "mqtt.enabled")

    host = mqtt.get("host", "").strip()
    if enabled and not host:
        raise ConfigError("mqtt.host must be set when mqtt.enabled = yes")

    try:
        port = int(mqtt.get("port", str(DEFAULT_MQTT_CONFIG.port)))
    except ValueError as exc:
        raise ConfigError("mqtt.port must be an integer") from exc

    try:
        qos = int(mqtt.get("qos", str(DEFAULT_MQTT_CONFIG.qos)))
    except ValueError as exc:
        raise ConfigError("mqtt.qos must be an integer") from exc
    if qos not in _QOS_LEVELS:
        raise ConfigError(f"mqtt.qos must be one of {_QOS_LEVELS}, got {qos!r}")

    return MqttConfig(
        enabled=enabled,
        host=host,
        port=port,
        username=mqtt.get("username", "").strip(),
        password=mqtt.get("password", "").strip(),
        use_tls=_parse_bool(mqtt.get("use_tls", "no"), "mqtt.use_tls"),
        topic_prefix=mqtt.get("topic_prefix", DEFAULT_MQTT_CONFIG.topic_prefix).strip(),
        qos=qos,
        retain=_parse_bool(mqtt.get("retain", "yes"), "mqtt.retain"),
    )


def load_alert_config(config_path: str) -> AlertConfig:
    """Load the ``[alerting]`` section of the daemon config file, if present.

    Args:
        config_path: Path to the INI configuration file.

    Returns:
        The parsed alert configuration, or ``DEFAULT_ALERT_CONFIG``
        (disabled) if the file has no ``[alerting]`` section.

    Raises:
        ConfigError: If the file is missing or an ``[alerting]`` value is
            invalid, including enabling it with nothing to check or without
            any notification URLs.
    """
    path = Path(config_path)
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")

    parser = configparser.ConfigParser()
    parser.read(path)

    if not parser.has_section("alerting"):
        return DEFAULT_ALERT_CONFIG

    alerting = parser["alerting"]
    enabled = _parse_bool(alerting.get("enabled", "no"), "alerting.enabled")

    urls_raw = alerting.get("apprise_urls", "").strip()
    apprise_urls = [url.strip() for url in urls_raw.split(",") if url.strip()]
    if enabled and not apprise_urls:
        raise ConfigError("alerting.apprise_urls must be set when alerting.enabled = yes")

    try:
        stale_after_days = int(
            alerting.get("stale_after_days", str(DEFAULT_ALERT_CONFIG.stale_after_days))
        )
    except ValueError as exc:
        raise ConfigError("alerting.stale_after_days must be an integer") from exc
    if stale_after_days < 0:
        raise ConfigError("alerting.stale_after_days must be zero or positive")

    try:
        high_temp_alert_celsius = float(
            alerting.get(
                "high_temp_alert_celsius", str(DEFAULT_ALERT_CONFIG.high_temp_alert_celsius)
            )
        )
    except ValueError as exc:
        raise ConfigError("alerting.high_temp_alert_celsius must be a number") from exc
    if high_temp_alert_celsius < 0:
        raise ConfigError("alerting.high_temp_alert_celsius must be zero or positive")

    low_temp_alert_celsius = None
    low_temp_raw = alerting.get("low_temp_alert_celsius", "").strip()
    if low_temp_raw:
        try:
            low_temp_alert_celsius = float(low_temp_raw)
        except ValueError as exc:
            raise ConfigError("alerting.low_temp_alert_celsius must be a number") from exc

    if (
        enabled
        and stale_after_days == 0
        and high_temp_alert_celsius == 0
        and low_temp_alert_celsius is None
    ):
        raise ConfigError(
            "alerting.enabled = yes but nothing is configured to check -- set "
            "stale_after_days, high_temp_alert_celsius, or low_temp_alert_celsius"
        )

    return AlertConfig(
        enabled=enabled,
        apprise_urls=apprise_urls,
        stale_after_days=stale_after_days,
        high_temp_alert_celsius=high_temp_alert_celsius,
        low_temp_alert_celsius=low_temp_alert_celsius,
        state_path=alerting.get("state_path", DEFAULT_ALERT_CONFIG.state_path).strip(),
    )


def load_api_config(config_path: str) -> ApiConfig:
    """Load the ``[api]`` section of the daemon config file, if present.

    Args:
        config_path: Path to the INI configuration file.

    Returns:
        The parsed API configuration, or ``DEFAULT_API_CONFIG`` (disabled,
        bound to loopback) if the file has no ``[api]`` section.

    Raises:
        ConfigError: If the file is missing or an ``[api]`` value is invalid.
    """
    path = Path(config_path)
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")

    parser = configparser.ConfigParser()
    parser.read(path)

    if not parser.has_section("api"):
        return DEFAULT_API_CONFIG

    api = parser["api"]

    try:
        port = int(api.get("port", str(DEFAULT_API_CONFIG.port)))
    except ValueError as exc:
        raise ConfigError("api.port must be an integer") from exc

    return ApiConfig(
        enabled=_parse_bool(api.get("enabled", "no"), "api.enabled"),
        host=api.get("host", DEFAULT_API_CONFIG.host).strip() or DEFAULT_API_CONFIG.host,
        port=port,
        token=api.get("token", "").strip(),
    )


def load_profiles_config(config_path: str) -> ProfilesConfig:
    """Load the ``[profiles]`` section of the daemon config file, if present.

    Note that whether the ntfy or dunstify path is actually usable also
    depends on ``[api] enabled`` -- that cross-check happens where both
    configs are loaded together (the daemon's startup), not here, since
    this loader only sees its own section.

    Args:
        config_path: Path to the INI configuration file.

    Returns:
        The parsed profiles configuration, or ``DEFAULT_PROFILES_CONFIG``
        (disabled) if the file has no ``[profiles]`` section.

    Raises:
        ConfigError: If the file is missing, enabled without any names, or
            a numeric value is invalid.
    """
    path = Path(config_path)
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")

    parser = configparser.ConfigParser()
    parser.read(path)

    if not parser.has_section("profiles"):
        return DEFAULT_PROFILES_CONFIG

    profiles = parser["profiles"]
    enabled = _parse_bool(profiles.get("enabled", "no"), "profiles.enabled")

    names_raw = profiles.get("names", "").strip()
    names = [name.strip() for name in names_raw.split(",") if name.strip()]
    if enabled and not names:
        raise ConfigError("profiles.names must be set when profiles.enabled = yes")

    try:
        dunstify_timeout_seconds = int(
            profiles.get(
                "dunstify_timeout_seconds",
                str(DEFAULT_PROFILES_CONFIG.dunstify_timeout_seconds),
            )
        )
    except ValueError as exc:
        raise ConfigError("profiles.dunstify_timeout_seconds must be an integer") from exc

    try:
        assign_window_seconds = int(
            profiles.get(
                "assign_window_seconds",
                str(DEFAULT_PROFILES_CONFIG.assign_window_seconds),
            )
        )
    except ValueError as exc:
        raise ConfigError("profiles.assign_window_seconds must be an integer") from exc
    if assign_window_seconds < 0:
        raise ConfigError("profiles.assign_window_seconds must be zero or positive")

    return ProfilesConfig(
        enabled=enabled,
        names=names,
        ntfy_url=profiles.get("ntfy_url", "").strip(),
        ntfy_token=profiles.get("ntfy_token", "").strip(),
        api_base_url=(
            profiles.get("api_base_url", DEFAULT_PROFILES_CONFIG.api_base_url).strip()
            or DEFAULT_PROFILES_CONFIG.api_base_url
        ),
        dunstify_timeout_seconds=dunstify_timeout_seconds,
        assign_window_seconds=assign_window_seconds,
    )


def persist_discovered_address(config_path: Path, address: str) -> None:
    """Write a newly discovered device's address back to the config file.

    Rewrites only the ``address =`` line within the ``[daemon]`` section in
    place, so comments and formatting elsewhere in the file are preserved.

    Args:
        config_path: Path to the INI configuration file to update.
        address: BLE address of the discovered device.
    """
    lines = config_path.read_text().splitlines(keepends=True)
    in_daemon_section = False
    address_written = False

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_daemon_section = stripped == "[daemon]"
            continue
        if not in_daemon_section:
            continue
        if stripped.startswith("address") and "=" in stripped and not address_written:
            lines[i] = f"address = {address}\n"
            address_written = True

    config_path.write_text("".join(lines))
