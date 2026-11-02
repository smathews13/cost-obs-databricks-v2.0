"""Cost-alert evaluation and delivery.

The Alerts settings section persists thresholds (daily budget, per-workspace budget,
day-over-day spike %, anomaly sensitivity) and a Slack webhook — but on its own that
was configuration only: nothing compared actual spend to the thresholds or delivered
anything. This module closes that gap.

`evaluate_alerts()` compares the latest day's spend (from the app's own managed tables —
account spend, not shared sources) against the configured thresholds and returns the
breaches. `run_alert_check(send=...)` evaluates and, when a Slack webhook is configured,
posts a consolidated digest. It runs once nightly after the MV refresh (gated on a
webhook being set — configuring one is the opt-in) and can be triggered manually from
the Alerts section.
"""
import logging

logger = logging.getLogger(__name__)


def evaluate_alerts() -> dict:
    """Compare the latest day's spend to configured thresholds. Never raises."""
    from backend.db import execute_query, get_catalog_schema
    from backend.routers.settings import _load_alert_thresholds, anomaly_spike_threshold

    catalog, schema = get_catalog_schema()
    if not (catalog and schema):
        return {"breaches": [], "evaluated": None, "error": "storage not configured"}

    th = _load_alert_thresholds()
    daily_budget = float(th.get("daily_budget") or 0)
    ws_budget = float(th.get("workspace_budget") or 0)
    spike_threshold = anomaly_spike_threshold()

    breaches: list[dict] = []
    latest_date = None
    latest_spend = 0.0
    try:
        # Alerts are on the account's OWN spend — base tables, not the unified views,
        # so a Delta-shared source can't distort a budget/spike alert.
        delta_sql = (
            f"SELECT usage_date, SUM(total_spend) AS s FROM `{catalog}`.`{schema}`.`daily_usage_summary` "
            f"GROUP BY usage_date ORDER BY usage_date DESC LIMIT 2"
        )
        rows = execute_query(delta_sql, no_cache=True)
        from backend.lakebase_read import dual_read
        rows = dual_read(
            "alerts_daily",
            "SELECT usage_date, SUM(total_spend) AS s FROM daily_usage_summary "
            "GROUP BY usage_date ORDER BY usage_date DESC LIMIT 2",
            {},
            "",
            rows,
            ["usage_date"],
        )
        if rows:
            latest_date = str(rows[0].get("usage_date"))
            latest_spend = float(rows[0].get("s") or 0)
            if daily_budget and latest_spend > daily_budget:
                breaches.append({"type": "daily_budget", "date": latest_date,
                                 "value": latest_spend, "threshold": daily_budget})
            if len(rows) > 1:
                prev = float(rows[1].get("s") or 0)
                if prev > 0:
                    change_pct = (latest_spend - prev) / prev * 100
                    if abs(change_pct) >= spike_threshold:
                        breaches.append({"type": "spike", "date": latest_date, "value": latest_spend,
                                         "change_percent": round(change_pct, 1), "threshold": spike_threshold})
    except Exception as e:
        logger.warning("Alert evaluation (daily) failed: %s", e)
        return {"breaches": [], "evaluated": None, "error": str(e)[:200]}

    if ws_budget and latest_date:
        try:
            ws_sql = (
                f"SELECT workspace_id, SUM(total_spend) AS s "
                f"FROM `{catalog}`.`{schema}`.`daily_workspace_breakdown` "
                f"WHERE usage_date = :d GROUP BY workspace_id HAVING SUM(total_spend) > :b "
                f"ORDER BY s DESC LIMIT 10"
            )
            ws_rows = execute_query(ws_sql, {"d": latest_date, "b": ws_budget}, no_cache=True)
            from backend.lakebase_read import dual_read
            ws_rows = dual_read(
                "alerts_workspace",
                "SELECT workspace_id, SUM(total_spend) AS s FROM daily_workspace_breakdown "
                "WHERE usage_date = %(d)s GROUP BY workspace_id "
                "HAVING SUM(total_spend) > %(b)s ORDER BY s DESC LIMIT 10",
                {"d": latest_date, "b": ws_budget},
                "",
                ws_rows or [],
                ["workspace_id"],
            )
            for r in (ws_rows or []):
                breaches.append({"type": "workspace_budget", "date": latest_date,
                                 "workspace_id": str(r.get("workspace_id")),
                                 "value": float(r.get("s") or 0), "threshold": ws_budget})
        except Exception as e:
            logger.debug("Alert evaluation (per-workspace) skipped: %s", e)

    return {"breaches": breaches, "evaluated": {"date": latest_date, "daily_spend": latest_spend},
            "thresholds": {"daily_budget": daily_budget, "workspace_budget": ws_budget,
                           "spike_threshold_percent": spike_threshold}}


def _format_slack(breaches: list[dict]) -> str:
    lines = [":rotating_light: *Cost Observability — alerts*"]
    for b in breaches:
        if b["type"] == "daily_budget":
            lines.append(f"• *Daily budget exceeded* on {b['date']}: ${b['value']:,.2f} (limit ${b['threshold']:,.0f})")
        elif b["type"] == "spike":
            lines.append(f"• *Spend spike* on {b['date']}: {b['change_percent']:+.1f}% day-over-day (≥ {b['threshold']:.0f}%)")
        elif b["type"] == "workspace_budget":
            lines.append(f"• *Workspace budget exceeded* ({b['workspace_id']}) on {b['date']}: ${b['value']:,.2f} (limit ${b['threshold']:,.0f})")
    return "\n".join(lines)


def run_alert_check(send: bool = True) -> dict:
    """Evaluate alerts and, when a Slack webhook is configured and `send`, post a digest.
    Returns the evaluation plus a `sent` flag. Never raises."""
    import httpx

    from backend.routers.settings import _load_webhook_settings

    result = evaluate_alerts()
    breaches = result.get("breaches", [])
    result["sent"] = False
    if not send or not breaches:
        return result

    url = (_load_webhook_settings() or {}).get("slack_webhook_url", "")
    if not url:
        result["delivery"] = "no webhook configured"
        return result
    try:
        with httpx.Client() as client:
            resp = client.post(url, json={"text": _format_slack(breaches)}, timeout=10)
        result["sent"] = resp.status_code == 200
        if resp.status_code != 200:
            result["delivery"] = f"Slack returned {resp.status_code}"
    except Exception as e:
        result["delivery"] = str(e)[:200]
        logger.warning("Alert delivery failed: %s", e)
    return result
