"""Conservative, read-only dependencies for the guided automation review."""
from collections.abc import Mapping
import re

from homeassistant.components import automation
from homeassistant.helpers.template import Template

ENTITY = re.compile(r"\b[a-z_]+\.[a-z0-9_]+\b")


def templates(value):
    if isinstance(value, Template):
        yield value.template
    elif isinstance(value, str) and ("{{" in value or "{%" in value):
        yield value
    elif isinstance(value, Mapping):
        for child in value.values():
            yield from templates(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from templates(child)


def find_automation_links(hass, entity_ids, device_ids):
    """Return reasons plus uncertain templates; never execute any HA action.

    Template dependency tracking describes states read now, not every possible
    future branch. Domain-wide reads are deliberately labelled possible links.
    Unresolved templates remain visible for human review.
    """
    sources = set(entity_ids)
    indirect = set()
    # A group can be nested inside another group. Resolve the reverse closure.
    changed = True
    states = hass.states.async_all()
    while changed:
        changed = False
        for state in states:
            members = state.attributes.get("entity_id", [])
            if isinstance(members, str):
                members = [members]
            if isinstance(members, (list, tuple)) and sources.intersection(members) and state.entity_id not in sources:
                sources.add(state.entity_id)
                indirect.add(state.entity_id)
                changed = True
    reasons = {}
    uncertain = set()
    def add(eid, reason):
        reasons.setdefault(eid, set()).add(reason)
    for did in device_ids:
        for eid in automation.automations_with_device(hass, did):
            add(eid, "Appareil référencé par Home Assistant")
    for source in sources:
        for eid in automation.automations_with_entity(hass, source):
            add(eid, ("Via groupe : " if source in indirect else "Entité référencée : ") + source)
    component = hass.data.get(automation.DATA_COMPONENT)
    for item in component.entities if component else []:
        eid = item.entity_id
        # _trigger_config contains expanded blueprint inputs on supported HA.
        configs = [getattr(item, "raw_config", {}), getattr(item, "_trigger_config", []),
                   getattr(getattr(item, "action_script", None), "sequence", [])]
        for source in set(templates(configs)):
            literal = set(ENTITY.findall(source)) & sources
            if literal:
                add(eid, "Template : " + ", ".join(sorted(literal)))
            try:
                info = Template(source, hass).async_render_to_info()
                read = set(info.entities or ()) & sources
                domains = set(info.domains or ()) | set(info.domains_lifecycle or ())
                domain_read = any(x.split(".", 1)[0] in domains for x in sources)
                if info.all_states or info.all_states_lifecycle or domain_read:
                    add(eid, "Lien possible : template parcourant un domaine ou tous les états")
                elif read:
                    add(eid, "Template lisant : " + ", ".join(sorted(read)))
                if info.exception or not (literal or read or domain_read or info.all_states or info.all_states_lifecycle):
                    uncertain.add(eid)
            except Exception:
                # A missing trigger/variable must not hide an automation from review.
                uncertain.add(eid)
    return {eid: sorted(value) for eid, value in reasons.items()}, uncertain
