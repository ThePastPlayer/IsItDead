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
from homeassistant.core import HomeAssistant, SupportsResponse, callback
from homeassistant.helpers import (
    area_registry as ar,
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_state_report_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .test_mode import GuidedTest
from .zigbee import ZigbeeEvidence
from .health import assess, deadline, number, timestamp

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

    # Register once: static routes survive an integration reload.
    # Register the frontend static directory
    frontend_path = hass.config.path("custom_components/is_it_dead/frontend")
    try:
        # Modern HA (2024.7+): async_register_static_paths
        from homeassistant.components.http import StaticPathConfig
        if not hass.data.get("is_it_dead_static_registered", False):
            await hass.http.async_register_static_paths(
                [StaticPathConfig("/is_it_dead_ui", frontend_path, False)]
            )
            hass.data["is_it_dead_static_registered"] = True
        _LOGGER.debug("Registered static path via async_register_static_paths")
    except (ImportError, AttributeError):
        # Fallback for older HA versions
        try:
            hass.http.register_static_path("/is_it_dead_ui", frontend_path, False)
            _LOGGER.debug("Registered static path via register_static_path (legacy)")
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Could not register static path for frontend panel")

    # Register the sidebar panel
    from homeassistant.components.panel_custom import async_register_panel
    try:
        await async_register_panel(
            hass,
            frontend_url_path="is_it_dead",
            webcomponent_name="is-it-dead-panel-v1-3-0",
            sidebar_title="Is It Dead?",
            sidebar_icon="mdi:battery-alert",
            module_url="/is_it_dead_ui/is_it_dead_panel.js?v=1.3.0",
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

    async def async_handle_check_device(call):
        for m in hass.data[DOMAIN].values():
            if call.data["device_id"] in m._device_entity_map:
                await m.zigbee.probe(call.data["device_id"])
                m._refresh_health()
                m.notify_listeners(None)
                break

    from homeassistant.helpers.service import async_register_admin_service

    async def test_service(call):
        m = next(iter(hass.data[DOMAIN].values()))
        if call.service == "test_preview":
            return m.guided_test.preview(call.data.get("device_ids"))
        if call.service == "start_test":
            return await m.guided_test.start(call.data["device_ids"], call.data["automation_ids"], call.data["minutes"])
        if call.service == "end_test":
            return await m.guided_test.stop()
        if call.service == "confirm_test":
            return m.guided_test.confirm(call.data["device_id"])
        return m.guided_test.snapshot()

    test_schemas = {
        "test_preview": {vol.Optional("device_ids"): [cv.string]},
        "start_test": {vol.Required("device_ids"): [cv.string], vol.Required("automation_ids"): [cv.entity_id],
                       vol.Optional("minutes", default=30): vol.All(vol.Coerce(int), vol.Range(min=1, max=120))},
        "end_test": {}, "test_status": {}, "confirm_test": {vol.Required("device_id"): cv.string},
    }
    for name, schema in test_schemas.items():
        if not hass.services.has_service(DOMAIN, name):
            async_register_admin_service(hass, DOMAIN, name, test_service, schema=vol.Schema(schema),
                                         supports_response=SupportsResponse.ONLY)

    # Register device-level services
    device_schema = vol.Schema({vol.Required("device_id"): cv.string})
    for svc_name, handler in (
        ("check_device", async_handle_check_device),
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
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

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
                "relearn_device", "check_device", "test_preview", "start_test",
                "end_test", "test_status", "confirm_test",
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
        self._tracked_ids = set()
        self._startup = dt_util.utcnow().timestamp()
        self.zigbee = ZigbeeEvidence(self)
        self.guided_test = GuidedTest(self)
        self._health_cache = {}
        self._pending = {}
        self._signal_pending = {}
        self._next_publish = 0.0
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

        # Old state-change averages are not heartbeat evidence. Preserve snoozes.
        for info in self.learned_data.get("entities", {}).values():
            if "learning_started" not in info:
                info.pop("last_report_ts", None)
                info["intervals"] = []
                info["count"] = 0

        # Set or maintain learning phase start timestamp
        if "learning_start_time" not in self.learned_data:
            self.learned_data["learning_start_time"] = dt_util.utcnow().isoformat()
            await self._store.async_save(self.learned_data)

        # Build device-entity mapping and start tracking
        self._rebuild_device_map()

        self._refresh_tracking()
        await self.zigbee.refresh()
        self._refresh_health()

        await self.guided_test.initialize()

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
        await self.guided_test.close()
        await self.zigbee.close()
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

        candidates = set(self.hass.states.async_entity_ids()) | set(entity_reg.entities)
        for entity_id in sorted(candidates):
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
                    device_id = f"__standalone_{entity_id}__"
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
                        area = area_reg.async_get_area(area_id)
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

        _LOGGER.debug("get_monitored_devices returning %d devices", len(result))
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
        """Report current per-device evidence gaps, not a stale global boot date."""
        return not self._health_cache or any(
            h["health_status"] == "learning" for h in self._health_cache.values()
        )

    def get_timeout_for_device(self, device_id: str) -> float:
        rows = self._health_cache.get(device_id, {}).get("entity_details", [])
        thresholds = [r["timeout"] for r in rows if r.get("timeout")]
        return max(thresholds) if thresholds else self.max_timeout * 3600

    def update_learned_data(self, entity_id: str, interval: float, count: int = 1) -> None:
        if number(interval) is None or interval <= 1:
            return
        info = self.learned_data.setdefault("entities", {}).setdefault(entity_id, {})
        samples = info.setdefault("intervals", [])
        samples.append(interval)
        info["intervals"] = samples[-50:]
        info["count"] = len(info["intervals"])
        info["average_interval"] = sum(info["intervals"]) / info["count"]

    def _refresh_tracking(self):
        ids = set(self._entity_to_device)
        if ids == self._tracked_ids:
            return
        if self._unsub_state_change:
            self._unsub_state_change()
        self._tracked_ids = ids
        # Filtered state_reported catches unchanged values too. The filter consults
        # the current map and never subscribes to every entity in Home Assistant.
        unsubs = []
        if ids:
            unsubs = [
                async_track_state_change_event(self.hass, ids, self._async_handle_state_change),
                async_track_state_report_event(self.hass, ids, self._async_handle_state_change),
            ]
        self._unsub_state_change = lambda: [unsub() for unsub in unsubs]

    def _evidence(self, device_id):
        now = dt_util.utcnow().timestamp()
        registry = er.async_get(self.hass)
        rows = []
        native = self.zigbee.snapshot(device_id)
        seen_values = [native["last_seen"]] if native.get("last_seen") else []
        for ent in er.async_entries_for_device(registry, device_id):
            state = self.hass.states.get(ent.entity_id)
            if state and ent.disabled_by is None and not state.attributes.get("restored"):
                value = timestamp(state.state, now) if ent.entity_id.endswith("last_seen") else timestamp(state.attributes.get("last_seen"), now)
                if value is not None:
                    seen_values.append(value)
        device_last_seen = max(seen_values) if seen_values else None
        for eid in self.get_entities_for_device(device_id):
            state = self.hass.states.get(eid)
            reg = registry.async_get(eid)
            attrs = state.attributes if state else {}
            dc = attrs.get("device_class") or getattr(reg, "original_device_class", None)
            platform = getattr(reg, "platform", "")
            diagnostic = dc in ("battery", "signal_strength", "timestamp") or any(
                tag in eid for tag in ("battery", "linkquality", "rssi", "last_seen")
            )
            physical = platform != "battery_notes" and not diagnostic
            valid = bool(state and state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE) and not attrs.get("restored"))
            info = self.learned_data.get("entities", {}).get(eid, {})
            last = timestamp(info.get("last_report_ts"), now)
            if native.get("available") is False:
                valid = False
            source = "integration_report"
            explicit = device_last_seen
            if explicit is not None:
                last, source = explicit, "zigbee_last_seen" if native.get("last_seen") == explicit else "last_seen"
            manual = self.custom_timeouts.get(eid)
            timeout = deadline(info, manual, self.min_timeout, self.max_timeout,
                               self.multiplier, self.learning_period, now)
            if explicit is not None and timeout is None:
                timeout = self.max_timeout * 3600
            # Motion/contact/button changes are not a periodic physical heartbeat.
            if eid.startswith("binary_sensor.") and not manual and explicit is None:
                timeout = None
            rows.append({"entity_id": eid, "physical": physical, "valid": valid,
                         "state": state.state if state else STATE_UNAVAILABLE,
                         "last": last, "source": source, "timeout": timeout,
                         "last_reported": dt_util.utc_from_timestamp(last).isoformat() if last else None})
        # Devices exposing only a battery reading still deserve surveillance.
        if rows and not any(r["physical"] for r in rows):
            for row in rows:
                if "battery" in row["entity_id"] and getattr(registry.async_get(row["entity_id"]), "platform", "") != "battery_notes":
                    row["physical"] = True
        return rows

    def _refresh_health(self):
        now = dt_util.utcnow().timestamp()
        for did in self._device_entity_map:
            rows = self._evidence(did)
            result = assess(rows, now, self._startup, self.min_timeout)
            native = self.zigbee.snapshot(did)
            if native.get("bridge_available") is False:
                result.update(health_status="suspected", reason="zigbee_bridge_offline", candidate=None, confidence="high")
            elif result["health_status"] == "alive" and native.get("last_seen"):
                result.update(reason="recent_zigbee_report", confidence="high")
            candidate = result["candidate"]
            previous = self._pending.get(did)
            if not candidate:
                self._pending.pop(did, None)
            elif previous is None or previous[0] != candidate:
                self._pending[did] = (candidate, now)
            elif now - previous[1] >= max(300, self.update_interval * 60):
                result = assess(rows, now, self._startup, self.min_timeout, confirmed=True)
            self._health_cache[did] = result
        for did in set(self._health_cache) - set(self._device_entity_map):
            self._health_cache.pop(did, None)
            self._pending.pop(did, None)

    def evaluate_device_health(self, device_id: str) -> dict[str, Any]:
        return self._health_cache.get(device_id, {
            "health_status": "learning", "reason": "not_monitored", "confidence": "none",
            "silent_entities": [], "active_entities": [], "entity_details": [],
        })

    def is_snoozed(self, device_id):
        values = self.learned_data.get("snoozed", {})
        ids = self.get_entities_for_device(device_id)
        now = dt_util.utcnow().timestamp()
        return bool(ids) and all((timestamp(values.get(eid), float("inf")) or 0) > now for eid in ids)

    def battery_warning(self, device_id):
        _, level = self.get_battery_info_for_device(device_id)
        if level is not None and level <= 20:
            return True
        registry = er.async_get(self.hass)
        for ent in er.async_entries_for_device(registry, device_id):
            state = self.hass.states.get(ent.entity_id)
            if state and state.attributes.get("device_class") == "battery" and ent.domain == "binary_sensor" and state.state == "on":
                return True
        return False

    def network_info(self, device_id):
        registry = er.async_get(self.hass)
        readings = []
        now = dt_util.utcnow().timestamp()
        for ent in er.async_entries_for_device(registry, device_id):
            state = self.hass.states.get(ent.entity_id)
            if not state or ent.disabled_by is not None:
                continue
            value = number(state.state)
            unit = state.attributes.get("unit_of_measurement")
            if value is None:
                continue
            kind = "rssi" if unit == "dBm" else "lqi" if "linkquality" in ent.entity_id else None
            if not kind or (kind == "rssi" and not -130 <= value <= 0) or (kind == "lqi" and not 0 <= value <= 255):
                continue
            last = timestamp(self.learned_data.get("entities", {}).get(ent.entity_id, {}).get("last_report_ts"), now)
            fresh = last is not None and now - last <= min(86400, self.max_timeout * 3600)
            readings.append({"entity_id": ent.entity_id, "kind": kind, "value": value,
                             "unit": unit, "fresh": fresh, "reported_at": last, "weak": fresh and value <= (-85 if kind == "rssi" else 40)})
        return readings

    def network_warning(self, device_id):
        pending = self._signal_pending.get(device_id)
        if pending is None:
            return False
        since, first_report = pending
        return (dt_util.utcnow().timestamp() - since >= max(300, self.update_interval * 60)
                and any(r["weak"] and r["reported_at"] > first_report for r in self.network_info(device_id)))

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
                            value = number(sensor_state.state)
                            if value is not None and 0 <= value <= 100 and unit == "%":
                                return entry.entity_id, value
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
        return {"depletion_time": None, "depletion_days": None,
                "status": "No reliable lifetime prediction from percentage alone"}

    async def async_backfill_history(self) -> None:
        """Recorder stores changes, not all physical reports: never learn heartbeats from it."""
        return

    # ── Event handlers ──────────────────────────────────────────────────

    @callback
    def _async_handle_state_change(self, event) -> None:
        """Handle real-time state change events — update device-level data."""
        self.guided_test.observe(event)
        entity_id = event.data["entity_id"]
        new_state = event.data["new_state"]

        if not new_state or new_state.attributes.get("restored"):
            return

        # Find which device this entity belongs to
        device_id = self._entity_to_device.get(entity_id)

        if new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return

        new_ts = new_state.last_reported or new_state.last_updated
        native = self.zigbee.snapshot(device_id) if device_id else {}
        explicit = native.get("last_seen") or timestamp(new_state.attributes.get("last_seen"), dt_util.utcnow().timestamp())
        if explicit is not None:
            new_ts = dt_util.utc_from_timestamp(explicit)
        if not new_ts:
            return

        # Update entity-level learned data
        entities_data = self.learned_data.setdefault("entities", {})
        entity_info = entities_data.setdefault(
            entity_id, {"count": 0, "average_interval": 0.0}
        )

        entity_info.setdefault("learning_started", new_ts.isoformat())
        last_ts_str = entity_info.get("last_report_ts")
        if last_ts_str:
            last_ts = dt_util.parse_datetime(last_ts_str)
            if last_ts:
                interval = (new_ts - last_ts).total_seconds()
                if interval > 1.0:
                    self.update_learned_data(entity_id, interval)

        if last_ts_str and dt_util.parse_datetime(last_ts_str) >= new_ts:
            return
        entity_info["last_report_ts"] = new_ts.isoformat()

        # Update device-level battery tracking
        if device_id and not device_id.startswith("__"):
            bat_id, bat_lvl = self.get_battery_info_for_device(device_id)
            if bat_id and bat_lvl is not None:
                self.update_battery_history(device_id, bat_id, bat_lvl)

        # State report volume can be high; publish at most once per minute.
        now = dt_util.utcnow().timestamp()
        if now >= self._next_publish:
            self._next_publish = now + 60
            self._refresh_health()
            self.notify_listeners(None)

    async def _async_handle_periodic(self, _now_time) -> None:
        """Run periodic check across all devices and save data."""
        # Refresh device map to pick up new entities
        self._rebuild_device_map()

        self._refresh_tracking()
        await self.zigbee.refresh()
        self._refresh_health()
        if self.async_add_new_devices_callback:
            self.async_add_new_devices_callback(list(self._device_entity_map))

        # Update battery for all devices
        for device_id in self._device_entity_map:
            if not device_id.startswith("__"):
                bat_id, bat_lvl = self.get_battery_info_for_device(device_id)
                if bat_id and bat_lvl is not None:
                    self.update_battery_history(device_id, bat_id, bat_lvl)

        now = dt_util.utcnow().timestamp()
        for did in self._device_entity_map:
            weak = [r for r in self.network_info(did) if r["weak"]]
            if weak:
                self._signal_pending.setdefault(did, (now, max(r["reported_at"] for r in weak)))
            else:
                self._signal_pending.pop(did, None)
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
