#!/usr/bin/env python3
"""Atropos failover — router auto-failover (nain → omni → local).

Pings the active router; after ``failover.retries`` consecutive failures it
switches to the next router in ``failover.order``, persists the switch to
~/.atropos/failover_state.json and sends an alert. When every router in
the order has failed, it sets a terminal ``all_down`` state instead of
wrap-around re-pinging. A manual ``route set`` (or any successful ping)
resets the failure counter; ``failover.hold_minutes`` lets manual choices
stay authoritative for a grace period.

Settings (all via core/settings): failover.enabled (default True),
failover.order (default [nain, omni, local]), failover.retries (default 2),
failover.hold_minutes (default 60).
"""
import json
import time
from pathlib import Path

from . import detect, router, settings

STATE_FILE = "failover_state.json"


def _state_path() -> Path:
    """Failover state file (~/.atropos/failover_state.json)."""
    return detect.atropos_home() / STATE_FILE


def _default_state() -> dict:
    """Fresh failover state."""
    active = router.get().get("active", "nain")
    return {
        "active": active,
        "failures": 0,
        "last_check": 0,
        "last_switch": None,
        "switches": [],
        "all_down": False,
        "hold_until": 0,
    }


def load_state() -> dict:
    """Current failover state (persisted)."""
    try:
        p = _state_path()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                base = _default_state()
                base.update(data)
                return base
    except Exception:
        pass
    return _default_state()


def save_state(state: dict):
    """Persist failover state."""
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception:
        pass


def status() -> dict:
    """Public status payload for API/CLI."""
    state = load_state()
    state["order"] = settings.get("failover.order", ["nain", "omni", "local"])
    state["retries"] = settings.get("failover.retries", 2)
    state["enabled"] = settings.get("failover.enabled", True)
    return state


def _send_alert(message: str):
    """Telegram alert gated by alerts.events.router."""
    try:
        from .alerts import send_alert
        if settings.get("alerts.events.router", True):
            send_alert(message)
    except Exception:
        pass


def _switch_to(name: str, state: dict) -> dict:
    """Perform the switch: router.set_active + apply_all + record."""
    from .dashboard import history_log
    r = router.set_active(name)
    router.apply_all()
    now = int(time.time())
    switch = {"ts": now, "from": state.get("active") or "?", "to": name}
    switches = state.get("switches", [])
    switches.append(switch)
    if len(switches) > 50:
        switches = switches[-50:]
    state.update({
        "active": name,
        "failures": 0,
        "last_switch": now,
        "switches": switches,
        "all_down": False,
        # an automatic switch must never hold itself: resume failover
        # checks immediately (manual route set IS the hold).
        "hold_until": 0,
    })
    save_state(state)
    _send_alert(f"Router failover: {switch['from']} → {switch['to']}")
    report = f"failover: {switch['from']} → {switch['to']} (router down)"
    try:
        history_log("failover", report)
    except Exception:
        pass
    return switch


def check_now(timeout: float = 8.0) -> dict:
    """Run one failover check against the active router.

    Returns the state dict after the check. Never raises — network and
    config errors are folded into the failure accounting.
    """
    if not settings.get("failover.enabled", True):
        state = load_state()
        state["failures"] = 0
        return state

    state = load_state()
    current = router.get().get("active", "nain")
    # a manual choice outside failover resets the counter
    if state.get("active") != current:
        state["active"] = current
        state["failures"] = 0
        state["all_down"] = False

    now = int(time.time())
    order = settings.get("failover.order", ["nain", "omni", "local"])
    retries = settings.get("failover.retries", 2)
    hold = settings.get("failover.hold_minutes", 60) * 60

    # manual-choice grace period: hold the active router even if failing
    if state.get("hold_until", 0) > now:
        result = {"ok": True, "held": True, "state": state}
        state["failures"] = 0
        save_state(state)
        return result

    ping = router.ping(current, timeout=timeout)
    state["last_check"] = now

    if ping.get("ok"):
        if state.get("failures", 0) != 0 or state.get("all_down"):
            state["failures"] = 0
            state["all_down"] = False
            save_state(state)
        return {"ok": True, "ping": ping, "state": state}

    state["failures"] = state.get("failures", 0) + 1
    save_state(state)

    # If the active router is the LAST in the order, there is no switch
    # left to make — the retries grace exists only to prevent flapping
    # switches, so a single failure of the last router is terminal.
    next_name = _next_in_order(order, current)
    if next_name is None:
        first_time = not state.get("all_down")
        state["all_down"] = True
        save_state(state)
        if first_time:
            _send_alert("All routers down — failover exhausted (nain/omni/local)")
        return {"ok": False, "all_down": True, "ping": ping, "state": state}

    if state["failures"] < retries:
        return {"ok": True, "note": f"failure {state['failures']}/{retries}",
                "ping": ping, "state": state}

    switch = _switch_to(next_name, state)
    return {"ok": False, "switched": switch, "ping": ping, "state": state}


def _next_in_order(order: list, current: str):
    """Next router after ``current`` in ``order`` (no wrap-around re-ping).

    Returns None when exhausted. Only nain/omni/local (schema-validated).
    """
    order = [o for o in order if o in router.available()]
    if not order:
        return None
    try:
        idx = order.index(current)
    except ValueError:
        return order[0]
    if idx + 1 >= len(order):
        return None
    return order[idx + 1]


def mark_manual(name: str):
    """Record a manual router choice (holds failover for hold_minutes)."""
    state = load_state()
    state["active"] = name
    state["failures"] = 0
    state["all_down"] = False
    state["hold_until"] = int(time.time()) + settings.get("failover.hold_minutes", 60) * 60
    save_state(state)


def reset():
    """Reset failover state (counter + switches)."""
    state = _default_state()
    save_state(state)
    return state


if __name__ == "__main__":
    import json as _json
    if len(__import__("sys").argv) > 1 and __import__("sys").argv[1] == "status":
        print(_json.dumps(status(), indent=2, ensure_ascii=False))
    else:
        print(_json.dumps(check_now(), indent=2, ensure_ascii=False))