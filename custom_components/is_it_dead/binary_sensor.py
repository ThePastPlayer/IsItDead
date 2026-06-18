"""Binary sensor platform for Is It Dead?."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Is It Dead? binary sensors."""
    manager = hass.data[DOMAIN][entry.entry_id]

    added_entities: set[str] = set()

    @callback
    def async_add_new_entities(entity_ids: list[str]) -> None:
        """Dynamically add new binary sensors for discovered entities."""
        sensors = []
        for entity_id in entity_ids:
            if entity_id in added_entities:
                continue
            added_entities.add(entity_id)
            sensors.append(IsItDeadSensor(manager, entity_id))

        if sensors:
            _LOGGER.info("Adding %d new dead-sensor trackers", len(sensors))
            async_add_entities(sensors)

    # Register the callback in the manager so new entities are discovered dynamically
    manager.async_add_new_entities_callback = async_add_new_entities

    # Add the aggregate alert sensor first
    alert_sensor = IsItDeadAlert(manager)
    async_add_entities([alert_sensor])

    # Initial load of monitored entities
    async_add_new_entities(manager.get_monitored_entities())


class IsItDeadSensor(BinarySensorEntity):
    """Binary sensor representing whether a single monitored entity is dead."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_should_poll = False

    def __init__(self, manager: Any, monitored_entity_id: str) -> None:
        """Initialize the sensor."""
        self.manager = manager
        self.monitored_entity_id = monitored_entity_id

        # Unique ID based on the monitored entity
        self._attr_unique_id = f"is_it_dead_{monitored_entity_id}"

    async def async_added_to_hass(self) -> None:
        """Register subscription listener when added to Home Assistant."""
        self.async_on_remove(self.manager.subscribe(self._async_on_manager_update))
        # Initial check for Repairs issue registry
        self._async_manage_repairs_issue()

    async def async_will_remove_from_hass(self) -> None:
        """Clean up repairs issue when entity is removed."""
        ir.async_delete_issue(self.hass, DOMAIN, f"sensor_dead_{self.monitored_entity_id}")

    @callback
    def _async_on_manager_update(self, entity_id: str | None) -> None:
        """Handle status update event from the manager."""
        if entity_id is None or entity_id == self.monitored_entity_id:
            self._async_manage_repairs_issue()
            self.async_write_ha_state()

    @callback
    def _async_manage_repairs_issue(self) -> None:
        """Raise or dismiss a Repairs issue based on sensor state."""
        issue_id = f"sensor_dead_{self.monitored_entity_id}"

        if self.is_dead_raw:
            state = self.hass.states.get(self.monitored_entity_id)
            friendly_name = state.name if state else self.monitored_entity_id

            # Resolve battery type
            battery_type = self._resolve_battery_type()

            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="sensor_dead",
                translation_placeholders={
                    "name": friendly_name,
                    "entity_id": self.monitored_entity_id,
                    "battery_type": battery_type,
                },
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    def _resolve_battery_type(self) -> str:
        """Resolve the battery type for this device from Battery Notes or a battery_type sensor."""
        entity_reg = er.async_get(self.hass)
        reg_entry = entity_reg.async_get(self.monitored_entity_id)
        if reg_entry and reg_entry.device_id:
            for entry in er.async_entries_for_device(entity_reg, reg_entry.device_id):
                if entry.domain == "sensor":
                    sensor_state = self.hass.states.get(entry.entity_id)
                    if sensor_state:
                        # Battery Notes: battery_type attribute on the battery sensor
                        bat_type_attr = sensor_state.attributes.get("battery_type")
                        if bat_type_attr:
                            return str(bat_type_attr)
                        # Fallback: entity whose ID ends with _battery_type
                        if entry.entity_id.endswith("_battery_type"):
                            return str(sensor_state.state)
        return "Unknown"

    @property
    def name(self) -> str:
        """Return the name of the binary sensor."""
        state = self.hass.states.get(self.monitored_entity_id)
        if state and state.name:
            return f"{state.name} Is Dead"
        entity_name = self.monitored_entity_id.split(".")[-1]
        return f"{entity_name.replace('_', ' ').title()} Is Dead"

    @property
    def is_on(self) -> bool:
        """Return True if the monitored entity is considered dead and not muted/snoozed."""
        # 1. Check if snooze is active
        snooze_until_str = self.manager.learned_data.get("snoozed", {}).get(self.monitored_entity_id)
        if snooze_until_str:
            snooze_until = dt_util.parse_datetime(snooze_until_str)
            if snooze_until and dt_util.utcnow() < snooze_until:
                return False

        # 2. Evaluate raw dead state
        return self.is_dead_raw

    @property
    def is_dead_raw(self) -> bool:
        """Evaluate if the sensor has timed out (ignoring snooze)."""
        state = self.hass.states.get(self.monitored_entity_id)

        # If it doesn't exist in state machine, it's dead
        if not state:
            return True

        # If unavailable or unknown, it's offline/dead
        if state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return True

        # Retrieve last reported timestamp
        last_reported = state.last_reported or state.last_updated
        if not last_reported:
            entities_data = self.manager.learned_data.get("entities", {})
            entity_info = entities_data.get(self.monitored_entity_id)
            if entity_info and "last_report_ts" in entity_info:
                last_reported = dt_util.parse_datetime(entity_info["last_report_ts"])

        if not last_reported:
            if self.manager.is_learning():
                return False
            return True

        elapsed = (dt_util.utcnow() - last_reported).total_seconds()
        timeout = self.manager.get_timeout_for_entity(self.monitored_entity_id)

        return elapsed > timeout

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return entity specific state attributes."""
        state = self.hass.states.get(self.monitored_entity_id)

        last_reported = None
        if state:
            last_reported = state.last_reported or state.last_updated

        entities_data = self.manager.learned_data.get("entities", {})
        entity_info = entities_data.get(self.monitored_entity_id) or {}

        if not last_reported and "last_report_ts" in entity_info:
            last_reported = dt_util.parse_datetime(entity_info["last_report_ts"])

        timeout = self.manager.get_timeout_for_entity(self.monitored_entity_id)
        average_interval = entity_info.get("average_interval", 0.0)
        report_count = entity_info.get("count", 0)

        elapsed = None
        if last_reported:
            elapsed = (dt_util.utcnow() - last_reported).total_seconds()

        # Resolve battery data dynamically
        battery_entity_id, battery_level = self.manager.get_battery_info(self.monitored_entity_id)
        depletion = self.manager.estimate_battery_depletion(self.monitored_entity_id)

        # Pre-emptive low-battery warning prediction (< 7 days)
        low_battery_warning = False
        if depletion and depletion.get("depletion_days") is not None:
            if depletion["depletion_days"] < 7.0:
                low_battery_warning = True

        # Resolve Area registry metadata
        area_id = None
        area_name = None
        entity_reg = er.async_get(self.hass)
        reg_entry = entity_reg.async_get(self.monitored_entity_id)
        if reg_entry:
            area_id = reg_entry.area_id
            if not area_id and reg_entry.device_id:
                dev_reg = dr.async_get(self.hass)
                device = dev_reg.async_get(reg_entry.device_id)
                if device:
                    area_id = device.area_id

            if area_id:
                area_reg = ar.async_get(self.hass)
                area = area_reg.async_get(area_id)
                if area:
                    area_name = area.name

        # Resolve Battery Type (e.g. from Battery Notes)
        battery_type = self._resolve_battery_type()

        # Resolve Snooze
        snooze_until_str = self.manager.learned_data.get("snoozed", {}).get(self.monitored_entity_id)
        snooze_active = False
        if snooze_until_str:
            snooze_until = dt_util.parse_datetime(snooze_until_str)
            if snooze_until and dt_util.utcnow() < snooze_until:
                snooze_active = True

        return {
            "monitored_entity_id": self.monitored_entity_id,
            "last_reported": last_reported.isoformat() if last_reported else None,
            "seconds_since_last_report": round(elapsed, 1) if elapsed is not None else None,
            "timeout_threshold_seconds": round(timeout, 1),
            "timeout_threshold_hours": round(timeout / 3600.0, 2),
            "average_report_interval_seconds": round(average_interval, 1),
            "average_report_interval_hours": round(average_interval / 3600.0, 2),
            "report_count": report_count,
            "learning_active": self.manager.is_learning(),
            "battery_entity_id": battery_entity_id,
            "battery_level": battery_level,
            "battery_depletion_estimate": depletion,
            "battery_type": battery_type,
            "area_id": area_id,
            "area_name": area_name,
            "low_battery_warning": low_battery_warning,
            "snooze_until": snooze_until_str if snooze_active else None,
            "is_dead_raw": self.is_dead_raw,
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
    def _async_on_manager_update(self, entity_id: str | None) -> None:
        """Update aggregate state on any entity change or check trigger."""
        if self.manager.async_add_new_entities_callback:
            self.manager.async_add_new_entities_callback(
                self.manager.get_monitored_entities()
            )
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        """Return True if any of the monitored entities are currently dead and not snoozed."""
        monitored = self.manager.get_monitored_entities()
        entities_data = self.manager.learned_data.get("entities", {})

        for entity_id in monitored:
            # Skip checking if snoozed
            snooze_until_str = self.manager.learned_data.get("snoozed", {}).get(entity_id)
            if snooze_until_str:
                snooze_until = dt_util.parse_datetime(snooze_until_str)
                if snooze_until and dt_util.utcnow() < snooze_until:
                    continue

            state = self.hass.states.get(entity_id)

            if not state:
                return True

            if state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                return True

            last_reported = state.last_reported or state.last_updated
            entity_info = entities_data.get(entity_id) or {}
            if not last_reported and "last_report_ts" in entity_info:
                last_reported = dt_util.parse_datetime(entity_info["last_report_ts"])

            if not last_reported:
                if self.manager.is_learning():
                    continue
                return True

            elapsed = (dt_util.utcnow() - last_reported).total_seconds()
            timeout = self.manager.get_timeout_for_entity(entity_id)

            if elapsed > timeout:
                return True

        return False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return lists of dead, alive, learning, snoozed, and warning entities."""
        monitored = self.manager.get_monitored_entities()
        entities_data = self.manager.learned_data.get("entities", {})

        dead_entities = []
        alive_entities = []
        learning_entities = []
        snoozed_entities = []
        low_battery_entities = []

        for entity_id in monitored:
            snooze_until_str = self.manager.learned_data.get("snoozed", {}).get(entity_id)
            is_snoozed = False
            if snooze_until_str:
                snooze_until = dt_util.parse_datetime(snooze_until_str)
                if snooze_until and dt_util.utcnow() < snooze_until:
                    is_snoozed = True

            state = self.hass.states.get(entity_id)
            is_dead_raw = False

            if not state or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                is_dead_raw = True
            else:
                last_reported = state.last_reported or state.last_updated
                entity_info = entities_data.get(entity_id) or {}
                if not last_reported and "last_report_ts" in entity_info:
                    last_reported = dt_util.parse_datetime(entity_info["last_report_ts"])

                if not last_reported:
                    if self.manager.is_learning():
                        learning_entities.append(entity_id)
                        continue
                    is_dead_raw = True
                else:
                    elapsed = (dt_util.utcnow() - last_reported).total_seconds()
                    timeout = self.manager.get_timeout_for_entity(entity_id)
                    if elapsed > timeout:
                        is_dead_raw = True

            # Resolve low-battery warnings (< 7 days depletion estimate)
            depletion = self.manager.estimate_battery_depletion(entity_id)
            if depletion and depletion.get("depletion_days") is not None:
                if depletion["depletion_days"] < 7.0:
                    low_battery_entities.append(entity_id)

            if is_snoozed:
                snoozed_entities.append(entity_id)
            elif is_dead_raw:
                dead_entities.append(entity_id)
            else:
                alive_entities.append(entity_id)

        return {
            "dead_entities": dead_entities,
            "dead_count": len(dead_entities),
            "alive_entities": alive_entities,
            "alive_count": len(alive_entities),
            "learning_entities": learning_entities,
            "snoozed_entities": snoozed_entities,
            "snoozed_count": len(snoozed_entities),
            "low_battery_entities": low_battery_entities,
            "low_battery_count": len(low_battery_entities),
            "total_monitored": len(monitored),
            "learning_active": self.manager.is_learning(),
        }
