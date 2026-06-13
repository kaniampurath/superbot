#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import pymysql
except Exception:
    pymysql = None

try:
    import redis
except Exception:
    redis = None


def load_env_file(path: str | None) -> None:
    if not path:
        return
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def inside_container() -> bool:
    return Path("/.dockerenv").exists() or env("HORIZON_INSIDE_CONTAINER", "").lower() in {"1", "true", "yes"}


def redis_client() -> Any | None:
    if redis is None:
        return None
    host = env("REDIS_HOST", "localhost")
    if host in {"redis", "mariadb"} and not inside_container():
        return None
    try:
        client = redis.Redis(host=host, port=int(env("REDIS_PORT", "6379")), decode_responses=True, socket_connect_timeout=1, socket_timeout=1)
        client.ping()
        return client
    except Exception:
        return None


def db_connection() -> Any | None:
    if pymysql is None:
        return None
    host = env("MYSQL_HOST", "localhost")
    if host in {"mariadb", "redis"} and not inside_container():
        return None
    try:
        return pymysql.connect(
            host=host,
            port=int(env("MYSQL_PORT", "3306")),
            user=env("MYSQL_USER", "horizon_user"),
            password=env("MYSQL_PASSWORD", "horizon_password"),
            database=env("MYSQL_DATABASE", "horizon_lab"),
            charset="utf8mb4",
            connect_timeout=3,
            read_timeout=3,
            write_timeout=3,
            cursorclass=pymysql.cursors.DictCursor,
        )
    except Exception:
        return None


def redis_json(client: Any | None, key: str) -> dict[str, Any]:
    if client is None:
        return {}
    try:
        value = client.get(key)
        return json.loads(value) if value else {}
    except Exception:
        return {}


def query_rows(conn: Any | None, sql: str) -> list[dict[str, Any]]:
    if conn is None:
        return []
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql)
            return list(cursor.fetchall())
    except Exception:
        return []


def query_scalar(conn: Any | None, sql: str, default: int = 0) -> int:
    rows = query_rows(conn, sql)
    if not rows:
        return default
    value = next(iter(rows[0].values()))
    return default if value is None else int(value)


def build_report() -> dict[str, Any]:
    client = redis_client()
    conn = db_connection()
    pnl = redis_json(client, "live_pnl")
    risk = redis_json(client, "risk_state")
    drift = redis_json(client, "drift_state")
    workers = []
    for worker in ["worker-marketdata", "worker-signal", "worker-risk", "worker-ml", "worker-order", "worker-pnl"]:
        payload = redis_json(client, f"worker_status:{worker}")
        workers.append({"worker": worker, "status": payload.get("status", "OFFLINE"), "last_seen": payload.get("last_seen", ""), "detail": payload.get("detail", {})})
    if conn is not None and all(row["status"] == "OFFLINE" for row in workers):
        workers = query_rows(conn, "SELECT worker_name AS worker, status, last_seen, detail_json AS detail FROM worker_heartbeat ORDER BY worker_name")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runtime": {
            "stage": env("SYSTEM_STAGE", "training"),
            "headless_capable": True,
            "ui_required": False,
            "symbols": [item.strip() for item in env("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT").split(",") if item.strip()],
            "strategy_interval": env("STRATEGY_INTERVAL", "15m"),
        },
        "connections": {"redis": client is not None, "mariadb": conn is not None},
        "pnl": {
            "realized": float(pnl.get("realized_pnl", 0.0) or 0.0),
            "unrealized": float(pnl.get("unrealized_pnl", 0.0) or 0.0),
            "daily": float(pnl.get("daily_pnl", 0.0) or 0.0),
            "equity": float(pnl.get("equity", env("STARTING_EQUITY", "100000")) or env("STARTING_EQUITY", "100000")),
            "drawdown_pct": float(pnl.get("current_dd_pct", 0.0) or 0.0),
            "ts": pnl.get("ts", ""),
        },
        "risk": risk,
        "drift": drift,
        "workers": workers,
        "orders": {
            "pending": query_scalar(conn, "SELECT COUNT(*) AS n FROM deployment_requests WHERE status='PENDING'"),
            "executed": query_scalar(conn, "SELECT COUNT(*) AS n FROM deployment_requests WHERE status='EXECUTED'"),
            "blocked": query_scalar(conn, "SELECT COUNT(*) AS n FROM deployment_requests WHERE status='BLOCKED'"),
        },
        "open_positions": query_scalar(conn, "SELECT COUNT(*) AS n FROM positions WHERE status='OPEN'"),
        "active_model": (query_rows(conn, "SELECT version, status, trained_rows, metrics_json, trained_at FROM model_registry WHERE status='ACTIVE' ORDER BY trained_at DESC LIMIT 1") or [{}])[0],
        "latest_signals": query_rows(conn, "SELECT symbol, side, price, composite_score, ml_confidence, deployable, validation_status, risk_status, ts FROM signals ORDER BY id DESC LIMIT 10"),
        "recent_orders": query_rows(conn, "SELECT created_at, symbol, side, mode, status, requested_size_usdt, block_reason FROM deployment_requests ORDER BY id DESC LIMIT 10"),
        "positions": query_rows(conn, "SELECT symbol, side, entry_price, current_price, size_usdt, unrealized_pnl, realized_pnl, status, updated_at FROM positions ORDER BY id DESC LIMIT 10"),
    }
    if conn is not None:
        conn.close()
    metrics_json = report["active_model"].get("metrics_json")
    if metrics_json:
        try:
            report["active_model"]["metrics"] = json.loads(metrics_json)
        except Exception:
            report["active_model"]["metrics"] = {}
        report["active_model"].pop("metrics_json", None)
    return report


def print_human(report: dict[str, Any]) -> None:
    pnl = report["pnl"]
    print("Horizon performance report")
    print(f"Generated: {report['generated_at']}")
    print(f"Stage: {report['runtime']['stage']} | Headless: yes | UI required: no")
    print(f"Connections: redis={report['connections']['redis']} mariadb={report['connections']['mariadb']}")
    print(f"Equity: ${pnl['equity']:,.2f} | Daily P&L: ${pnl['daily']:,.2f} | Realized: ${pnl['realized']:,.2f} | Unrealized: ${pnl['unrealized']:,.2f} | DD: {pnl['drawdown_pct']:.2f}%")
    print(f"Risk: {report.get('risk', {}).get('status', 'UNKNOWN')} | Drift: {report.get('drift', {}).get('status', 'UNKNOWN')} score={float(report.get('drift', {}).get('drift_score', 0) or 0):.1f}")
    print(f"Orders: pending={report['orders']['pending']} executed={report['orders']['executed']} blocked={report['orders']['blocked']} | Open positions={report['open_positions']}")
    model = report.get("active_model") or {}
    if model:
        metrics = model.get("metrics", {})
        print(f"Active ML: {model.get('version', '')} rows={model.get('trained_rows', 0)} accuracy={float(metrics.get('accuracy', 0) or 0):.3f} precision={float(metrics.get('precision', 0) or 0):.3f} recall={float(metrics.get('recall', 0) or 0):.3f}")
    print("\nWorkers")
    for row in report["workers"]:
        print(f"  {row.get('worker'):<18} {row.get('status', 'UNKNOWN'):<10} {row.get('last_seen', '')}")
    print("\nLatest signals")
    for row in report["latest_signals"][:6]:
        print(f"  {row.get('symbol',''):<8} {row.get('side',''):<5} score={float(row.get('composite_score', 0) or 0):>6.2f} ml={float(row.get('ml_confidence', 0) or 0):.2f} deployable={row.get('deployable')} ts={row.get('ts','')}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Print Horizon headless backend performance.")
    parser.add_argument("--env-file", default=os.getenv("ENV_FILE", ".env"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    load_env_file(args.env_file)
    report = build_report()
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_human(report)


if __name__ == "__main__":
    main()
