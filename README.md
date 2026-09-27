# Is It Dead? — device health monitoring

Home Assistant / HACS integration for battery sensors and other physical devices.
Version 1.2.0 separates **persistent unavailability**, **low battery**, **weak
signal**, and **insufficient evidence**. It cannot prove that a device is physically
broken or distinguish an empty battery from a failed radio/gateway.



## Installation

Add `https://github.com/ThePastPlayer/IsItDead` as a custom HACS integration
repository, download, restart Home Assistant, then add **Is It Dead?** under
Settings → Devices & services. The sidebar panel groups devices by room.
For manual installation, copy `custom_components/is_it_dead` into your configuration.

## Detection

- Device discovery includes registered entities whose state is temporarily missing.
  Newly discovered devices are subscribed on the next periodic scan (15 minutes by default).
- Both unchanged state reports and changed values are observed. Explicit `last_seen`
  has priority. Restored, unavailable and unknown states are not fresh evidence.
- Five-minute startup grace; anomalies must persist for at least one monitoring
  interval before becoming a probable offline alert.
- Automatic silence thresholds need at least eight intervals and the configured
  learning period (seven days by default). The upper reporting-interval percentile
  is multiplied by the configured margin. Known slow reports are not clipped below
  their observed cadence. Manual per-entity timeouts are exact, in hours.
- Motion/contact binary sensors do not acquire silence deadlines from inactivity
  unless an explicit last_seen or manual deadline is available.
- Battery warnings: at or below 20 percent, or a native battery-low binary sensor.
  Voltage is not percentage. Battery Notes supplies the replacement battery type.
- Weak signal: sustained RSSI ≤ -85 dBm or linkquality ≤ 40/255. These are
  indicative thresholds, not proof of a disconnection. A new weak report is required
  between checks; readings older than 24 hours cannot raise a signal warning.
- No speculative battery lifetime countdown; no recorder change-history backfill
  presented as physical heartbeat history; no automatically selected exclusions.

`last_reported` proves an integration wrote a value, **not necessarily receipt of
an actual device packet**. Integrations that continually republish a cache need a
real last_seen/availability signal for reliable offline detection. Unknown coverage
is displayed rather than silently labelled healthy. Initial heartbeat learning
restarts when migrating from the old unreliable averages; exclusions and snoozes remain.

## Reports and controls

The **À vérifier** panel filter collects probable offline devices, anomalies awaiting
confirmation, low batteries and weak signals. Each card explains the evidence.
`binary_sensor.is_it_dead_alert` is on for confirmed offline, low battery or persistent
weak signal (unless snoozed). Its attributes include `dead_devices`, `dead_device_ids`,
`low_battery_devices`, `weak_signal_devices` and stable `alert_device_ids`.

The installed automation blueprint targets an actual mobile-app device and reacts
when the affected device list changes, even if the aggregate remains on. Its push
opens `/is_it_dead` and offers a reversible 24-hour snooze. Create a blueprint
notification automation to enable pushes; installing the integration alone does not
send them. Existing dashboard service controls remain available:

- `snooze_device`, `exclude_device`, `relearn_device`: `device_id`.
- Legacy `snooze_entity`, `exclude_entity`, `relearn_entity`: `entity_id`.
- `set_manual_timeout`: `entity_id`, `timeout_hours` (0 removes the override).

## Development

Tests use Home Assistant 2026.7.2 with pytest and pytest-asyncio, Python 3.14 on Linux.
Install `requirements-test.txt` in an isolated environment.
Run `python -m pytest -q` and `node --check custom_components/is_it_dead/frontend/is_it_dead_panel.js`.
Live device identifiers, credentials and diagnostic dumps must not be committed.

## Native Zigbee monitoring (1.2)

ZHA: reads the actual zigpy last radio contact and ZHA availability.
Zigbee2MQTT: discovers topics from MQTT discovery and follows native availability,
bridge health and explicit `last_seen`. Enable availability and set
`advanced.last_seen: ISO_8601` in Zigbee2MQTT (restart may be required).
MQTT delivery or a retained cached value alone is never counted as a new radio packet.

The **Vérifier Zigbee** button calls `is_it_dead.check_device` with `device_id`.
ZHA reads a Basic cluster attribute without cache. Zigbee2MQTT only requests a
read-only property explicitly advertised with GET support by its converter.
A positive reply confirms contact; no reply is inconclusive. Reads are limited
to one concurrent request, 12 seconds and a five-minute cooldown per device.
Snooze only mutes alerts; passive monitoring continues. There is no repeated
reconfiguration, forced pairing, binding or automatic network-map scan.

Battery devices which sleep cannot be awakened remotely. Zigbee2MQTT defaults
to a 25-hour passive availability deadline; powered devices can be actively checked.
A topology map may contain old entries and is not proof that a battery device is
alive. Devices with no periodic heartbeat need a model-specific deadline or a
physical functional check; no software can eliminate that uncertainty.

The panel keeps expanded entity lists mounted during state refreshes. Install
frontend test dependencies with `npm ci`, then run `npm test`.
