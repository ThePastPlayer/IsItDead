"""Local battery records, explicit replacement history and community suggestions."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date
import json
from pathlib import Path

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util


def norm(value):
    return str(value or "").strip().casefold()


def lookup(rows, device):
    """Require manufacturer/model; never guess between different battery variants."""
    matches = []
    for row in rows:
        if norm(row.get("manufacturer")) != norm(device.manufacturer):
            continue
        expected, model = norm(row.get("model")), norm(device.model)
        method = row.get("model_match_method", "exact")
        matched = {"exact": model == expected, "startswith": model.startswith(expected),
                   "endswith": model.endswith(expected), "contains": expected in model}.get(method, False)
        if not expected or not matched:
            continue
        if any(row.get(key) and norm(row[key]) != norm(getattr(device, key, None)) for key in ("model_id", "hw_version")):
            continue
        score = (method == "exact", sum(bool(row.get(k)) for k in ("model_id", "hw_version")))
        matches.append((score, row))
    if not matches:
        return {}
    best = max(score for score, row in matches)
    rows = [row for score, row in matches if score == best]
    values = {(row["battery_type"], row.get("battery_quantity", 1)) for row in rows}
    if len(values) != 1 or any(norm(t) == "manual" for t, q in values):
        return {"ambiguous": True}
    battery_type, quantity = values.pop()
    return {"battery_type": battery_type, "quantity": quantity, "source": "Battery Notes — catalogue"}


class Batteries:
    def __init__(self, manager):
        self.manager = manager
        self.hass = manager.hass
        self.store = Store(self.hass, 1, "is_it_dead_batteries_" + manager.config_entry.entry_id)
        self.records = {}
        self.catalog = []
        self.cache = {}
        self.lock = asyncio.Lock()

    async def initialize(self):
        self.records = await self.store.async_load() or {}
        def load():
            return json.loads((Path(__file__).parent / "data/battery_library.json").read_text(encoding="utf-8"))["devices"]
        self.catalog = await self.hass.async_add_executor_job(load)
        self.cache.clear()

    def external(self, did):
        result = {}
        for entry in er.async_entries_for_device(er.async_get(self.hass), did):
            if entry.platform != "battery_notes":
                continue
            state = self.hass.states.get(entry.entity_id)
            if not state:
                continue
            attrs = state.attributes
            if attrs.get("battery_type") and norm(attrs["battery_type"]) not in ("unknown", "unavailable"):
                result.update(battery_type=str(attrs["battery_type"]), quantity=attrs.get("battery_quantity", 1),
                              comment=attrs.get("note", ""), source="Battery Notes — intégration")
            value = attrs.get("battery_last_replaced")
            if entry.unique_id and entry.unique_id.endswith("_battery_last_replaced"):
                value = state.state
            if value:
                parsed = dt_util.parse_datetime(str(value))
                if parsed:
                    result["last_replaced"] = dt_util.as_local(parsed).date().isoformat()
        return result

    def snapshot(self, did, full=False):
        device = dr.async_get(self.hass).async_get(did)
        key = tuple(getattr(device, k, None) for k in ("manufacturer", "model", "model_id", "hw_version"))
        if key not in self.cache:
            self.cache[key] = lookup(self.catalog, device) if device else {}
        suggested = self.cache[key]
        external = self.external(did)
        record = self.records.get(did, {})
        result = {"battery_type": "", "quantity": 1, "comment": "", "last_replaced": None,
                  "confirmed": False, **suggested, **external}
        result.update({k: v for k, v in record.items() if k not in ("history", "battery_notes_import")})
        result["suggestion"] = suggested
        history = sorted(record.get("history", []), key=lambda x: (x["date"], x.get("recorded_at", "")), reverse=True)
        if history:
            result["last_replaced"] = history[0]["date"]
        result["history_count"] = len(history)
        if full:
            result["history"] = deepcopy(history)
        try:
            result["days_since_replacement"] = max(0, (dt_util.now().date() - date.fromisoformat(result["last_replaced"])).days)
        except (TypeError, ValueError):
            result["days_since_replacement"] = None
        return result

    def list_profiles(self):
        devices = self.manager.get_monitored_devices()
        result = []
        for did in set(devices) | set(self.records):
            profile = self.snapshot(did)
            result.append({**profile, "device_id": did, "name": devices.get(did, {}).get("name") or profile.get("name") or did,
                           "monitored": did in devices})
        return sorted(result, key=lambda x: x["name"].casefold())

    async def import_battery_notes(self, dry_run=True):
        """Copy source records before removal; keep local edits and source metadata."""
        entries = self.hass.config_entries.async_entries("battery_notes")
        configs = []
        wrapped = []
        for entry in entries:
            for sub in getattr(entry, "subentries", {}).values():
                if sub.subentry_type == "battery_note":
                    configs.append({"title": sub.title, "data": dict(sub.data), "source_id": sub.subentry_id})
            if entry.data.get("device_id"):
                configs.append({"title": entry.title, "data": dict(entry.data), "source_id": entry.entry_id})
            runtime = getattr(entry, "runtime_data", None)
            for coordinator in getattr(runtime, "subentry_coordinators", {}).values():
                battery = getattr(coordinator, "wrapped_battery", None)
                if battery:
                    wrapped.append(battery.entity_id)
        def load():
            path = Path(self.hass.config.path(".storage/battery_notes.storage"))
            if not path.exists():
                return []
            return json.loads(path.read_text(encoding="utf-8")).get("data", {}).get("devices", [])
        stored = await self.hass.async_add_executor_job(load)
        async with self.lock:
            records = deepcopy(self.records)
            imported = []
            source_rows = {r.get("device_id"): r for r in stored if r.get("device_id")}
            candidates = list(configs)
            configured = {r["data"].get("device_id") for r in configs}
            candidates += [{"title": "", "data": {"device_id": did}, "source_id": did}
                           for did in source_rows if did not in configured]
            dates = 0
            for candidate in candidates:
                data = candidate["data"]
                did = data.get("device_id")
                if not did:
                    raise HomeAssistantError("Une fiche Battery Notes sans appareil doit être migrée manuellement avant retrait.")
                key = did
                # Preserve multiple notes for one device as separate archive records.
                if key in records:
                    if records[key].get("battery_notes_import", {}).get("source_id") == candidate["source_id"]:
                        continue
                    key = "battery-notes:" + candidate["source_id"]
                if key in records:
                    continue
                device = dr.async_get(self.hass).async_get(did)
                name = candidate["title"] or (device.name_by_user or device.name if device else None) or "Ancien appareil " + did
                storage = source_rows.get(did, {})
                external = self.external(did)
                profile = {"name": name, "battery_type": data.get("battery_type", external.get("battery_type", "")),
                           "quantity": data.get("battery_quantity", external.get("quantity", 1)),
                           "comment": data.get("note", external.get("comment", "")),
                           "confirmed": bool(data.get("battery_type")), "source": "Import Battery Notes",
                           "history": [], "battery_notes_import": {"source_id": candidate["source_id"],
                           "device_id": did, "config": data, "storage": storage}}
                value = storage.get("battery_last_replaced")
                parsed = dt_util.parse_datetime(str(value)) if value else None
                date_value = dt_util.as_local(parsed).date().isoformat() if parsed else external.get("last_replaced")
                if date_value:
                    profile["history"].append({"id": "battery-notes-import", "date": date_value,
                        "original_timestamp": value, "source": "Battery Notes", "battery_type": profile["battery_type"],
                        "quantity": profile["quantity"], "comment": profile["comment"]})
                    dates += 1
                records[key] = profile
                imported.append(key)
            if not dry_run:
                await self.store.async_save(records)
                self.records = records
                self.manager.notify_listeners(None)
            return {"dry_run": dry_run, "configured_notes": len(configs), "source_storage_rows": len(stored),
                    "imported": len(imported), "imported_dates": dates, "record_ids": imported,
                    "wrapped_entities": sorted(set(wrapped))}

    async def save(self, did, battery_type, quantity, comment, replacement_date=None, request_id=None):
        if did not in self.manager.get_monitored_devices() and did not in self.records:
            raise HomeAssistantError("Cet appareil n'est plus surveillé.")
        if replacement_date:
            try:
                parsed = date.fromisoformat(replacement_date)
            except ValueError as err:
                raise HomeAssistantError("Date de remplacement invalide.") from err
            if parsed > dt_util.now().date():
                raise HomeAssistantError("La date de remplacement ne peut pas être dans le futur.")
            if not request_id:
                raise HomeAssistantError("Identifiant du remplacement manquant.")
        async with self.lock:
            records = deepcopy(self.records)
            current = records.setdefault(did, {})
            history = current.setdefault("history", [])
            if replacement_date and any(e["id"] == request_id for e in history):
                return self.snapshot(did, True)
            # Preserve an existing Battery Notes replacement when making the first local edit.
            external = self.external(did)
            external_date = external.get("last_replaced")
            if not history and external_date:
                history.append({"id": "battery-notes-import", "date": external_date,
                                "comment": external.get("comment", ""), "source": "Battery Notes",
                                "battery_type": external.get("battery_type", ""), "quantity": external.get("quantity", 1)})
            current.update(battery_type=battery_type.strip(), quantity=quantity, comment=comment.strip(),
                           confirmed=bool(battery_type.strip()), source="Fiche IsItDead")
            if replacement_date:
                history.append({"id": request_id, "date": replacement_date, "recorded_at": dt_util.utcnow().isoformat(),
                                "battery_type": battery_type.strip(), "quantity": quantity, "comment": comment.strip(),
                                "source": "IsItDead"})
            # Publish only after durable storage succeeds.
            await self.store.async_save(records)
            self.records = records
            self.manager.notify_listeners(did)
            return self.snapshot(did, True)
