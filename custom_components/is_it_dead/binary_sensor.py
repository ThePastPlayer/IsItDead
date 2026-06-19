"""Binary sensor platform for Is It Dead? — Device-centric (v2)."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Is It Dead? binary sensors — one per device."""
    manager = hass.data[DOMAIN][entry.entry_id]

    added_devices: set[str] = set()

    @callback
    def async_add_new_devices(device_ids: list[str]) -> None:
        """Dynamically add new binary sensors for discovered devices."""
        sensors = []
        for device_id in device_ids:
            if device_id in added_devices:
                continue
            added_devices.add(device_id)
            sensors.append(IsItDeadDeviceSensor(manager, device_id))

        if sensors:
            _LOGGER.info("Adding %d new device health trackers", len(sensors))
            async_add_entities(sensors)

    # Register the callback so new devices are discovered dynamically
    manager.async_add_new_devices_callback = async_add_new_devices

    # Add the aggregate alert sensor first
    alert_sensor = IsItDeadAlert(manager)
    async_add_entities([alert_sensor])

    # Initial load of monitored devices
    try:
        devices = manager.get_monitored_devices()
        _LOGGER.info("Initial device scan found %d devices", len(devices))
        async_add_new_devices(list(devices.keys()))
    except Exception as err:
        _LOGGER.error(
            "Failed to load monitored devices during setup: %s", err, exc_info=True
        )


class IsItDeadDeviceSensor(BinarySensorEntity):
    """Binary sensor representing whether a physical device is dead.

    One sensor per tracked device. Aggregates health across all entities
    belonging to the device.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_should_poll = False

    def __init__(self, manager: Any, tracked_device_id: str) -> None:
        """Initialize the device sensor."""
        self.manager = manager
        self.tracked_device_id = tracked_device_id

        # Unique ID based on the tracked device
        self._attr_unique_id = f"is_it_dead_device_{tracked_device_id}"

    async def async_added_to_hass(self) -> None:
        """Register subscription listener when added to Home Assistant."""
        self.async_on_remove(self.manager.subscribe(self._async_on_manager_update))
        self._async_manage_repairs_issue()

    async def async_will_remove_from_hass(self) -> None:
        """Clean up repairs issue when entity is removed."""
        ir.async_delete_issue(self.hass, DOMAIN, f"device_dead_{self.tracked_device_id}")

    @callback
    def _async_on_manager_update(self, device_id: str | None) -> None:
        """Handle status update event from the manager."""
        if device_id is None or device_id == self.tracked_device_id:
            self._async_manage_repairs_issue()
            self.async_write_ha_state()

    @callback
    def _async_manage_repairs_issue(self) -> None:
        """Raise or dismiss a Repairs issue based on device health."""
        issue_id = f"device_dead_{self.tracked_device_id}"
        health = self._get_health()

        if health["health_status"] == "dead":
            devices = self.manager.get_monitored_devices()
            device_info = devices.get(self.tracked_device_id, {})
            device_name = device_info.get("name", self.tracked_device_id)
            battery_type = self.manager.resolve_battery_type(self.tracked_device_id)

            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="sensor_dead",
                translation_placeholders={
                    "name": device_name,
                    "entity_id": self.tracked_device_id,
                    "battery_type": battery_type,
                },
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    def _get_health(self) -> dict[str, Any]:
        """Get cached or computed health evaluation."""
        return self.manager.evaluate_device_health(self.tracked_device_id)

    @property
    def name(self) -> str:
        """Return the name of the binary sensor."""
        devices = self.manager.get_monitored_devices()
        device_info = devices.get(self.tracked_device_id, {})
        name = device_info.get("name", self.tracked_device_id)
        return f"{name} Is Dead"

    @property
    def is_on(self) -> bool:
        """Return True if the device is considered dead and not snoozed."""
        # Check if any entity on this device is snoozed
        snoozed = self.manager.learned_data.get("snoozed", {})
        entity_ids = self.manager.get_entities_for_device(self.tracked_device_id)
        all_snoozed = entity_ids and all(
            snoozed.get(eid) and dt_util.parse_datetime(snoozed[eid])
            and dt_util.utcnow() < dt_util.parse_datetime(snoozed[eid])
            for eid in entity_ids
        )
        if all_snoozed:
            return False

        health = self._get_health()
        return health["health_status"] == "dead"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return device-level state attributes for the frontend panel."""
        devices = self.manager.get_monitored_devices()
        device_info = devices.get(self.tracked_device_id, {})
        health = self._get_health()

        # Battery info
        battery_entity_id, battery_level = self.manager.get_battery_info_for_device(
            self.tracked_device_id
        )
        depletion = self.manager.estimate_battery_depletion(self.tracked_device_id)
        battery_type = self.manager.resolve_battery_type(self.tracked_device_id)

        low_battery_warning = False
        if depletion and depletion.get("depletion_days") is not None:
            if depletion["depletion_days"] < 7.0:
                low_battery_warning = True

        # Timeout info
        timeout = self.manager.get_timeout_for_device(self.tracked_device_id)

        # Report count — sum across all entities
        entities_data = self.manager.learned_data.get("entities", {})
        total_reports = 0
        best_avg_interval = None
        for eid in device_info.get("entities", []):
            ei = entities_data.get(eid, {})
            total_reports += ei.get("count", 0)
            avg = ei.get("average_interval", 0.0)
            if avg > 0 and (best_avg_interval is None or avg < best_avg_interval):
                best_avg_interval = avg

        # Snooze check
        snoozed = self.manager.learned_data.get("snoozed", {})
        snooze_until = None
        for eid in device_info.get("entities", []):
            s = snoozed.get(eid)
            if s:
                parsed = dt_util.parse_datetime(s)
                if parsed and dt_util.utcnow() < parsed:
                    snooze_until = s
                    break

        return {
            # Device identification
            "tracked_device_id": self.tracked_device_id,
            "device_name": device_info.get("name", "Unknown"),
            "manufacturer": device_info.get("manufacturer"),
            "model": device_info.get("model"),
            "area_name": device_info.get("area_name"),
            "integrations": device_info.get("integrations", []),
            # Entity info
            "entity_count": len(device_info.get("entities", [])),
            "entities": device_info.get("entities", []),
            # Health status
            "health_status": health["health_status"],
            "last_activity": health.get("last_activity"),
            "last_active_entity": health.get("last_active_entity"),
            "silent_entities": health.get("silent_entities", []),
            "active_entities": health.get("active_entities", []),
            "entity_details": health.get("entity_details", []),
            # Timing
            "average_report_interval_hours": round(best_avg_interval / 3600.0, 2) if best_avg_interval else 0,
            "timeout_threshold_hours": round(timeout / 3600.0, 2),
            "report_count": total_reports,
            # Battery
            "battery_entity_id": battery_entity_id,
            "battery_level": battery_level,
            "battery_type": battery_type,
            "battery_depletion_estimate": depletion,
            "low_battery_warning": low_battery_warning,
            # Status flags
            "learning_active": self.manager.is_learning(),
            "snooze_until": snooze_until,
            "is_dead_raw": health["health_status"] == "dead",
        }


class IsItDeadAlert(BinarySensorEntity):
    """Aggregate binary sensor that triggers when any monitored device goes dead."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_should_poll = False

    def __init__(self, manager: Any) -> None:
        """Initialize the aggregate sensor."""
        self.manager = manager
        self._attr_unique_id = "is_it_dead_aggregate_alert"
        self._attr_name = "Is It Dead Alert"

    async def async_added_to_hass(self) -> None:
        """Register subscription listener when added to Home Assistant."""
        self.async_on_remove(self.manager.subscribe(self._async_on_manager_update))

    @callback
    def _async_on_manager_update(self, device_id: str | None) -> None:
        """Update aggregate state on any device change or check trigger."""
        # Dynamically discover new devices
        try:
            if self.manager.async_add_new_devices_callback:
                devices = self.manager.get_monitored_devices()
                self.manager.async_add_new_devices_callback(list(devices.keys()))
        except Exception as err:
            _LOGGER.debug("Error discovering new devices: %s", err)
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        """Return True if any monitored device is currently dead (and not snoozed)."""
        try:
            devices = self.manager.get_monitored_devices()
        except Exception as err:
            _LOGGER.debug("Error getting devices for is_on: %s", err)
            return False
        snoozed = self.manager.learned_data.get("snoozed", {})

        for device_id, device_info in devices.items():
            # Check if entire device is snoozed
            entity_ids = device_info.get("entities", [])
            all_snoozed = entity_ids and all(
                snoozed.get(eid) and dt_util.parse_datetime(snoozed[eid])
                and dt_util.utcnow() < dt_util.parse_datetime(snoozed[eid])
                for eid in entity_ids
            )
            if all_snoozed:
                continue

            health = self.manager.evaluate_device_health(device_id)
            if health["health_status"] == "dead":
                return True

        return False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return lists of dead, alive, suspected, learning, and snoozed devices."""
        try:
            devices = self.manager.get_monitored_devices()
        except Exception as err:
            _LOGGER.debug("Error getting devices for attributes: %s", err)
            return {"error": str(err), "total_monitored": 0}
        snoozed_data = self.manager.learned_data.get("snoozed", {})

        dead_devices = []
        alive_devices = []
        suspected_devices = []
        learning_devices = []
        snoozed_devices = []
        low_battery_devices = []

        for device_id, device_info in devices.items():
            device_name = device_info.get("name", device_id)
            entity_ids = device_info.get("entities", [])

            # Check snooze
            all_snoozed = entity_ids and all(
                snoozed_data.get(eid) and dt_util.parse_datetime(snoozed_data[eid])
                and dt_util.utcnow() < dt_util.parse_datetime(snoozed_data[eid])
                for eid in entity_ids
            )
            if all_snoozed:
                snoozed_devices.append(device_name)
                continue

            health = self.manager.evaluate_device_health(device_id)
            status = health["health_status"]

            if status == "dead":
                dead_devices.append(device_name)
            elif status == "suspected":
                suspected_devices.append(device_name)
            elif status == "learning":
                learning_devices.append(device_name)
            else:
                alive_devices.append(device_name)

            # Check low battery
            depletion = self.manager.estimate_battery_depletion(device_id)
            if depletion and depletion.get("depletion_days") is not None:
                if depletion["depletion_days"] < 7.0:
                    low_battery_devices.append(device_name)

        return {
            "dead_devices": dead_devices,
            "dead_count": len(dead_devices),
            "alive_devices": alive_devices,
            "alive_count": len(alive_devices),
            "suspected_devices": suspected_devices,
            "suspected_count": len(suspected_devices),
            "learning_devices": learning_devices,
            "learning_count": len(learning_devices),
            "snoozed_devices": snoozed_devices,
            "snoozed_count": len(snoozed_devices),
            "low_battery_devices": low_battery_devices,
            "low_battery_count": len(low_battery_devices),
            "total_monitored": len(devices),
            "learning_active": self.manager.is_learning(),
        }
