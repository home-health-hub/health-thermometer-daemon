"""Check for stale, feverish, or hypothermic readings and notify via Apprise."""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import apprise

from ._version import __version__
from .categories import celsius
from .config import (
    AlertConfig,
    ConfigError,
    PatientConfig,
    load_alert_config,
    load_config,
    load_profile_details,
    load_profiles_config,
)

# Minimum time between repeat staleness alerts for the same device, so a
# once-hourly check doesn't re-notify every single run while data stays old.
_STALE_ALERT_THROTTLE = timedelta(days=1)


@dataclass
class Alert:
    """One triggered alert and the Apprise URLs it should be sent to.

    Routing is per-alert rather than global because a profile can override
    ``apprise_urls`` (see ``_effective_apprise_urls``) -- Alice's alerts and
    Bob's alerts don't necessarily go to the same place.
    """

    urls: list[str]
    message: str


def _load_state(state_path: str) -> dict[str, dict[str, str]]:
    """Load per-address alert state, tolerating a missing or corrupt file."""
    path = Path(state_path)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state_path: str, state: dict[str, dict[str, str]]) -> None:
    """Persist per-address alert state, creating the parent directory if needed."""
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def _all_addresses(db_path: str) -> list[str]:
    """Return every distinct device address with at least one reading."""
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute("SELECT DISTINCT address FROM readings").fetchall()
        return [row[0] for row in rows]
    finally:
        connection.close()


def _latest_reading(db_path: str, address: str) -> tuple | None:
    """Return the most recent reading row for one device address."""
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(
            "SELECT recorded_at, value, unit, profile "
            "FROM readings WHERE address = ? "
            "ORDER BY recorded_at DESC LIMIT 1",
            (address,),
        ).fetchone()
    finally:
        connection.close()


def _effective_stale_after_days(alert_config: AlertConfig, patient: PatientConfig | None) -> int:
    """Resolve the staleness threshold: the profile's override, or the global default."""
    if patient is not None and patient.stale_after_days is not None:
        return patient.stale_after_days
    return alert_config.stale_after_days


def _effective_high_temp_alert_celsius(
    alert_config: AlertConfig, patient: PatientConfig | None
) -> float:
    """Resolve the fever threshold: the profile's override, or the global default."""
    if patient is not None and patient.high_temp_alert_celsius is not None:
        return patient.high_temp_alert_celsius
    return alert_config.high_temp_alert_celsius


def _effective_apprise_urls(alert_config: AlertConfig, patient: PatientConfig | None) -> list[str]:
    """Resolve notification targets: the profile's override, or the global default.

    A profile's ``apprise_urls`` replaces the global list rather than
    extending it, so routing is precise (Alice's alerts go to Alice) rather
    than broadcasting everyone's readings to a shared list by default.
    """
    if patient is not None and patient.apprise_urls:
        return patient.apprise_urls
    return alert_config.apprise_urls


def check_alerts(
    db_path: str,
    alert_config: AlertConfig,
    profile_configs: dict[str, PatientConfig] | None = None,
    now: datetime | None = None,
) -> list[Alert]:
    """Evaluate staleness, fever, and hypothermia conditions.

    Checked independently for every device address with at least one
    reading. A fever or hypothermia alert only fires the first time a given
    "latest reading" is seen, so it isn't repeated on every subsequent run
    until a new reading arrives. A staleness alert repeats at most once per
    ``_STALE_ALERT_THROTTLE`` while the condition persists.

    If the latest reading is tagged with a profile (see the daemon's
    ntfy/dunstify tagging) and ``profile_configs`` has a
    ``[profile.<name>]`` entry for it, that profile's
    ``high_temp_alert_celsius`` / ``stale_after_days`` / ``apprise_urls``
    override the global ``[alerting]`` values for that device's checks.
    Untagged readings (profile tagging disabled, or not yet answered)
    always use the global values. ``low_temp_alert_celsius`` is never
    overridden per profile -- there's no [profile.<name>] field for it
    (see config.py's PatientConfig).

    Threshold comparisons always convert the stored value to Celsius first
    (see categories.celsius), regardless of which unit the device itself
    reported the reading in.

    Args:
        db_path: Path to the SQLite database file.
        alert_config: Parsed [alerting] configuration.
        profile_configs: Profile name -> PatientConfig, for resolving
            per-profile overrides. None/empty means no overrides apply.
        now: Current UTC time; injectable for testing. Defaults to
            ``datetime.now(timezone.utc)``.

    Returns:
        Triggered alerts (empty if nothing was triggered), each carrying
        its own destination URLs. The caller is responsible for actually
        sending them.
    """
    now = now or datetime.now(timezone.utc)
    profile_configs = profile_configs or {}
    state = _load_state(alert_config.state_path)
    alerts: list[Alert] = []

    for address in _all_addresses(db_path):
        slot_state = state.get(address, {})
        row = _latest_reading(db_path, address)
        if row is None:
            continue

        recorded_at, value, unit, profile = row
        latest_dt = datetime.fromisoformat(recorded_at)
        label = f"{profile} ({address})" if profile else address

        patient = profile_configs.get(profile) if profile else None
        urls = _effective_apprise_urls(alert_config, patient)
        stale_after_days = _effective_stale_after_days(alert_config, patient)
        high_temp_alert_celsius = _effective_high_temp_alert_celsius(alert_config, patient)

        if stale_after_days > 0:
            if now - latest_dt > timedelta(days=stale_after_days):
                last_alert = slot_state.get("last_stale_alert_at")
                last_alert_dt = datetime.fromisoformat(last_alert) if last_alert else None
                if last_alert_dt is None or now - last_alert_dt > _STALE_ALERT_THROTTLE:
                    alerts.append(
                        Alert(
                            urls,
                            f"No reading from {label} in over {stale_after_days} day(s) "
                            f"(last: {recorded_at})",
                        )
                    )
                    slot_state["last_stale_alert_at"] = now.isoformat()
            else:
                slot_state.pop("last_stale_alert_at", None)

        already_seen = slot_state.get("last_seen_recorded_at") == recorded_at
        if not already_seen:
            value_c = celsius(value, unit)
            if high_temp_alert_celsius > 0 and value_c >= high_temp_alert_celsius:
                alerts.append(
                    Alert(
                        urls,
                        f"Fever-range reading from {label}: {value:.1f}°{unit} "
                        f"({value_c:.1f}°C, at or above {high_temp_alert_celsius}°C)",
                    )
                )
            if (
                alert_config.low_temp_alert_celsius is not None
                and value_c <= alert_config.low_temp_alert_celsius
            ):
                alerts.append(
                    Alert(
                        urls,
                        f"Hypothermia-range reading from {label}: {value:.1f}°{unit} "
                        f"({value_c:.1f}°C, at or below {alert_config.low_temp_alert_celsius}°C)",
                    )
                )

        slot_state["last_seen_recorded_at"] = recorded_at
        state[address] = slot_state

    _save_state(alert_config.state_path, state)
    return alerts


def send_alerts(alerts: list[Alert]) -> None:
    """Send each alert via Apprise to its own destination URLs.

    Args:
        alerts: Alerts to send, each with its own resolved URL list (see
            ``check_alerts``). An alert with no URLs (global list empty and
            no profile override) is silently skipped -- there's nowhere to
            send it.
    """
    for alert in alerts:
        if not alert.urls:
            continue
        notifier = apprise.Apprise()
        for url in alert.urls:
            notifier.add(url)
        notifier.notify(title="Health Thermometer Alert", body=alert.message)


def _load_profile_configs(config_path: str, names: list[str]) -> dict[str, PatientConfig]:
    """Load every profile's [profile.<name>] section, for alert-override resolution.

    Args:
        config_path: Path to the INI configuration file.
        names: Profile names to load (typically ``profiles_config.names``).

    Returns:
        Profile name -> PatientConfig.

    Raises:
        ConfigError: If any profile's section is invalid.
    """
    return {name: load_profile_details(config_path, name) for name in names}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="health-thermometer-alert-check",
        description="Check for stale, feverish, or hypothermic readings and notify via Apprise.",
    )
    parser.add_argument(
        "-c", "--config", required=True, help="Path to the daemon's INI config file"
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code.
    """
    args = _parse_args(argv)

    try:
        db_path = load_config(args.config).db_path
        alert_config = load_alert_config(args.config)
        profiles_config = load_profiles_config(args.config)
    except ConfigError as exc:
        print(f"Error: {exc}")
        return 1

    if not alert_config.enabled:
        print("Alerting is disabled (alerting.enabled = no).")
        return 0

    try:
        profile_configs = (
            _load_profile_configs(args.config, profiles_config.names)
            if profiles_config.enabled
            else {}
        )
    except ConfigError as exc:
        print(f"Error: {exc}")
        return 1

    alerts = check_alerts(db_path, alert_config, profile_configs)
    if not alerts:
        print("No alerts triggered.")
        return 0

    send_alerts(alerts)
    for alert in alerts:
        print(f"ALERT: {alert.message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
