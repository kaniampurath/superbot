#!/usr/bin/env python3
from __future__ import annotations

import importlib
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class FakeState:
    def __init__(self) -> None:
        self.store: dict[str, Any] = {}
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.locks: set[str] = set()

    @property
    def ok(self) -> bool:
        return True

    def set_json(self, key: str, value: dict[str, Any], ex: int | None = None) -> None:
        self.store[key] = {"value": value, "ttl": ex}

    def get_json(self, key: str) -> dict[str, Any] | None:
        item = self.store.get(key)
        if isinstance(item, dict) and "value" in item:
            return item["value"]
        return None

    def publish(self, channel: str, value: dict[str, Any]) -> None:
        self.published.append((channel, value))

    def publish_audit(self, value: dict[str, Any]) -> None:
        self.publish("audit_events", value)
        self.publish("audit_updates", value)

    def push_json(self, key: str, value: dict[str, Any]) -> None:
        self.store.setdefault(key, {"value": [], "ttl": None})["value"].append(value)

    def lock(self, key: str, ttl: int) -> bool:
        self.locks.add(key)
        return True


class CaptureDb:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, engine: object | None, statement: str, params: dict[str, Any]) -> None:
        self.calls.append((" ".join(statement.split()), dict(params)))

    def has_sql(self, needle: str) -> bool:
        return any(needle in statement for statement, _ in self.calls)

    def params_for(self, needle: str) -> list[dict[str, Any]]:
        return [params for statement, params in self.calls if needle in statement]


def configure_env() -> None:
    os.environ.setdefault("SYMBOLS", "BTCUSDT")
    os.environ.setdefault("RUN_MODE", "validation-once")
    os.environ.setdefault("VALIDATION_BACKTEST_BARS", "120")
    os.environ.setdefault("VALIDATION_WORKER_SECONDS", "300")
    os.environ.setdefault("VALIDATION_HISTORY_DAYS", "3")
    os.environ.setdefault("VALIDATION_STORE_PATHS", "false")
    os.environ.setdefault("TRAINING_AUTO_REQUIRES_VALIDATION", "true")
    os.environ.setdefault("MEAN_REVERSION_RESEARCH_ONLY", "true")


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def load_app() -> Any:
    configure_env()
    return importlib.import_module("horizon_institutional_live_production_grade")


def scenario_validation_worker_headless(app: Any) -> dict[str, Any]:
    state = FakeState()
    capture = CaptureDb()
    original_db_execute = app.db_execute
    original_fetch = app.fetch_binance_klines
    try:
        app.db_execute = capture.execute
        app.fetch_binance_klines = lambda symbol, interval, limit=120: app.synthetic_ohlcv(symbol, bars=limit)
        started = time.perf_counter()
        statuses = app.run_validation_cycle(state, object(), use_locks=False)
        elapsed = time.perf_counter() - started
    finally:
        app.db_execute = original_db_execute
        app.fetch_binance_klines = original_fetch

    latest = state.get_json("latest_validation:BTCUSDT")
    assert_true(statuses.get("BTCUSDT") in {"GREEN", "AMBER", "RED"}, "validation worker did not return a valid status")
    assert_true(bool(latest), "validation worker did not publish latest_validation:{symbol}")
    assert_true(state.get_json("worker_status:worker-validation") is not None, "validation worker heartbeat missing")
    assert_true(any(channel == "validation_updates" for channel, _ in state.published), "validation update was not published")
    assert_true(any(channel == "journal_events" for channel, _ in state.published), "validation journal event was not published")
    assert_true(any(channel == "handoff_events" for channel, _ in state.published), "validation handoff event was not published")
    assert_true(capture.has_sql("validation_state"), "validation_state was not upserted")
    assert_true(capture.has_sql("backtest_runs"), "compact backtest history was not written")
    assert_true(capture.has_sql("walk_forward_runs"), "walk-forward history was not written")
    assert_true(capture.has_sql("monte_carlo_runs"), "Monte Carlo history was not written")
    assert_true(capture.has_sql("trading_journal"), "validation journal row was not written")
    assert_true(capture.has_sql("handoff_events"), "handoff event row was not written")
    assert_true(capture.has_sql("DELETE FROM backtest_runs"), "backtest retention pruning did not run")

    summary_params = capture.params_for("validation_state")[0]
    summary = json.loads(summary_params["summary_json"])
    assert_true(summary["backtest"]["equity_curve"] == [], "storage-heavy equity curve should be suppressed by default")
    monte_params = capture.params_for("monte_carlo_runs")[0]
    assert_true(json.loads(monte_params["worst_path_json"]) == [], "storage-heavy Monte Carlo path should be suppressed by default")
    json.dumps(summary, allow_nan=False)

    return {
        "status": statuses["BTCUSDT"],
        "elapsed_seconds": elapsed,
        "db_calls": len(capture.calls),
        "published_events": len(state.published),
    }


def scenario_order_gate_requires_validation(app: Any) -> dict[str, Any]:
    risk = {"status": "RISK_OK"}
    drift = {"status": "OK"}
    base = {
        "side": "BUY",
        "deployable": True,
        "training_auto_approved": True,
        "ml_confidence": max(app.CFG.training_auto_approve_min_ml_confidence, app.CFG.min_ml_confidence) + 0.05,
        "confidence": 0.75,
        "suggested_usdt": min(10.0, app.CFG.training_auto_approve_max_position_usdt),
        "cross_exchange_spread_bps": 0.0,
        "model_slippage_bps": 0.0,
    }
    blocked, blocked_reason = app.order_request_is_allowed({**base, "validation_status": "AMBER"}, risk, drift, app.CFG)
    allowed, allowed_reason = app.order_request_is_allowed({**base, "validation_status": "GREEN"}, risk, drift, app.CFG)
    assert_true(not blocked and "Validation state is not green" in blocked_reason, "training auto gate should require green validation")
    assert_true(allowed and allowed_reason == "OK", "green validation should allow the otherwise valid training-auto request")
    return {"amber_allowed": blocked, "amber_reason": blocked_reason, "green_allowed": allowed}


def scenario_ui_independence_contract() -> dict[str, Any]:
    prod_compose = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    app_source = (ROOT / "horizon_institutional_live_production_grade.py").read_text(encoding="utf-8")
    assert_true("worker-validation:" in prod_compose, "production compose missing worker-validation")
    assert_true('profiles: ["ui"]' in prod_compose, "UI service must stay optional behind a compose profile")
    assert_true("RUN_MODE: validation" in prod_compose, "validation worker RUN_MODE missing")
    assert_true("latest_validation_snapshot(state, engine, selected" in app_source, "UI should read validation state rather than own persistence")
    assert_true("persist_validation_snapshot(engine, validation)" not in app_source, "UI still appears to persist validation snapshots")
    return {"compose_validation_worker": True, "ui_profile_optional": True, "ui_reads_validation": True}


def scenario_performance_budget(app: Any, runs: int = 3) -> dict[str, Any]:
    timings = []
    capture = CaptureDb()
    original_db_execute = app.db_execute
    original_fetch = app.fetch_binance_klines
    try:
        app.db_execute = capture.execute
        app.fetch_binance_klines = lambda symbol, interval, limit=120: app.synthetic_ohlcv(symbol, bars=limit)
        for _ in range(runs):
            started = time.perf_counter()
            app.run_validation_cycle(FakeState(), object(), use_locks=False)
            timings.append(time.perf_counter() - started)
    finally:
        app.db_execute = original_db_execute
        app.fetch_binance_klines = original_fetch
    p95 = max(timings)
    assert_true(p95 < 10.0, f"validation cycle too slow for one symbol offline: {p95:.3f}s")
    return {"runs": runs, "avg_seconds": statistics.mean(timings), "p95_seconds": p95}


def main() -> int:
    app = load_app()
    scenarios = [
        ("validation_worker_headless", lambda: scenario_validation_worker_headless(app)),
        ("order_gate_requires_validation", lambda: scenario_order_gate_requires_validation(app)),
        ("ui_independence_contract", scenario_ui_independence_contract),
        ("validation_performance_budget", lambda: scenario_performance_budget(app)),
    ]
    results: list[dict[str, Any]] = []
    failed = False
    for name, scenario in scenarios:
        started = time.perf_counter()
        try:
            detail = scenario()
            results.append({"scenario": name, "status": "PASS", "elapsed_seconds": time.perf_counter() - started, "detail": detail})
        except Exception as exc:
            failed = True
            results.append({"scenario": name, "status": "FAIL", "elapsed_seconds": time.perf_counter() - started, "error": str(exc)})
    print(json.dumps({"suite": "headless_functional_performance", "results": results}, indent=2, default=str))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
