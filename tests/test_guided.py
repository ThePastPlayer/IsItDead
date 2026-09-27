"""Physical-test evidence and recovery of temporarily suspended automations."""
import copy
import time
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import Event
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.exceptions import HomeAssistantError
from is_it_dead.test_mode import GuidedTest
from test_health import manager, add_device


async def setup_session(manager):
    device, _, temp = add_device(manager)
    manager._rebuild_device_map()
    manager._refresh_tracking()
    manager.guided_test.store = AsyncMock()
    manager.hass.states.async_set("automation.enabled", "on")
    manager.hass.states.async_set("automation.disabled", "off")
    async def off(call):
        assert manager.guided_test.store.async_save.await_count > 0
        assert call.data["stop_actions"] is False
        manager.hass.states.async_set(call.data["entity_id"], "off")
    async def on(call):
        manager.hass.states.async_set(call.data["entity_id"], "on")
    manager.hass.services.async_register("automation", "turn_off", off)
    manager.hass.services.async_register("automation", "turn_on", on)
    return device, temp


@pytest.mark.asyncio
async def test_expiry_restores_only_originally_enabled_automations(manager):
    device, _ = await setup_session(manager)
    g = manager.guided_test
    await g.start([device.id], ["automation.enabled", "automation.disabled"], 1)
    assert manager.hass.states.get("automation.enabled").state == "off"
    assert g.session["restore"] == ["automation.enabled"]
    g.session["expires_at"] = 0
    await g.tick()
    assert manager.hass.states.get("automation.enabled").state == "on"
    assert manager.hass.states.get("automation.disabled").state == "off"
    assert g.session["phase"] == "finished"


@pytest.mark.asyncio
async def test_restart_recovers_journal_after_automations_load(manager):
    device, _ = await setup_session(manager)
    await manager.guided_test.start([device.id], ["automation.enabled"], 30)
    recovered = GuidedTest(manager)
    recovered.store = AsyncMock()
    recovered.store.async_load.return_value = copy.deepcopy(manager.guided_test.session)
    await recovered.initialize()
    assert recovered.session["phase"] == "restoring"
    manager.hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await manager.hass.async_block_till_done()
    assert manager.hass.states.get("automation.enabled").state == "on"
    assert not recovered.session["restore"]
    await recovered.close()


@pytest.mark.asyncio
async def test_failure_during_suspension_rolls_back(manager):
    device, _ = await setup_session(manager)
    manager.hass.states.async_set("automation.second", "on")
    async def fail_second(call):
        if call.data["entity_id"] == "automation.second":
            raise HomeAssistantError("failure")
        manager.hass.states.async_set(call.data["entity_id"], "off")
    manager.hass.services.async_register("automation", "turn_off", fail_second)
    with pytest.raises(HomeAssistantError):
        await manager.guided_test.start([device.id], ["automation.enabled", "automation.second"], 30)
    assert manager.hass.states.get("automation.enabled").state == "on"
    assert manager.hass.states.get("automation.second").state == "on"
    assert not manager.guided_test.session["restore"]


@pytest.mark.asyncio
async def test_failed_restoration_is_kept_for_retry(manager):
    device, _ = await setup_session(manager)
    g = manager.guided_test
    await g.start([device.id], ["automation.enabled"], 30)
    async def fail(call):
        raise HomeAssistantError("temporarily unavailable")
    manager.hass.services.async_register("automation", "turn_on", fail)
    await g.stop()
    assert g.session["restore"] == ["automation.enabled"]
    assert g.session["phase"] == "restoring"
    async def recover(call):
        manager.hass.states.async_set(call.data["entity_id"], "on")
    manager.hass.services.async_register("automation", "turn_on", recover)
    await g.stop()
    assert g.session["phase"] == "finished"


@pytest.mark.asyncio
async def test_cached_reports_do_not_validate_but_value_change_does(manager):
    device, temp = await setup_session(manager)
    g = manager.guided_test
    await g.start([device.id], [], 30)
    manager.hass.states.async_set(temp.entity_id, "20", {"device_class": "temperature"})
    assert g.session["devices"][device.id]["status"] == "waiting"
    manager.hass.states.async_set(temp.entity_id, "21", {"device_class": "temperature"})
    await manager.hass.async_block_till_done()
    assert g.session["devices"][device.id]["evidence"] == "state_change"


@pytest.mark.asyncio
async def test_only_new_radio_contact_validates_zigbee(manager):
    device, temp = await setup_session(manager)
    g = manager.guided_test
    manager.zigbee.sources[device.id] = {"backend": "zha", "last_seen": time.time()-100}
    await g.start([device.id], [], 30)
    manager.hass.states.async_set(temp.entity_id, "22")
    await manager.hass.async_block_till_done()
    await g.tick()
    assert g.session["devices"][device.id]["status"] == "waiting"
    manager.zigbee.sources[device.id]["last_seen"] = time.time()
    await g.tick()
    assert g.session["devices"][device.id]["evidence"] == "radio"


@pytest.mark.asyncio
async def test_manual_confirmation_is_labelled_and_overlap_rejected(manager):
    device, _ = await setup_session(manager)
    g = manager.guided_test
    await g.start([device.id], [], 30)
    with pytest.raises(HomeAssistantError):
        await g.start([device.id], [], 30)
    g.confirm(device.id)
    assert g.session["devices"][device.id]["evidence"] == "manual"
    await g.stop()
    with pytest.raises(HomeAssistantError):
        g.confirm(device.id)


@pytest.mark.asyncio
async def test_preview_finds_entity_and_device_references_without_mutation(manager, monkeypatch):
    device, temp = await setup_session(manager)
    from homeassistant.components import automation
    monkeypatch.setattr(automation, "automations_with_entity", lambda h, e: ["automation.enabled"] if e == temp.entity_id else [])
    monkeypatch.setattr(automation, "automations_with_device", lambda h, d: ["automation.disabled"])
    p = manager.guided_test.preview([device.id])
    assert {a["entity_id"] for a in p["automations"] if a["related"]} == {"automation.enabled", "automation.disabled"}
    assert manager.hass.states.get("automation.enabled").state == "on"

@pytest.mark.asyncio
async def test_walk_defaults_to_devices_without_reliable_response(manager):
    device, _ = await setup_session(manager)
    manager.zigbee.sources[device.id] = {"backend":"zha", "last_seen":time.time()-10, "available":True}
    preview = manager.guided_test.preview()
    assert not preview["devices"][0]["needs_wake"]
    manager.zigbee.sources[device.id]["available"] = False
    assert manager.guided_test.preview()["devices"][0]["needs_wake"]
