"""Conservative evidence rules, independent of Home Assistant for regression tests.

A state write proves that an integration wrote a value, not that a physical radio
packet arrived. Explicit last_seen is preferred; event-only sensors never acquire
an automatic silence deadline from their changes.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def timestamp(value, now):
    try:
        result = number(value)
        if result is None:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if result.tzinfo is None:
                return None
            result = result.timestamp()
        elif result > 1e11:
            result /= 1000
        return result if 0 < result <= now else None
    except (ValueError, TypeError, OverflowError):
        return None


def deadline(info, manual, minimum, maximum, multiplier, learning_days, now):
    """Manual deadlines are exact; automatic deadlines require sustained learning."""
    manual = number(manual)
    if manual is not None and manual > 0:
        return manual * 3600
    samples = [v for x in info.get("intervals", []) if (v := number(x)) is not None and v > 1]
    start = timestamp(info.get("learning_started"), now)
    if len(samples) < 8 or start is None or now - start < learning_days * 86400:
        return None
    # Upper tail tolerates variable intervals. Do not turn a known slow device
    # into a false positive by clipping its observed cadence to the max setting.
    samples.sort()
    p95 = samples[math.ceil(len(samples) * .95) - 1]
    return max(minimum * 3600, min(p95 * multiplier, maximum * 3600), p95 * 1.5)


def assess(rows, now, startup, minimum, confirmed=False):
    """Separate unavailable devices, stale heartbeat evidence, and unknown coverage."""
    eligible = [r for r in rows if r["physical"]]
    active, silent = [], []
    for row in eligible:
        age = now - row["last"] if row["last"] is not None else None
        row["elapsed_seconds"] = age
        row["overdue"] = bool(row["timeout"] is not None and age is not None and age > row["timeout"])
        if row["valid"] and age is not None and age <= (row["timeout"] or minimum * 3600):
            active.append(row["entity_id"])
        elif row["overdue"] or not row["valid"]:
            silent.append(row["entity_id"])
    candidate = None
    if eligible and all(not r["valid"] for r in eligible):
        candidate = "unavailable"
    elif not active and any(r["overdue"] for r in eligible):
        candidate = "heartbeat_overdue"
    if now - startup < 300:
        status, reason = "learning", "startup_grace"
        candidate = None
    elif candidate:
        status = "dead" if confirmed else "suspected"
        reason = candidate
    elif active:
        status, reason = "alive", "recent_integration_report"
    else:
        status, reason = "learning", "insufficient_heartbeat_evidence"
    seen = [r for r in eligible if r["last"] is not None]
    latest = max(seen, key=lambda r: r["last"]) if seen else None
    return {
        "health_status": status, "reason": reason, "candidate": candidate,
        "confidence": "medium" if candidate == "heartbeat_overdue" else "high" if candidate else "limited",
        "last_activity": datetime.fromtimestamp(latest["last"], timezone.utc).isoformat() if latest else None,
        "last_active_entity": latest["entity_id"] if latest else None,
        "silent_entities": silent, "active_entities": active, "entity_details": rows,
    }
