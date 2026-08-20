# Project notes for health-thermometer-daemon

## Related repos to watch

- **health-thermometer-ble** -- https://github.com/home-health-hub/health-thermometer-ble
  -- this daemon's own BLE protocol library, pulled as a `git+https`
  dependency in `pyproject.toml` (not a versioned PyPI release). A fix or
  feature added there doesn't reach this daemon automatically: it needs
  `pip install --upgrade` to pick it up. Unlike this org's other
  driver/daemon pairs, that library targets the public Bluetooth SIG
  Health Thermometer Profile generically rather than one manufacturer's
  device -- see its own `CLAUDE.md` for what that means for this daemon
  (no single "the device" to test against; any compliant thermometer
  should work).

- **etekcity-bp-daemon** -- https://github.com/home-health-hub/etekcity-bp-daemon
  -- the architecture template this daemon's project layout, config
  sections, and profile-tagging system were deliberately modeled on. Not a
  code dependency, just a design reference: if that project adopts a new
  pattern worth borrowing, it's worth checking. The two diverge in a few
  places that matter (see below), so don't blindly copy without adjusting
  for them.

- **etekcity-scale-daemon** -- https://github.com/home-health-hub/etekcity-scale-daemon
  -- the other sibling using the same "no device-side user slot, so ask
  a human" profile design this daemon also uses.

## How this daemon differs from its blood-pressure sibling

- **No user slots.** `etekcity-bp-daemon`'s device reports one of two
  hardware "user slots" per reading; a Health Thermometer Profile device
  has no such concept at all -- every reading arrives fully unidentified.
  `profile` starts `NULL` on every insert (see `_reading_to_row` in
  `cli.py`), not derived from anything device-side.
- **One metric, not two.** There's no systolic/diastolic pair -- the chart
  in `report.py` draws one line per person, not two, and there's no
  goal-progress feature (a blood-pressure goal like "keep it under
  130/80" doesn't have a temperature analogue worth building yet).
- **Fever/hypothermia bands, not AHA categories.** `categories.py`'s
  thresholds are a common clinical rule of thumb (CDC/Mayo Clinic style),
  not a single authoritative medical standard the way the AHA's
  blood-pressure categories are -- see that module's own docstring.

## Open questions

- **Main-loop connection lifecycle (the big one).** `cli.py`'s
  `run_daemon` reconnects for every single reading (connect -> read one
  measurement -> disconnect -> repeat) instead of holding one persistent
  connection open the way `etekcity-bp-daemon`'s `BloodPressureMonitor`
  does (subscribe once, stay connected). This was a deliberate choice, not
  an oversight: whether a Health Thermometer Profile device stays
  connectable indefinitely, or is only briefly connectable around a
  measurement button-press, is unconfirmed -- nobody has tested
  `health-thermometer-ble`'s driver against real hardware yet (see that
  repo's own `CLAUDE.md`). The reconnect-per-reading shape works correctly
  under *either* assumption; a single long-lived connect-and-subscribe
  would silently stop working the moment a real device turned out to need
  the second behavior. See `run_daemon`'s docstring in `cli.py` for the
  full reasoning. Once real hardware is available: confirm which behavior
  the device(s) actually exhibit, and if it turns out to stay connectable
  indefinitely, consider whether switching to a persistent-connection
  model would meaningfully reduce reconnect overhead/battery use enough to
  be worth the added complexity (retry/reconnect-on-drop logic that the
  current per-reading design gets for free).
- **Whether `--once`'s short-lived-connection retry interacts badly with
  a device that takes many seconds to complete a single measurement
  cycle.** `_attempt_one_reading` uses the same timeout for both connect
  and read; unverified whether that's generous enough across real devices
  once one is available to test against.
- **Discovery reliability.** Same caveat `health-thermometer-ble`'s own
  `CLAUDE.md` documents: `discover()` filters by advertised Health
  Thermometer Service UUID, and it's unconfirmed whether every target
  device actually advertises that UUID in scan responses (some BLE stacks
  only expose services after connecting). If discovery comes up empty
  against real hardware, this daemon has no fallback path (e.g. connect by
  address without a UUID-filtered scan) -- worth adding if that turns out
  to be needed.
- **Profile-tagging UX under the reconnect-per-reading loop.** Each
  reading triggers a background ntfy/dunstify prompt (`_prompt_for_profile`
  in `cli.py`) concurrently with the *next* connection attempt starting.
  This hasn't been exercised against a real rapid-fire usage pattern (e.g.
  several people taking readings back-to-back) to confirm prompts don't
  pile up confusingly.

## Verification status

See the README's warning banner for current hardware-verification status.
Nothing in this daemon -- or its `health-thermometer-ble` dependency -- has
been run against a real thermometer yet.
