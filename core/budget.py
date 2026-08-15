#!/usr/bin/env python3
"""Atropos budget — per-router monthly token usage and quota gate, stdlib only.

Usage is estimated two ways:

  * baseline — the sum of token columns in the local Hermes state.db
    (hermes_home()/state.db or hermes_home()/data/state.db, read-only);
    any error or missing table simply counts 0,
  * live — every call to ``record(router, tokens)`` increments the counter
    in ~/.atropos/budget_usage.json, shaped ``{router: {"YYYY-MM": tokens}}``.

``over_budget()`` consults settings (budget.enabled, budget.monthly_tokens)
and ``check_and_alert()`` fires a Telegram alert and optionally fails over
to the cheapest enabled router when the gate trips.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import detect

USAGE_FILE = "budget_usage.json"


def usage_path() -> Path:
    """Location of the per-router/month token ledger."""
    return detect.atropos_home() / USAGE_FILE


def _load_usage() -> dict:
    try:
        data = json.loads(usage_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_usage(data: dict):
    usage_path().parent.mkdir(parents=True, exist_ok=True)
    usage_path().write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def month_key(dt: datetime | None = None) -> str:
    """Bucket key for a datetime: ``YYYY-MM`` (UTC, local default)."""
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m")


def _state_db_candidates() -> list:
    home = detect.hermes_home()
    return [home / "state.db", home / "data" / "state.db"]


def estimate_from_state_db(router: str | None = None) -> int:
    """Baseline tokens per router from the local Hermes state.db.

    Queries the first existing candidate db read-only, summing every
    column whose name suggests tokens (``*token*``); when ``router`` is
    given, only rows where the router column matches it are counted.
    Returns 0 on any error (db missing, no table, bad schema).
    """
    dbs = [p for p in _state_db_candidates() if p.exists()]
    if not dbs:
        return 0
    try:
        conn = sqlite3.connect(f"file:{dbs[0]}?mode=ro", uri=True)
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            total = 0
            for table in tables:
                try:
                    cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
                except Exception:
                    continue
                token_cols = [c for c in cols if "token" in c.lower()]
                if not token_cols:
                    continue
                for col in token_cols:
                    try:
                        if router:
                            rcol = next((c for c in cols if "router" in c.lower()), None)
                            if not rcol:
                                continue
                            cur = conn.execute(
                                f'SELECT COALESCE(SUM("{col}"),0) FROM "{table}" '
                                f'WHERE "{rcol}" = ?', (router,))
                        else:
                            cur = conn.execute(f'SELECT COALESCE(SUM("{col}"),0) FROM "{table}"')
                        total += int(cur.fetchone()[0] or 0)
                    except Exception:
                        continue
            return total
        finally:
            conn.close()
    except Exception:
        return 0


def record(router: str, tokens: int) -> dict:
    """Record a per-call token increment for a router in the current month.

    Returns the router's updated usage dict.
    """
    tokens = max(0, int(tokens or 0))
    data = _load_usage()
    entry = data.setdefault(router, {})
    key = month_key()
    entry[key] = int(entry.get(key, 0)) + tokens
    _save_usage(data)
    return entry


def _usage_for(router: str, month: str) -> int:
    return int(_load_usage().get(router, {}).get(month, 0) or 0)


def usage(month: str | None = None) -> dict:
    """Report usage for a month (default: current).

    Returns {per_router, total, budget, over, pct} where per_router is
    ``{router: {"estimated": n, "live": n, "total": n}}``.
    """
    from . import settings
    month = month or month_key()
    budget = int(settings.get("budget.monthly_tokens", 0) or 0)
    enabled = bool(settings.get("budget.enabled", False))

    routers = set(_load_usage().keys())
    routers.update(settings.ROUTER_NAMES)
    per_router = {}
    total = 0
    for name in sorted(routers):
        live = _usage_for(name, month)
        est = estimate_from_state_db(name)
        t = est + live
        per_router[name] = {"estimated": est, "live": live, "total": t}
        total += t

    over = bool(enabled and budget > 0 and total >= budget)
    pct = round(total / budget * 100, 1) if budget > 0 else 0.0
    return {"per_router": per_router, "total": total, "budget": budget,
            "over": over, "pct": pct}


def over_budget() -> bool:
    """True when the budget gate is enabled and usage meets/exceeds quota."""
    return usage()["over"]


def _cheapest_router() -> str | None:
    """Cheapest enabled router by total usage this month (ties → first)."""
    from . import settings
    report = usage()
    rows = sorted(
        (r for r in report["per_router"].items()
         if r[0] in settings.ROUTER_NAMES),
        key=lambda kv: (kv[1]["total"], settings.ROUTER_NAMES.index(kv[0])),
    )
    return rows[0][0] if rows else None


def check_and_alert() -> dict:
    """Run the budget gate: alert when over, fail over to the cheapest router.

    Both actions are gated: the Telegram alert needs alerts.enabled, and
    failover needs budget.auto_failover. Returns {alerted, failed_over,
    active_router, total, budget}.
    """
    from . import router as router_mod
    from . import settings

    report = usage()
    result = {"alerted": False, "failed_over": False,
              "active_router": router_mod.get().get("active", ""),
              "total": report["total"], "budget": report["budget"]}
    if not report["over"]:
        return result

    try:
        if settings.get("alerts.enabled", True):
            from . import alerts
            msg = (f"Budget exceeded: {report['total']:,} / {report['budget']:,} "
                   f"tokens ({report['pct']}%) this month")
            result["alerted"] = bool(alerts.send_alert(msg, force=True))
    except Exception:
        pass

    if settings.get("budget.auto_failover", False):
        try:
            cheapest = _cheapest_router()
            if cheapest and cheapest != result["active_router"]:
                router_mod.set_active(cheapest)
                result["failed_over"] = True
                result["active_router"] = cheapest
        except Exception:
            pass
    return result


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "record":
        record(sys.argv[2], int(sys.argv[3]))
    elif len(sys.argv) > 1 and sys.argv[1] == "check":
        print(json.dumps(check_and_alert(), indent=2, ensure_ascii=False))
    else:
        print(json.dumps(usage(), indent=2, ensure_ascii=False))
