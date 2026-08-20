# health-thermometer-daemon

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white) ![Bash](https://img.shields.io/badge/shell-Bash-4EAA25?logo=gnu-bash&logoColor=white) ![Docker](https://img.shields.io/badge/container-Docker-2496ED?logo=docker&logoColor=white) ![Bluetooth LE](https://img.shields.io/badge/Bluetooth-LE-0082FC?logo=bluetooth&logoColor=white)

[![License: GPL-3.0](https://img.shields.io/badge/license-GPL--3.0-blue)](https://github.com/home-health-hub/health-thermometer-daemon/blob/main/LICENSE) [![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/home-health-hub/health-thermometer-daemon#contributing) [![Discussions](https://img.shields.io/badge/discussions-welcome-blue)](https://github.com/home-health-hub/health-thermometer-daemon/discussions)

A standalone Linux daemon that connects to any standard Bluetooth **Health
Thermometer Profile** device over Bluetooth Low Energy (BLE) and logs its
readings to a local SQLite database. No cloud account, no companion app, no
Home Assistant required.

It's a thin wrapper around the
[`health-thermometer-ble`](https://github.com/home-health-hub/health-thermometer-ble)
library, packaged to run unattended as a `systemd` service on something like
a Raspberry Pi sitting near the thermometer.

> [!WARNING]
> **Not yet tested against real hardware.** This daemon's own
> `health-thermometer-ble` dependency has not been run against a real
> thermometer yet (see that package's own README warning banner and
> `CLAUDE.md`). This daemon's main run loop was written to work correctly
> under either possible answer to an open question about that device
> class's BLE connection lifecycle -- see
> [CLAUDE.md](CLAUDE.md#open-questions) for the reasoning -- but the whole
> stack, from discovery through a recorded reading, is unverified end to
> end. Treat it as logically-correct-on-paper until that's happened. This
> notice will be removed once confirmed.

**Disclaimer: This is an unofficial, independently developed project, built
directly against the public Bluetooth SIG Health Thermometer Profile
specification. It is not affiliated with, officially maintained by, or in
any way officially connected with any thermometer manufacturer. Nothing
here is medical advice; the fever/hypothermia band labels and alerting are
informational only. Talk to a doctor about your temperature readings.**

## Supported devices

Any device implementing the standard Bluetooth SIG Health Thermometer
Service (`0x1809`) should work, not a hardcoded product list -- see
[`health-thermometer-ble`](https://github.com/home-health-hub/health-thermometer-ble#protocol-notes)
for which devices' protocol has actually been cross-confirmed against real
client code so far (iHealth TS28B, Beurer FT95).

## Features

- Scans for the device on first run, then pins its BLE address into the
  config file so future restarts connect directly instead of re-scanning
- Records every reading (value, unit, optional body-site/timestamp/battery
  fields) to a local SQLite database
- Runs as a `systemd` service with automatic restart on failure
- Optional PDF/CSV reports shaded by fever/hypothermia band, with a
  temperature trend chart and a choice of table layout (full, compact, or a
  weekly/monthly rollup for long histories)
- Optional Apprise-based alerting on stale data, fever-range readings, or
  (if configured) hypothermia-range readings
- Optional read-only HTTP API and MQTT publishing
- Optional "who was this?" profile tagging for a device shared by more than
  one person, via ntfy or dunstify -- there's no device-side "user slot" at
  all for this device class, unlike a shared blood-pressure monitor or
  scale, so every reading starts fully unidentified until tagged
- Optional per-profile report personalization (name/email/notes, preferred
  unit/date format/page size)
- Optional per-profile alert routing and fever-threshold overrides, so
  different people's alerts can go to different places

## Installation

Requires Python 3.11+.

### Quick install

```bash
git clone https://github.com/home-health-hub/health-thermometer-daemon.git
cd health-thermometer-daemon
sudo ./install.sh
```

This creates a venv at `/opt/health-thermometer-daemon`, installs the
package from the checkout, seeds `/etc/health-thermometer-daemon/config.ini`
(if it doesn't already exist), creates a `health-thermometer-daemon` system
user, and installs and enables the systemd service. It also installs (but
does not enable) the
[scheduled report generation](#scheduled-report-generation) and
[alerting](#alerting) timer units, and the [HTTP API](#http-api) service.
It's safe to re-run: it skips steps that are already done. Edit the config
and `sudo systemctl restart health-thermometer-daemon` afterward.

`config.ini` can hold real secrets (ntfy/API tokens, `apprise_urls` with
embedded credentials), so `install.sh` sets it to mode `600`, owned by the
`health-thermometer-daemon` user, every time it runs (including on
re-runs, in case it was ever loosened). Running the CLI tools by hand
afterward needs `sudo -u health-thermometer-daemon`, e.g.:

```bash
sudo -u health-thermometer-daemon health-thermometer-report --config /etc/health-thermometer-daemon/config.ini
```

### Manual install

```bash
python3 -m venv /opt/health-thermometer-daemon/venv
/opt/health-thermometer-daemon/venv/bin/pip install /path/to/health-thermometer-daemon  # this checkout
```

#### Config file

```bash
sudo mkdir -p /etc/health-thermometer-daemon
sudo cp config/health-thermometer-daemon.ini.example /etc/health-thermometer-daemon/config.ini
sudo "$EDITOR" /etc/health-thermometer-daemon/config.ini
```

Leave `[daemon] address` empty to auto-discover the device on first run
(power it on / take a reading while the daemon is scanning). Once found,
the daemon writes the address back into this file so it reconnects
directly on every future start. See
[config/health-thermometer-daemon.ini.example](config/health-thermometer-daemon.ini.example)
for every setting, with inline documentation.

Validate a config file without starting the daemon:

```bash
health-thermometer-daemon --config /etc/health-thermometer-daemon/config.ini --check-config
```

#### systemd service

```bash
sudo cp systemd/health-thermometer-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now health-thermometer-daemon
journalctl -u health-thermometer-daemon -f
```

### Scheduled report generation

Optional and not enabled by default. Generates a timestamped PDF into
`/var/lib/health-thermometer-daemon/reports/` on a schedule:

```bash
sudo cp systemd/health-thermometer-report-generate.service systemd/health-thermometer-report-generate.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now health-thermometer-report-generate.timer
```

Defaults to `OnCalendar=weekly`. Configure via the `HEALTH_THERMOMETER_CONFIG`
and `HEALTH_THERMOMETER_REPORT_DIR` environment variables in the `.service`
unit rather than flags, since the timer invokes it with a fixed command line.

### Alerting

Also optional and not enabled by default.
`health-thermometer-alert-check` checks every device's most recent reading
for three conditions and notifies via
[Apprise](https://github.com/caronc/apprise) (100+ supported services:
Discord, Telegram, Slack, email, Pushover, generic webhooks, etc.) when
triggered:

- **Staleness**: no reading in over `stale_after_days` days.
- **Fever range**: the latest reading is at or above
  `high_temp_alert_celsius`.
- **Hypothermia range**: the latest reading is at or below
  `low_temp_alert_celsius`, if set (unlike the fever threshold, this check
  is off by default).

```ini
[alerting]
enabled = yes
apprise_urls = tgram://bot_token/chat_id, mailto://user:password@gmail.com
stale_after_days = 2
high_temp_alert_celsius = 38.0
low_temp_alert_celsius = 35.0
```

Run it periodically with the bundled timer:

```bash
sudo cp systemd/health-thermometer-alert-check.service systemd/health-thermometer-alert-check.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now health-thermometer-alert-check.timer
```

Defaults to `OnCalendar=hourly`. A repeat staleness alert is throttled to at
most once per day while the condition persists; a fever- or
hypothermia-range alert only fires once per newly-arrived reading, not on
every check. State is tracked per device address in `alerting.state_path`
(default `/var/lib/health-thermometer-daemon/alert-state.json`). Delete it
to reset throttling. `--check-config` reports whether `[alerting]` is
enabled and how many URLs it parsed, without actually sending anything.

If a reading is tagged with a profile (see [Profiles](#profiles)), that
profile's `[profile.<name>]` section can override the destination and
fever threshold just for its own alerts; see
[Per-profile alert routing](#per-profile-alert-routing).

### HTTP API

Also optional and not enabled by default. `health-thermometer-api` runs a
small read-only HTTP server exposing the same data as the other tools. It
reads the SQLite database directly and works whether or not the daemon is
currently running.

```ini
[api]
enabled = yes
host = 127.0.0.1
port = 8080
token =
```

```bash
sudo cp systemd/health-thermometer-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now health-thermometer-api.service
```

Endpoints:

All routes are versioned under `/api/v1/`.

| Method & path | Description |
|---|---|
| `GET /api/v1/health` | Unauthenticated liveness check: `{"status": "ok", "version": "..."}`. |
| `GET /api/v1/capabilities` | Unauthenticated description of what this daemon measures, its profile model, timestamp semantics, and MQTT status. |
| `GET /api/v1/latest[?address=...&profile=...]` | Most recent reading per device, as JSON. |
| `GET /api/v1/report[?format=pdf\|csv&period=...&from=...&to=...&address=...&profile=...]` | Generates a report on demand using the same `[report]` config as `health-thermometer-report`, returned as a file download. |
| `GET`/`POST /api/v1/assign-profile?id=...&profile=...[&confirm=1]` | Tags a reading with a profile name (see [Profiles](#profiles)). |

```bash
curl http://127.0.0.1:8080/api/v1/latest
curl -o report.pdf "http://127.0.0.1:8080/api/v1/report?period=30d"
```

**There's no TLS built in.** `host` defaults to `127.0.0.1` (loopback only)
for a reason: don't bind it to `0.0.0.0` or a LAN-facing interface without
putting a reverse proxy (with TLS and its own auth) in front of it. Setting
`api.token` requires an `Authorization: Bearer <token>` header on every
endpoint except `/api/v1/health` and `/api/v1/capabilities`, which is worth
doing even on loopback if other local users/processes on the same host
shouldn't see readings:

```bash
curl -H "Authorization: Bearer <token>" http://127.0.0.1:8080/api/v1/latest
```

If `api.host` isn't a loopback address and `api.token` is blank, both
`health-thermometer-api` (at startup) and `--check-config` print a warning.
It's not blocked outright, since a reverse proxy handling auth in front is
a legitimate setup, but forgetting to set a token before exposing the API
on the LAN is a plausible mistake worth surfacing rather than letting it
pass silently.

### Profiles

For a device shared by more than one person: `[profiles]` asks "who was
this?" after each reading and tags it. Unlike a shared blood-pressure
monitor or scale, this device class has no device-side "user slot" at all
-- a thermometer reading has no identity of its own, so every reading
starts fully unidentified. This mirrors
[`etekcity-scale-daemon`](https://github.com/home-health-hub/etekcity-scale-daemon)'s
and
[`etekcity-bp-daemon`](https://github.com/home-health-hub/etekcity-bp-daemon)'s
profile systems.

```ini
[profiles]
enabled = yes
names = Alice, Bob, Charlie
```

Two delivery paths, chosen automatically based on whether `[api]` is enabled:

- **`[api]` enabled**: an [ntfy](https://ntfy.sh) notification (Android/iOS
  apps, or any browser) with one HTTP action button per name in
  `profiles.names`. Tapping a button hits this API's `/api/v1/assign-profile`
  endpoint directly, tagging that specific reading. Requires
  `profiles.ntfy_url` (and `profiles.api_base_url` pointing at wherever the
  API is actually reachable from your phone/desktop; `127.0.0.1` only works
  if ntfy and the API run on the same machine).
- **`[api]` disabled**: a local [dunstify](https://dunst-project.org) prompt
  instead, since ntfy's action buttons would have nothing to call back to
  without the API running. This resolves synchronously and tags the reading
  directly, no network round-trip. It needs the `dunst` notification daemon
  and a real desktop/D-Bus session, which makes it a better fit for running
  the daemon on your own desktop or laptop than an unattended headless Pi
  (the usual deployment for this daemon), so ntfy is the practical choice
  there.

```bash
curl "http://127.0.0.1:8080/api/v1/assign-profile?id=42&profile=Alice"
```

If `profiles.assign_window_seconds` is set, this fails with `409` for a
reading older than that window: a safety net for delayed ntfy notifications
(tapped long after connectivity returns, potentially tagging a now-stale
reading someone's forgotten about) rather than a limit on manual
corrections. Add `&confirm=1` to tag an old reading on purpose:

```bash
curl "http://127.0.0.1:8080/api/v1/assign-profile?id=42&profile=Alice&confirm=1"
```

`--check-config` cross-checks `profiles.names` against the database: if a
name was removed or renamed but readings tagged with the old name still
exist, it prints a warning (not an error; the exit code stays `0`) so that
history doesn't just silently stop being explainable.

#### Per-profile report personalization

Give a profile its own `[profile.<name>]` section (name/email, notes, and
report preferences), and `health-thermometer-report --profile <name>` /
the API's `?profile=` will use it:

```ini
[profile.Alice]
name = Alice Smith
email = alice@example.com
notes = Recovering from flu
unit = f
```

- `name`/`email`/`notes` print below the report title, handy when handing
  a printed report to a doctor (`notes` for clinical context).
- `unit`/`date_format`/`page_size` each independently override the matching
  `[report]` setting for this profile's reports only, so one household
  member can see Celsius while another sees Fahrenheit.

#### Per-profile alert routing

The same `[profile.<name>]` section can also override `[alerting]` for
alerts triggered by that profile's readings (untagged readings always use
the global values):

```ini
[profile.Alice]
apprise_urls = tgram://bot_token/alice_chat_id
stale_after_days = 1
high_temp_alert_celsius = 38.5
```

- `apprise_urls` **replaces** the global `[alerting] apprise_urls` for this
  profile's alerts rather than adding to it, so Alice's alerts go to her
  phone and Bob's go to his instead of everyone seeing a shared feed.
  Leave blank to just use the global list.
- `stale_after_days`/`high_temp_alert_celsius` override the matching
  `[alerting]` value for this profile only; leave blank to inherit it.
- `low_temp_alert_celsius` is never overridden per profile -- it's a fixed
  medical threshold, not a personal preference.

None of this is required. A profile with no `[profile.<name>]` section at
all still tags and reports/alerts normally, just without the
personalization. `--check-config` validates every configured profile's
section and reports how many parsed cleanly (`details_valid=N/M`).

## Manual usage

### On-demand capture instead of a long-running service

```bash
health-thermometer-daemon --config /etc/health-thermometer-daemon/config.ini --once --once-timeout 60
```

Connects, waits up to `--once-timeout` seconds for a single reading, records
it, and exits. Exit code is `1` if no reading arrived in time. For when you'd
rather not run the daemon continuously: start it by hand right before (or
while) taking a reading, instead of taking the reading and finding nothing
was listening.

## Database schema

One `readings` table, one row per completed measurement:

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER | Primary key |
| `recorded_at` | TEXT | ISO-8601 UTC timestamp of when the daemon received the reading |
| `measured_at` | TEXT | ISO-8601 timestamp from the device's own optional embedded date-time field, NULL if the device didn't include one |
| `address` | TEXT | Device BLE address |
| `profile` | TEXT | Tagged profile name (see [Profiles](#profiles)), NULL until answered |
| `value` | REAL | Temperature value, exactly as the device reported it -- no unit conversion at storage time |
| `unit` | TEXT | "C" or "F", per the device's own Flags byte |
| `temperature_type` | TEXT | Optional body-site string (e.g. "Ear (usually ear lobe)"), if the device included one |
| `battery_percent` | INTEGER | Battery level from the device's Battery Service, if exposed |
| `error_code` | TEXT | Reserved for future use; currently always NULL |

## Reports

```bash
# Every reading on record
health-thermometer-report --config /etc/health-thermometer-daemon/config.ini

# Preset ranges: 7d, 30d, 90d, 1y, all (default: all)
health-thermometer-report --config /etc/health-thermometer-daemon/config.ini --period 30d

# Explicit date range (--to defaults to now if omitted)
health-thermometer-report --config /etc/health-thermometer-daemon/config.ini --from 2026-01-01 --to 2026-03-01

# Point directly at a database file instead of a config
health-thermometer-report --db /var/lib/health-thermometer-daemon/readings.db --format csv --output report.csv

# Restrict to one profile
health-thermometer-report --config /etc/health-thermometer-daemon/config.ini --profile Alice
```

PDF reports include a temperature trend chart, a reading table shaded by
fever/hypothermia band, and (if `report.include_summary = yes`) an
average/min/max summary with a category breakdown, handy to print and
bring to a doctor's appointment. `--profile <name>` (requires `--config`)
also personalizes the report from that profile's `[profile.<name>]`
section; see
[Per-profile report personalization](#per-profile-report-personalization).

`report.include_chart`/`include_table` independently toggle the chart and
table off if you don't want them, and `report.table_layout` picks the
table's shape: `full` (one row per reading, the default), `compact` (same
per-reading detail, packed into 3 side-by-side column groups), or `rollup`
(one row per week/month: avg/min/max, reading count, and the worst
fever/hypothermia band seen that period). For a long history, `rollup`
paired with the chart is generally more useful than paging through a year
of individual readings.

**Reports spanning more than one person are split per person, not blended.**
If a report's rows include more than one distinct profile (or untagged
readings from more than one physical device), averaging everyone's
temperature together into one number would be medically meaningless, so
the chart gets one colored line per person (with a legend), the summary
prints one avg/min/max block per person, and the `rollup` layout adds a
"Who" column and buckets by `(period, person)` instead of just `(period)`.
The full/compact per-reading tables already label each row via the "Who"
column (`report.include_profile = yes`), so they're unaffected. This is the
right default for a household report shared by everyone using the device,
but if you want a single person's data instead (e.g. to bring to a doctor's
appointment), pass `--profile <name>` (or `?profile=` via the API) rather
than filtering the combined report after the fact.

## Pruning old data

```bash
# See how many readings older than 365 days would be deleted
health-thermometer-prune --config /etc/health-thermometer-daemon/config.ini --older-than 365

# Actually delete them (also reclaims disk space with VACUUM)
health-thermometer-prune --config /etc/health-thermometer-daemon/config.ini --older-than 365 --yes
```

## MQTT

```ini
[mqtt]
enabled = yes
host = mqtt.example.com
topic_prefix = health_thermometer_daemon
```

Each reading publishes as JSON to `<topic_prefix>/<device address>/state`. A
broker outage is logged and non-fatal; it never blocks local recording to
SQLite.

## Troubleshooting

- **Device never discovered**: make sure it's powered on / a reading is
  taken while the daemon is scanning, and that no other app (e.g. a
  manufacturer's own companion app, nRF Connect) is already connected to it
  -- most of these devices only accept one connection at a time.
- **`No Bluetooth scanner available`**: check `bluetoothctl` shows an
  adapter, and that the `health-thermometer-daemon` system user is in the
  `bluetooth` group (the systemd unit sets `SupplementaryGroups=bluetooth`).
- **Config errors**: run `--check-config` for a section-by-section report of
  what's wrong.

## Acknowledgments

- Built on
  [`health-thermometer-ble`](https://github.com/home-health-hub/health-thermometer-ble),
  which implements the public Bluetooth SIG Health Thermometer Profile
  directly rather than a manufacturer-proprietary protocol.
- Project layout modeled on
  [`etekcity-bp-daemon`](https://github.com/home-health-hub/etekcity-bp-daemon)
  and
  [`etekcity-scale-daemon`](https://github.com/home-health-hub/etekcity-scale-daemon).
- Code review, implementation, and documentation assisted by
  [Claude](https://www.anthropic.com/claude).

## Contributing

Contributions are welcome!

- **Bug reports**: [Open an issue](https://github.com/home-health-hub/health-thermometer-daemon/issues).
- **Everything else** (questions, feature requests, ideas, general discussion): [Use Discussions](https://github.com/home-health-hub/health-thermometer-daemon/discussions).
- Pull requests are welcome for bug fixes or discussed features.

## License

This project is licensed under the **GNU General Public License v3.0**.

See [LICENSE](LICENSE) for more information.
