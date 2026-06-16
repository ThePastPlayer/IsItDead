# ⚡ Is It Dead? (is_it_dead)

[![HACS Custom Badge](https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge)](https://github.com/hacs/integration)
[![Open your Home Assistant instance and open a repository in HACS.](https://my.home-assistant.io/badge/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ThePastPlayer&repository=IsItDead&category=integration)

**Is It Dead?** is a HACS-compatible Home Assistant custom integration designed to monitor rechargeable battery-powered Zigbee end devices and other sensors for sudden range/battery silences. 

Rather than relying on static, arbitrary timeout rules (which fail for devices with highly variable reporting rates), this integration **learns the check-in heartbeats** of each sensor to compute a **dynamic, mathematically optimized timeout threshold**.

---

## 🚀 Key Features

*   **Surveillance-by-Default**: Automatically monitors all sensors in your configured domains. No manual setup required—devices are actively tracked unless explicitly excluded.
*   **Heartbeat-Based Learning**: Learns reporting intervals (averages computed over up to 50 data points) and adapts dynamically. Backfills averages instantly from your recorder database at boot.
*   **Proposed Exclusions**: Detects and flags entities that have never sent a state change, allowing you to exclude them with a single click during setup or configuration.
*   **Native HA Repairs**: Automatically generates a Home Assistant Repair issue when a sensor dies, telling you exactly which battery type (e.g. *CR2032*, *2x AAA*) is required for replacement. The issue clears itself automatically as soon as the sensor checks back in.
*   **Dashboard Card Grouping by Area**: A dedicated, premium sidebar dashboard panel groups your monitored sensors by their physical Home Assistant Areas, displaying battery levels, relative elapsed time, check-in intervals, and depletion predictions.
*   **Quick Control Actions**: Inline controls on each dashboard card let you **Snooze** alerts, **Exclude** entities, trigger **Re-learning**, or override timeouts **manually**.
*   **Battery Notes Synergy**: Auto-resolves associated `_battery_type` sensors to expose required battery shapes on dashboard cards and inside Repair issues.
*   **Pre-emptive Warnings**: Triggers early warnings when remaining battery life is predicted to drop under 7 days, showing warning indicators *before* the sensor goes offline.
*   **Actionable Notification Blueprints**: Includes a prepackaged blueprint offering push notifications to your mobile app with inline action buttons to Snooze or Exclude the silent sensor.

---

## 📥 One-Click Installation

Click the button below to automatically add this repository to HACS and open the setup panel:

[![Open your Home Assistant instance and open a repository in HACS.](https://my.home-assistant.io/badge/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ThePastPlayer&repository=IsItDead&category=integration)

---

## 🛠️ Manual Installation & Setup

1.  Open **HACS** in your Home Assistant sidebar.
2.  Click the three dots in the top right corner and select **Custom repositories**.
3.  Enter `https://github.com/ThePastPlayer/IsItDead` as the Repository and select **Integration** as the Category.
4.  Click **Add**, then select **Is It Dead?** from the HACS list and download it.
5.  **Restart** Home Assistant.
6.  Go to **Settings > Devices & Services > Add Integration**, search for **Is It Dead?**, and follow the prompts.

---

## ⚙️ Configuration & Exclusions

You can configure the integration settings at any time by clicking **Configure** on the integration card:
*   **Monitored Domains**: Select which domains to watch (e.g., `sensor`, `binary_sensor`).
*   **Learning Period (Days)**: How long to monitor check-ins before enforcing calculations (defaults to 7 days, with fallback to maximum timeout during learning).
*   **Threshold Multiplier**: Set how many times the learned average check-in rate a sensor can miss before it is marked dead (e.g., $3.0\times$).
*   **Min / Max Timeout Limits**: Restrict calculations (e.g., minimum 1 hour, maximum 7 days) to prevent anomaly spikes.
*   **Excluded Entities**: Select entities to ignore from tracking (pre-populated with suggested inactive sensors).
*   **Custom Timeout YAML**: Provide manual overrides for specific entity IDs (e.g., `sensor.living_room_motion: 12.0` in hours).

---

## 🔌 Custom Service Endpoints

The integration registers four services for automation or custom dashboard calls:

### `is_it_dead.snooze_entity`
Temporarily mutes dead alerts for a specific sensor.
*   `entity_id` *(Required)*: The entity ID of the dead sensor.
*   `duration_hours` *(Optional, default: 24)*: Hours to mute. Set to `0` to unsnooze immediately.

### `is_it_dead.exclude_entity`
Excludes a sensor from monitoring, automatically updating the integration options.
*   `entity_id` *(Required)*: The entity ID of the sensor.

### `is_it_dead.relearn_entity`
Clears update interval statistics for a sensor and restarts the learning phase.
*   `entity_id` *(Required)*: The entity ID of the sensor.

### `is_it_dead.set_manual_timeout`
Sets a manual override timeout threshold for a specific sensor.
*   `entity_id` *(Required)*: The entity ID of the sensor.
*   `timeout_hours` *(Required)*: Override limit in hours. Set to `0` to disable the override.

---

## 🔔 Actionable Blueprint Automation

The integration automatically installs an automation blueprint located in your config folder under `blueprints/automation/is_it_dead/is_it_dead_alert.yaml`.

To set it up:
1.  Go to **Settings > Automations & Scenes > Blueprints**.
2.  Locate **Is It Dead? Actionable Alerts** and click **Create Automation**.
3.  Select your aggregate sensor (`binary_sensor.is_it_dead_alert`), choose your mobile device to receive push alerts, and save.
4.  When a sensor goes offline, your phone will receive a push notification with two actionable buttons:
    *   **Snooze 24h**: Automatically silences notifications for the dead sensor for 24 hours.
    *   **Exclude Silent**: Adds the sensor to the exclusions list to ignore it.
