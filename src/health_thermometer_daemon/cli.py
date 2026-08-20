"""Command-line entry point and daemon run loop."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import ssl
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import aiomqtt
from bleak.exc import BleakError
from health_thermometer_ble import (
    DeviceInfo,
    Reading,
    ThermometerBleClient,
    ThermometerError,
    discover,
)

from ._version import __version__
from .api import is_insecurely_exposed
from .config import (
    DEFAULT_API_CONFIG,
    DEFAULT_MQTT_CONFIG,
    DEFAULT_PROFILES_CONFIG,
    ApiConfig,
    ConfigError,
    DaemonConfig,
    MqttConfig,
    ProfilesConfig,
    load_alert_config,
    load_api_config,
    load_config,
    load_mqtt_config,
    load_profile_details,
    load_profiles_config,
    load_report_config,
    persist_discovered_address,
)
from .storage import ReadingStore, get_distinct_profiles, set_reading_profile

_LOGGER = logging.getLogger("health_thermometer_daemon")

#: BLE-layer exceptions worth retrying rather than crashing the daemon on.
#: Real adapters/stacks throw all sorts of things beyond this package's own
#: ThermometerError -- OSError covers D-Bus/socket-level failures, and
#: asyncio.TimeoutError covers a hung connect attempt. Caught broadly (see
#: run_daemon) rather than narrowly, the same lesson learned the hard way in
#: this org's trividia-truemetrix-daemon (an early version crashed on an
#: unhandled bleak/dbus-fast exception in a container with no D-Bus socket).
_RETRYABLE_BLE_ERRORS = (ThermometerError, BleakError, OSError, asyncio.TimeoutError)


async def discover_device(timeout: float = 60.0) -> str:
    """Scan for the first advertisement matching a Health Thermometer Profile device.

    Args:
        timeout: Seconds to scan before giving up.

    Returns:
        The discovered device's BLE address.

    Raises:
        TimeoutError: If no supported device is found within ``timeout``.
    """
    _LOGGER.info(
        "No device configured yet - scanning for a Health Thermometer Profile "
        "device (power it on / take a reading now)..."
    )
    devices = await discover(timeout=timeout)
    if not devices:
        raise TimeoutError(f"No supported device found within {timeout}s")
    return devices[0].address


def _reading_to_row(
    reading: Reading, device_info: DeviceInfo | None, address: str
) -> dict[str, object]:
    """Flatten a Reading (plus optional DeviceInfo) into storage-ready fields.

    ``profile`` always starts unset -- there's no device-side identity to
    auto-derive it from (see ``ProfilesConfig``), so it's tagged after the
    fact via ``_prompt_for_profile`` when profiles are enabled.
    """
    return {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "measured_at": reading.measured_at.isoformat() if reading.measured_at else None,
        "address": address,
        "profile": None,
        "value": reading.value,
        "unit": reading.unit,
        "temperature_type": reading.temperature_type,
        "battery_percent": device_info.battery_percent if device_info is not None else None,
        "error_code": None,
    }


@asynccontextmanager
async def _mqtt_connection(mqtt_config: MqttConfig):
    """Yield a connected MQTT client, or None if disabled or unreachable.

    A broker connection failure is logged and treated as non-fatal: BLE
    reading recording to the local database is the daemon's primary job and
    must not be blocked by an MQTT outage.

    Args:
        mqtt_config: Parsed [mqtt] configuration.

    Yields:
        A connected ``aiomqtt.Client``, or None if MQTT is disabled or the
        broker could not be reached.
    """
    if not mqtt_config.enabled:
        yield None
        return

    tls_context = ssl.create_default_context() if mqtt_config.use_tls else None
    try:
        async with aiomqtt.Client(
            hostname=mqtt_config.host,
            port=mqtt_config.port,
            username=mqtt_config.username or None,
            password=mqtt_config.password or None,
            tls_context=tls_context,
        ) as client:
            _LOGGER.info(
                "Connected to MQTT broker %s:%s", mqtt_config.host, mqtt_config.port
            )
            yield client
    except aiomqtt.MqttError as exc:
        _LOGGER.warning(
            "Could not connect to MQTT broker %s:%s (%s) -- continuing without "
            "MQTT publishing",
            mqtt_config.host,
            mqtt_config.port,
            exc,
        )
        yield None


async def _publish_reading(
    client: aiomqtt.Client, mqtt_config: MqttConfig, address: str, row: dict[str, object]
) -> None:
    """Publish one reading to MQTT as a JSON payload.

    Failures are logged, not raised -- a broker hiccup shouldn't be allowed
    to propagate into the daemon's main loop.

    Args:
        client: A connected MQTT client.
        mqtt_config: Supplies the topic prefix, QoS, and retain flag.
        address: The device's BLE address, used as the topic's last segment.
        row: The reading fields, as built by ``_reading_to_row``.
    """
    topic = f"{mqtt_config.topic_prefix}/{address}/state"
    try:
        await client.publish(
            topic, json.dumps(row), qos=mqtt_config.qos, retain=mqtt_config.retain
        )
    except aiomqtt.MqttError as exc:
        _LOGGER.warning("MQTT publish to %s failed: %s", topic, exc)


_NTFY_RETRY_DELAYS_SECONDS = (1, 2)
_NTFY_REQUEST_TIMEOUT_SECONDS = 5
# Worst case: every attempt hangs for the full per-request timeout, plus
# every retry delay in between -- used to size the daemon's shutdown wait
# for a still-retrying notification (see run_daemon's finally block).
_NTFY_MAX_RETRY_SECONDS = _NTFY_REQUEST_TIMEOUT_SECONDS * (
    len(_NTFY_RETRY_DELAYS_SECONDS) + 1
) + sum(_NTFY_RETRY_DELAYS_SECONDS)


def _reading_summary(row: dict[str, object]) -> str:
    """Render a reading as a short human-readable string for notifications."""
    return f"{row['value']:.1f}°{row['unit']}"


async def _notify_via_ntfy(
    row_id: int, row: dict[str, object], profiles_config: ProfilesConfig
) -> None:
    """Announce a new reading via ntfy, with one HTTP action button per profile.

    Each action calls back into the local HTTP API's ``/api/v1/assign-profile``
    endpoint when tapped, so the actual tagging happens later (whenever a
    human responds), not here.

    Retries up to twice (after 1s, then 2s) on a connection failure or a
    5xx server response, since those are often transient (e.g. the ntfy
    server restarting). A 4xx response is never retried, since trying again
    won't fix a bad token or malformed request.

    Args:
        row_id: The reading's primary key, to tag once a profile is chosen.
        row: The reading fields, for the notification body.
        profiles_config: Supplies the profile names, ntfy target, and the
            API base URL the action buttons call back into.
    """
    callback_base = f"{profiles_config.api_base_url}/api/v1/assign-profile"
    headers = {}
    if profiles_config.ntfy_token:
        headers["Authorization"] = f"Bearer {profiles_config.ntfy_token}"

    # JSON publishing requires POSTing to the server's root URL with the
    # topic in the body, not to <server>/<topic> like a plain-text publish.
    clean_url = profiles_config.ntfy_url.rstrip("/")
    ntfy_root, _, topic = clean_url.rpartition("/")
    if not ntfy_root:
        ntfy_root = clean_url

    payload = {
        "topic": topic,
        "title": "New temperature reading",
        "message": f"{_reading_summary(row)} -- who was this?",
        "actions": [
            {
                "action": "http",
                "label": name,
                "url": f"{callback_base}?id={row_id}&profile={name}",
                "method": "POST",
                "clear": True,
            }
            for name in profiles_config.names
        ],
    }

    last_error = None
    for attempt in range(len(_NTFY_RETRY_DELAYS_SECONDS) + 1):
        if attempt > 0:
            await asyncio.sleep(_NTFY_RETRY_DELAYS_SECONDS[attempt - 1])
        try:
            timeout = aiohttp.ClientTimeout(total=_NTFY_REQUEST_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(ntfy_root, json=payload, headers=headers) as response:
                    if response.status >= 500:
                        last_error = f"HTTP {response.status}: {await response.text()}"
                        continue
                    if response.status >= 400:
                        _LOGGER.warning(
                            "ntfy publish failed with HTTP %s: %s",
                            response.status,
                            await response.text(),
                        )
                    return
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_error = str(exc) or repr(exc)
            continue

    _LOGGER.warning(
        "ntfy publish failed after %d attempt(s): %s",
        len(_NTFY_RETRY_DELAYS_SECONDS) + 1,
        last_error,
    )


async def _prompt_via_dunstify(
    db_path: str,
    row_id: int,
    row: dict[str, object],
    profiles_config: ProfilesConfig,
) -> None:
    """Ask locally (via dunstify) which profile a reading belongs to.

    Blocks (within this background task, not the caller) until an action is
    chosen or the timeout elapses, then tags the row directly -- there's no
    HTTP API to call back into in this path, so the answer is applied here.

    Args:
        db_path: Path to the SQLite database file.
        row_id: The reading's primary key to tag.
        row: The reading fields, for the notification body.
        profiles_config: Supplies the profile names and timeout.
    """
    args = ["dunstify", "-t", str(profiles_config.dunstify_timeout_seconds * 1000)]
    for name in profiles_config.names:
        args += ["--action", f"{name},{name}"]
    args += ["New temperature reading", f"{_reading_summary(row)} -- who was this?"]

    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        stdout, _ = await asyncio.wait_for(
            process.communicate(), timeout=profiles_config.dunstify_timeout_seconds + 5
        )
    except (OSError, asyncio.TimeoutError) as exc:
        _LOGGER.warning("dunstify profile prompt failed: %s", exc)
        return

    chosen = stdout.decode().strip()
    if chosen not in profiles_config.names:
        _LOGGER.info("No profile chosen for reading %s (timed out or dismissed)", row_id)
        return

    if set_reading_profile(db_path, row_id, chosen):
        _LOGGER.info("Tagged reading %s as profile %s", row_id, chosen)


async def _prompt_for_profile(
    db_path: str,
    row_id: int,
    row: dict[str, object],
    profiles_config: ProfilesConfig,
    api_config: ApiConfig,
) -> None:
    """Dispatch to ntfy (if the API is reachable) or dunstify (if not).

    Args:
        db_path: Path to the SQLite database file.
        row_id: The reading's primary key to tag.
        row: The reading fields, for the notification body.
        profiles_config: Supplies profile names and per-path settings.
        api_config: Determines which path is used -- ntfy's action buttons
            have nothing to call back to without the API running.
    """
    if api_config.enabled:
        await _notify_via_ntfy(row_id, row, profiles_config)
    else:
        await _prompt_via_dunstify(db_path, row_id, row, profiles_config)


async def _sleep_or_stop(seconds: float, stop_event: asyncio.Event) -> None:
    """Sleep for ``seconds``, waking early if ``stop_event`` is set.

    Used for the retry backoff between connection attempts, so a stop
    signal received mid-retry-wait doesn't have to wait out the full
    interval before the daemon actually exits.
    """
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def _attempt_one_reading(
    address: str, connect_timeout: float, read_timeout: float
) -> tuple[Reading, DeviceInfo]:
    """Connect once, read device info and a single measurement, then disconnect.

    Kept as one short-lived connection per attempt rather than a persistent
    connection -- see run_daemon's docstring for why.

    Args:
        address: BLE address to connect to.
        connect_timeout: Seconds to wait for the connection itself.
        read_timeout: Seconds to wait for a measurement notification once
            connected (these devices only notify after a button press).

    Returns:
        The decoded reading and the device's identity/battery info.

    Raises:
        One of ``_RETRYABLE_BLE_ERRORS`` on any connection or read failure.
    """
    async with ThermometerBleClient(
        address, connect_timeout=connect_timeout, read_timeout=read_timeout
    ) as client:
        device_info = await client.get_device_info()
        reading = await client.read_once()
    return reading, device_info


async def run_daemon(
    config: DaemonConfig,
    once: bool = False,
    once_timeout: int = 60,
    mqtt_config: MqttConfig = DEFAULT_MQTT_CONFIG,
    profiles_config: ProfilesConfig = DEFAULT_PROFILES_CONFIG,
    api_config: ApiConfig = DEFAULT_API_CONFIG,
) -> bool:
    """Connect to the configured (or newly discovered) device and log readings.

    Design note -- why this loop reconnects for every single reading instead
    of holding one persistent connection open (unlike etekcity-bp-daemon's
    BloodPressureMonitor, which subscribes once and stays connected):
    whether a Health Thermometer Profile device stays connectable
    indefinitely, or only becomes connectable briefly around a measurement
    button-press, is an open question -- nobody has tested this package's
    driver (health-thermometer-ble) against real hardware yet (see that
    repo's own CLAUDE.md and this daemon's CLAUDE.md "Open questions").
    Connect -> read one measurement -> disconnect -> repeat works correctly
    under *either* assumption: if the device stays connectable, each cycle
    just reconnects instantly and blocks in read_once() until the next
    button press (its own natural pacing, no busy-loop); if the device is
    only briefly connectable, this is the only shape that can ever succeed
    at all. A single long-lived connect-and-subscribe, by contrast, would
    silently stop working the moment a real device turned out to need the
    second behavior.

    Args:
        config: Loaded daemon configuration.
        once: If True, make exactly one connection attempt and exit after a
            single reading (or once_timeout seconds without one), instead
            of retrying indefinitely -- for an on-demand capture run by
            hand right before (or while) taking a reading.
        once_timeout: Seconds to wait for one reading before giving up (also
            used as the discovery timeout in --once mode). Only used when
            ``once`` is True.
        mqtt_config: Optional MQTT publishing configuration. If enabled,
            each reading is also published as JSON. A broker outage is
            logged and non-fatal -- it never blocks local recording.
        profiles_config: Optional who-was-this tagging. If enabled, each
            reading triggers a background notification (ntfy or dunstify,
            chosen based on ``api_config.enabled``) asking which profile it
            belongs to.
        api_config: Determines which profile-notification path is used.

    Returns:
        True if at least one reading was recorded.
    """
    address = config.address
    store = ReadingStore(config.db_path)
    stop_event = asyncio.Event()
    reading_received = False

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    _LOGGER.info(
        "Starting health-thermometer-daemon %s%s%s",
        __version__,
        f" for device at {address}" if address else " (auto-discovering device)",
        f" (once, {once_timeout}s timeout)" if once else "",
    )

    async with _mqtt_connection(mqtt_config) as mqtt_client:
        background_tasks: list[asyncio.Task] = []

        while not stop_event.is_set():
            if not address:
                discovery_timeout = float(once_timeout) if once else 60.0
                try:
                    address = await discover_device(discovery_timeout)
                except TimeoutError as exc:
                    _LOGGER.warning(str(exc))
                    if once:
                        break
                    await _sleep_or_stop(config.retry_seconds, stop_event)
                    continue
                persist_discovered_address(config.config_path, address)
                _LOGGER.info("Discovered device at %s - saved to %s", address, config.config_path)

            connect_timeout = float(once_timeout) if once else 15.0
            read_timeout = float(once_timeout) if once else 15.0
            try:
                reading, device_info = await _attempt_one_reading(
                    address, connect_timeout, read_timeout
                )
            except _RETRYABLE_BLE_ERRORS as exc:
                _LOGGER.warning("Connection/read attempt failed: %s", exc)
                if once:
                    break
                _LOGGER.info("Retrying in %ss", config.retry_seconds)
                await _sleep_or_stop(config.retry_seconds, stop_event)
                continue

            row = _reading_to_row(reading, device_info, address)
            row_id = store.record(**row)
            reading_received = True
            _LOGGER.info(
                "Recorded reading from %s: %.1f°%s",
                address,
                row["value"],
                row["unit"],
            )
            if mqtt_client is not None:
                background_tasks.append(
                    asyncio.create_task(
                        _publish_reading(mqtt_client, mqtt_config, address, row)
                    )
                )
            if profiles_config.enabled:
                background_tasks.append(
                    asyncio.create_task(
                        _prompt_for_profile(
                            config.db_path, row_id, row, profiles_config, api_config
                        )
                    )
                )

            if once:
                break

        if background_tasks:
            # A pending dunstify prompt can legitimately take up to its
            # configured timeout to resolve, and a retrying ntfy publish can
            # take up to its own worst-case retry budget; give either that
            # long instead of cutting it off at the same 5s used for quick
            # MQTT publishes, especially in --once mode where the loop exits
            # as soon as the reading is recorded.
            if profiles_config.enabled and not api_config.enabled:
                wait_timeout = profiles_config.dunstify_timeout_seconds + 5
            elif profiles_config.enabled and api_config.enabled:
                wait_timeout = _NTFY_MAX_RETRY_SECONDS
            else:
                wait_timeout = 5
            await asyncio.wait(background_tasks, timeout=wait_timeout)

    store.close()
    return reading_received


def _check_config(config_path: str) -> int:
    """Validate a config file against every section loader, without running.

    Args:
        config_path: Path to the INI configuration file.

    Returns:
        0 if the file is valid (a summary is printed), 1 otherwise (each
        error is printed).
    """
    if not Path(config_path).is_file():
        print(f"Error: Config file not found: {config_path}")
        return 1

    errors: list[str] = []
    daemon_config = report_config = None
    mqtt_config = alert_config = api_config = profiles_config = None

    try:
        daemon_config = load_config(config_path)
    except ConfigError as exc:
        errors.append(str(exc))
    try:
        report_config = load_report_config(config_path)
    except ConfigError as exc:
        errors.append(str(exc))
    try:
        mqtt_config = load_mqtt_config(config_path)
    except ConfigError as exc:
        errors.append(str(exc))
    try:
        alert_config = load_alert_config(config_path)
    except ConfigError as exc:
        errors.append(str(exc))
    try:
        api_config = load_api_config(config_path)
    except ConfigError as exc:
        errors.append(str(exc))
    try:
        profiles_config = load_profiles_config(config_path)
    except ConfigError as exc:
        errors.append(str(exc))

    if (
        not errors
        and profiles_config.enabled
        and api_config.enabled
        and not profiles_config.ntfy_url
    ):
        errors.append(
            "profiles.enabled = yes with [api] enabled requires profiles.ntfy_url to be set"
        )

    orphaned_profiles: list[str] = []
    profile_details_valid = 0
    if not errors:
        tagged_profiles = get_distinct_profiles(daemon_config.db_path)
        orphaned_profiles = sorted(tagged_profiles - set(profiles_config.names))

        for name in profiles_config.names:
            try:
                load_profile_details(config_path, name)
                profile_details_valid += 1
            except ConfigError as exc:
                errors.append(str(exc))

    if errors:
        print(f"{config_path}: INVALID")
        for error in errors:
            print(f"  - {error}")
        return 1

    print(f"{config_path}: OK")
    print(
        "  daemon: address="
        f"{daemon_config.address or '(auto-discover)'} "
        f"retry_seconds={daemon_config.retry_seconds}"
    )
    print(f"  storage: db_path={daemon_config.db_path}")
    print(f"  daemon: log_level={daemon_config.log_level}")
    print(
        "  report: unit="
        f"{report_config.unit} date_format={report_config.date_format} "
        f"page_size={report_config.page_size}"
    )
    print(
        "  mqtt: enabled="
        f"{'yes' if mqtt_config.enabled else 'no'} "
        f"host={mqtt_config.host or '(unset)'} port={mqtt_config.port}"
    )
    print(
        "  alerting: enabled="
        f"{'yes' if alert_config.enabled else 'no'} "
        f"stale_after_days={alert_config.stale_after_days} "
        f"high_temp_alert_celsius={alert_config.high_temp_alert_celsius} "
        f"low_temp_alert_celsius={alert_config.low_temp_alert_celsius} "
        f"urls={len(alert_config.apprise_urls)}"
    )
    print(
        "  api: enabled="
        f"{'yes' if api_config.enabled else 'no'} "
        f"host={api_config.host} port={api_config.port} "
        f"token={'(set)' if api_config.token else '(none)'}"
    )
    print(
        "  profiles: enabled="
        f"{'yes' if profiles_config.enabled else 'no'} "
        f"names={len(profiles_config.names)} "
        f"path={'ntfy' if api_config.enabled else 'dunstify'} "
        f"details_valid={profile_details_valid}/{len(profiles_config.names)}"
    )
    if orphaned_profiles:
        print(
            "  warning: readings tagged with profile(s) not in profiles.names: "
            f"{', '.join(orphaned_profiles)} (still filterable via --profile, "
            "but there's no way to re-tag them via ntfy/dunstify anymore -- "
            "add them back to profiles.names if this wasn't intentional)"
        )
    if is_insecurely_exposed(api_config):
        print(
            f"  warning: api.host is {api_config.host!r} (not loopback) but "
            "api.token is unset -- anyone who can reach this address can read "
            "readings and generate reports. Set api.token, or bind to 127.0.0.1."
        )
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="health-thermometer-daemon",
        description=(
            "Standalone BLE daemon that logs standard Bluetooth Health "
            "Thermometer Profile readings to a local SQLite database."
        ),
    )
    parser.add_argument(
        "-c", "--config", required=True, help="Path to the daemon's INI configuration file"
    )
    parser.add_argument(
        "-k",
        "--check-config",
        action="store_true",
        help="Validate the config file and exit, without starting the daemon",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging (overrides the config file's log level)",
    )
    parser.add_argument(
        "-o",
        "--once",
        action="store_true",
        help=(
            "Make one connection attempt, record a single reading, and exit, "
            "instead of retrying indefinitely (run by hand right before "
            "taking a reading, instead of a long-running service)"
        ),
    )
    parser.add_argument(
        "-w",
        "--once-timeout",
        dest="once_timeout",
        type=int,
        default=60,
        metavar="SECONDS",
        help=(
            "Seconds to wait for discovery/connect/a reading in --once mode "
            "(default: %(default)s)"
        ),
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

    if args.check_config:
        return _check_config(args.config)

    try:
        config = load_config(args.config)
        mqtt_config = load_mqtt_config(args.config)
        profiles_config = load_profiles_config(args.config)
        api_config = load_api_config(args.config)
    except ConfigError as exc:
        logging.basicConfig(level=logging.ERROR)
        _LOGGER.error(str(exc))
        return 1

    if profiles_config.enabled and api_config.enabled and not profiles_config.ntfy_url:
        logging.basicConfig(level=logging.ERROR)
        _LOGGER.error(
            "profiles.enabled = yes with [api] enabled requires profiles.ntfy_url to be set"
        )
        return 1

    log_level = "DEBUG" if args.verbose else config.log_level
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        reading_received = asyncio.run(
            run_daemon(
                config,
                once=args.once,
                once_timeout=args.once_timeout,
                mqtt_config=mqtt_config,
                profiles_config=profiles_config,
                api_config=api_config,
            )
        )
    except (TimeoutError, ConfigError) as exc:
        _LOGGER.error(str(exc))
        return 1
    except KeyboardInterrupt:
        return 0

    if args.once and not reading_received:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
