"""The Is It Dead? integration — Device-centric health monitoring (v2)."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from datetime import timedelta
from typing import Any

import voluptuous as vol
import yaml

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BATTERY_ONLY,
    CONF_CUSTOM_TIMEOUTS,
    CONF_EXCLUDED_ENTITIES,
    CONF_EXCLUDED_INTEGRATIONS,
    CONF_LEARNING_PERIOD,
    CONF_MAX_TIMEOUT,
    CONF_MIN_TIMEOUT,
    CONF_MONITORED_DOMAINS,
    CONF_MULTIPLIER,
    CONF_STANDALONE_ENTITIES,
    CONF_UPDATE_INTERVAL,
    DEFAULT_BATTERY_ONLY,
    DEFAULT_LEARNING_PERIOD,
    DEFAULT_MAX_TIMEOUT,
    DEFAULT_MIN_TIMEOUT,
    DEFAULT_MONITORED_DOMAINS,
    DEFAULT_MULTIPLIER,
    DEFAULT_STANDALONE_ENTITIES,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    PLATFORMS,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Is It Dead? from a config entry."""
    _LOGGER.info("Setting up Is It Dead? v2 (device-centric)")
    hass.data.setdefault(DOMAIN, {})

    try:
        manager = IsItDeadManager(hass, entry)
    except Exception as err:
        _LOGGER.error("Failed to create IsItDeadManager: %s", err, exc_info=True)
        raise

    hass.data[DOMAIN][entry.entry_id] = manager

    try:
        await manager.async_initialize()
    except Exception as err:
        _LOGGER.error("Failed to initialize manager: %s", err, exc_info=True)
        raise

    # Forward setup to platforms (binary_sensor)
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception as err:
        _LOGGER.error("Failed to set up binary_sensor platform: %s", err, exc_info=True)
        raise

    # Register the frontend static directory
    frontend_path = hass.config.path("custom_components/is_it_dead/frontend")
    try:
        # Modern HA (2024.7+): async_register_static_paths
        from homeassistant.components.http import StaticPathConfig
        await hass.http.async_register_static_paths(
            [StaticPathConfig("/is_it_dead_ui", frontend_path, False)]
        )
        _LOGGER.debug("Registered static path via async_register_static_paths")
    except (ImportError, AttributeError):
        # Fallback for older HA versions
        try:
            hass.http.register_static_path("/is_it_dead_ui", frontend_path, False)
            _LOGGER.debug("Registered static path via register_static_path (legacy)")
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Could not register static path for frontend panel")

    # Register the sidebar panel
    from homeassistant.components.frontend import async_register_panel
    try:
        async_register_panel(
            hass,
            frontend_url_path="is_it_dead",
            webcomponent_name="is-it-dead-panel",
            sidebar_title="Is It Dead?",
            sidebar_icon="mdi:battery-alert",
            module_url="/is_it_dead_ui/is_it_dead_panel.js",
            require_admin=False,
        )
        _LOGGER.info("Registered 'Is It Dead?' sidebar panel")
    except Exception as err:
        _LOGGER.error("Failed to register sidebar panel: %s", err)

    # Copy blueprint to local blueprints directory (run blocking I/O in executor)
    blueprint_src = hass.config.path("custom_components/is_it_dead/blueprints/is_it_dead_alert.yaml")
    blueprint_dest_dir = hass.config.path("blueprints/automation/is_it_dead")
    blueprint_dest = os.path.join(blueprint_dest_dir, "is_it_dead_alert.yaml")

    def _copy_blueprint() -> None:
        os.makedirs(blueprint_dest_dir, exist_ok=True)
        shutil.copy(blueprint_src, blueprint_dest)

    try:
        await hass.async_add_executor_job(_copy_blueprint)
        _LOGGER.info("Copied actionable alert blueprint successfully")
    except Exception as err:
        _LOGGER.error("Failed to copy actionable blueprint: %s", err)

    # ── Device-level service handlers ───────────────────────────────────
    async def async_handle_exclude_device(call) -> None:
        """Exclude a device from monitoring by adding all its entities to exclusion."""
        device_id = call.data["device_id"]
        for m in hass.data[DOMAIN].values():
            device_entities = m.get_entities_for_device(device_id)
            if device_entities:
                excluded = list(m.excluded_entities)
                for eid in device_entities:
                    if eid not in excluded:
                        excluded.append(eid)
                hass.config_entries.async_update_entry(
                    m.config_entry,
                    options={**m.config_entry.options, CONF_EXCLUDED_ENTITIES: excluded},
                )
                break

    async def async_handle_snooze_device(call) -> None:
        """Snooze all entities for a device."""
        device_id = call.data["device_id"]
        hours = float(call.data.get("duration_hours", 24))
        for m in hass.data[DOMAIN].values():
            device_entities = m.get_entities_for_device(device_id)
            if device_entities:
                snoozed = m.learned_data.setdefault("snoozed", {})
                if hours <= 0:
                    for eid in device_entities:
                        snoozed.pop(eid, None)
                else:
                    expire_time = dt_util.utcnow() + timedelta(hours=hours)
                    for eid in device_entities:
                        snoozed[eid] = expire_time.isoformat()
                await m._store.async_save(m.learned_data)
                m.notify_listeners(None)
                break

    async def async_handle_relearn_device(call) -> None:
        """Reset learned data for all entities of a device."""
        device_id = call.data["device_id"]
        for m in hass.data[DOMAIN].values():
            device_entities = m.get_entities_for_device(device_id)
            if device_entities:
                entities_data = m.learned_data.setdefault("entities", {})
                for eid in device_entities:
                    if eid in entities_data:
                        entities_data[eid] = {"count": 0, "average_interval": 0.0}
                await m._store.async_save(m.learned_data)
                m.notify_listeners(None)
                break

    # Legacy entity-level services (kept for backward compatibility)
    async def async_handle_exclude(call) -> None:
        entity_id = call.data["entity_id"]
        for m in hass.data[DOMAIN].values():
            excluded = list(m.excluded_entities)
            if entity_id not in excluded:
                excluded.append(entity_id)
                hass.config_entries.async_update_entry(
                    m.config_entry,
                    options={**m.config_entry.options, CONF_EXCLUDED_ENTITIES: excluded},
                )
            break

    async def async_handle_snooze(call) -> None:
        entity_id = call.data["entity_id"]
        hours = float(call.data.get("duration_hours", 24))
        for m in hass.data[DOMAIN].values():
            snoozed = m.learned_data.setdefault("snoozed", {})
            if hours <= 0:
                snoozed.pop(entity_id, None)
            else:
                expire_time = dt_util.utcnow() + timedelta(hours=hours)
                snoozed[entity_id] = expire_time.isoformat()
            await m._store.async_save(m.learned_data)
            m.notify_listeners(entity_id)
            break

    async def async_handle_relearn(call) -> None:
        entity_id = call.data["entity_id"]
        for m in hass.data[DOMAIN].values():
            entities_data = m.learned_data.setdefault("entities", {})
            if entity_id in entities_data:
                entities_data[entity_id] = {"count": 0, "average_interval": 0.0}
                await m._store.async_save(m.learned_data)
                m.notify_listeners(entity_id)
            break

    async def async_handle_set_manual_timeout(call) -> None:
        entity_id = call.data["entity_id"]
        hours = float(call.data["timeout_hours"])
        for m in hass.data[DOMAIN].values():
            custom_timeouts = dict(m.custom_timeouts)
            if hours <= 0:
                custom_timeouts.pop(entity_id, None)
            else:
                custom_timeouts[entity_id] = hours

            yaml_str = yaml.dump(custom_timeouts)
            hass.config_entries.async_update_entry(
                m.config_entry,
                options={**m.config_entry.options, CONF_CUSTOM_TIMEOUTS: yaml_str},
            )
            break

    # Register device-level services
    device_schema = vol.Schema({vol.Required("device_id"): cv.string})
    for svc_name, handler in (
        ("exclude_device", async_handle_exclude_device),
        ("relearn_device", async_handle_relearn_device),
    ):
        if not hass.services.has_service(DOMAIN, svc_name):
            hass.services.async_register(DOMAIN, svc_name, handler, schema=device_schema)

    if not hass.services.has_service(DOMAIN, "snooze_device"):
        hass.services.async_register(
            DOMAIN,
            "snooze_device",
            async_handle_snooze_device,
            schema=vol.Schema({
                vol.Required("device_id"): cv.string,
                vol.Optional("duration_hours", default=24.0): vol.Coerce(float),
            }),
        )

    # Register legacy entity-level services
    if not hass.services.has_service(DOMAIN, "exclude_entity"):
        hass.services.async_register(
            DOMAIN, "exclude_entity", async_handle_exclude,
            schema=vol.Schema({vol.Required("entity_id"): cv.entity_id}),
        )
    if not hass.services.has_service(DOMAIN, "snooze_entity"):
        hass.services.async_register(
            DOMAIN, "snooze_entity", async_handle_snooze,
            schema=vol.Schema({
                vol.Required("entity_id"): cv.entity_id,
                vol.Optional("duration_hours", default=24.0): vol.Coerce(float),
            }),
        )
    if not hass.services.has_service(DOMAIN, "relearn_entity"):
        hass.services.async_register(
            DOMAIN, "relearn_entity", async_handle_relearn,
            schema=vol.Schema({vol.Required("entity_id"): cv.entity_id}),
        )
    if not hass.services.has_service(DOMAIN, "set_manual_timeout"):
        hass.services.async_register(
            DOMAIN, "set_manual_timeout", async_handle_set_manual_timeout,
            schema=vol.Schema({
                vol.Required("entity_id"): cv.entity_id,
                vol.Required("timeout_hours"): vol.Coerce(float),
            }),
        )

    # Start background database history backfilling
    entry.async_create_background_task(
        hass, manager.async_backfill_history(), "is_it_dead_backfill"
    )

    # Watch for entry updates (options changes) and reload if they happen
    entry.async_on_unload(entry.add_to_updates_listener(async_reload_entry))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        # Remove the sidebar panel
        from homeassistant.components.frontend import async_remove_panel
        async_remove_panel(hass, "is_it_dead")

        manager = hass.data[DOMAIN].pop(entry.entry_id)
        await manager.async_unload()

        # Unregister services if this is the last entry
        if not hass.data[DOMAIN]:
            all_services = (
                "exclude_entity", "snooze_entity", "relearn_entity",
                "set_manual_timeout", "exclude_device", "snooze_device",
                "relearn_device",
            )
            for service in all_services:
                if hass.services.has_service(DOMAIN, service):
                    hass.services.async_remove(DOMAIN, service)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await hass.config_entries.async_reload(entry.entry_id)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  IsItDeadManager — Device-centric tracking engine (v2)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class IsItDeadManager:
    """Manages device-level state tracking, learning, and health assessment."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the manager."""
        self.hass = hass
        self.config_entry = entry

        # Load options (or fall back to config data)
        self.monitored_domains = entry.options.get(
            CONF_MONITORED_DOMAINS,
            entry.data.get(CONF_MONITORED_DOMAINS, DEFAULT_MONITORED_DOMAINS),
        )
        self.learning_period = entry.options.get(
            CONF_LEARNING_PERIOD,
            entry.data.get(CONF_LEARNING_PERIOD, DEFAULT_LEARNING_PERIOD),
        )
        self.multiplier = entry.options.get(
            CONF_MULTIPLIER, entry.data.get(CONF_MULTIPLIER, DEFAULT_MULTIPLIER)
        )
        self.min_timeout = entry.options.get(
            CONF_MIN_TIMEOUT, entry.data.get(CONF_MIN_TIMEOUT, DEFAULT_MIN_TIMEOUT)
        )
        self.max_timeout = entry.options.get(
            CONF_MAX_TIMEOUT, entry.data.get(CONF_MAX_TIMEOUT, DEFAULT_MAX_TIMEOUT)
        )
        self.update_interval = entry.options.get(
            CONF_UPDATE_INTERVAL,
            entry.data.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
        )
        self.excluded_entities = entry.options.get(CONF_EXCLUDED_ENTITIES, [])
        self.excluded_integrations = entry.options.get(CONF_EXCLUDED_INTEGRATIONS, [])
        self.battery_only = entry.options.get(
            CONF_BATTERY_ONLY,
            entry.data.get(CONF_BATTERY_ONLY, DEFAULT_BATTERY_ONLY),
        )
        self.standalone_entities = entry.options.get(
            CONF_STANDALONE_ENTITIES,
            entry.data.get(CONF_STANDALONE_ENTITIES, DEFAULT_STANDALONE_ENTITIES),
        )

        # Parse custom overrides (Entity ID -> Hours)
        custom_raw = entry.options.get(CONF_CUSTOM_TIMEOUTS, "")
        self.custom_timeouts: dict[str, float] = {}
        if isinstance(custom_raw, str) and custom_raw.strip():
            try:
                parsed = yaml.safe_load(custom_raw)
                if isinstance(parsed, dict):
                    self.custom_timeouts = {
                        str(k): float(v) for k, v in parsed.items()
                    }
            except Exception as err:
                _LOGGER.error("Failed to parse custom timeouts YAML: %s", err)

        self.learned_data: dict[str, Any] = {}
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.listeners: list[Any] = []
        self._unsub_state_change = None
        self._unsub_periodic = None
        self.async_add_new_devices_callback = None

        # Cache: device_id -> list of entity_ids
        self._device_entity_map: dict[str, list[str]] = {}
        # Cache: entity_id -> device_id (reverse lookup)
        self._entity_to_device: dict[str, str] = {}

    async def async_initialize(self) -> None:
        """Load storage and start tracking listeners."""
        # Load persisted data (handles v1 -> v2 migration)
        stored = await self._store.async_load() or {}
        if stored and "entities" in stored and "devices" not in stored:
            # v1 data — migrate: keep entity-level data, initialize device structure
            _LOGGER.info("Migrating v1 entity-level data to v2 device-centric format")
            self.learned_data = stored
            self.learned_data.setdefault("devices", {})
        else:
            self.learned_data = stored

        # Set or maintain learning phase start timestamp
        if "learning_start_time" not in self.learned_data:
            self.learned_data["learning_start_time"] = dt_util.utcnow().isoformat()
            await self._store.async_save(self.learned_data)

        # Build device-entity mapping and start tracking
        self._rebuild_device_map()

        all_entity_ids = []
        for entities in self._device_entity_map.values():
            all_entity_ids.extend(entities)

        if all_entity_ids:
            self._unsub_state_change = async_track_state_change_event(
                self.hass, all_entity_ids, self._async_handle_state_change
            )

        # Populate initial battery history
        for device_id, entity_ids in self._device_entity_map.items():
            for entity_id in entity_ids:
                bat_id, bat_lvl = self.get_battery_info_for_device(device_id)
                if bat_id and bat_lvl is not None:
                    self.update_battery_history(device_id, bat_id, bat_lvl)
                    break  # One battery check per device is enough

        # Periodic check for timeouts
        self._unsub_periodic = async_track_time_interval(
            self.hass,
            self._async_handle_periodic,
            timedelta(minutes=self.update_interval),
        )

    async def async_unload(self) -> None:
        """Unsubscribe listeners and save final data."""
        if self._unsub_state_change:
            self._unsub_state_change()
        if self._unsub_periodic:
            self._unsub_periodic()
        await self._store.async_save(self.learned_data)

    # ── Device discovery ────────────────────────────────────────────────

    def _rebuild_device_map(self) -> None:
        """Build the device_id -> [entity_ids] mapping from registries."""
        entity_reg = er.async_get(self.hass)

        self._device_entity_map = {}
        self._entity_to_device = {}
        # Cache battery check results per device to avoid redundant lookups
        battery_check_cache: dict[str, bool] = {}

        total_states = 0
        domain_matched = 0
        skipped_disabled = 0
        skipped_self = 0
        skipped_integration = 0
        skipped_excluded = 0
        skipped_standalone = 0
        skipped_battery = 0

        for state in self.hass.states.async_all():
            entity_id = state.entity_id
            domain = entity_id.split(".")[0]

            if domain not in self.monitored_domains:
                continue
            total_states += 1
            domain_matched += 1

            reg_entry = entity_reg.async_get(entity_id)
            if reg_entry:
                if reg_entry.disabled_by is not None:
                    skipped_disabled += 1
                    continue
                if reg_entry.platform == DOMAIN:
                    skipped_self += 1
                    continue
                if reg_entry.platform in self.excluded_integrations:
                    skipped_integration += 1
                    continue

            if entity_id in self.excluded_entities:
                skipped_excluded += 1
                continue

            # Determine device_id
            device_id = None
            if reg_entry and reg_entry.device_id:
                device_id = reg_entry.device_id
            else:
                # Standalone entity (no device)
                if self.standalone_entities == "ignore":
                    skipped_standalone += 1
                    continue
                elif self.standalone_entities == "group":
                    device_id = "__standalone__"
                else:  # "track" — each gets its own virtual device
                    device_id = f"__standalone_{entity_id}__"

            # Battery-only filter: check at device level (cached)
            if self.battery_only and not device_id.startswith("__"):
                if device_id not in battery_check_cache:
                    battery_check_cache[device_id] = self._is_device_battery_powered(
                        device_id, entity_reg
                    )
                if not battery_check_cache[device_id]:
                    skipped_battery += 1
                    continue

            self._device_entity_map.setdefault(device_id, [])
            if entity_id not in self._device_entity_map[device_id]:
                self._device_entity_map[device_id].append(entity_id)
            self._entity_to_device[entity_id] = device_id

        _LOGGER.info(
            "Device map built: %d devices, %d entities tracked "
            "(domain_matched=%d, skipped: disabled=%d, self=%d, integration=%d, "
            "excluded=%d, standalone=%d, battery=%d)",
            len(self._device_entity_map),
            sum(len(v) for v in self._device_entity_map.values()),
            domain_matched,
            skipped_disabled,
            skipped_self,
            skipped_integration,
            skipped_excluded,
            skipped_standalone,
            skipped_battery,
        )

    def _is_device_battery_powered(self, device_id: str, entity_reg: er.EntityRegistry) -> bool:
        """Check if a device has any battery sensor among its entities."""
        try:
            for entry in er.async_entries_for_device(entity_reg, device_id):
                # Check registry-level device_class (use getattr for compat)
                orig_dc = getattr(entry, "original_device_class", None)
                entry_dc = getattr(entry, "device_class", None)
                if orig_dc == "battery" or entry_dc == "battery":
                    return True
                # Check state-level device_class
                sibling_state = self.hass.states.get(entry.entity_id)
                if sibling_state:
                    dc = sibling_state.attributes.get("device_class")
                    if dc == "battery":
                        return True
                    # Also check for battery attributes
                    for attr in ("battery", "battery_level", "battery_state"):
                        if attr in sibling_state.attributes:
                            return True
        except Exception as err:
            _LOGGER.debug("Error checking battery for device %s: %s", device_id, err)
        return False

    def get_monitored_devices(self) -> dict[str, dict[str, Any]]:
        """Get the current device_id -> device_info mapping."""
        self._rebuild_device_map()

        dev_reg = dr.async_get(self.hass)
        entity_reg = er.async_get(self.hass)
        area_reg = ar.async_get(self.hass)

        result = {}
        for device_id, entity_ids in self._device_entity_map.items():
            if not entity_ids:
                continue

            try:
                device = dev_reg.async_get(device_id) if not device_id.startswith("__") else None

                # Gather integrations providing entities for this device
                integrations = set()
                for eid in entity_ids:
                    reg = entity_reg.async_get(eid)
                    if reg:
                        integrations.add(reg.platform)

                # Resolve area name
                area_name = None
                if device:
                    area_id = getattr(device, "area_id", None)
                    if area_id:
                        area = area_reg.async_get(area_id)
                        if area:
                            area_name = getattr(area, "name", None)

                # Resolve device name safely
                device_name = "Unknown"
                if device:
                    device_name = (
                        getattr(device, "name_by_user", None)
                        or getattr(device, "name", None)
                        or "Unknown Device"
                    )
                elif device_id == "__standalone__":
                    device_name = "Unassigned Entities"
                elif entity_ids:
                    device_name = entity_ids[0]

                result[device_id] = {
                    "device_id": device_id,
                    "name": device_name,
                    "manufacturer": getattr(device, "manufacturer", None) if device else None,
                    "model": getattr(device, "model", None) if device else None,
                    "area_name": area_name,
                    "integrations": sorted(integrations),
                    "entities": entity_ids,
                }
            except Exception as err:
                _LOGGER.error(
                    "Error building device info for %s: %s", device_id, err, exc_info=True
                )

        _LOGGER.info("get_monitored_devices returning %d devices", len(result))
        return result

    def get_entities_for_device(self, device_id: str) -> list[str]:
        """Get entity IDs belonging to a device."""
        if not self._device_entity_map:
            self._rebuild_device_map()
        return self._device_entity_map.get(device_id, [])

    def get_all_monitored_entity_ids(self) -> list[str]:
        """Get flat list of all monitored entity IDs across all devices."""
        if not self._device_entity_map:
            self._rebuild_device_map()
        result = []
        for entities in self._device_entity_map.values():
            result.extend(entities)
        return result

    # ── Learning & timeout logic ────────────────────────────────────────

    def is_learning(self) -> bool:
        """Check if the global learning phase is active."""
        start_time_str = self.learned_data.get("learning_start_time")
        if not start_time_str:
            return True
        start_time = dt_util.parse_datetime(start_time_str)
        if not start_time:
            return True
        return (dt_util.utcnow() - start_time) < timedelta(days=self.learning_period)

    def get_timeout_for_device(self, device_id: str) -> float:
        """Get the timeout threshold (seconds) for a device.

        Uses the BEST (shortest) learned interval among all entities in the device.
        """
        entities_data = self.learned_data.get("entities", {})
        entity_ids = self.get_entities_for_device(device_id)

        best_interval = None
        for entity_id in entity_ids:
            # Check custom override
            if entity_id in self.custom_timeouts:
                custom_sec = self.custom_timeouts[entity_id] * 3600.0
                if best_interval is None or custom_sec < best_interval:
                    best_interval = custom_sec
                continue

            entity_info = entities_data.get(entity_id)
            if entity_info and entity_info.get("count", 0) > 0:
                avg = entity_info["average_interval"]
                if best_interval is None or avg < best_interval:
                    best_interval = avg

        if best_interval is not None:
            timeout = best_interval * self.multiplier
            min_sec = self.min_timeout * 3600.0
            max_sec = self.max_timeout * 3600.0
            return max(min(timeout, max_sec), min_sec)

        # Fallback: max timeout
        return self.max_timeout * 3600.0

    def update_learned_data(self, entity_id: str, interval: float, count: int = 1) -> None:
        """Calculate and store running average update interval for an entity."""
        entities_data = self.learned_data.setdefault("entities", {})
        entity_info = entities_data.setdefault(
            entity_id, {"count": 0, "average_interval": 0.0}
        )

        current_count = entity_info.get("count", 0)
        current_avg = entity_info.get("average_interval", 0.0)

        if current_count == 0:
            entity_info["average_interval"] = interval
            entity_info["count"] = count
        else:
            new_count = min(current_count + count, 50)
            entity_info["average_interval"] = (
                current_avg * (new_count - count) + interval * count
            ) / new_count
            entity_info["count"] = new_count

    # ── Device health assessment ────────────────────────────────────────

    def evaluate_device_health(self, device_id: str) -> dict[str, Any]:
        """Evaluate the health status of a device and return detailed info.

        Returns a dict with:
            health_status: "alive" | "suspected" | "dead" | "learning"
            last_activity: ISO datetime of most recent entity report
            last_active_entity: entity_id that reported most recently
            silent_entities: list of entity_ids that haven't reported within threshold
            active_entities: list of entity_ids that reported recently
            entity_details: list of dicts with per-entity info
        """
        entity_ids = self.get_entities_for_device(device_id)
        if not entity_ids:
            return {
                "health_status": "dead",
                "last_activity": None,
                "last_active_entity": None,
                "silent_entities": [],
                "active_entities": [],
                "entity_details": [],
            }

        now = dt_util.utcnow()
        timeout = self.get_timeout_for_device(device_id)
        entities_data = self.learned_data.get("entities", {})

        last_activity = None
        last_active_entity = None
        silent_entities = []
        active_entities = []
        entity_details = []
        has_any_data = False

        for entity_id in entity_ids:
            state = self.hass.states.get(entity_id)
            entity_info = entities_data.get(entity_id, {})

            # Determine last reported time
            last_reported = None
            if state:
                last_reported = state.last_reported or state.last_updated
            if not last_reported and "last_report_ts" in entity_info:
                last_reported = dt_util.parse_datetime(entity_info["last_report_ts"])

            detail = {
                "entity_id": entity_id,
                "last_reported": last_reported.isoformat() if last_reported else None,
                "state": state.state if state else "unavailable",
            }
            entity_details.append(detail)

            if last_reported:
                has_any_data = True
                elapsed = (now - last_reported).total_seconds()
                if elapsed > timeout:
                    silent_entities.append(entity_id)
                else:
                    active_entities.append(entity_id)

                # Track most recent activity across all entities
                if last_activity is None or last_reported > last_activity:
                    last_activity = last_reported
                    last_active_entity = entity_id
            elif state and state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                silent_entities.append(entity_id)
            elif not state:
                silent_entities.append(entity_id)
            else:
                # Entity exists but no timestamp — treat as unknown
                if not self.is_learning():
                    silent_entities.append(entity_id)

        # Determine health status
        if not has_any_data:
            health = "learning" if self.is_learning() else "dead"
        elif len(active_entities) > 0 and len(silent_entities) > 0:
            health = "suspected"
        elif len(active_entities) == 0:
            health = "dead"
        else:
            health = "alive"

        return {
            "health_status": health,
            "last_activity": last_activity.isoformat() if last_activity else None,
            "last_active_entity": last_active_entity,
            "silent_entities": silent_entities,
            "active_entities": active_entities,
            "entity_details": entity_details,
        }

    # ── Battery helpers ─────────────────────────────────────────────────

    def get_battery_info_for_device(self, device_id: str) -> tuple[str | None, float | None]:
        """Get battery entity ID and level for a device."""
        if device_id.startswith("__"):
            return None, None

        entity_reg = er.async_get(self.hass)
        for entry in er.async_entries_for_device(entity_reg, device_id):
            if entry.domain == "sensor":
                sensor_state = self.hass.states.get(entry.entity_id)
                if sensor_state:
                    dc = sensor_state.attributes.get("device_class")
                    unit = sensor_state.attributes.get("unit_of_measurement")
                    if dc == "battery" or (unit == "%" and "battery" in entry.entity_id):
                        try:
                            return entry.entity_id, float(sensor_state.state)
                        except (ValueError, TypeError):
                            pass
        return None, None

    def resolve_battery_type(self, device_id: str) -> str:
        """Resolve battery type from Battery Notes or a battery_type sensor."""
        if device_id.startswith("__"):
            return "Unknown"
        entity_reg = er.async_get(self.hass)
        for entry in er.async_entries_for_device(entity_reg, device_id):
            if entry.domain == "sensor":
                sensor_state = self.hass.states.get(entry.entity_id)
                if sensor_state:
                    bat_type = sensor_state.attributes.get("battery_type")
                    if bat_type:
                        return str(bat_type)
                    if entry.entity_id.endswith("_battery_type"):
                        return str(sensor_state.state)
        return "Unknown"

    def update_battery_history(self, device_id: str, battery_entity_id: str, current_level: float) -> None:
        """Track battery level changes to estimate depletion time."""
        if not battery_entity_id or current_level is None:
            return

        battery_tracking = self.learned_data.setdefault("battery_tracking", {})
        battery_data = battery_tracking.setdefault(device_id, {})
        history_list = battery_data.setdefault("history", [])

        now_iso = dt_util.utcnow().isoformat()

        if not history_list:
            history_list.append({"ts": now_iso, "val": current_level})
        else:
            last_entry = history_list[-1]
            if last_entry["val"] != current_level:
                history_list.append({"ts": now_iso, "val": current_level})

        if len(history_list) > 5:
            history_list.pop(0)

    def estimate_battery_depletion(self, device_id: str) -> dict[str, Any]:
        """Estimate remaining battery life and depletion date for a device."""
        battery_tracking = self.learned_data.get("battery_tracking", {})
        battery_data = battery_tracking.get(device_id, {})
        history_list = battery_data.get("history", [])

        # Check for recharging
        for idx in range(1, len(history_list)):
            if history_list[idx]["val"] > history_list[idx - 1]["val"]:
                return {
                    "depletion_time": None, "depletion_days": None,
                    "discharge_rate_per_day": None,
                    "status": "Battery charged, recalculating...",
                }

        if len(history_list) < 2:
            return {
                "depletion_time": None, "depletion_days": None,
                "discharge_rate_per_day": None,
                "status": "Learning battery discharge...",
            }

        first, last = history_list[0], history_list[-1]
        first_time = dt_util.parse_datetime(first["ts"])
        last_time = dt_util.parse_datetime(last["ts"])
        if not first_time or not last_time:
            return {"status": "Error parsing history"}

        time_diff = (last_time - first_time).total_seconds()
        val_diff = first["val"] - last["val"]

        if val_diff <= 0 or time_diff == 0:
            return {
                "depletion_time": None, "depletion_days": None,
                "discharge_rate_per_day": 0.0, "status": "Battery stable",
            }

        rate_per_day = (val_diff / time_diff) * 86400.0
        days_remaining = last["val"] / rate_per_day
        depletion_dt = last_time + timedelta(days=days_remaining)

        return {
            "depletion_time": depletion_dt.isoformat(),
            "depletion_days": round(days_remaining, 1),
            "discharge_rate_per_day": round(rate_per_day, 3),
            "status": f"Estimated remaining: {round(days_remaining, 1)} days",
        }

    # ── History backfill ────────────────────────────────────────────────

    async def async_backfill_history(self) -> None:
        """Backfill average intervals using recorder history (runs in background)."""
        try:
            from homeassistant.components.recorder import get_instance
            recorder = get_instance(self.hass)
            await recorder.async_db_ready
        except (ImportError, AttributeError):
            from homeassistant.components.recorder import DOMAIN as RECORDER_DOMAIN
            if RECORDER_DOMAIN in self.hass.data:
                recorder = self.hass.data[RECORDER_DOMAIN]
                if hasattr(recorder, "db_connected"):
                    try:
                        await recorder.db_connected
                    except Exception as err:
                        _LOGGER.error("Error waiting for recorder connection: %s", err)
        except Exception as err:
            _LOGGER.error("Error waiting for recorder: %s", err)

        _LOGGER.info("Starting background history backfill for 'Is It Dead?'")
        entity_ids = self.get_all_monitored_entity_ids()
        if not entity_ids:
            _LOGGER.info("No entities found to backfill history")
            return

        start_time = dt_util.utcnow() - timedelta(days=self.learning_period)
        end_time = dt_util.utcnow()

        entities_data = self.learned_data.setdefault("entities", {})
        for entity_id in entity_ids:
            entities_data.setdefault(entity_id, {"count": 0, "average_interval": 0.0})

        try:
            from homeassistant.components.recorder.history import get_significant_states
        except ImportError:
            _LOGGER.warning("Could not import get_significant_states — skipping backfill")
            return

        chunk_size = 15
        for i in range(0, len(entity_ids), chunk_size):
            chunk = entity_ids[i : i + chunk_size]
            try:
                states_history = await self.hass.async_add_executor_job(
                    get_significant_states, self.hass, start_time, end_time, chunk,
                )
                for entity_id, states in states_history.items():
                    if len(states) < 2:
                        continue
                    intervals = []
                    for j in range(1, len(states)):
                        t1 = states[j - 1].last_reported or states[j - 1].last_updated
                        t2 = states[j].last_reported or states[j].last_updated
                        if t1 and t2:
                            diff = (t2 - t1).total_seconds()
                            if diff > 1.0:
                                intervals.append(diff)
                    if intervals:
                        avg_interval = sum(intervals) / len(intervals)
                        self.update_learned_data(entity_id, avg_interval, len(intervals))
                        last_state = states[-1]
                        last_ts = last_state.last_reported or last_state.last_updated
                        if last_ts:
                            entities_data[entity_id]["last_report_ts"] = last_ts.isoformat()
            except Exception as err:
                _LOGGER.error("History backfill failed for chunk %s: %s", chunk, err)
            await asyncio.sleep(0.1)

        await self._store.async_save(self.learned_data)
        _LOGGER.info("Finished history backfill successfully")
        self.notify_listeners(None)

    # ── Event handlers ──────────────────────────────────────────────────

    @callback
    def _async_handle_state_change(self, event) -> None:
        """Handle real-time state change events — update device-level data."""
        entity_id = event.data["entity_id"]
        new_state = event.data["new_state"]

        if not new_state:
            return

        # Find which device this entity belongs to
        device_id = self._entity_to_device.get(entity_id)

        if new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            self.notify_listeners(device_id)
            return

        new_ts = new_state.last_reported or new_state.last_updated
        if not new_ts:
            return

        # Update entity-level learned data
        entities_data = self.learned_data.setdefault("entities", {})
        entity_info = entities_data.setdefault(
            entity_id, {"count": 0, "average_interval": 0.0}
        )

        last_ts_str = entity_info.get("last_report_ts")
        if last_ts_str:
            last_ts = dt_util.parse_datetime(last_ts_str)
            if last_ts:
                interval = (new_ts - last_ts).total_seconds()
                if interval > 1.0:
                    self.update_learned_data(entity_id, interval)

        entity_info["last_report_ts"] = new_ts.isoformat()

        # Update device-level battery tracking
        if device_id and not device_id.startswith("__"):
            bat_id, bat_lvl = self.get_battery_info_for_device(device_id)
            if bat_id and bat_lvl is not None:
                self.update_battery_history(device_id, bat_id, bat_lvl)

        self.notify_listeners(device_id)

    async def _async_handle_periodic(self, _now_time) -> None:
        """Run periodic check across all devices and save data."""
        # Refresh device map to pick up new entities
        self._rebuild_device_map()

        # Update battery for all devices
        for device_id in self._device_entity_map:
            if not device_id.startswith("__"):
                bat_id, bat_lvl = self.get_battery_info_for_device(device_id)
                if bat_id and bat_lvl is not None:
                    self.update_battery_history(device_id, bat_id, bat_lvl)

        await self._store.async_save(self.learned_data)
        self.notify_listeners(None)

    # ── Pub/sub for binary sensors ──────────────────────────────────────

    def subscribe(self, callback_func) -> Any:
        """Register a binary sensor callback for update notifications."""
        self.listeners.append(callback_func)

        def unsubscribe():
            if callback_func in self.listeners:
                self.listeners.remove(callback_func)

        return unsubscribe

    def notify_listeners(self, device_id: str | None) -> None:
        """Notify registered sensors that a re-evaluation is needed."""
        for listener in self.listeners:
            listener(device_id)
