"""Battery records: conservative lookup and durable explicit replacements."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from datetime import timedelta
import pytest
from test_health import manager, add_device
from is_it_dead.batteries import Batteries, lookup
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util


def test_catalog_avoids_guessing_variants_and_manual_entries():
    device = SimpleNamespace(manufacturer="IKEA", model="PARASOLL", model_id=None, hw_version=None)
    rows = [{"manufacturer":"IKEA", "model":"PARASOLL", "battery_type":"AAA"}]
    assert lookup(rows, device)["battery_type"] == "AAA"
    assert lookup(rows + [{**rows[0], "battery_type":"CR2032"}], device) == {"ambiguous":True}
    assert lookup([{**rows[0], "hw_version":"2"}], device) == {}
    assert lookup([{**rows[0], "battery_type":"MANUAL"}], device) == {"ambiguous":True}
    assert lookup([{**rows[0], "manufacturer":"other"}], device) == {}


@pytest.mark.asyncio
async def test_explicit_replacement_notes_history_retry_and_reload(manager):
    d, _, _ = add_device(manager)
    manager._rebuild_device_map()
    b = manager.batteries
    b.store = AsyncMock()
    today = dt_util.now().date()
    before = (today-timedelta(days=10)).isoformat()
    result = await b.save(d.id, "AAA", 2, "Lithium <test>")
    assert result["last_replaced"] is None
    assert result["confirmed"]
    result = await b.save(d.id, "AAA", 2, "Lithium <test>", before, "first")
    assert result["days_since_replacement"] == 10
    assert result["history_count"] == 1
    await b.save(d.id, "AAA", 2, "Lithium <test>", before, "first")
    assert b.snapshot(d.id)["history_count"] == 1
    await b.save(d.id, "AAA", 2, "Alcalines", today.isoformat(), "second")
    result = await b.save(d.id, "AAA", 2, "Note corrigée")
    assert result["history"][1]["comment"] == "Lithium <test>"
    assert result["history"][0]["comment"] == "Alcalines"
    assert result["days_since_replacement"] == 0
    restored = Batteries(manager)
    restored.store = AsyncMock()
    restored.store.async_load.return_value = b.store.async_save.call_args.args[0]
    await restored.initialize()
    assert restored.snapshot(d.id, True)["history"] == result["history"]
    assert len(restored.catalog) > 2000
    with pytest.raises(HomeAssistantError):
        await b.save(d.id, "AAA", 2, "", (today+timedelta(days=1)).isoformat(), "future")
    b.store.async_save.side_effect = OSError("disk unavailable")
    with pytest.raises(OSError):
        await b.save(d.id, "AA", 1, "not saved")
    assert b.snapshot(d.id)["battery_type"] == "AAA"


@pytest.mark.asyncio
async def test_battery_notes_fallback_preserved_on_first_local_edit(manager):
    from homeassistant.helpers import entity_registry as er
    d, _, _ = add_device(manager)
    manager._rebuild_device_map()
    reg = er.async_get(manager.hass)
    entry = reg.async_get_or_create("sensor", "battery_notes", "test_battery_type", device_id=d.id)
    manager.hass.states.async_set(entry.entity_id,"2x AAA",{"battery_type":"AAA","battery_quantity":2,"note":"Ancienne note","battery_last_replaced":"2026-01-01T12:00:00+00:00"})
    b=manager.batteries;b.store=AsyncMock()
    assert b.snapshot(d.id)["battery_type"] == "AAA"
    result=await b.save(d.id,"CR2032",1,"Ma note")
    assert result["last_replaced"] == "2026-01-01"
    assert result["history"][0]["source"] == "Battery Notes"
    assert b.snapshot(d.id)["battery_type"] == "CR2032"
    assert manager.hass.states.get(entry.entity_id).attributes["note"] == "Ancienne note"


@pytest.mark.asyncio
async def test_import_preserves_archives_dates_notes_and_is_idempotent(manager, tmp_path):
    import json
    from unittest.mock import Mock
    d, _, _=add_device(manager);manager._rebuild_device_map()
    sub=SimpleNamespace(subentry_type="battery_note",subentry_id="note1",title="Capteur WC",
        data={"device_id":d.id,"battery_type":"AAA","battery_quantity":2,"note":"Lithium"})
    manager.hass.config_entries.async_entries.return_value=[SimpleNamespace(subentries={"note1":sub},data={})]
    path=tmp_path/'.storage/battery_notes.storage';path.parent.mkdir(exist_ok=True)
    source={"data":{"devices":[{"device_id":d.id,"battery_last_replaced":"2025-01-01T12:00:00+00:00"},
                                {"device_id":"old-device","battery_last_replaced":"2024-01-01T12:00:00+00:00"}]}}
    path.write_text(json.dumps(source))
    b=manager.batteries;b.store=AsyncMock()
    result=await b.import_battery_notes(True)
    assert result["imported"]==2 and result["imported_dates"]==2
    assert b.records=={} and not b.store.async_save.called
    await b.import_battery_notes(False)
    assert b.snapshot(d.id)["comment"]=="Lithium"
    assert b.snapshot(d.id)["last_replaced"]=="2025-01-01"
    assert b.snapshot("old-device")["last_replaced"]=="2024-01-01"
    assert any(d["device_id"]=="old-device" for d in b.list_profiles())
    assert (await b.import_battery_notes(False))["imported"]==0
    assert json.loads(path.read_text())==source
    # The archive remains editable even without a monitored physical device.
    await b.save("old-device","CR2032",1,"Ancienne fiche")
    assert b.snapshot("old-device")["battery_type"]=="CR2032"
