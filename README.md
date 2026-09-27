# Is It Dead? — device health monitoring

Home Assistant / HACS integration for battery sensors and other physical devices.
Version 1.4.0 separates **persistent unavailability**, **low battery**, **weak
signal**, and **insufficient evidence**. It cannot prove that a device is physically
broken or distinguish an empty battery from a failed radio/gateway.



## Installation

Add `https://github.com/ThePastPlayer/IsItDead` as a custom HACS integration
repository, download, restart Home Assistant, then add **Is It Dead?** under
Settings → Devices & services. The sidebar panel groups devices by room.
For manual installation, copy `custom_components/is_it_dead` into your configuration.

## Frontend upgrade compatibility

Version 1.3.2 removes obsolete `is_it_dead_panel.js.gz` / `.br` files left by
older manual installs before registering static routes. Home Assistant otherwise
serves those files to browsers requesting compression, even when the plain JS
is newer. The versioned URL also refreshes existing browser/service-worker caches.

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

## Guided physical test (1.3)

Open **Tester les capteurs à réveiller / reprendre** (administrator account required).
The default checklist contains devices without a reliable response, grouped by
room. You can also select all monitored devices. Confirm the selection,
then review the automations to suspend. Direct device/entity references are
suggested; dynamic templates, scripts and external automations are not guaranteed
to be detected. You can add other automations or deselect safeguards in the review.
Nothing is suspended until **Suspendre la sélection et démarrer** is pressed.
Already-running automation actions continue; only new triggers are suspended.

The mobile full-screen checklist keeps completed rows in place with a green tick
and a struck-through device name, so you can walk through the rooms with your
phone. It records new native radio contacts, generic physical
value changes (limited evidence), and explicit manual confirmations separately.
Cached unchanged reports do not validate a test. A new radio contact proves
communication, not the complete sensor function: physically exercise the sensor
when testing that function. Any monitored device can be selected, including
SwitchBot and devices without a remotely readable Zigbee attribute.

The UI session lasts at most 30 minutes (service API allows 1–120). **Terminer et
réactiver maintenant** restores only automations enabled before the test. Closing
the panel does not end the test: the server still enforces its deadline. A persisted
recovery journal restores suspensions after a Home Assistant restart, once
Home Assistant and its automations are available again. Failed restorations stay
in the journal and retry automatically; their entity IDs are shown on the test
screen. No restoration can run while Home Assistant itself is shut down.

The panel displays **1.3.2** and uses a versioned custom-element name to avoid
reusing a previous panel already registered in the browser. Reload the Home
Assistant app/page after updating. **Vérifier Zigbee** appears only on devices
with a native ZHA/Zigbee2MQTT adapter; **Test guidé** is available on all cards.

### History during a test

This mode suspends selected Home Assistant automations; it does not intercept
sensor events. Normal sensor changes remain visible and recorded in Home Assistant.
Recorder filtering alone would not prevent automation triggers, and disabling an
entity can prevent observing its state or publish its current alarm when restored.
No universal sensor-isolation or history-suppression guarantee is provided.
Return sensors to their normal physical state before ending the tour (for example,
dry a leak probe after testing it). External automation systems are not suspended.

### Panel loading compatibility (1.3.2)

The frontend also registers legacy panel element names, so cached Home Assistant
panel metadata can still mount it. The current module uses a distinct versioned
URL path rather than relying only on a query parameter for cache invalidation.
Multiple module imports do not throw duplicate custom-element registration errors.
Frontend regressions cover HA's create-element / set-properties / append sequence
for both legacy names and the current name.

### Guided automation review (1.3.3)

The review includes Home Assistant references, nested groups, literal template
references, and dependencies observed while rendering templates without executing
actions. Domain-wide templates are labelled as possible links, so conservative
suggestions may include unrelated automations. Unresolved/dynamic branches and
external automation systems cannot be exhaustively detected. Every automation
remains visible for manual selection, with a reason for each suggestion.

## Batteries and notes (1.4.0)

Open **Piles et notes** on a device card to review the proposed type and quantity,
correct them, and write a comment (for example the chemistry or brand installed).
Saving the profile validates these fields without logging a replacement.
**Enregistrer ce remplacement** records the selected date and a snapshot of the
fields in a persistent local history. Backdated replacements are supported; future
dates are rejected. A percentage increase alone never records a replacement.
The device card shows the last replacement age and the current comment.

Suggestions use the Battery Notes community catalogue bundled with this release
(2,328 entries, snapshot 2026-09-27). Manufacturer/model matches are case insensitive;
hardware/model variants are respected and ambiguous entries require manual input.
The catalogue works offline and is refreshed through integration releases.
Existing Battery Notes sensor metadata may prefill the profile and its replacement
date; local confirmed fields take precedence. IsItDead does not write back to
Battery Notes. Profile/history storage is separate from health-learning resets.
These edit/read services require Home Assistant administrator access.

Catalogue attribution: [Battery Notes by Andrew Jackson and contributors](https://github.com/andrew-codechimp/HA-Battery-Notes),
MIT license. The exact upstream revision and license are included in
`custom_components/is_it_dead/data/BATTERY_NOTES_SOURCE.json` and
`BATTERY_NOTES_LICENSE.txt`. The UI and record management are implemented in IsItDead.

### Migrating from Battery Notes

The admin service `is_it_dead.import_battery_notes` defaults to `dry_run: true`.
With `dry_run: false`, it durably copies configured notes and stored replacement
dates into the local book, including records for devices no longer monitored.
Repeated imports are idempotent; conflicting existing local profiles are retained
and the imported note is kept separately. Original source fields and timestamps
are retained in storage. **Carnet des piles** exposes these archive records.
This service never uninstalls Battery Notes: back up and verify the source, audit
references to its entities/services and restore any original sensors it hid before
separately removing the integration. Battery Notes provides its last known
replacement per device; importing cannot reconstruct dates absent from its store.
