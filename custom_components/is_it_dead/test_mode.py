"""Guided physical tests with durable, bounded automation suspension."""
from __future__ import annotations

import asyncio
import time

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from datetime import timedelta


class GuidedTest:
    def __init__(self, manager):
        self.manager = manager
        self.hass = manager.hass
        self.store = Store(self.hass, 1, "is_it_dead_guided_test")
        self.session = {}
        self.lock = asyncio.Lock()
        self.unsubs = []

    async def initialize(self):
        self.session = await self.store.async_load() or {}
        self.unsubs.append(async_track_time_interval(self.hass, self.tick, timedelta(seconds=2)))
        self.unsubs.append(self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self.stop))
        if self.session.get("active") or self.session.get("restore"):
            # A restart always ends the test. Wait until automations are loaded.
            self.session["phase"] = "restoring"
            if self.hass.is_running:
                await self.stop()
            else:
                self.unsubs.append(self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, self.stop))

    async def close(self):
        for unsub in self.unsubs:
            unsub()
        self.unsubs.clear()
        await self.stop()

    def preview(self, device_ids=None):
        from homeassistant.components.automation import automations_with_device, automations_with_entity
        devices = self.manager.get_monitored_devices()
        selected = set(devices if device_ids is None else device_ids)
        if not selected <= devices.keys():
            raise HomeAssistantError("Un appareil sélectionné n'est plus surveillé.")
        related = set()
        rows = []
        registry = er.async_get(self.hass)
        for did, device in devices.items():
            native = self.manager.zigbee.snapshot(did)
            health = self.manager.evaluate_device_health(did)
            recent_radio = bool(native.get("last_seen") and
                time.time() - native["last_seen"] <= min(25*3600, self.manager.max_timeout*3600) and
                native.get("available") is not False and native.get("bridge_available") is not False)
            needs_wake = native.get("available") is False or health.get("health_status") in ("dead", "suspected") or (
                not recent_radio and health.get("health_status") != "alive")
            rows.append({"device_id": did, "name": device["name"], "area": device.get("area_name"),
                         "native_radio": bool(native.get("backend")), "last_seen": native.get("last_seen"),
                         "needs_wake": needs_wake, "reason": health.get("reason"),
                         "bridge_offline": native.get("bridge_available") is False})
            if did in selected:
                related.update(automations_with_device(self.hass, did))
                for entity in er.async_entries_for_device(registry, did):
                    related.update(automations_with_entity(self.hass, entity.entity_id))
        automations = [{"entity_id": s.entity_id, "name": s.name, "state": s.state,
                        "related": s.entity_id in related}
                       for s in self.hass.states.async_all("automation")]
        rows.sort(key=lambda d: ((d.get("area") or "~").casefold(), d["name"].casefold()))
        return {"devices": rows, "automations": automations, "session": self.snapshot()}

    def snapshot(self):
        return {k: v for k, v in self.session.items() if k != "baselines"}

    async def start(self, device_ids, automation_ids, minutes):
        async with self.lock:
            if self.session.get("active") or self.session.get("restore"):
                raise HomeAssistantError("Un test ou une restauration est déjà en cours.")
            preview = self.preview(device_ids)
            if not device_ids:
                raise HomeAssistantError("Sélectionne au moins un appareil.")
            selected = set(automation_ids)
            known = {a["entity_id"]: a for a in preview["automations"]}
            if not selected <= known.keys() or any(known[e]["state"] not in ("on", "off") for e in selected):
                raise HomeAssistantError("Une automatisation sélectionnée est absente ou indisponible.")
            # Never turn on an automation which was already disabled before the test.
            restore = sorted(e for e in selected if known[e]["state"] == "on")
            self.session = {"active": True, "phase": "starting", "expires_at": time.time()+minutes*60,
                            "restore": restore, "automation_ids": sorted(selected),
                            "devices": {d["device_id"]: {**d, "status": "waiting"}
                                        for d in preview["devices"] if d["device_id"] in device_ids}}
            # Write-ahead journal: a crash during turn_off still leaves a recovery list.
            await self.store.async_save(self.session)
            try:
                for eid in restore:
                    async with asyncio.timeout(10):
                        await self.hass.services.async_call("automation", "turn_off",
                            {"entity_id": eid, "stop_actions": False}, blocking=True)
                    state = self.hass.states.get(eid)
                    if state is None or state.state != "off":
                        raise HomeAssistantError("La suspension d'une automatisation n'a pas été confirmée.")
                self.session.update(phase="active", started_at=time.time(), baselines={
                    did: self.manager.zigbee.snapshot(did).get("last_seen", 0) for did in device_ids})
                await self.store.async_save(self.session)
            except BaseException:
                await self._restore()
                raise
            return self.snapshot()

    async def _restore(self):
        if not self.session:
            return
        self.session["phase"] = "restoring"
        self.session["active"] = False
        pending = list(self.session.get("restore", []))
        errors = []
        for eid in pending:
            try:
                state = self.hass.states.get(eid)
                if state is None or state.state not in ("on", "off"):
                    raise HomeAssistantError("absente ou indisponible")
                if state.state == "off":
                    async with asyncio.timeout(10):
                        await self.hass.services.async_call("automation", "turn_on", {"entity_id": eid}, blocking=True)
                if self.hass.states.get(eid).state != "on":
                    raise HomeAssistantError("réactivation non confirmée")
                self.session["restore"].remove(eid)
                await self.store.async_save(self.session)
            except Exception:
                errors.append(eid)
        self.session["restoration_errors"] = errors
        self.session["phase"] = "restoring" if self.session.get("restore") else "finished"
        await self.store.async_save(self.session)

    async def stop(self, _event=None):
        async with self.lock:
            await self._restore()
            return self.snapshot()

    async def tick(self, _now=None):
        if self.lock.locked() or not self.session:
            return
        async with self.lock:
            if self.session.get("phase") == "restoring":
                if self.hass.is_running:
                    await self._restore()
                return
            if not self.session.get("active"):
                return
            if time.time() >= self.session["expires_at"]:
                await self._restore()
                return
            for did in self.session.get("devices", {}):
                seen = self.manager.zigbee.snapshot(did).get("last_seen", 0)
                if seen > max(self.session.get("baselines", {}).get(did, 0), self.session["started_at"]):
                    self._mark(did, "radio", seen)

    def _mark(self, did, evidence, when):
        device = self.session.get("devices", {}).get(did)
        if device and device["status"] == "waiting":
            device.update(status="observed", evidence=evidence, observed_at=when)

    @callback
    def observe(self, event):
        """Generic changes are explicitly weaker evidence than a native radio packet."""
        if self.session.get("phase") != "active":
            return
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if not old or not new or old.state in ("unknown", "unavailable") or new.state in ("unknown", "unavailable") or new.attributes.get("restored"):
            return
        did = self.manager._entity_to_device.get(new.entity_id)
        if did not in self.session.get("devices", {}) or self.manager.zigbee.snapshot(did).get("backend"):
            return
        physical = any(r["entity_id"] == new.entity_id and r["physical"] for r in self.manager._evidence(did))
        if physical and old.state != new.state and new.last_updated.timestamp() > self.session["started_at"]:
            self._mark(did, "state_change", new.last_updated.timestamp())

    def confirm(self, device_id):
        if self.session.get("phase") != "active" or device_id not in self.session.get("devices", {}):
            raise HomeAssistantError("Aucun test actif pour cet appareil.")
        self._mark(device_id, "manual", time.time())
        return self.snapshot()
