"""Native Zigbee evidence and bounded, read-only diagnostics.

No reconfigure, binding, pairing, attribute writes or background topology scans.
Adapter failures remain unknown; a timeout is never proof of a dead battery.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime

from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr

from .health import timestamp


def discover_topics(info):
    """Use MQTT discovery, never guess a broker/base topic from an entity name."""
    for entity in info.get("entities", []):
        config = entity.get("discovery_data", {}).get("payload", {})
        if not isinstance(config, dict):
            continue
        origin = config.get("origin", {}).get("name", "")
        identifiers = config.get("device", {}).get("identifiers", [])
        if origin != "Zigbee2MQTT" and not any(str(x).startswith("zigbee2mqtt_") for x in identifiers):
            continue
        state = config.get("state_topic")
        availability = config.get("availability", [])
        if isinstance(availability, dict):
            availability = [availability]
        bridge = next((a.get("topic") for a in availability if isinstance(a, dict) and a.get("topic", "").endswith("/bridge/state")), None)
        if state and bridge and state.startswith(bridge[:-len("bridge/state")]) and "/bridge/" not in state:
            return state, bridge[:-len("/bridge/state")]
    return None


class ZigbeeEvidence:
    def __init__(self, manager):
        self.manager = manager
        self.hass = manager.hass
        self.sources = {}
        self._unsubs = {}
        self._bridge = {}
        self._inventory = {}
        self._last_probe = {}
        self._probe_lock = asyncio.Lock()

    async def close(self):
        for unsub in self._unsubs.values():
            unsub()
        self._unsubs.clear()

    async def refresh(self):
        """Subscribe to native device reports; no radio requests in this scan."""
        for did in self.manager._device_entity_map:
            device = dr.async_get(self.hass).async_get(did)
            if not device:
                continue
            if any(domain == "zha" for domain, _ in device.identifiers):
                self.snapshot(did)
                continue
            if not any(domain == "mqtt" and str(value).startswith("zigbee2mqtt_") for domain, value in device.identifiers):
                continue
            try:
                from homeassistant.components.mqtt import debug_info, async_subscribe
                topics = discover_topics(debug_info.info_for_device(self.hass, did))
                if not topics:
                    continue
                topic, base = topics
                source = self.sources.setdefault(did, {})
                source.update(backend="zigbee2mqtt", topic=topic, base=base)
                for path, kind in [(topic, "report"), (topic + "/availability", "availability"),
                                   (base + "/bridge/state", "bridge"), (base + "/bridge/devices", "inventory")]:
                    key = (did, path)
                    if key not in self._unsubs:
                        @callback
                        def received(message, device_id=did, message_kind=kind):
                            self.receive(device_id, message_kind, message.payload, message.retain)
                        self._unsubs[key] = await async_subscribe(self.hass, path, received)
            except (ImportError, KeyError, AttributeError, RuntimeError, TypeError, ValueError):
                self.sources.setdefault(did, {}).update(backend="zigbee2mqtt", diagnostic="source_not_ready")

    @callback
    def receive(self, did, kind, payload, retained=False):
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            data = payload
        source = self.sources.setdefault(did, {"backend": "zigbee2mqtt"})
        if kind == "bridge":
            self._bridge[source.get("base")] = data.get("state") if isinstance(data, dict) else data
        elif kind == "inventory" and isinstance(data, list):
            self._inventory[source.get("base")] = data
        elif kind == "availability":
            value = data.get("state") if isinstance(data, dict) else data
            if value in ("online", "offline"):
                source["available"] = value == "online"
        elif kind == "report" and isinstance(data, dict):
            seen = timestamp(data.get("last_seen"), time.time())
            if seen is not None and seen >= source.get("last_seen", 0):
                source.update(last_seen=seen, evidence="zigbee_last_seen")
            if not retained:
                source["last_mqtt_received"] = time.time()
            source["last_seen_enabled"] = "last_seen" in data
        # UI refresh remains throttled by the manager; native facts are read on its next check.

    def _zha_device(self, did):
        device = dr.async_get(self.hass).async_get(did)
        if not device:
            return None
        ieee = next((value for domain, value in device.identifiers if domain == "zha"), None)
        if ieee is None:
            return None
        from homeassistant.components.zha.helpers import get_zha_gateway
        from zigpy.types import EUI64
        return get_zha_gateway(self.hass).get_device(EUI64.convert(ieee))

    def snapshot(self, did):
        source = self.sources.setdefault(did, {})
        try:
            device = self._zha_device(did)
            if device is not None:
                seen = device.last_seen
                if isinstance(seen, datetime):
                    seen = seen.timestamp()
                seen = timestamp(seen, time.time())
                source.update(backend="zha", available=device.available,
                              evidence="zigbee_last_seen", mains_powered=device.is_mains_powered)
                if seen is not None:
                    source["last_seen"] = seen
        except (ImportError, KeyError, AttributeError, RuntimeError, ValueError):
            # HA/ZHA version differences must not take down device monitoring.
            pass
        result = dict(source)
        if source.get("backend") == "zigbee2mqtt":
            result["bridge_available"] = self._bridge.get(source.get("base")) == "online"
            if source.get("base") not in self._bridge:
                result["bridge_available"] = None
        return result

    async def probe(self, did):
        """One bounded read; no command which changes a device's physical output."""
        if did not in self.manager._device_entity_map:
            return {"status": "not_monitored"}
        now = time.time()
        if now - self._last_probe.get(did, 0) < 300:
            return {"status": "cooldown", "retry_after_seconds": 300}
        if self._probe_lock.locked():
            return {"status": "busy"}
        self._last_probe[did] = now
        async with self._probe_lock:
            source = self.snapshot(did)
            try:
                async with asyncio.timeout(12):
                    if source.get("backend") == "zha":
                        device = self._zha_device(did)
                        clusters = device.async_get_clusters()
                        basic = next((types["in"][0] for types in clusters.values() if 0 in types.get("in", {})), None)
                        if basic is None:
                            result = {"status": "unsupported_read"}
                        else:
                            before = source.get("last_seen", 0)
                            await basic.read_attributes([0], allow_cache=False, only_cache=False)
                            result = {"status": "radio_reply" if self.snapshot(did).get("last_seen", 0) > before else "inconclusive"}
                    elif source.get("backend") == "zigbee2mqtt":
                        # /get support is explicitly advertised by the converter (access bit 4).
                        inventory = self._inventory.get(source.get("base"), [])
                        friendly = source["topic"][len(source["base"])+1:]
                        device = next((d for d in inventory if d.get("friendly_name") == friendly), {})
                        exposes = device.get("definition", {}).get("exposes", []) if device.get("definition") else []
                        readable = [e["property"] for e in exposes if e.get("property") in {"battery", "temperature", "humidity", "voltage", "illuminance"} and e.get("access", 0) & 4]
                        if not readable:
                            result = {"status": "no_supported_read", "note": "Passive reports remain monitored; sleeping devices cannot be woken remotely."}
                        elif source.get("bridge_available") is False:
                            result = {"status": "bridge_offline"}
                        else:
                            from homeassistant.components.mqtt import async_publish
                            before = source.get("last_seen", 0)
                            await async_publish(self.hass, source["topic"] + "/get", json.dumps({readable[0]: ""}), retain=False)
                            for _ in range(10):
                                await asyncio.sleep(1)
                                if self.snapshot(did).get("last_seen", 0) > before:
                                    break
                            result = {"status": "new_radio_contact" if self.snapshot(did).get("last_seen", 0) > before else "inconclusive"}
                    else:
                        result = {"status": "no_native_zigbee_source"}
            except TimeoutError:
                result = {"status": "inconclusive", "note": "No reply; sleeping, network fault and power loss cannot be distinguished."}
            except Exception as error:
                result = {"status": "inconclusive", "error_type": type(error).__name__}
            self.sources.setdefault(did, {})["last_probe"] = {**result, "checked_at": time.time()}
            return result
