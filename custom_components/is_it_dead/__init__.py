"""The Is It Dead? integration."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
import os
import shutil
import yaml

from typing import Any
import voluptuous as vol

from homeassistant.components.frontend import async_register_panel, async_remove_panel
import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CUSTOM_TIMEOUTS,
    CONF_EXCLUDED_ENTITIES,
    CONF_LEARNING_PERIOD,
    CONF_MAX_TIMEOUT,
    CONF_MIN_TIMEOUT,
    CONF_MONITORED_DOMAINS,
    CONF_MULTIPLIER,
    CONF_UPDATE_INTERVAL,
    DEFAULT_LEARNING_PERIOD,
    DEFAULT_MAX_TIMEOUT,
    DEFAULT_MIN_TIMEOUT,
    DEFAULT_MONITORED_DOMAINS,
    DEFAULT_MULTIPLIER,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    PLATFORMS,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Is It Dead? from a config entry."""
    manager = IsItDeadManager(hass, entry)
    await manager.async_initialize()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = manager

    # Forward setup to platforms (binary_sensor)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register the frontend static directory (guard against re-registration on reload)
    try:
        hass.http.register_static_path(
            url="/is_it_dead_ui",
            path=hass.config.path("custom_components/is_it_dead/frontend"),
            cache_headers=False,
        )
    except Exception:  # noqa: BLE001
        pass  # Already registered from a previous load

    # Register the sidebar panel
    async_register_panel(
        hass,
        frontend_url_path="is_it_dead",
        webcomponent_name="is-it-dead-panel",
        sidebar_title="Is It Dead?",
        sidebar_icon="mdi:battery-alert",
        module_url="/is_it_dead_ui/is_it_dead_panel.js",
    )

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

    # Define custom service handlers
    async def async_handle_exclude(call) -> None:
        entity_id = call.data["entity_id"]
        for m in hass.data[DOMAIN].values():
            if entity_id in m.get_monitored_entities():
                excluded = list(m.excluded_entities)
                if entity_id not in excluded:
                    excluded.append(entity_id)
                    hass.config_entries.async_update_entry(
                        m.config_entry,
                        options={**m.config_entry.options, CONF_EXCLUDED_ENTITIES: excluded}
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
                options={**m.config_entry.options, CONF_CUSTOM_TIMEOUTS: yaml_str}
            )
            break

    # Register services with validation schemas
    if not hass.services.has_service(DOMAIN, "exclude_entity"):
        hass.services.async_register(
            DOMAIN,
            "exclude_entity",
            async_handle_exclude,
            schema=vol.Schema({
                vol.Required("entity_id"): cv.entity_id,
            }),
        )
    if not hass.services.has_service(DOMAIN, "snooze_entity"):
        hass.services.async_register(
            DOMAIN,
            "snooze_entity",
            async_handle_snooze,
            schema=vol.Schema({
                vol.Required("entity_id"): cv.entity_id,
                vol.Optional("duration_hours", default=24.0): vol.Coerce(float),
            }),
        )
    if not hass.services.has_service(DOMAIN, "relearn_entity"):
        hass.services.async_register(
            DOMAIN,
            "relearn_entity",
            async_handle_relearn,
            schema=vol.Schema({
                vol.Required("entity_id"): cv.entity_id,
            }),
        )
    if not hass.services.has_service(DOMAIN, "set_manual_timeout"):
        hass.services.async_register(
            DOMAIN,
            "set_manual_timeout",
            async_handle_set_manual_timeout,
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
        async_remove_panel(hass, "is_it_dead")

        manager = hass.data[DOMAIN].pop(entry.entry_id)
        await manager.async_unload()

        # Unregister services if this is the last entry
        if not hass.data[DOMAIN]:
            for service in ("exclude_entity", "snooze_entity", "relearn_entity", "set_manual_timeout"):
                if hass.services.has_service(DOMAIN, service):
                    hass.services.async_remove(DOMAIN, service)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await hass.config_entries.async_reload(entry.entry_id)


class IsItDeadManager:
    """Manages the state tracking, calculations, and learning logic."""

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
        self.async_add_new_entities_callback = None

    async def async_initialize(self) -> None:
        """Load storage and start tracking listeners."""
        # Load persisted data
        self.learned_data = await self._store.async_load() or {}

        # Set or maintain learning phase start timestamp
        if "learning_start_time" not in self.learned_data:
            self.learned_data["learning_start_time"] = dt_util.utcnow().isoformat()
            await self._store.async_save(self.learned_data)

        # Track active monitored entities
        monitored_entities = self.get_monitored_entities()
        if monitored_entities:
            self._unsub_state_change = async_track_state_change_event(
                self.hass, monitored_entities, self._async_handle_state_change
            )

        # Populate initial battery history for already monitored entities
        for entity_id in monitored_entities:
            bat_id, bat_lvl = self.get_battery_info(entity_id)
            if bat_id and bat_lvl is not None:
                self.update_battery_history(entity_id, bat_id, bat_lvl)

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

    def is_learning(self) -> bool:
        """Check if the global learning phase is active."""
        start_time_str = self.learned_data.get("learning_start_time")
        if not start_time_str:
            return True
        start_time = dt_util.parse_datetime(start_time_str)
        if not start_time:
            return True
        return (dt_util.utcnow() - start_time) < timedelta(
            days=self.learning_period
        )

    def get_monitored_entities(self) -> list[str]:
        """Get filtered list of all currently active monitored entities."""
        monitored = []
        entity_reg = er.async_get(self.hass)

        for state in self.hass.states.async_all():
            entity_id = state.entity_id
            domain = entity_id.split(".")[0]

            if domain not in self.monitored_domains:
                continue



            # Check if entity is managed by this integration (self-monitoring check)
            reg_entry = entity_reg.async_get(entity_id)
            if reg_entry:
                if reg_entry.disabled_by is not None:
                    continue
                if reg_entry.platform == DOMAIN:
                    continue

            if entity_id in self.excluded_entities:
                continue

            monitored.append(entity_id)

        return monitored

    def get_timeout_for_entity(self, entity_id: str) -> float:
        """Get calculated or custom timeout threshold (in seconds) for an entity."""
        # 1. Custom override
        if entity_id in self.custom_timeouts:
            return self.custom_timeouts[entity_id] * 3600.0

        # 2. Learned average-based timeout
        entities_data = self.learned_data.get("entities", {})
        entity_info = entities_data.get(entity_id)
        if entity_info and entity_info.get("count", 0) > 0:
            avg = entity_info["average_interval"]
            timeout = avg * self.multiplier
            min_sec = self.min_timeout * 3600.0
            max_sec = self.max_timeout * 3600.0
            return max(min(timeout, max_sec), min_sec)

        # 3. Fallback: use maximum timeout (learning or otherwise)
        return self.max_timeout * 3600.0

    def update_learned_data(self, entity_id: str, interval: float, count: int = 1) -> None:
        """Calculate and store running average update interval."""
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
            new_count = min(current_count + count, 50)  # Cap update history weight at 50
            entity_info["average_interval"] = (
                current_avg * (new_count - count) + interval * count
            ) / new_count
            entity_info["count"] = new_count

    def get_battery_info(self, entity_id: str) -> tuple[str | None, float | None]:
        """Get the battery entity ID and current battery level for a monitored entity."""
        state = self.hass.states.get(entity_id)
        if not state:
            return None, None

        # 1. Check if the entity itself has a battery attribute
        for attr in ("battery", "battery_level", "battery_state"):
            if attr in state.attributes:
                try:
                    return entity_id, float(state.attributes[attr])
                except (ValueError, TypeError):
                    pass

        # 2. Look up the device registry
        entity_reg = er.async_get(self.hass)
        reg_entry = entity_reg.async_get(entity_id)
        if not reg_entry or not reg_entry.device_id:
            return None, None

        device_id = reg_entry.device_id

        # Find all entities for this device and locate the battery sensor
        for entry in er.async_entries_for_device(entity_reg, device_id):
            if entry.domain == "sensor":
                sensor_state = self.hass.states.get(entry.entity_id)
                if sensor_state:
                    device_class = sensor_state.attributes.get("device_class")
                    unit = sensor_state.attributes.get("unit_of_measurement")
                    if device_class == "battery" or unit == "%" or "battery" in entry.entity_id:
                        try:
                            return entry.entity_id, float(sensor_state.state)
                        except (ValueError, TypeError):
                            pass

        return None, None

    def update_battery_history(self, entity_id: str, battery_entity_id: str, current_level: float) -> None:
        """Track battery level changes to estimate depletion time."""
        if not battery_entity_id or current_level is None:
            return

        battery_tracking = self.learned_data.setdefault("battery_tracking", {})
        battery_data = battery_tracking.setdefault(entity_id, {})
        history_list = battery_data.setdefault("history", [])

        now_iso = dt_util.utcnow().isoformat()

        if not history_list:
            history_list.append({"ts": now_iso, "val": current_level})
        else:
            last_entry = history_list[-1]
            if last_entry["val"] != current_level:
                history_list.append({"ts": now_iso, "val": current_level})

        # Cap history records at last 5 changes
        if len(history_list) > 5:
            history_list.pop(0)

    def estimate_battery_depletion(self, entity_id: str) -> dict[str, Any]:
        """Estimate remaining battery life and depletion date."""
        battery_tracking = self.learned_data.setdefault("battery_tracking", {})
        battery_data = battery_tracking.setdefault(entity_id, {})
        history_list = battery_data.setdefault("history", [])

        # Check for recharging in the history
        for idx in range(1, len(history_list)):
            if history_list[idx]["val"] > history_list[idx - 1]["val"]:
                battery_data["history"] = history_list[idx:]
                return {
                    "depletion_time": None,
                    "depletion_days": None,
                    "discharge_rate_per_day": None,
                    "status": "Battery charged, recalculating..."
                }

        if len(history_list) < 2:
            return {
                "depletion_time": None,
                "depletion_days": None,
                "discharge_rate_per_day": None,
                "status": "Learning battery discharge..."
            }

        first = history_list[0]
        last = history_list[-1]

        first_time = dt_util.parse_datetime(first["ts"])
        last_time = dt_util.parse_datetime(last["ts"])

        if not first_time or not last_time:
            return {"status": "Error parsing history"}

        time_diff = (last_time - first_time).total_seconds()
        val_diff = first["val"] - last["val"]

        # Handle battery recharging: reset tracking if level increased
        if val_diff < 0:
            battery_data["history"] = [{"ts": last["ts"], "val": last["val"]}]
            return {
                "depletion_time": None,
                "depletion_days": None,
                "discharge_rate_per_day": None,
                "status": "Battery charged, recalculating..."
            }

        if val_diff == 0 or time_diff == 0:
            return {
                "depletion_time": None,
                "depletion_days": None,
                "discharge_rate_per_day": 0.0,
                "status": "Battery stable"
            }

        rate_per_sec = val_diff / time_diff
        rate_per_day = rate_per_sec * 86400.0

        current_val = last["val"]

        if rate_per_day <= 0:
            return {
                "depletion_time": None,
                "depletion_days": None,
                "discharge_rate_per_day": 0.0,
                "status": "Battery stable"
            }

        days_remaining = current_val / rate_per_day
        depletion_dt = last_time + timedelta(days=days_remaining)

        return {
            "depletion_time": depletion_dt.isoformat(),
            "depletion_days": round(days_remaining, 1),
            "discharge_rate_per_day": round(rate_per_day, 3),
            "status": f"Estimated remaining: {round(days_remaining, 1)} days"
        }

    async def async_backfill_history(self) -> None:
        """Backfill average intervals using recorder history (runs in background)."""
        # Wait for the recorder database to be fully initialized
        try:
            from homeassistant.components.recorder import get_instance
            recorder = get_instance(self.hass)
            await recorder.async_db_ready
        except (ImportError, AttributeError):
            # Fallback for older HA versions
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
        entity_ids = self.get_monitored_entities()
        if not entity_ids:
            _LOGGER.info("No entities found to backfill history")
            return

        start_time = dt_util.utcnow() - timedelta(days=self.learning_period)
        end_time = dt_util.utcnow()

        # Initialize proposed exclusions list for entities that haven't reported yet
        entities_data = self.learned_data.setdefault("entities", {})
        for entity_id in entity_ids:
            entities_data.setdefault(
                entity_id, {"count": 0, "average_interval": 0.0}
            )

        # Query database in small chunks of 15 entities to avoid database lockups
        # Lazy import — the recorder history API location varies across HA versions
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
                    get_significant_states,
                    self.hass,
                    start_time,
                    end_time,
                    chunk,
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
                        self.update_learned_data(
                            entity_id, avg_interval, len(intervals)
                        )
                        # Prepopulate last reported time
                        last_state = states[-1]
                        last_ts = last_state.last_reported or last_state.last_updated
                        if last_ts:
                            entities_data[entity_id]["last_report_ts"] = last_ts.isoformat()

            except Exception as err:
                _LOGGER.error("History backfill failed for chunk %s: %s", chunk, err)

            # Yield control back to the event loop
            await asyncio.sleep(0.1)

        await self._store.async_save(self.learned_data)
        _LOGGER.info("Finished history backfill successfully")

        # Notify entities to recheck states now that averages are updated
        self.notify_listeners(None)

    @callback
    def _async_handle_state_change(self, event) -> None:
        """Handle real-time state change events."""
        entity_id = event.data["entity_id"]
        new_state = event.data["new_state"]

        if not new_state:
            return

        # Notify sensor immediately if state goes unavailable or unknown
        if new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            self.notify_listeners(entity_id)
            return

        new_ts = new_state.last_reported or new_state.last_updated
        if not new_ts:
            return

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
        
        # Capture battery updates if present in state attributes
        bat_id, bat_lvl = self.get_battery_info(entity_id)
        if bat_id and bat_lvl is not None:
            self.update_battery_history(entity_id, bat_id, bat_lvl)

        self.notify_listeners(entity_id)

    async def _async_handle_periodic(self, _now_time) -> None:
        """Run periodic check across all entities and save data."""
        # Periodically check and update battery records for monitored entities
        for entity_id in self.get_monitored_entities():
            bat_id, bat_lvl = self.get_battery_info(entity_id)
            if bat_id and bat_lvl is not None:
                self.update_battery_history(entity_id, bat_id, bat_lvl)

        # Periodically save learned data to disk
        await self._store.async_save(self.learned_data)
        # Notify all entities to check their dead status
        self.notify_listeners(None)

    def subscribe(self, callback_func) -> Any:
        """Register a binary sensor callback for update notifications."""
        self.listeners.append(callback_func)

        def unsubscribe():
            if callback_func in self.listeners:
                self.listeners.remove(callback_func)

        return unsubscribe

    def notify_listeners(self, entity_id: str | None) -> None:
        """Notify registered sensors that a re-evaluation is needed."""
        for listener in self.listeners:
            listener(entity_id)
