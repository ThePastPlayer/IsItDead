"""Regression scenarios using HA's real state machine and entity registry."""
import importlib.util
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components"))

from is_it_dead.health import assess, deadline, number, timestamp
from is_it_dead import IsItDeadManager
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, device_registry as dr
from unittest.mock import Mock

NOW = datetime(2026, 9, 27, tzinfo=timezone.utc).timestamp()


def row(**changes):
    return dict(entity_id="sensor.temperature", physical=True, valid=True,
                last=NOW - 60, timeout=3600, **changes) if not changes else {
        **row(), **changes}


def test_unavailable_never_counts_as_recent_activity():
    r = assess([row(valid=False, last=NOW)], NOW, NOW - 1000, 1, True)
    assert r["health_status"] == "dead"
    assert r["active_entities"] == []


def test_startup_grace_and_confirmation():
    rows = [row(valid=False)]
    assert assess(rows, NOW, NOW - 10, 1, True)["health_status"] == "learning"
    assert assess(rows, NOW, NOW - 1000, 1)["health_status"] == "suspected"


def test_quiet_door_has_no_inferred_deadline():
    assert assess([row(last=NOW - 999999, timeout=None)], NOW, NOW-1000, 1, True)["health_status"] == "learning"


def test_metadata_cannot_keep_physical_device_alive():
    r = assess([row(last=NOW-10000), row(physical=False)], NOW, NOW-1000, 1, True)
    assert r["health_status"] == "dead"


def test_healthy_sibling_prevents_dead_device():
    r = assess([row(last=NOW-10000), row()], NOW, NOW-1000, 1, True)
    assert r["health_status"] == "alive"


def test_manual_timeout_is_not_multiplied_or_clipped():
    assert deadline({}, .5, 1, 168, 3, 7, NOW) == 1800


def test_learning_is_per_entity_and_uses_upper_tail():
    info = {"intervals": [100]*48 + [3600]*2, "learning_started": NOW-8*86400}
    assert deadline(info, None, 1, 168, 3, 7, NOW) == 3600
    info["learning_started"] = NOW-86400
    assert deadline(info, None, 1, 168, 3, 7, NOW) is None


def test_known_slow_reports_are_not_clipped_to_false_deadline():
    info = {"intervals": [10*86400]*10, "learning_started": NOW-100*86400}
    assert deadline(info, None, 1, 168, 3, 7, NOW) >= 15*86400


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", None, "unknown"])
def test_invalid_numbers(value):
    assert number(value) is None


def test_timestamps_reject_future_naive_and_accept_milliseconds():
    assert timestamp(NOW+100, NOW) is None
    assert timestamp("2026-09-26T12:00:00", NOW) is None
    assert timestamp((NOW-10)*1000, NOW) == NOW-10


@pytest.fixture
async def manager(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    entry = Mock(options={}, data={}, entry_id="test")
    manager = IsItDeadManager(hass, entry)
    manager.learned_data = {"entities": {}}
    hass.config_entries = Mock()
    hass.config_entries.async_get_entry.return_value = Mock(disabled_by=None, domain="test", entry_id="test", pref_disable_new_entities=False)
    hass.data[dr.DATA_REGISTRY] = dr.DeviceRegistry(hass)
    await dr.async_get(hass).async_load()
    await er.async_get(hass).async_load()
    yield manager
    if manager._unsub_state_change:
        manager._unsub_state_change()
    await hass.async_stop(force=True)


def add_device(manager, suffix="one"):
    hass = manager.hass
    device = dr.async_get(hass).async_get_or_create(config_entry_id="test", identifiers={("test", suffix)})
    registry = er.async_get(hass)
    battery = registry.async_get_or_create("sensor", "test", "bat-"+suffix, device_id=device.id, original_device_class="battery")
    temp = registry.async_get_or_create("sensor", "test", "temp-"+suffix, device_id=device.id, original_device_class="temperature")
    hass.states.async_set(battery.entity_id, "15", {"device_class": "battery", "unit_of_measurement": "%"})
    hass.states.async_set(temp.entity_id, "20", {"device_class": "temperature"})
    return device, battery, temp


@pytest.mark.asyncio
async def test_dynamic_discovery_unchanged_reports_and_low_battery(manager):
    device, battery, temp = add_device(manager)
    manager._rebuild_device_map()
    manager._refresh_tracking()
    manager.hass.states.async_set(temp.entity_id, "20", {"device_class": "temperature"})
    assert "last_report_ts" in manager.learned_data["entities"][temp.entity_id]
    assert manager.battery_warning(device.id)
    other, _, new_temp = add_device(manager, "two")
    manager._rebuild_device_map()
    manager._refresh_tracking()
    manager.hass.states.async_set(new_temp.entity_id, "20", {"device_class": "temperature"})
    assert new_temp.entity_id in manager.learned_data["entities"]
    manager.hass.states.async_remove(new_temp.entity_id)
    manager._rebuild_device_map()
    assert new_temp.entity_id in manager.get_entities_for_device(other.id)


@pytest.mark.asyncio
async def test_unavailable_does_not_overwrite_last_good_report(manager):
    device, _, temp = add_device(manager)
    manager._rebuild_device_map(); manager._refresh_tracking()
    manager.hass.states.async_set(temp.entity_id, "21")
    previous = manager.learned_data["entities"][temp.entity_id]["last_report_ts"]
    manager.hass.states.async_set(temp.entity_id, "unavailable")
    assert manager.learned_data["entities"][temp.entity_id]["last_report_ts"] == previous


@pytest.mark.asyncio
async def test_explicit_last_seen_beats_cached_state_write(manager):
    device, _, temp = add_device(manager)
    manager._rebuild_device_map(); manager._refresh_tracking()
    old = (datetime.now(timezone.utc)-timedelta(days=10)).isoformat()
    manager.hass.states.async_set(temp.entity_id, "20", {"last_seen": old})
    rows = manager._evidence(device.id)
    temp_row = next(r for r in rows if r["entity_id"] == temp.entity_id)
    assert temp_row["source"] == "last_seen"
    assert temp_row["last"] == datetime.fromisoformat(old).timestamp()


@pytest.mark.asyncio
async def test_restored_state_and_voltage_are_not_fresh_battery_evidence(manager):
    device, battery, temp = add_device(manager)
    manager._rebuild_device_map(); manager._refresh_tracking()
    manager.hass.states.async_set(temp.entity_id, "21", {"restored": True})
    assert temp.entity_id not in manager.learned_data["entities"]
    manager.hass.states.async_set(battery.entity_id, "3.0", {"device_class": "battery", "unit_of_measurement": "V"})
    assert manager.get_battery_info_for_device(device.id)[1] is None
    assert not manager.battery_warning(device.id)


@pytest.mark.asyncio
async def test_low_signal_requires_persistence_and_fresh_data(manager):
    device, _, _ = add_device(manager)
    ent = er.async_get(manager.hass).async_get_or_create("sensor", "test", "rssi", device_id=device.id)
    manager._rebuild_device_map(); manager._refresh_tracking()
    manager.hass.states.async_set(ent.entity_id, "-95", {"unit_of_measurement": "dBm"})
    assert manager.network_info(device.id)[0]["weak"]
    assert not manager.network_warning(device.id)
    first_report = manager.network_info(device.id)[0]["reported_at"]
    manager._signal_pending[device.id] = (datetime.now(timezone.utc).timestamp()-1000, first_report)
    assert not manager.network_warning(device.id)  # One old sample is not persistence.
    manager.hass.states.async_set(ent.entity_id, "-96", {"unit_of_measurement": "dBm"})
    assert manager.network_warning(device.id)
    manager.learned_data["entities"][ent.entity_id]["last_report_ts"] = "2020-01-01T00:00:00+00:00"
    assert not manager.network_info(device.id)[0]["weak"]


@pytest.mark.asyncio
async def test_low_battery_aggregate_and_snooze(manager):
    from is_it_dead.binary_sensor import IsItDeadAlert, IsItDeadDeviceSensor
    device, _, _ = add_device(manager)
    manager._rebuild_device_map(); manager._refresh_health()
    aggregate = IsItDeadAlert(manager)
    sensor = IsItDeadDeviceSensor(manager, device.id)
    from homeassistant.helpers import area_registry as ar
    await ar.async_get(manager.hass).async_load()
    area = ar.async_get(manager.hass).async_create("Living room")
    dr.async_get(manager.hass).async_update_device(device.id, area_id=area.id)
    assert manager.get_monitored_devices()[device.id]["area_name"] == "Living room"
    assert aggregate.is_on and sensor.is_on
    assert aggregate.extra_state_attributes["alert_device_ids"] == [device.id]
    until = (datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
    manager.learned_data["snoozed"] = {eid: until for eid in manager.get_entities_for_device(device.id)}
    assert not aggregate.is_on and not sensor.is_on


def test_supported_core_setup_apis():
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.components.panel_custom import async_register_panel
    import inspect
    assert hasattr(ConfigEntry, "add_update_listener")
    assert inspect.iscoroutinefunction(async_register_panel)


@pytest.mark.asyncio
async def test_native_offline_overrides_cached_values_and_bridge_fault_is_separate(manager):
    import time
    device, _, temp = add_device(manager)
    manager._rebuild_device_map()
    manager._startup = time.time()-10000
    manager.zigbee.sources[device.id] = {"backend":"zigbee2mqtt", "base":"z", "available":False, "last_seen":time.time()-100}
    manager.zigbee._bridge["z"] = "online"
    manager._refresh_health()
    assert manager.evaluate_device_health(device.id)["candidate"] == "unavailable"
    manager._pending[device.id] = ("unavailable", time.time()-10000)
    manager._refresh_health()
    assert manager.evaluate_device_health(device.id)["health_status"] == "dead"
    manager.zigbee._bridge["z"] = "offline"
    manager._refresh_health()
    assert manager.evaluate_device_health(device.id)["reason"] == "zigbee_bridge_offline"
    assert manager.evaluate_device_health(device.id)["health_status"] == "suspected"


@pytest.mark.asyncio
async def test_mqtt_republish_is_not_a_new_radio_packet(manager):
    import time
    device, _, _ = add_device(manager)
    z = manager.zigbee
    z.receive(device.id, "report", '{"temperature":20}', False)
    assert "last_seen" not in z.snapshot(device.id)
    old = time.time()-10000
    import json
    z.receive(device.id, "report", json.dumps({"last_seen":old}), True)
    z.receive(device.id, "report", json.dumps({"last_seen":old-50}), False)
    assert z.snapshot(device.id)["last_seen"] == old


@pytest.mark.asyncio
async def test_probe_without_supported_read_never_marks_device_dead(manager):
    device, _, _ = add_device(manager)
    manager._rebuild_device_map()
    manager.zigbee.sources[device.id] = {"backend":"zigbee2mqtt", "topic":"z/sensor", "base":"z"}
    assert (await manager.zigbee.probe(device.id))["status"] == "no_supported_read"
    assert (await manager.zigbee.probe(device.id))["status"] == "cooldown"
    assert "available" not in manager.zigbee.sources[device.id]


def test_discovery_topics_do_not_assume_default_base():
    from is_it_dead.zigbee import discover_topics
    info = {"entities":[{"discovery_data":{"payload":{"origin":{"name":"Zigbee2MQTT"}, "state_topic":"house/zigbee/door", "availability":[{"topic":"house/zigbee/bridge/state"}]}}}]}
    assert discover_topics(info) == ("house/zigbee/door", "house/zigbee")
    assert discover_topics({"entities":[]}) is None
