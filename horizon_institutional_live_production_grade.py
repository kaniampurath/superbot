from __future__ import annotations

import json
import hashlib
import html as html_lib
import hmac
import math
import os
import random
import ssl
import socket
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode
from uuid import uuid4

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import websocket
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

try:
    import redis
except Exception:  # pragma: no cover
    redis = None


load_dotenv()
UTC = timezone.utc


def env_str(name: str, default: str) -> str:
    return os.getenv(name, default)


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except ValueError:
        return default


def env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def iso_now() -> str:
    return now_utc().isoformat()


def age_seconds(value: Any, default: float = 999999.0) -> float:
    try:
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        else:
            timestamp = timestamp.tz_convert("UTC")
        return max(0.0, (pd.Timestamp.now(tz=UTC) - timestamp).total_seconds())
    except Exception:
        return default


def fresh_payload(payload: dict[str, Any] | None, max_age_seconds: int = 180) -> bool:
    if not payload:
        return False
    return age_seconds(payload.get("ts")) <= max_age_seconds


def utc_naive_timestamp(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert("UTC").tz_localize(None)
    return timestamp


def format_profit_factor(value: Any) -> str:
    try:
        profit_factor = float(value)
    except (TypeError, ValueError):
        return "Need data"
    if math.isinf(profit_factor):
        return "No losses"
    if math.isnan(profit_factor):
        return "Need data"
    return f"{profit_factor:.2f}"


def format_money(value: Any, decimals: int = 2) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        amount = 0.0
    return f"${amount:,.{decimals}f}"


def pnl_status(value: Any) -> str:
    try:
        return "GREEN" if float(value) >= 0 else "RED"
    except (TypeError, ValueError):
        return "AMBER"


def interval_to_seconds(interval: str) -> int:
    text_value = str(interval or "1m").strip().lower()
    try:
        amount = int(text_value[:-1])
    except ValueError:
        return 60
    suffix = text_value[-1:]
    if suffix == "m":
        return amount * 60
    if suffix == "h":
        return amount * 3600
    if suffix == "d":
        return amount * 86400
    if suffix == "s":
        return amount
    return 60


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        numeric = float(value)
        return numeric if np.isfinite(numeric) else 0.0
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    return value


@dataclass(frozen=True)
class Config:
    run_mode: str = env_str("RUN_MODE", "ui")
    mysql_host: str = env_str("MYSQL_HOST", "localhost")
    mysql_port: int = env_int("MYSQL_PORT", 3306)
    mysql_database: str = env_str("MYSQL_DATABASE", "horizon_lab")
    mysql_user: str = env_str("MYSQL_USER", "horizon_user")
    mysql_password: str = env_str("MYSQL_PASSWORD", "horizon_password")
    redis_host: str = env_str("REDIS_HOST", "localhost")
    redis_port: int = env_int("REDIS_PORT", 6379)
    symbols: tuple[str, ...] = tuple(s.strip().upper() for s in env_str("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT").split(",") if s.strip())
    interval: str = env_str("KLINE_INTERVAL", "1m")
    system_stage: str = env_str("SYSTEM_STAGE", "training").strip().lower()
    training_auto_approve_paper: bool = env_bool("TRAINING_AUTO_APPROVE_PAPER", True)
    auto_approve_order_mode: str = env_str("AUTO_APPROVE_ORDER_MODE", "TESTNET").strip().upper()
    training_auto_approve_min_ml_confidence: float = env_float("TRAINING_AUTO_APPROVE_MIN_ML_CONFIDENCE", 0.50)
    training_auto_approve_max_position_usdt: float = env_float("TRAINING_AUTO_APPROVE_MAX_POSITION_USDT", 50)
    strategy_interval: str = env_str("STRATEGY_INTERVAL", "15m")
    mean_reversion_z: float = env_float("MEAN_REVERSION_Z", 2.7)
    mean_reversion_hold_bars: int = env_int("MEAN_REVERSION_HOLD_BARS", 8)
    mean_reversion_rsi_buy: float = env_float("MEAN_REVERSION_RSI_BUY", 35)
    mean_reversion_rsi_sell: float = env_float("MEAN_REVERSION_RSI_SELL", 65)
    mean_reversion_min_volume_z: float = env_float("MEAN_REVERSION_MIN_VOLUME_Z", -0.5)
    mean_reversion_taker_filter: bool = env_bool("MEAN_REVERSION_TAKER_FILTER", True)
    mean_reversion_research_only: bool = env_bool("MEAN_REVERSION_RESEARCH_ONLY", True)
    deploy_symbol_whitelist: tuple[str, ...] = tuple(s.strip().upper() for s in env_str("DEPLOY_SYMBOL_WHITELIST", "SOLUSDT,XRPUSDT").split(",") if s.strip())
    higher_timeframe_interval: str = env_str("HIGHER_TIMEFRAME_INTERVAL", "4h")
    max_mean_reversion_adx: float = env_float("MAX_MEAN_REVERSION_ADX", 22)
    volatility_falling_ratio: float = env_float("VOLATILITY_FALLING_RATIO", 0.95)
    expected_move_cost_multiple: float = env_float("EXPECTED_MOVE_COST_MULTIPLE", 3.0)
    orderbook_confirmation_min_obi: float = env_float("ORDERBOOK_CONFIRMATION_MIN_OBI", -0.10)
    orderbook_confirmation_max_obi: float = env_float("ORDERBOOK_CONFIRMATION_MAX_OBI", 0.10)
    rolling_validation_trades: int = env_int("ROLLING_VALIDATION_TRADES", 30)
    validation_worker_seconds: int = env_int("VALIDATION_WORKER_SECONDS", 900)
    validation_backtest_bars: int = env_int("VALIDATION_BACKTEST_BARS", 360)
    validation_history_days: int = env_int("VALIDATION_HISTORY_DAYS", 14)
    validation_store_paths: bool = env_bool("VALIDATION_STORE_PATHS", False)
    training_auto_requires_validation: bool = env_bool("TRAINING_AUTO_REQUIRES_VALIDATION", True)
    ml_enabled: bool = env_bool("ML_ENABLED", True)
    ml_confidence_gate_enabled: bool = env_bool("ML_CONFIDENCE_GATE_ENABLED", True)
    min_ml_confidence: float = env_float("MIN_ML_CONFIDENCE", 0.62)
    ml_training_bars: int = env_int("ML_TRAINING_BARS", 1000)
    ml_retrain_seconds: int = env_int("ML_RETRAIN_SECONDS", 3600)
    ml_min_training_rows: int = env_int("ML_MIN_TRAINING_ROWS", 100)
    ml_min_accuracy: float = env_float("ML_MIN_ACCURACY", 0.52)
    ml_min_precision: float = env_float("ML_MIN_PRECISION", 0.40)
    ml_min_recall: float = env_float("ML_MIN_RECALL", 0.03)
    ml_promote_only_if_better: bool = env_bool("ML_PROMOTE_ONLY_IF_BETTER", True)
    ml_candidate_quality_weight: float = env_float("ML_CANDIDATE_QUALITY_WEIGHT", 1.5)
    ml_balance_classes: bool = env_bool("ML_BALANCE_CLASSES", True)
    ml_drift_gate_enabled: bool = env_bool("ML_DRIFT_GATE_ENABLED", True)
    ml_drift_block_threshold: float = env_float("ML_DRIFT_BLOCK_THRESHOLD", 2.0)
    ml_drift_warning_threshold: float = env_float("ML_DRIFT_WARNING_THRESHOLD", 1.0)
    min_validation_profit_factor: float = env_float("MIN_VALIDATION_PROFIT_FACTOR", 1.2)
    min_validation_expectancy_bps: float = env_float("MIN_VALIDATION_EXPECTANCY_BPS", 5)
    max_validation_drawdown_pct: float = env_float("MAX_VALIDATION_DRAWDOWN_PCT", 8)
    starting_equity: float = env_float("STARTING_EQUITY", 100000)
    max_kelly_fraction: float = env_float("MAX_KELLY_FRACTION", 0.05)
    max_position_usdt: float = env_float("MAX_POSITION_USDT", 1000)
    max_daily_loss_pct: float = env_float("MAX_DAILY_LOSS_PCT", 2)
    max_portfolio_dd_pct: float = env_float("MAX_PORTFOLIO_DD_PCT", 5)
    max_trades_per_day: int = env_int("MAX_TRADES_PER_DAY", 20)
    fee_bps: float = env_float("FEE_BPS", 10)
    slippage_bps: float = env_float("SLIPPAGE_BPS", 5)
    paper_trading: bool = env_bool("PAPER_TRADING", True)
    enable_real_testnet_orders: bool = env_bool("ENABLE_REAL_TESTNET_ORDERS", True)
    market_data_source: str = env_str("MARKET_DATA_SOURCE", "TESTNET_WS").strip().upper()
    binance_rest_base_url: str = env_str("BINANCE_REST_BASE_URL", "https://testnet.binance.vision").rstrip("/")
    binance_ws_base_url: str = env_str("BINANCE_WS_BASE_URL", "wss://stream.testnet.binance.vision:9443").rstrip("/")
    market_data_intervals: tuple[str, ...] = tuple(s.strip() for s in env_str("MARKET_DATA_INTERVALS", "1m,5m,15m").split(",") if s.strip())
    market_data_history_limit: int = env_int("MARKET_DATA_HISTORY_LIMIT", 500)
    market_data_rest_backfill_seconds: int = env_int("MARKET_DATA_REST_BACKFILL_SECONDS", 900)
    market_data_ws_stale_seconds: int = env_int("MARKET_DATA_WS_STALE_SECONDS", 90)
    market_data_ws_reconnect_seconds: int = env_int("MARKET_DATA_WS_RECONNECT_SECONDS", 10)
    market_data_trust_env_proxy: bool = env_bool("MARKET_DATA_TRUST_ENV_PROXY", False)
    binance_verify_tls: bool = env_bool("BINANCE_VERIFY_TLS", True)


CFG = Config()


def testnet_credentials_present() -> bool:
    key = os.getenv("testnet_key") or os.getenv("TESTNET_KEY") or os.getenv("BINANCE_TESTNET_API_KEY")
    secret = os.getenv("testnet_secret") or os.getenv("TESTNET_SECRET") or os.getenv("BINANCE_TESTNET_API_SECRET")
    return bool(key and secret)


def testnet_credentials() -> tuple[str, str]:
    key = os.getenv("testnet_key") or os.getenv("TESTNET_KEY") or os.getenv("BINANCE_TESTNET_API_KEY") or ""
    secret = os.getenv("testnet_secret") or os.getenv("TESTNET_SECRET") or os.getenv("BINANCE_TESTNET_API_SECRET") or ""
    return key, secret


def place_binance_spot_testnet_order(symbol: str, side: str, price: float, size_usdt: float) -> dict[str, Any]:
    key, secret = testnet_credentials()
    if not key or not secret:
        raise RuntimeError("Missing Binance Spot Testnet credentials.")
    endpoint = "https://testnet.binance.vision/api/v3/order"
    quantity = max(size_usdt / max(price, 1e-9), 0.0)
    params: dict[str, Any] = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "timestamp": int(time.time() * 1000),
        "recvWindow": 5000,
    }
    if side == "BUY":
        params["quoteOrderQty"] = f"{size_usdt:.2f}"
    else:
        params["quantity"] = f"{quantity:.8f}"
    query = urlencode(params)
    signature = hmac.new(secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    response = requests.post(f"{endpoint}?{query}&signature={signature}", headers={"X-MBX-APIKEY": key}, timeout=10)
    if response.status_code >= 400:
        raise RuntimeError(f"Testnet order rejected: {response.status_code} {response.text[:300]}")
    return response.json()


class RedisState:
    def __init__(self, cfg: Config):
        self.client = None
        self.cfg = cfg
        self.connect()

    def connect(self) -> None:
        cfg = self.cfg
        if redis is None:
            return
        try:
            if cfg.run_mode == "ui" and cfg.redis_host in {"localhost", "127.0.0.1", "::1"}:
                probe = socket.create_connection((cfg.redis_host, cfg.redis_port), timeout=0.25)
                probe.close()
            self.client = redis.Redis(
                host=cfg.redis_host,
                port=cfg.redis_port,
                decode_responses=True,
                socket_connect_timeout=0.4,
                socket_timeout=0.4,
                health_check_interval=0,
            )
            self.client.ping()
        except Exception:
            self.client = None

    @property
    def ok(self) -> bool:
        return self.client is not None

    def set_json(self, key: str, value: dict[str, Any], ex: int | None = None) -> None:
        if not self.client:
            self.connect()
        if self.client:
            try:
                self.client.set(key, json.dumps(value, default=str), ex=ex)
            except Exception:
                self.client = None

    def get_json(self, key: str) -> dict[str, Any] | None:
        if not self.client:
            self.connect()
        if not self.client:
            return None
        try:
            value = self.client.get(key)
        except Exception:
            self.client = None
            return None
        if not value:
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None

    def publish(self, channel: str, value: dict[str, Any]) -> None:
        if not self.client:
            self.connect()
        if self.client:
            try:
                self.client.publish(channel, json.dumps(value, default=str))
            except Exception:
                self.client = None

    def publish_audit(self, value: dict[str, Any]) -> None:
        self.publish("audit_events", value)
        self.publish("audit_updates", value)

    def push_json(self, key: str, value: dict[str, Any]) -> None:
        if not self.client:
            self.connect()
        if self.client:
            try:
                self.client.lpush(key, json.dumps(value, default=str))
            except Exception:
                self.client = None

    def lock(self, key: str, ttl: int) -> bool:
        if not self.client:
            self.connect()
        if not self.client:
            return True
        try:
            return bool(self.client.set(key, "1", nx=True, ex=ttl))
        except Exception:
            self.client = None
            return True


def db_engine(cfg: Config) -> Engine | None:
    url = f"mysql+pymysql://{cfg.mysql_user}:{cfg.mysql_password}@{cfg.mysql_host}:{cfg.mysql_port}/{cfg.mysql_database}?charset=utf8mb4&connect_timeout=3"
    try:
        if cfg.run_mode == "ui" and cfg.mysql_host in {"localhost", "127.0.0.1", "::1"}:
            probe = socket.create_connection((cfg.mysql_host, cfg.mysql_port), timeout=0.25)
            probe.close()
        engine = create_engine(url, pool_pre_ping=True, pool_recycle=1800)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return engine
    except Exception:
        return None


SCHEMA_SQL = [
    """CREATE TABLE IF NOT EXISTS strategies (id BIGINT PRIMARY KEY AUTO_INCREMENT, name VARCHAR(80) UNIQUE, status VARCHAR(30), created_at DATETIME(6))""",
    """CREATE TABLE IF NOT EXISTS strategy_versions (id BIGINT PRIMARY KEY AUTO_INCREMENT, strategy_id BIGINT, version VARCHAR(40), config_json JSON, created_at DATETIME(6))""",
    """CREATE TABLE IF NOT EXISTS market_ticks (id BIGINT PRIMARY KEY AUTO_INCREMENT, symbol VARCHAR(24), price DOUBLE, source VARCHAR(30), data_quality VARCHAR(30), ts DATETIME(6), KEY idx_ticks_symbol_ts(symbol, ts))""",
    """CREATE TABLE IF NOT EXISTS orderbook_snapshots (id BIGINT PRIMARY KEY AUTO_INCREMENT, symbol VARCHAR(24), bid_volume DOUBLE, ask_volume DOUBLE, obi DOUBLE, spread_bps DOUBLE, liquidity_score DOUBLE, data_quality VARCHAR(30), ts DATETIME(6))""",
    """CREATE TABLE IF NOT EXISTS funding_rates (id BIGINT PRIMARY KEY AUTO_INCREMENT, symbol VARCHAR(24), funding_rate DOUBLE, percentile DOUBLE, data_quality VARCHAR(30), ts DATETIME(6))""",
    """CREATE TABLE IF NOT EXISTS open_interest_snapshots (id BIGINT PRIMARY KEY AUTO_INCREMENT, symbol VARCHAR(24), open_interest DOUBLE, oi_change DOUBLE, data_quality VARCHAR(30), ts DATETIME(6))""",
    """CREATE TABLE IF NOT EXISTS signals (id BIGINT PRIMARY KEY AUTO_INCREMENT, idempotency_key VARCHAR(160) UNIQUE, symbol VARCHAR(24), side VARCHAR(8), price DOUBLE, composite_score DOUBLE, z_score DOUBLE, rsi DOUBLE, volume_z DOUBLE, adx DOUBLE, expected_reversion_bps DOUBLE, ml_confidence DOUBLE, ml_model_version VARCHAR(80), obi DOUBLE, cross_exchange_spread_bps DOUBLE, funding_pressure DOUBLE, open_interest_signal DOUBLE, win_p_est DOUBLE, payoff_b DOUBLE, kelly_fraction DOUBLE, suggested_usdt DOUBLE, deployable BOOLEAN, confidence DOUBLE, rationale TEXT, validation_status VARCHAR(20), risk_status VARCHAR(20), ts DATETIME(6))""",
    """CREATE TABLE IF NOT EXISTS deployment_requests (id BIGINT PRIMARY KEY AUTO_INCREMENT, idempotency_key VARCHAR(160) UNIQUE, signal_key VARCHAR(160), symbol VARCHAR(24), side VARCHAR(8), requested_size_usdt DOUBLE, requested_price DOUBLE, mode VARCHAR(20), status VARCHAR(30), requested_by VARCHAR(80), request_json JSON, block_reason TEXT, created_at DATETIME(6), processed_at DATETIME(6), KEY idx_deploy_status(status, created_at))""",
    """CREATE TABLE IF NOT EXISTS paper_orders (id BIGINT PRIMARY KEY AUTO_INCREMENT, idempotency_key VARCHAR(160) UNIQUE, signal_id BIGINT, symbol VARCHAR(24), side VARCHAR(8), size_usdt DOUBLE, status VARCHAR(30), created_at DATETIME(6))""",
    """CREATE TABLE IF NOT EXISTS testnet_orders (id BIGINT PRIMARY KEY AUTO_INCREMENT, idempotency_key VARCHAR(160) UNIQUE, signal_id BIGINT, symbol VARCHAR(24), side VARCHAR(8), size_usdt DOUBLE, status VARCHAR(30), created_at DATETIME(6))""",
    """CREATE TABLE IF NOT EXISTS executions (id BIGINT PRIMARY KEY AUTO_INCREMENT, order_id BIGINT, venue VARCHAR(30), symbol VARCHAR(24), side VARCHAR(8), price DOUBLE, quantity DOUBLE, fee DOUBLE, ts DATETIME(6))""",
    """CREATE TABLE IF NOT EXISTS positions (id BIGINT PRIMARY KEY AUTO_INCREMENT, symbol VARCHAR(24), side VARCHAR(8), venue VARCHAR(20) DEFAULT 'PAPER', entry_time DATETIME(6), entry_price DOUBLE, size_usdt DOUBLE, quantity DOUBLE, stop_price DOUBLE, target_price DOUBLE, current_price DOUBLE, unrealized_pnl DOUBLE, realized_pnl DOUBLE, status VARCHAR(30), signal_id BIGINT, rationale TEXT, updated_at DATETIME(6), KEY idx_positions_status_venue(status, venue))""",
    """CREATE TABLE IF NOT EXISTS pnl_snapshots (id BIGINT PRIMARY KEY AUTO_INCREMENT, realized_pnl DOUBLE, unrealized_pnl DOUBLE, daily_pnl DOUBLE, equity DOUBLE, current_dd_pct DOUBLE, ts DATETIME(6))""",
    """CREATE TABLE IF NOT EXISTS backtest_runs (id BIGINT PRIMARY KEY AUTO_INCREMENT, symbol VARCHAR(24), total_trades INT, win_rate DOUBLE, profit_factor DOUBLE, expectancy DOUBLE, avg_r DOUBLE, max_drawdown DOUBLE, sharpe_like DOUBLE, largest_winner DOUBLE, largest_loser DOUBLE, consecutive_losses INT, equity_curve_json JSON, ts DATETIME(6))""",
    """CREATE TABLE IF NOT EXISTS backtest_trades (id BIGINT PRIMARY KEY AUTO_INCREMENT, run_id BIGINT, symbol VARCHAR(24), side VARCHAR(8), entry_price DOUBLE, exit_price DOUBLE, pnl DOUBLE, r_multiple DOUBLE, entry_time DATETIME(6), exit_time DATETIME(6))""",
    """CREATE TABLE IF NOT EXISTS walk_forward_runs (id BIGINT PRIMARY KEY AUTO_INCREMENT, symbol VARCHAR(24), train_perf DOUBLE, test_perf DOUBLE, degradation_pct DOUBLE, parameter_stability DOUBLE, status VARCHAR(20), overfit_warning TEXT, ts DATETIME(6))""",
    """CREATE TABLE IF NOT EXISTS monte_carlo_runs (id BIGINT PRIMARY KEY AUTO_INCREMENT, symbol VARCHAR(24), median_ending_equity DOUBLE, p5_ending_equity DOUBLE, p95_ending_equity DOUBLE, prob_dd_breach DOUBLE, prob_ruin DOUBLE, expected_max_dd DOUBLE, worst_path_json JSON, ts DATETIME(6))""",
    """CREATE TABLE IF NOT EXISTS validation_state (symbol VARCHAR(24) PRIMARY KEY, status VARCHAR(20), total_trades INT, win_rate DOUBLE, profit_factor DOUBLE, expectancy DOUBLE, max_drawdown DOUBLE, rolling_profit_factor DOUBLE, rolling_expectancy DOUBLE, walk_status VARCHAR(20), monte_status VARCHAR(20), summary_json JSON, updated_at DATETIME(6), KEY idx_validation_updated(updated_at))""",
    """CREATE TABLE IF NOT EXISTS handoff_events (id BIGINT PRIMARY KEY AUTO_INCREMENT, idempotency_key VARCHAR(220) UNIQUE, stage VARCHAR(40), symbol VARCHAR(24), status VARCHAR(20), input_ref VARCHAR(180), output_ref VARCHAR(180), next_owner VARCHAR(80), reason TEXT, metadata_json JSON, created_at DATETIME(6), KEY idx_handoff_stage_created(stage, created_at), KEY idx_handoff_symbol_created(symbol, created_at))""",
    """CREATE TABLE IF NOT EXISTS feature_snapshots (id BIGINT PRIMARY KEY AUTO_INCREMENT, idempotency_key VARCHAR(180) UNIQUE, symbol VARCHAR(24), side VARCHAR(8), strategy_interval VARCHAR(16), feature_json JSON, source VARCHAR(40), ts DATETIME(6), KEY idx_feature_symbol_ts(symbol, ts))""",
    """CREATE TABLE IF NOT EXISTS trade_outcomes (id BIGINT PRIMARY KEY AUTO_INCREMENT, feature_id BIGINT NULL, idempotency_key VARCHAR(180) UNIQUE, symbol VARCHAR(24), side VARCHAR(8), label INT, forward_return DOUBLE, max_favorable_bps DOUBLE, max_adverse_bps DOUBLE, horizon_bars INT, outcome_json JSON, ts DATETIME(6))""",
    """CREATE TABLE IF NOT EXISTS model_registry (id BIGINT PRIMARY KEY AUTO_INCREMENT, model_name VARCHAR(80), version VARCHAR(80), status VARCHAR(30), feature_list_json JSON, feature_importance_json JSON, model_json JSON, metrics_json JSON, trained_rows INT, trained_at DATETIME(6), KEY idx_model_status(model_name, status, trained_at))""",
    """CREATE TABLE IF NOT EXISTS ml_predictions (id BIGINT PRIMARY KEY AUTO_INCREMENT, idempotency_key VARCHAR(180) UNIQUE, symbol VARCHAR(24), side VARCHAR(8), model_version VARCHAR(80), confidence DOUBLE, threshold DOUBLE, feature_json JSON, actual_outcome INT NULL, actual_return DOUBLE NULL, training_date DATETIME(6) NULL, evaluated_at DATETIME(6) NULL, created_at DATETIME(6), KEY idx_ml_symbol_created(symbol, created_at))""",
    """CREATE TABLE IF NOT EXISTS drift_snapshots (id BIGINT PRIMARY KEY AUTO_INCREMENT, live_win_rate DOUBLE, backtest_win_rate DOUBLE, live_expectancy DOUBLE, backtest_expectancy DOUBLE, live_slippage_bps DOUBLE, modeled_slippage_bps DOUBLE, live_trade_frequency DOUBLE, expected_trade_frequency DOUBLE, live_drawdown DOUBLE, expected_drawdown DOUBLE, status VARCHAR(20), ts DATETIME(6))""",
    """CREATE TABLE IF NOT EXISTS risk_events (id BIGINT PRIMARY KEY AUTO_INCREMENT, event_type VARCHAR(80), severity VARCHAR(20), message TEXT, state_json JSON, ts DATETIME(6))""",
    """CREATE TABLE IF NOT EXISTS audit_log (id BIGINT PRIMARY KEY AUTO_INCREMENT, event_type VARCHAR(80), actor VARCHAR(80), symbol VARCHAR(24), message TEXT, metadata_json JSON, ts DATETIME(6))""",
    """CREATE TABLE IF NOT EXISTS trading_journal (id BIGINT PRIMARY KEY AUTO_INCREMENT, idempotency_key VARCHAR(220) UNIQUE, journal_code VARCHAR(40), journal_type VARCHAR(40), severity VARCHAR(20), actor VARCHAR(80), symbol VARCHAR(24), side VARCHAR(8), signal_key VARCHAR(180), model_version VARCHAR(80), confidence DOUBLE, expected_reversion_bps DOUBLE, actual_outcome INT NULL, actual_return DOUBLE NULL, feature_json JSON, context_json JSON, lesson_json JSON, created_at DATETIME(6), KEY idx_journal_code_created(journal_code, created_at), KEY idx_journal_symbol_created(symbol, created_at))""",
    """CREATE TABLE IF NOT EXISTS journal_actions (id BIGINT PRIMARY KEY AUTO_INCREMENT, action_key VARCHAR(120) UNIQUE, title VARCHAR(220), status VARCHAR(30), rationale TEXT, source_summary TEXT, suppress_json JSON, approved_by VARCHAR(80), approved_at DATETIME(6), updated_at DATETIME(6), KEY idx_journal_actions_status(status, updated_at))""",
    """CREATE TABLE IF NOT EXISTS worker_heartbeat (worker_name VARCHAR(80) PRIMARY KEY, status VARCHAR(30), pid INT, host VARCHAR(120), last_seen DATETIME(6), detail_json JSON)""",
]

SCHEMA_MIGRATIONS = [
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS rsi DOUBLE",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS volume_z DOUBLE",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS adx DOUBLE",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS expected_reversion_bps DOUBLE",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS ml_confidence DOUBLE",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS ml_model_version VARCHAR(80)",
    "ALTER TABLE model_registry ADD COLUMN IF NOT EXISTS feature_importance_json JSON",
    "ALTER TABLE ml_predictions ADD COLUMN IF NOT EXISTS actual_outcome INT NULL",
    "ALTER TABLE ml_predictions ADD COLUMN IF NOT EXISTS actual_return DOUBLE NULL",
    "ALTER TABLE ml_predictions ADD COLUMN IF NOT EXISTS training_date DATETIME(6) NULL",
    "ALTER TABLE ml_predictions ADD COLUMN IF NOT EXISTS evaluated_at DATETIME(6) NULL",
    "ALTER TABLE positions ADD COLUMN IF NOT EXISTS venue VARCHAR(20) DEFAULT 'PAPER'",
    "UPDATE positions SET venue='PAPER' WHERE venue IS NULL OR venue=''",
    "CREATE TABLE IF NOT EXISTS trading_journal (id BIGINT PRIMARY KEY AUTO_INCREMENT, idempotency_key VARCHAR(220) UNIQUE, journal_code VARCHAR(40), journal_type VARCHAR(40), severity VARCHAR(20), actor VARCHAR(80), symbol VARCHAR(24), side VARCHAR(8), signal_key VARCHAR(180), model_version VARCHAR(80), confidence DOUBLE, expected_reversion_bps DOUBLE, actual_outcome INT NULL, actual_return DOUBLE NULL, feature_json JSON, context_json JSON, lesson_json JSON, created_at DATETIME(6), KEY idx_journal_code_created(journal_code, created_at), KEY idx_journal_symbol_created(symbol, created_at))",
    "CREATE TABLE IF NOT EXISTS journal_actions (id BIGINT PRIMARY KEY AUTO_INCREMENT, action_key VARCHAR(120) UNIQUE, title VARCHAR(220), status VARCHAR(30), rationale TEXT, source_summary TEXT, suppress_json JSON, approved_by VARCHAR(80), approved_at DATETIME(6), updated_at DATETIME(6), KEY idx_journal_actions_status(status, updated_at))",
    "CREATE TABLE IF NOT EXISTS validation_state (symbol VARCHAR(24) PRIMARY KEY, status VARCHAR(20), total_trades INT, win_rate DOUBLE, profit_factor DOUBLE, expectancy DOUBLE, max_drawdown DOUBLE, rolling_profit_factor DOUBLE, rolling_expectancy DOUBLE, walk_status VARCHAR(20), monte_status VARCHAR(20), summary_json JSON, updated_at DATETIME(6), KEY idx_validation_updated(updated_at))",
    "CREATE TABLE IF NOT EXISTS handoff_events (id BIGINT PRIMARY KEY AUTO_INCREMENT, idempotency_key VARCHAR(220) UNIQUE, stage VARCHAR(40), symbol VARCHAR(24), status VARCHAR(20), input_ref VARCHAR(180), output_ref VARCHAR(180), next_owner VARCHAR(80), reason TEXT, metadata_json JSON, created_at DATETIME(6), KEY idx_handoff_stage_created(stage, created_at), KEY idx_handoff_symbol_created(symbol, created_at))",
]


def init_schema(engine: Engine | None) -> None:
    if engine is None:
        return
    with engine.begin() as conn:
        for statement in SCHEMA_SQL:
            conn.execute(text(statement))
        for statement in SCHEMA_MIGRATIONS:
            conn.execute(text(statement))
        conn.execute(text("INSERT IGNORE INTO strategies(name, status, created_at) VALUES ('institutional_mispricing_v1', 'ACTIVE', :ts)"), {"ts": now_utc().replace(tzinfo=None)})


def migrate_database(engine: Engine | None) -> int:
    if engine is None:
        print("Database unavailable; schema migration skipped.", file=sys.stderr)
        return 1
    init_schema(engine)
    print(f"Database schema ready: {len(SCHEMA_SQL)} tables checked, {len(SCHEMA_MIGRATIONS)} migrations applied.")
    return 0


def db_execute(engine: Engine | None, statement: str, params: dict[str, Any]) -> None:
    if engine is None:
        return
    try:
        with engine.begin() as conn:
            conn.execute(text(statement), params)
    except SQLAlchemyError:
        return


JOURNAL_CODES: dict[str, dict[str, str]] = {
    "SIG_BUY": {"type": "SIGNAL", "severity": "INFO", "meaning": "Buy candidate generated"},
    "SIG_SELL": {"type": "SIGNAL", "severity": "INFO", "meaning": "Sell candidate generated"},
    "SIG_HOLD": {"type": "SIGNAL", "severity": "INFO", "meaning": "No-trade scan recorded"},
    "SIG_BLOCKED": {"type": "GATE", "severity": "AMBER", "meaning": "Candidate blocked by model, risk, drift, liquidity, or config"},
    "AUTO_APPROVED": {"type": "APPROVAL", "severity": "INFO", "meaning": "Training auto-approval queued a paper/testnet request"},
    "ORDER_EXECUTED": {"type": "EXECUTION", "severity": "INFO", "meaning": "Order request created a paper/testnet position"},
    "ORDER_BLOCKED": {"type": "EXECUTION", "severity": "AMBER", "meaning": "Order request blocked by final execution gate"},
    "TESTNET_ORDER_FAILED": {"type": "EXECUTION", "severity": "WARNING", "meaning": "Testnet venue rejected or failed an order attempt"},
    "OUTCOME_WIN": {"type": "FEEDBACK", "severity": "INFO", "meaning": "Prediction outcome was profitable or favorable"},
    "OUTCOME_LOSS": {"type": "FEEDBACK", "severity": "WARNING", "meaning": "Prediction outcome was unprofitable or unfavorable"},
    "MISSED_PROFITABLE_CANDIDATE": {"type": "FEEDBACK", "severity": "AMBER", "meaning": "Blocked candidate would have been profitable over the learning horizon"},
    "MODEL_PROMOTED": {"type": "MODEL", "severity": "INFO", "meaning": "Candidate model promoted to active"},
    "MODEL_REJECTED": {"type": "MODEL", "severity": "WARNING", "meaning": "Candidate model rejected by promotion gates"},
    "VALIDATION_PASS": {"type": "VALIDATION", "severity": "INFO", "meaning": "Backtest, walk-forward, and Monte Carlo validation passed"},
    "VALIDATION_WARN": {"type": "VALIDATION", "severity": "AMBER", "meaning": "Validation has warnings or insufficient evidence"},
    "VALIDATION_FAIL": {"type": "VALIDATION", "severity": "WARNING", "meaning": "Validation failed a required safety or performance gate"},
}


def journal_write(
    engine: Engine | None,
    state: RedisState | None,
    *,
    key: str,
    code: str,
    actor: str,
    symbol: str = "",
    side: str = "",
    signal_key: str = "",
    model_version: str = "",
    confidence: float | None = None,
    expected_reversion_bps: float | None = None,
    actual_outcome: int | None = None,
    actual_return: float | None = None,
    features: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    lesson: dict[str, Any] | None = None,
) -> None:
    spec = JOURNAL_CODES.get(code, {"type": "SYSTEM", "severity": "INFO", "meaning": code})
    payload = {
        "idempotency_key": key,
        "journal_code": code,
        "journal_type": spec["type"],
        "severity": spec["severity"],
        "actor": actor,
        "symbol": symbol,
        "side": side,
        "signal_key": signal_key,
        "model_version": model_version,
        "confidence": confidence,
        "expected_reversion_bps": expected_reversion_bps,
        "actual_outcome": actual_outcome,
        "actual_return": actual_return,
        "feature_json": json.dumps(features or {}),
        "context_json": json.dumps({**(context or {}), "meaning": spec.get("meaning", code)}),
        "lesson_json": json.dumps(lesson or {}),
        "created_at": now_utc().replace(tzinfo=None),
    }
    db_execute(
        engine,
        """INSERT IGNORE INTO trading_journal(idempotency_key, journal_code, journal_type, severity, actor, symbol, side, signal_key, model_version, confidence, expected_reversion_bps, actual_outcome, actual_return, feature_json, context_json, lesson_json, created_at)
           VALUES(:idempotency_key, :journal_code, :journal_type, :severity, :actor, :symbol, :side, :signal_key, :model_version, :confidence, :expected_reversion_bps, :actual_outcome, :actual_return, :feature_json, :context_json, :lesson_json, :created_at)""",
        payload,
    )
    if state is not None:
        state.publish("journal_events", payload)


def journal_code_rows() -> pd.DataFrame:
    return pd.DataFrame(
        [{"Code": code, "Type": spec["type"], "Severity": spec["severity"], "Meaning": spec["meaning"]} for code, spec in JOURNAL_CODES.items()]
    )


APPROVED_JOURNAL_ACTIONS: list[dict[str, Any]] = [
    {
        "action_key": "suppress_non_deployable_auto_approval",
        "title": "Stop auto-approval for non-deployable/config-blocked signals",
        "rationale": "XRP SIG_BUY was repeatedly generated with research_only, trend_regime_adx_high, and validation_not_green:AMBER, then still auto-approved.",
        "source_summary": "Approved from journal review on 2026-06-14.",
        "suppress": ["research_only_auto_approval", "non_deployable_auto_approval"],
    },
    {
        "action_key": "symbol_open_position_cooldown",
        "title": "Add per-symbol open-position cooldown before creating new deployment requests",
        "rationale": "Repeated XRP ORDER_BLOCKED entries had the same final gate reason: an open position already exists for this symbol.",
        "source_summary": "Approved from journal review on 2026-06-14.",
        "suppress": ["open_position_exists", "repeated_order_blocked_open_position"],
    },
    {
        "action_key": "require_validation_for_training_auto_approval",
        "title": "Require validation evidence before training auto-approval",
        "rationale": "Validation was AMBER with zero trades across symbols while XRP continued to auto-approve.",
        "source_summary": "Approved from journal review on 2026-06-14.",
        "suppress": ["validation_not_green_auto_approval", "zero_trade_validation_auto_approval"],
    },
    {
        "action_key": "collect_cross_symbol_labels",
        "title": "Collect labeled paper outcomes for ETH, SOL, BNB, and XRP before trusting the model cross-symbol",
        "rationale": "Feedback labels were concentrated in BTCUSDT while ETHUSDT, SOLUSDT, BNBUSDT, and XRPUSDT had no labels.",
        "source_summary": "Approved from journal review on 2026-06-14.",
        "suppress": ["missing_symbol_feedback", "cross_symbol_label_gap"],
    },
    {
        "action_key": "do_not_promote_current_model",
        "title": "Do not promote the current model; improve labels/features first",
        "rationale": "Recent model decisions were rejected for accuracy_below_threshold around 35-36% accuracy.",
        "source_summary": "Approved from journal review on 2026-06-14.",
        "suppress": ["model_rejected_accuracy_below_threshold", "improve_labels_features_before_promotion"],
    },
    {
        "action_key": "separate_trend_regime_candidates",
        "title": "Separate trend-regime candidates instead of relaxing ADX globally",
        "rationale": "trend_regime_adx_high appeared repeatedly, especially for XRP where the buy setup had ADX around 40.6.",
        "source_summary": "Approved from journal review on 2026-06-14.",
        "suppress": ["trend_regime_adx_high", "avoid_global_adx_relaxation"],
    },
    {
        "action_key": "exclude_research_only_from_live_edge_metrics",
        "title": "Keep research_only out of live-edge metrics",
        "rationale": "research_only dominated blocker counts and is configuration noise, not market failure.",
        "source_summary": "Approved from journal review on 2026-06-14.",
        "suppress": ["research_only", "config_gated_noise"],
    },
    {
        "action_key": "training_only_cost_hurdle_review",
        "title": "Review training-only cost hurdle after more labels, not for production",
        "rationale": "expected_move_below_cost_hurdle appeared frequently and should only be relaxed cautiously for paper/training exploration.",
        "source_summary": "Approved from journal review on 2026-06-14.",
        "suppress": ["expected_move_below_cost_hurdle", "training_cost_hurdle_review"],
    },
    {
        "action_key": "analyze_participation_volatility_filters",
        "title": "Analyze low-participation and volatility filters before relaxing",
        "rationale": "low_participation and volatility_not_falling appeared repeatedly, mostly for BTC/ETH/SOL.",
        "source_summary": "Approved from journal review on 2026-06-14.",
        "suppress": ["low_participation", "volatility_not_falling"],
    },
]


def mark_approved_journal_actions(engine: Engine | None, actor: str = "operator") -> int:
    if engine is None:
        return 0
    init_schema(engine)
    approved_at = now_utc().replace(tzinfo=None)
    inserted = 0
    with engine.begin() as conn:
        for action in APPROVED_JOURNAL_ACTIONS:
            result = conn.execute(
                text(
                    """INSERT INTO journal_actions(action_key, title, status, rationale, source_summary, suppress_json, approved_by, approved_at, updated_at)
                       VALUES(:action_key, :title, 'APPROVED', :rationale, :source_summary, :suppress_json, :approved_by, :approved_at, :updated_at)
                       ON DUPLICATE KEY UPDATE status='APPROVED', rationale=VALUES(rationale), source_summary=VALUES(source_summary), suppress_json=VALUES(suppress_json), approved_by=VALUES(approved_by), updated_at=VALUES(updated_at)"""
                ),
                {
                    "action_key": action["action_key"],
                    "title": action["title"],
                    "rationale": action["rationale"],
                    "source_summary": action["source_summary"],
                    "suppress_json": json.dumps(action.get("suppress", [])),
                    "approved_by": actor,
                    "approved_at": approved_at,
                    "updated_at": approved_at,
                },
            )
            inserted += int(result.rowcount or 0)
    return inserted


def approved_journal_action_rows(engine: Engine | None) -> pd.DataFrame:
    rows = db_rows(
        engine,
        """SELECT action_key, title, status, rationale, source_summary, approved_by, approved_at, updated_at
           FROM journal_actions
           WHERE status='APPROVED'
           ORDER BY approved_at DESC, action_key ASC""",
    )
    return pd.DataFrame(rows)


def record_handoff(
    engine: Engine | None,
    state: RedisState | None,
    *,
    key: str,
    stage: str,
    symbol: str,
    status: str,
    input_ref: str,
    output_ref: str,
    next_owner: str,
    reason: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    payload = {
        "idempotency_key": key,
        "stage": stage,
        "symbol": symbol,
        "status": status,
        "input_ref": input_ref,
        "output_ref": output_ref,
        "next_owner": next_owner,
        "reason": reason,
        "metadata_json": json.dumps(metadata or {}),
        "created_at": now_utc().replace(tzinfo=None),
    }
    db_execute(
        engine,
        """INSERT IGNORE INTO handoff_events(idempotency_key, stage, symbol, status, input_ref, output_ref, next_owner, reason, metadata_json, created_at)
           VALUES(:idempotency_key, :stage, :symbol, :status, :input_ref, :output_ref, :next_owner, :reason, :metadata_json, :created_at)""",
        payload,
    )
    if state is not None:
        state.set_json(f"latest_handoff:{stage}:{symbol}", {**payload, "created_at": iso_now()}, ex=3600)
        state.publish("handoff_events", {**payload, "created_at": iso_now()})


def heartbeat(engine: Engine | None, state: RedisState, worker: str, status: str = "ONLINE", detail: dict[str, Any] | None = None) -> None:
    payload = {"worker": worker, "status": status, "pid": os.getpid(), "host": socket.gethostname(), "last_seen": iso_now(), "detail": detail or {}}
    state.set_json(f"worker_status:{worker}", payload, ex=90)
    db_execute(
        engine,
        """INSERT INTO worker_heartbeat(worker_name, status, pid, host, last_seen, detail_json)
           VALUES(:worker, :status, :pid, :host, :last_seen, :detail)
           ON DUPLICATE KEY UPDATE status=VALUES(status), pid=VALUES(pid), host=VALUES(host), last_seen=VALUES(last_seen), detail_json=VALUES(detail_json)""",
        {"worker": worker, "status": status, "pid": os.getpid(), "host": socket.gethostname(), "last_seen": now_utc().replace(tzinfo=None), "detail": json.dumps(detail or {})},
    )


def simulated_price(symbol: str) -> float:
    bases = {"BTCUSDT": 105000, "ETHUSDT": 3600, "SOLUSDT": 165, "BNBUSDT": 680, "XRPUSDT": 2.15}
    base = bases.get(symbol, 100)
    drift = math.sin(time.time() / 45 + len(symbol)) * 0.003
    noise = random.uniform(-0.002, 0.002)
    return round(base * (1 + drift + noise), 4)


def binance_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = CFG.market_data_trust_env_proxy
    return session


@contextmanager
def market_proxy_environment() -> Any:
    if CFG.market_data_trust_env_proxy:
        yield
        return
    keys = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]
    prior = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ.pop(key, None)
        yield
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def binance_rest_url(path: str) -> str:
    return f"{CFG.binance_rest_base_url}{path}"


def binance_get(path: str, params: dict[str, Any] | None = None, timeout: int = 5) -> Any:
    response = binance_session().get(binance_rest_url(path), params=params or {}, timeout=timeout, verify=CFG.binance_verify_tls)
    response.raise_for_status()
    return response.json()


def fetch_binance_price(symbol: str) -> tuple[float, str]:
    try:
        return float(binance_get("/api/v3/ticker/price", {"symbol": symbol}, timeout=3)["price"]), "LIVE"
    except Exception:
        return 0.0, "UNAVAILABLE"


def fetch_binance_klines(symbol: str, interval: str, limit: int = 180) -> pd.DataFrame | None:
    try:
        rows = binance_get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit}, timeout=5)
        columns = [
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
            "quote_volume",
            "n_trades",
            "taker_buy_base",
            "taker_buy_quote",
            "ignore",
        ]
        frame = pd.DataFrame(rows, columns=columns)
        for column in ["open", "high", "low", "close", "volume", "quote_volume", "taker_buy_base", "taker_buy_quote"]:
            frame[column] = frame[column].astype(float)
        frame["time"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True).dt.tz_convert(None)
        return frame[["time", "open", "high", "low", "close", "volume", "quote_volume", "taker_buy_base", "taker_buy_quote"]]
    except Exception:
        return None


def candle_frame_to_payload(symbol: str, interval: str, frame: pd.DataFrame, source: str, closed: bool = True) -> dict[str, Any]:
    if frame is None or frame.empty:
        rows: list[dict[str, Any]] = []
    else:
        clean = frame.tail(max(CFG.market_data_history_limit, 50)).copy()
        clean["time"] = pd.to_datetime(clean["time"], utc=True).dt.tz_convert(None)
        rows = [
            {
                "time": pd.Timestamp(row.time).isoformat(),
                "open": float(row.open),
                "high": float(row.high),
                "low": float(row.low),
                "close": float(row.close),
                "volume": float(row.volume),
                "quote_volume": float(getattr(row, "quote_volume", 0.0) or 0.0),
                "taker_buy_quote": float(getattr(row, "taker_buy_quote", 0.0) or 0.0),
            }
            for row in clean.itertuples(index=False)
        ]
    return {"symbol": symbol, "interval": interval, "source": source, "closed": closed, "rows": rows, "ts": iso_now()}


def candle_payload_to_frame(payload: dict[str, Any] | None) -> pd.DataFrame | None:
    if not payload or not payload.get("rows"):
        return None
    frame = pd.DataFrame(payload["rows"])
    if frame.empty:
        return None
    frame["time"] = pd.to_datetime(frame["time"], utc=True).dt.tz_convert(None)
    for column in ["open", "high", "low", "close", "volume", "quote_volume", "taker_buy_quote"]:
        if column not in frame.columns:
            frame[column] = 0.0
        frame[column] = frame[column].astype(float)
    return frame[["time", "open", "high", "low", "close", "volume", "quote_volume", "taker_buy_quote"]].sort_values("time").reset_index(drop=True)


def aggregate_candles(frame: pd.DataFrame, interval: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    if interval == "1m":
        return frame.tail(CFG.market_data_history_limit).reset_index(drop=True)
    rule = {"5m": "5min", "15m": "15min"}.get(interval)
    if not rule:
        return frame.tail(CFG.market_data_history_limit).reset_index(drop=True)
    grouped = frame.copy()
    grouped["bucket"] = pd.to_datetime(grouped["time"]).dt.floor(rule)
    result = (
        grouped.groupby("bucket", as_index=False)
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
            quote_volume=("quote_volume", "sum"),
            taker_buy_quote=("taker_buy_quote", "sum"),
        )
        .rename(columns={"bucket": "time"})
    )
    return result.tail(CFG.market_data_history_limit).reset_index(drop=True)


def latest_candle_frame(state: RedisState, symbol: str, interval: str, limit: int = 180) -> pd.DataFrame | None:
    frame = candle_payload_to_frame(state.get_json(f"latest_klines:{symbol}:{interval}") if state.ok else None)
    if frame is None or frame.empty:
        return None
    return frame.tail(limit).reset_index(drop=True)


def store_candle_frames(state: RedisState, symbol: str, one_minute_frame: pd.DataFrame, source: str, closed: bool = True) -> None:
    if one_minute_frame is None or one_minute_frame.empty:
        return
    for interval in CFG.market_data_intervals:
        frame = aggregate_candles(one_minute_frame, interval)
        payload = candle_frame_to_payload(symbol, interval, frame, source=source, closed=closed)
        state.set_json(f"latest_klines:{symbol}:{interval}", payload, ex=max(CFG.market_data_rest_backfill_seconds * 3, 1800))
        state.set_json(f"latest_kline:{symbol}:{interval}", payload, ex=max(CFG.market_data_rest_backfill_seconds * 3, 1800))
        state.publish("kline_updates", payload)


def simulated_orderbook() -> dict[str, Any]:
    bid_volume = random.uniform(100, 500)
    ask_volume = random.uniform(100, 500)
    spread_bps = random.uniform(0.5, 8)
    buy_slippage_bps = spread_bps / 2 + random.uniform(0.1, 3.0)
    sell_slippage_bps = spread_bps / 2 + random.uniform(0.1, 3.0)
    return {
        "bid_volume": bid_volume,
        "ask_volume": ask_volume,
        "obi": (bid_volume - ask_volume) / max(bid_volume + ask_volume, 1e-9),
        "spread_bps": spread_bps,
        "buy_slippage_bps": buy_slippage_bps,
        "sell_slippage_bps": sell_slippage_bps,
        "model_slippage_bps": max(buy_slippage_bps, sell_slippage_bps),
        "liquidity_score": random.uniform(0.55, 0.95),
        "data_quality": "SIMULATED",
    }


def unavailable_orderbook(reason: str = "UNAVAILABLE") -> dict[str, Any]:
    return {
        "bid_volume": 0.0,
        "ask_volume": 0.0,
        "bid_notional_20": 0.0,
        "ask_notional_20": 0.0,
        "obi": 0.0,
        "spread_bps": 999.0,
        "buy_slippage_bps": 999.0,
        "sell_slippage_bps": 999.0,
        "model_slippage_bps": 999.0,
        "liquidity_score": 0.0,
        "data_quality": reason,
    }


def unavailable_signal(symbol: str, price: float = 0.0, reason: str = "market_data_unavailable") -> dict[str, Any]:
    blockers = [reason]
    return {
        "symbol": symbol,
        "side": "HOLD",
        "candidate_side": "HOLD",
        "price": float(price or 0.0),
        "composite_score": 0.0,
        "z_score": 0.0,
        "rsi": 50.0,
        "volume_z": 0.0,
        "taker_buy_ratio": 0.5,
        "atr_pct": 0.0,
        "adx": 0.0,
        "realized_vol_ratio": 0.0,
        "expected_reversion_bps": 0.0,
        "htf_trend": 0.0,
        "strategy_interval": CFG.strategy_interval,
        "hold_bars": CFG.mean_reversion_hold_bars,
        "research_only": CFG.mean_reversion_research_only,
        "deployment_blockers": blockers,
        "obi": 0.0,
        "cross_exchange_spread_bps": 0.0,
        "funding_pressure": 0.0,
        "open_interest_signal": 0.0,
        "model_slippage_bps": 999.0,
        "spread_bps": 999.0,
        "market_source": reason.upper(),
        "market_data_quality": "UNAVAILABLE",
        "ml_confidence": 0.0,
        "ml_model_version": "not_scored",
        "win_p_est": 0.0,
        "payoff_b": 0.0,
        "expected_value": 0.0,
        "confidence": 0.0,
        "kelly_fraction": 0.0,
        "suggested_usdt": 0.0,
        "deployable": False,
        "validation_status": "AMBER",
        "risk_status": "OK",
        "rationale": f"Live data unavailable: {reason}.",
        "ts": iso_now(),
    }


def fetch_coinbase_price(symbol: str) -> float | None:
    base = symbol.removesuffix("USDT")
    if base == symbol:
        return None
    try:
        response = requests.get(f"https://api.coinbase.com/v2/prices/{base}-USD/spot", timeout=3)
        response.raise_for_status()
        return float(response.json()["data"]["amount"])
    except Exception:
        return None


def fetch_kraken_price(symbol: str) -> float | None:
    base = symbol.removesuffix("USDT")
    kraken_pair = {"BTC": "XXBTZUSD", "ETH": "XETHZUSD", "SOL": "SOLUSD", "XRP": "XXRPZUSD", "BNB": "BNBUSD"}.get(base)
    if not kraken_pair:
        return None
    try:
        response = requests.get("https://api.kraken.com/0/public/Ticker", params={"pair": kraken_pair}, timeout=3)
        response.raise_for_status()
        result = response.json().get("result", {})
        first = next(iter(result.values()))
        return float(first["c"][0])
    except Exception:
        return None


def fetch_funding_state(symbol: str) -> dict[str, Any]:
    try:
        response = requests.get("https://fapi.binance.com/fapi/v1/fundingRate", params={"symbol": symbol, "limit": 20}, timeout=3)
        response.raise_for_status()
        rows = response.json()
        rates = [float(row["fundingRate"]) for row in rows if "fundingRate" in row]
        if not rates:
            raise ValueError("empty funding")
        latest = rates[-1]
        percentile = float(pd.Series(rates).rank(pct=True).iloc[-1])
        return {"symbol": symbol, "funding_rate": latest, "percentile": percentile, "data_quality": "LIVE", "ts": iso_now()}
    except Exception:
        return {"symbol": symbol, "funding_rate": 0.0, "percentile": 0.5, "data_quality": "UNAVAILABLE", "ts": iso_now()}


def fetch_open_interest_state(symbol: str, previous_oi: float | None = None) -> dict[str, Any]:
    try:
        response = requests.get("https://fapi.binance.com/fapi/v1/openInterest", params={"symbol": symbol}, timeout=3)
        response.raise_for_status()
        current = float(response.json()["openInterest"])
        quality = "LIVE"
    except Exception:
        current = float(previous_oi or 0.0)
        quality = "UNAVAILABLE"
    base = previous_oi if previous_oi and previous_oi > 0 else current
    oi_change = (current - base) / max(base, 1e-9)
    return {"symbol": symbol, "open_interest": current, "oi_change": oi_change, "data_quality": quality, "ts": iso_now()}


def cross_exchange_state(symbol: str, binance_price: float) -> dict[str, Any]:
    references = {
        "coinbase": fetch_coinbase_price(symbol),
        "kraken": fetch_kraken_price(symbol),
    }
    valid = {venue: price for venue, price in references.items() if price and price > 0}
    if not valid:
        return {
            "symbol": symbol,
            "binance_price": binance_price,
            "reference_price": 0.0,
            "reference_venues": {},
            "cross_exchange_spread_bps": 0.0,
            "stale": True,
            "data_quality": "UNAVAILABLE",
            "ts": iso_now(),
        }
    else:
        data_quality = "LIVE"
    reference_price = float(np.median(list(valid.values())))
    spread_bps = ((binance_price - reference_price) / max(reference_price, 1e-9)) * 10000
    return {
        "symbol": symbol,
        "binance_price": binance_price,
        "reference_price": reference_price,
        "reference_venues": valid,
        "cross_exchange_spread_bps": float(spread_bps),
        "stale": False,
        "data_quality": data_quality,
        "ts": iso_now(),
    }


def fetch_orderbook(symbol: str) -> dict[str, Any]:
    try:
        data = binance_get("/api/v3/depth", {"symbol": symbol, "limit": 20}, timeout=3)
        bids = [(float(p), float(q)) for p, q in data.get("bids", [])]
        asks = [(float(p), float(q)) for p, q in data.get("asks", [])]
        if not bids or not asks:
            raise ValueError("empty depth")
        bid_volume = sum(q for _, q in bids)
        ask_volume = sum(q for _, q in asks)
        bid_notional = sum(p * q for p, q in bids[:20])
        ask_notional = sum(p * q for p, q in asks[:20])
        mid = (bids[0][0] + asks[0][0]) / 2
        spread_bps = ((asks[0][0] - bids[0][0]) / mid) * 10000
        obi = (bid_volume - ask_volume) / max(bid_volume + ask_volume, 1e-9)
        liquidity_score = min(1.0, (bid_notional + ask_notional) / 250000)

        def simulated_vwap_slippage(book: list[tuple[float, float]], side: str, notional: float = 1000.0) -> float:
            remaining = notional
            cost = 0.0
            qty = 0.0
            for price, amount in book:
                level_notional = price * amount
                take = min(remaining, level_notional)
                cost += take
                qty += take / price
                remaining -= take
                if remaining <= 1e-9:
                    break
            if qty <= 0 or remaining > 1e-6:
                return 999.0
            vwap = cost / qty
            if side == "BUY":
                return max(0.0, ((vwap - mid) / mid) * 10000)
            return max(0.0, ((mid - vwap) / mid) * 10000)

        buy_slippage_bps = simulated_vwap_slippage(asks, "BUY")
        sell_slippage_bps = simulated_vwap_slippage(bids, "SELL")
        return {
            "bid_volume": bid_volume,
            "ask_volume": ask_volume,
            "bid_notional_20": bid_notional,
            "ask_notional_20": ask_notional,
            "obi": obi,
            "spread_bps": spread_bps,
            "buy_slippage_bps": buy_slippage_bps,
            "sell_slippage_bps": sell_slippage_bps,
            "model_slippage_bps": max(buy_slippage_bps, sell_slippage_bps),
            "liquidity_score": liquidity_score,
            "data_quality": "LIVE",
        }
    except Exception:
        return unavailable_orderbook("UNAVAILABLE")


def alpha_signal(symbol: str, price: float, orderbook: dict[str, Any], cfg: Config, market_frame: pd.DataFrame | None = None, htf_frame: pd.DataFrame | None = None, allow_live_fetch: bool = True) -> dict[str, Any]:
    frame = market_frame if market_frame is not None and len(market_frame) >= 30 else None
    if frame is None:
        return unavailable_signal(symbol, price, "kline_history_unavailable")
    indicators = add_indicators(frame)
    latest = indicators.iloc[-1] if not indicators.empty else pd.Series({"z": 0.0, "rsi": 50.0, "vol_z": 0.0, "ema20": price, "ema50": price, "taker_buy_ratio": 0.5, "adx": 99.0, "realized_vol_fast": 1.0, "realized_vol_slow": 0.0, "expected_reversion_bps": 0.0})
    z_score = float(latest.get("z", 0.0))
    rsi = float(latest.get("rsi", 50.0))
    volume_z = float(latest.get("vol_z", 0.0))
    taker_buy_ratio = float(latest.get("taker_buy_ratio", 0.5))
    atr_pct = float(latest.get("atr_pct", 0.0))
    adx = float(latest.get("adx", 99.0))
    realized_vol_fast = float(latest.get("realized_vol_fast", 1.0))
    realized_vol_slow = float(latest.get("realized_vol_slow", 0.0))
    realized_vol_ratio = realized_vol_fast / max(realized_vol_slow, 1e-9)
    expected_reversion_bps = float(latest.get("expected_reversion_bps", 0.0))
    htf = htf_frame if htf_frame is not None and len(htf_frame) >= 30 else fetch_binance_klines(symbol, cfg.higher_timeframe_interval, limit=120) if allow_live_fetch else None
    htf_indicators = add_indicators(htf) if htf is not None and len(htf) >= 30 else pd.DataFrame()
    htf_latest = htf_indicators.iloc[-1] if not htf_indicators.empty else None
    htf_trend = 0.0 if htf_latest is None else float((htf_latest["ema20"] - htf_latest["ema50"]) / max(float(htf_latest["close"]), 1e-9))
    cross_spread = float(orderbook.get("cross_exchange_spread_bps", 0.0))
    funding_pressure = float(np.clip(float(orderbook.get("funding_pressure", 0.0)), -1, 1))
    oi_signal = float(np.clip(float(orderbook.get("open_interest_signal", 0.0)), -1, 1))
    trend = float(np.clip((float(latest.get("ema20", price)) - float(latest.get("ema50", price))) / max(price, 1e-9) * 100, -1, 1))
    execution_quality = max(0.0, min(1.0, orderbook["liquidity_score"] - orderbook["spread_bps"] / 30))
    score = (
        -0.35 * np.tanh(z_score / 2)
        + 0.20 * orderbook["obi"]
        - 0.10 * np.tanh(cross_spread / 10)
        + 0.15 * funding_pressure
        + 0.10 * oi_signal
        - 0.10 * trend
        + 0.10 * ((execution_quality - 0.5) * 2)
    )
    confidence = float(np.clip(abs(score) * 1.7 + execution_quality * 0.25, 0, 1))
    side = "BUY" if z_score <= -cfg.mean_reversion_z else "SELL" if z_score >= cfg.mean_reversion_z else "HOLD"
    candidate_side = side
    blockers: list[str] = []
    if cfg.mean_reversion_research_only:
        blockers.append("research_only")
    if cfg.deploy_symbol_whitelist and symbol not in cfg.deploy_symbol_whitelist:
        blockers.append("symbol_not_whitelisted")
    if side == "BUY" and rsi > cfg.mean_reversion_rsi_buy:
        blockers.append("buy_rsi_not_oversold")
    if side == "SELL" and rsi < cfg.mean_reversion_rsi_sell:
        blockers.append("sell_rsi_not_overbought")
    if volume_z < cfg.mean_reversion_min_volume_z:
        blockers.append("low_participation")
    if cfg.mean_reversion_taker_filter and side == "BUY" and taker_buy_ratio < 0.42:
        blockers.append("buy_taker_flow_not_confirmed")
    if cfg.mean_reversion_taker_filter and side == "SELL" and taker_buy_ratio > 0.58:
        blockers.append("sell_taker_flow_not_confirmed")
    if side == "BUY" and orderbook["obi"] < cfg.orderbook_confirmation_min_obi:
        blockers.append("buy_orderbook_not_confirmed")
    if side == "SELL" and orderbook["obi"] > cfg.orderbook_confirmation_max_obi:
        blockers.append("sell_orderbook_not_confirmed")
    if adx > cfg.max_mean_reversion_adx:
        blockers.append("trend_regime_adx_high")
    if realized_vol_fast > realized_vol_slow * cfg.volatility_falling_ratio:
        blockers.append("volatility_not_falling")
    if expected_reversion_bps < cfg.expected_move_cost_multiple * (2 * cfg.fee_bps + cfg.slippage_bps):
        blockers.append("expected_move_below_cost_hurdle")
    if side == "BUY" and htf_trend < 0:
        blockers.append("higher_timeframe_downtrend")
    if side == "SELL" and htf_trend > 0:
        blockers.append("higher_timeframe_uptrend")
    side_slippage = orderbook.get("buy_slippage_bps", 999.0) if side == "BUY" else orderbook.get("sell_slippage_bps", 999.0)
    if orderbook["spread_bps"] > 10:
        blockers.append("spread_too_wide")
    if side_slippage > CFG.slippage_bps:
        blockers.append("slippage_too_high")
    if confidence < 0.35:
        blockers.append("confidence_too_low")
    if blockers:
        side = "HOLD"
    win_p = float(np.clip(0.50 + confidence * 0.18, 0.45, 0.72))
    payoff_b = float(np.clip(1.15 + abs(z_score) * 0.18, 0.75, 2.5))
    kelly = ((payoff_b * win_p) - (1 - win_p)) / max(payoff_b, 1e-9)
    kelly = max(0.0, min(kelly, cfg.max_kelly_fraction))
    suggested = min(cfg.max_position_usdt, cfg.starting_equity * kelly)
    deployable = side != "HOLD" and confidence >= 0.45 and suggested > 0 and orderbook["spread_bps"] <= 10 and side_slippage <= CFG.slippage_bps
    return {
        "symbol": symbol,
        "side": side,
        "candidate_side": candidate_side,
        "price": price,
        "composite_score": float(score),
        "z_score": z_score,
        "rsi": rsi,
        "volume_z": volume_z,
        "taker_buy_ratio": taker_buy_ratio,
        "atr_pct": atr_pct,
        "adx": adx,
        "realized_vol_ratio": realized_vol_ratio,
        "expected_reversion_bps": expected_reversion_bps,
        "htf_trend": htf_trend,
        "strategy_interval": cfg.strategy_interval,
        "hold_bars": cfg.mean_reversion_hold_bars,
        "research_only": cfg.mean_reversion_research_only,
        "deployment_blockers": blockers,
        "obi": float(orderbook["obi"]),
        "cross_exchange_spread_bps": cross_spread,
        "funding_pressure": funding_pressure,
        "open_interest_signal": oi_signal,
        "model_slippage_bps": float(side_slippage if side != "HOLD" else orderbook.get("model_slippage_bps", 0.0)),
        "spread_bps": float(orderbook["spread_bps"]),
        "market_source": "CANDLE_BUFFER",
        "market_data_quality": str(orderbook.get("data_quality", "UNKNOWN")),
        "ml_confidence": 0.0,
        "ml_model_version": "not_scored",
        "win_p_est": win_p,
        "payoff_b": payoff_b,
        "expected_value": (win_p * payoff_b) - (1 - win_p),
        "confidence": confidence,
        "kelly_fraction": kelly,
        "suggested_usdt": suggested,
        "deployable": deployable,
        "validation_status": "GREEN" if deployable else "AMBER",
        "risk_status": "OK",
        "rationale": f"MR {cfg.strategy_interval}: z={z_score:.2f}, RSI={rsi:.1f}, vol_z={volume_z:.2f}, ADX={adx:.1f}, exp_move={expected_reversion_bps:.1f}bps, blockers={','.join(blockers) or 'none'}.",
        "ts": iso_now(),
    }


def synthetic_ohlcv(symbol: str, bars: int = 360) -> pd.DataFrame:
    seed = int(hashlib.sha256(symbol.encode("utf-8")).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)
    base = simulated_price(symbol)
    returns = rng.normal(0.00008, 0.0025, bars)
    close = base * np.cumprod(1 + returns)
    high = close * (1 + rng.uniform(0.0002, 0.002, bars))
    low = close * (1 - rng.uniform(0.0002, 0.002, bars))
    open_ = np.r_[close[0], close[:-1]]
    volume = rng.uniform(100, 2000, bars)
    quote_volume = volume * close
    taker_buy_quote = quote_volume * rng.uniform(0.42, 0.58, bars)
    return pd.DataFrame(
        {
            "time": pd.date_range(end=utc_naive_timestamp(pd.Timestamp.now(tz=UTC)), periods=bars, freq="min"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "quote_volume": quote_volume,
            "taker_buy_quote": taker_buy_quote,
        }
    )


def add_indicators(frame: pd.DataFrame, lookback: int = 40) -> pd.DataFrame:
    data = frame.copy()
    if len(data) < 30:
        return pd.DataFrame()
    basis_window = min(lookback, max(10, len(data) // 2))
    atr_window = min(14, max(5, len(data) // 3))
    rsi_window = atr_window
    adx_window = atr_window
    volume_window = min(50, max(10, len(data) // 2))
    fast_window = min(20, max(5, len(data) // 3))
    slow_window = min(80, max(fast_window + 1, len(data) - 5))
    if "quote_volume" not in data:
        data["quote_volume"] = data["volume"] * data["close"]
    if "taker_buy_quote" not in data:
        data["taker_buy_quote"] = data["quote_volume"] * 0.5
    data["ret"] = data["close"].pct_change()
    data["ema20"] = data["close"].ewm(span=20, adjust=False).mean()
    data["ema50"] = data["close"].ewm(span=50, adjust=False).mean()
    data["basis"] = data["close"].rolling(basis_window).mean()
    data["std"] = data["close"].rolling(basis_window).std()
    data["z"] = (data["close"] - data["basis"]) / data["std"].replace(0, np.nan)
    true_range = pd.concat(
        [
            data["high"] - data["low"],
            (data["high"] - data["close"].shift()).abs(),
            (data["low"] - data["close"].shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    data["atr"] = true_range.rolling(atr_window).mean()
    data["atr_pct"] = data["atr"] / data["close"]
    up_move = data["high"].diff()
    down_move = -data["low"].diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    plus_di = 100 * pd.Series(plus_dm, index=data.index).rolling(adx_window).sum() / true_range.rolling(adx_window).sum().replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=data.index).rolling(adx_window).sum() / true_range.rolling(adx_window).sum().replace(0, np.nan)
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)) * 100
    data["adx"] = dx.rolling(adx_window).mean()
    delta = data["close"].diff()
    gain = delta.clip(lower=0).rolling(rsi_window).mean()
    loss = (-delta.clip(upper=0)).rolling(rsi_window).mean()
    rs = gain / loss.replace(0, np.nan)
    data["rsi"] = 100 - (100 / (1 + rs))
    data["vol_z"] = (data["quote_volume"] - data["quote_volume"].rolling(volume_window).mean()) / data["quote_volume"].rolling(volume_window).std().replace(0, np.nan)
    data["taker_buy_ratio"] = data["taker_buy_quote"] / data["quote_volume"].replace(0, np.nan)
    data["realized_vol_fast"] = data["ret"].rolling(fast_window).std()
    data["realized_vol_slow"] = data["ret"].rolling(slow_window).std()
    data["expected_reversion_bps"] = ((data["basis"] - data["close"]).abs() / data["close"].replace(0, np.nan)) * 10000
    return data.dropna().reset_index(drop=True)


def run_backtest_for_frame(symbol: str, frame: pd.DataFrame, z_trigger: float | None = None, hold_bars: int | None = None) -> pd.DataFrame:
    z_trigger = CFG.mean_reversion_z if z_trigger is None else z_trigger
    hold_bars = CFG.mean_reversion_hold_bars if hold_bars is None else hold_bars
    data = add_indicators(frame)
    trades: list[dict[str, Any]] = []
    next_i = 60
    for i in range(60, max(60, len(data) - hold_bars)):
        if i < next_i:
            continue
        row = data.iloc[i]
        side = "BUY" if row["z"] <= -z_trigger else "SELL" if row["z"] >= z_trigger else None
        if side is None:
            continue
        if side == "BUY" and row["rsi"] > CFG.mean_reversion_rsi_buy:
            continue
        if side == "SELL" and row["rsi"] < CFG.mean_reversion_rsi_sell:
            continue
        if row["vol_z"] < CFG.mean_reversion_min_volume_z:
            continue
        if CFG.mean_reversion_taker_filter and side == "BUY" and row["taker_buy_ratio"] < 0.42:
            continue
        if CFG.mean_reversion_taker_filter and side == "SELL" and row["taker_buy_ratio"] > 0.58:
            continue
        if row["adx"] > CFG.max_mean_reversion_adx:
            continue
        if row["realized_vol_fast"] > row["realized_vol_slow"] * CFG.volatility_falling_ratio:
            continue
        if row["expected_reversion_bps"] < CFG.expected_move_cost_multiple * (2 * CFG.fee_bps + CFG.slippage_bps):
            continue
        entry = float(row["close"])
        exit_index = i + hold_bars
        exit_px = float(data.iloc[exit_index]["close"])
        path = data.iloc[i + 1 : i + hold_bars + 1]
        if side == "BUY":
            touched = path[path["high"] >= row["basis"]]
            if not touched.empty:
                exit_index = int(touched.index[0])
                exit_px = float(row["basis"])
        else:
            touched = path[path["low"] <= row["basis"]]
            if not touched.empty:
                exit_index = int(touched.index[0])
                exit_px = float(row["basis"])
        gross = (exit_px / entry - 1) if side == "BUY" else (entry / exit_px - 1)
        net = gross - 2 * CFG.fee_bps / 10000 - CFG.slippage_bps / 10000
        trades.append({"time": row["time"], "symbol": symbol, "side": side, "entry": entry, "exit": exit_px, "ret": net, "z": float(row["z"]), "rsi": float(row["rsi"]), "vol_z": float(row["vol_z"]), "adx": float(row["adx"]), "expected_reversion_bps": float(row["expected_reversion_bps"])})
        next_i = exit_index + hold_bars
    return pd.DataFrame(trades)


def metrics_from_trades(trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty:
        return {
            "total_trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "expectancy": 0.0,
            "avg_r": 0.0,
            "max_drawdown": 0.0,
            "sharpe_like": 0.0,
            "largest_winner": 0.0,
            "largest_loser": 0.0,
            "consecutive_losses": 0,
            "equity_curve": [CFG.starting_equity],
        }
    returns = trades["ret"].astype(float)
    equity_curve = (CFG.starting_equity * (1 + returns).cumprod()).tolist()
    equity = pd.Series(equity_curve)
    drawdown = equity / equity.cummax() - 1
    wins = returns[returns > 0]
    losses = returns[returns <= 0]
    loss_streak = 0
    max_loss_streak = 0
    for value in returns:
        loss_streak = loss_streak + 1 if value <= 0 else 0
        max_loss_streak = max(max_loss_streak, loss_streak)
    return {
        "total_trades": int(len(returns)),
        "win_rate": float((returns > 0).mean()),
        "profit_factor": float(wins.sum() / abs(losses.sum())) if abs(losses.sum()) > 0 else float("inf"),
        "expectancy": float(returns.mean()),
        "avg_r": float(returns.mean() / max(abs(losses.mean()) if not losses.empty else returns.std(), 1e-9)),
        "max_drawdown": float(drawdown.min()),
        "sharpe_like": float(returns.mean() / (returns.std() + 1e-9) * math.sqrt(252)) if len(returns) > 3 else 0.0,
        "largest_winner": float(returns.max()),
        "largest_loser": float(returns.min()),
        "consecutive_losses": int(max_loss_streak),
        "equity_curve": equity_curve,
    }


ML_FEATURES = [
    "z_abs",
    "z_signed",
    "rsi_distance",
    "volume_z",
    "adx",
    "expected_reversion_bps",
    "taker_buy_ratio",
    "atr_pct",
    "realized_vol_ratio",
    "htf_trend",
    "obi",
    "spread_bps",
    "model_slippage_bps",
    "funding_pressure",
    "open_interest_signal",
]


ML_FEATURE_DESCRIPTIONS = {
    "z_abs": "Absolute mean-reversion stretch.",
    "z_signed": "Direction of price stretch versus basis.",
    "rsi_distance": "How far RSI is beyond the buy/sell threshold.",
    "volume_z": "Volume participation versus recent normal.",
    "adx": "Trend strength; high values are bad for mean reversion.",
    "expected_reversion_bps": "Estimated gross reversion opportunity in basis points.",
    "taker_buy_ratio": "Aggressive buy flow share.",
    "atr_pct": "Recent volatility as percent of price.",
    "realized_vol_ratio": "Fast volatility versus slow volatility.",
    "htf_trend": "Higher-timeframe trend context.",
    "obi": "Order-book imbalance.",
    "spread_bps": "Observed bid/ask or venue price gap.",
    "model_slippage_bps": "Estimated execution slippage.",
    "funding_pressure": "Funding/crowding pressure.",
    "open_interest_signal": "Open-interest behavior signal.",
}


def dedupe_training_frame(training: pd.DataFrame) -> pd.DataFrame:
    if training.empty:
        return training
    frame = training.copy()
    if "idempotency_key" in frame.columns:
        frame = frame.drop_duplicates(subset=["idempotency_key"], keep="last")
    else:
        subset = [col for col in ["symbol", "side", "time"] if col in frame.columns]
        if subset:
            frame = frame.drop_duplicates(subset=subset, keep="last")
    return frame.sort_values("time").reset_index(drop=True) if "time" in frame.columns else frame.reset_index(drop=True)


def training_sample_weights(training: pd.DataFrame) -> np.ndarray:
    weights = np.ones(len(training), dtype=float)
    if training.empty:
        return weights
    labels = training["label"].astype(int)
    if CFG.ml_balance_classes:
        for label in [0, 1]:
            count = int((labels == label).sum())
            if count:
                weights[labels == label] *= len(labels) / (2.0 * count)
    candidate_mask = training.get("side", pd.Series([""] * len(training))).astype(str).isin(["BUY", "SELL"]).to_numpy()
    stretch = training.get("z_abs", pd.Series([0.0] * len(training))).astype(float).to_numpy()
    opportunity = training.get("expected_reversion_bps", pd.Series([0.0] * len(training))).astype(float).to_numpy()
    quality_mask = candidate_mask & (stretch >= max(1.0, CFG.mean_reversion_z * 0.6)) & (opportunity >= (2 * CFG.fee_bps + CFG.slippage_bps))
    weights[quality_mask] *= max(CFG.ml_candidate_quality_weight, 1.0)
    return weights / max(float(weights.mean()), 1e-9)


def signal_features(signal: dict[str, Any]) -> dict[str, float]:
    z_score = float(signal.get("z_score", 0.0))
    rsi = float(signal.get("rsi", 50.0))
    side = str(signal.get("side", "HOLD"))
    candidate_side = str(signal.get("candidate_side", side))
    if side == "HOLD" and candidate_side in {"BUY", "SELL"}:
        side = candidate_side
    rsi_distance = (35.0 - rsi) if side == "BUY" else (rsi - 65.0) if side == "SELL" else abs(rsi - 50.0)
    return {
        "z_abs": abs(z_score),
        "z_signed": z_score,
        "rsi_distance": float(rsi_distance),
        "volume_z": float(signal.get("volume_z", 0.0)),
        "adx": float(signal.get("adx", 99.0)),
        "expected_reversion_bps": float(signal.get("expected_reversion_bps", 0.0)),
        "taker_buy_ratio": float(signal.get("taker_buy_ratio", 0.5)),
        "atr_pct": float(signal.get("atr_pct", 0.0)),
        "realized_vol_ratio": float(signal.get("realized_vol_ratio", 1.0)),
        "htf_trend": float(signal.get("htf_trend", 0.0)),
        "obi": float(signal.get("obi", 0.0)),
        "spread_bps": float(signal.get("spread_bps", signal.get("cross_exchange_spread_bps", 0.0))),
        "model_slippage_bps": float(signal.get("model_slippage_bps", 0.0)),
        "funding_pressure": float(signal.get("funding_pressure", 0.0)),
        "open_interest_signal": float(signal.get("open_interest_signal", 0.0)),
    }


def sigmoid(value: np.ndarray | float) -> np.ndarray | float:
    return 1.0 / (1.0 + np.exp(-np.clip(value, -40, 40)))


def fallback_ml_confidence(features: dict[str, float]) -> float:
    cost_hurdle = CFG.expected_move_cost_multiple * (2 * CFG.fee_bps + CFG.slippage_bps)
    score = (
        0.55
        + 0.05 * np.tanh((features["z_abs"] - CFG.mean_reversion_z) * 1.3)
        + 0.07 * np.tanh((features["expected_reversion_bps"] - cost_hurdle) / 50)
        - 0.06 * np.tanh((features["adx"] - CFG.max_mean_reversion_adx) / 10)
        - 0.05 * np.tanh((features["realized_vol_ratio"] - CFG.volatility_falling_ratio) * 3)
        - 0.04 * np.tanh(features["model_slippage_bps"] / max(CFG.slippage_bps, 1e-9))
    )
    return float(np.clip(score, 0.05, 0.95))


def training_candidates(symbol: str, frame: pd.DataFrame, htf_frame: pd.DataFrame | None = None) -> pd.DataFrame:
    data = add_indicators(frame)
    if htf_frame is not None and len(htf_frame) >= 80:
        htf = add_indicators(htf_frame)
        htf["htf_trend"] = (htf["ema20"] - htf["ema50"]) / htf["close"].replace(0, np.nan)
        data = pd.merge_asof(data.sort_values("time"), htf[["time", "htf_trend"]].sort_values("time"), on="time", direction="backward").dropna().reset_index(drop=True)
    else:
        data["htf_trend"] = 0.0
    rows: list[dict[str, Any]] = []
    horizon = CFG.mean_reversion_hold_bars
    relaxed_z = max(2.0, CFG.mean_reversion_z - 0.6)
    round_trip_cost = (2 * CFG.fee_bps + CFG.slippage_bps) / 10000
    for i in range(60, max(60, len(data) - horizon)):
        row = data.iloc[i]
        side = "BUY" if row["z"] <= -relaxed_z else "SELL" if row["z"] >= relaxed_z else None
        if side is None:
            continue
        entry = float(row["close"])
        path = data.iloc[i + 1 : i + horizon + 1]
        exit_px = float(data.iloc[i + horizon]["close"])
        if side == "BUY":
            touched = path[path["high"] >= row["basis"]]
            if not touched.empty:
                exit_px = float(row["basis"])
            favorable = ((path["high"].max() / entry) - 1) * 10000
            adverse = ((path["low"].min() / entry) - 1) * 10000
            gross = exit_px / entry - 1
        else:
            touched = path[path["low"] <= row["basis"]]
            if not touched.empty:
                exit_px = float(row["basis"])
            favorable = ((entry / path["low"].min()) - 1) * 10000
            adverse = ((entry / path["high"].max()) - 1) * 10000
            gross = entry / exit_px - 1
        net = gross - round_trip_cost
        feature_row = {
            "z_abs": abs(float(row["z"])),
            "z_signed": float(row["z"]),
            "rsi_distance": float((35 - row["rsi"]) if side == "BUY" else (row["rsi"] - 65)),
            "volume_z": float(row["vol_z"]),
            "adx": float(row["adx"]),
            "expected_reversion_bps": float(row["expected_reversion_bps"]),
            "taker_buy_ratio": float(row["taker_buy_ratio"]),
            "atr_pct": float(row["atr_pct"]),
            "realized_vol_ratio": float(row["realized_vol_fast"] / max(float(row["realized_vol_slow"]), 1e-9)),
            "htf_trend": float(row.get("htf_trend", 0.0)),
            "obi": 0.0,
            "spread_bps": 1.0,
            "model_slippage_bps": CFG.slippage_bps,
            "funding_pressure": 0.0,
            "open_interest_signal": 0.0,
            "symbol": symbol,
            "side": side,
            "time": row["time"],
            "forward_return": float(net),
            "max_favorable_bps": float(favorable),
            "max_adverse_bps": float(adverse),
            "label": int(net > 0),
        }
        rows.append(feature_row)
    return pd.DataFrame(rows)


def train_logistic_model(training: pd.DataFrame) -> dict[str, Any] | None:
    if training.empty or training["label"].nunique() < 2 or len(training) < 50:
        return None
    x = training[ML_FEATURES].astype(float).replace([np.inf, -np.inf], np.nan).fillna(0.0).to_numpy()
    y = training["label"].astype(float).to_numpy()
    weights_all = training_sample_weights(training)
    split = max(20, int(len(x) * 0.7))
    train_x, test_x = x[:split], x[split:]
    train_y, test_y = y[:split], y[split:]
    train_weights = weights_all[:split]
    mean = train_x.mean(axis=0)
    std = train_x.std(axis=0)
    std[std == 0] = 1.0
    train_x = (train_x - mean) / std
    test_x = (test_x - mean) / std if len(test_x) else train_x
    weights = np.zeros(train_x.shape[1])
    bias = 0.0
    lr = 0.05
    l2 = 0.01
    for _ in range(700):
        pred = sigmoid(train_x @ weights + bias)
        error = (pred - train_y) * train_weights
        weights -= lr * ((train_x.T @ error) / max(float(train_weights.sum()), 1e-9) + l2 * weights)
        bias -= lr * float(error.sum() / max(float(train_weights.sum()), 1e-9))
    test_pred = sigmoid(test_x @ weights + bias)
    test_label = test_y if len(test_y) else train_y
    binary = test_pred >= 0.5
    accuracy = float((binary == test_label).mean()) if len(test_label) else 0.0
    positives = test_label == 1
    precision = float(((binary == 1) & positives).sum() / max((binary == 1).sum(), 1))
    recall = float(((binary == 1) & positives).sum() / max(positives.sum(), 1))
    version = f"mr-logistic-{int(time.time())}"
    raw_importance = {name: abs(float(weight)) for name, weight in zip(ML_FEATURES, weights)}
    total_importance = sum(raw_importance.values()) or 1.0
    feature_importance = {name: value / total_importance for name, value in raw_importance.items()}
    return {
        "model_name": "mean_reversion_entry_confidence",
        "version": version,
        "features": ML_FEATURES,
        "feature_importance": feature_importance,
        "mean": mean.tolist(),
        "std": std.tolist(),
        "weights": weights.tolist(),
        "bias": bias,
        "metrics": {
            "accuracy": accuracy,
            "precision": precision,
            "recall": recall,
            "rows": int(len(training)),
            "positive_rate": float(y.mean()),
            "class_balanced": CFG.ml_balance_classes,
            "candidate_quality_weight": CFG.ml_candidate_quality_weight,
        },
    }


def predict_with_model(model: dict[str, Any] | None, features: dict[str, float]) -> tuple[float, str]:
    if not model:
        return fallback_ml_confidence(features), "heuristic-fallback"
    values = np.array([float(features.get(name, 0.0)) for name in model["features"]])
    mean = np.array(model["mean"])
    std = np.array(model["std"])
    weights = np.array(model["weights"])
    confidence = float(sigmoid(((values - mean) / std) @ weights + float(model["bias"])))
    return confidence, str(model.get("version", "unknown"))


def load_ml_model(engine: Engine | None, state: RedisState) -> dict[str, Any] | None:
    cached = state.get_json("ml_model:mean_reversion_entry_confidence")
    if cached:
        return cached
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT model_json FROM model_registry WHERE model_name='mean_reversion_entry_confidence' AND status='ACTIVE' ORDER BY trained_at DESC LIMIT 1")).fetchone()
        if row:
            model = json.loads(row.model_json)
            state.set_json("ml_model:mean_reversion_entry_confidence", model, ex=CFG.ml_retrain_seconds)
            return model
    except Exception:
        return None
    return None


def load_labeled_training_from_db(engine: Engine | None) -> pd.DataFrame:
    if engine is None:
        return pd.DataFrame()
    try:
        query = """
            SELECT f.idempotency_key, f.symbol, f.side, f.feature_json, o.label, o.forward_return, o.max_favorable_bps, o.max_adverse_bps, o.ts
            FROM feature_snapshots f
            JOIN trade_outcomes o ON o.idempotency_key = f.idempotency_key
            ORDER BY o.ts ASC
        """
        rows = pd.read_sql(query, engine)
    except Exception:
        return pd.DataFrame()
    records: list[dict[str, Any]] = []
    for _, row in rows.iterrows():
        try:
            features = json.loads(row["feature_json"] or "{}")
            record = {name: float(features.get(name, 0.0)) for name in ML_FEATURES}
            record.update(
                {
                    "symbol": row["symbol"],
                    "side": row["side"],
                    "idempotency_key": row["idempotency_key"],
                    "label": int(row["label"]),
                    "forward_return": float(row["forward_return"]),
                    "max_favorable_bps": float(row["max_favorable_bps"] or 0.0),
                    "max_adverse_bps": float(row["max_adverse_bps"] or 0.0),
                    "time": utc_naive_timestamp(row["ts"]),
                }
            )
            records.append(record)
        except Exception:
            continue
    return pd.DataFrame(records)


def latest_active_model(engine: Engine | None) -> dict[str, Any] | None:
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT model_json FROM model_registry WHERE model_name='mean_reversion_entry_confidence' AND status='ACTIVE' ORDER BY trained_at DESC LIMIT 1")).fetchone()
        return json.loads(row.model_json) if row else None
    except Exception:
        return None


def latest_model_candidate(engine: Engine | None) -> dict[str, Any]:
    rows = db_rows(
        engine,
        """SELECT version, status, trained_rows, feature_importance_json, metrics_json, trained_at
           FROM model_registry
           WHERE model_name='mean_reversion_entry_confidence'
           ORDER BY trained_at DESC LIMIT 1""",
    )
    if not rows:
        return {}
    row = rows[0]
    metrics = {}
    try:
        metrics = json.loads(row.get("metrics_json") or "{}")
    except Exception:
        metrics = {}
    importance = {}
    try:
        importance = json.loads(row.get("feature_importance_json") or "{}")
    except Exception:
        importance = {}
    return {**row, "metrics": metrics, "feature_importance": importance}


def model_promotable(candidate: dict[str, Any], active: dict[str, Any] | None, cfg: Config) -> tuple[bool, str]:
    metrics = candidate.get("metrics", {})
    if int(metrics.get("rows", 0)) < cfg.ml_min_training_rows:
        return False, "insufficient_training_rows"
    if float(metrics.get("accuracy", 0.0)) < cfg.ml_min_accuracy:
        return False, "accuracy_below_threshold"
    if float(metrics.get("precision", 0.0)) < cfg.ml_min_precision:
        return False, "precision_below_threshold"
    if float(metrics.get("recall", 0.0)) < cfg.ml_min_recall:
        return False, "recall_below_threshold"
    if cfg.ml_promote_only_if_better and active:
        active_metrics = active.get("metrics", {})
        candidate_score = float(metrics.get("precision", 0.0)) * 0.5 + float(metrics.get("recall", 0.0)) * 0.25 + float(metrics.get("accuracy", 0.0)) * 0.25
        active_score = float(active_metrics.get("precision", 0.0)) * 0.5 + float(active_metrics.get("recall", 0.0)) * 0.25 + float(active_metrics.get("accuracy", 0.0)) * 0.25
        if candidate_score < active_score:
            return False, "candidate_not_better_than_active"
    return True, "promoted"


def persist_model_candidate(engine: Engine | None, state: RedisState, model: dict[str, Any], status: str, reason: str) -> None:
    trained_at = now_utc().replace(tzinfo=None)
    model = {**model, "trained_at": trained_at.isoformat()}
    if engine is None:
        if status == "ACTIVE":
            state.set_json("ml_model:mean_reversion_entry_confidence", model, ex=CFG.ml_retrain_seconds)
        return
    with engine.begin() as conn:
        if status == "ACTIVE":
            conn.execute(text("UPDATE model_registry SET status='ARCHIVED' WHERE model_name='mean_reversion_entry_confidence' AND status='ACTIVE'"))
        metrics = {**model["metrics"], "promotion_reason": reason}
        conn.execute(
            text("INSERT INTO model_registry(model_name, version, status, feature_list_json, feature_importance_json, model_json, metrics_json, trained_rows, trained_at) VALUES(:name, :version, :status, :features, :importance, :model, :metrics, :rows, :ts)"),
            {
                "name": model["model_name"],
                "version": model["version"],
                "status": status,
                "features": json.dumps(model["features"]),
                "importance": json.dumps(model.get("feature_importance", {})),
                "model": json.dumps(model),
                "metrics": json.dumps(metrics),
                "rows": int(model["metrics"]["rows"]),
                "ts": trained_at,
            },
        )
    if status == "ACTIVE":
        state.set_json("ml_model:mean_reversion_entry_confidence", model, ex=CFG.ml_retrain_seconds)
    journal_write(
        engine,
        state,
        key=f"journal:{model['version']}:{'MODEL_PROMOTED' if status == 'ACTIVE' else 'MODEL_REJECTED'}",
        code="MODEL_PROMOTED" if status == "ACTIVE" else "MODEL_REJECTED",
        actor="worker-ml",
        model_version=str(model.get("version", "")),
        confidence=float(model.get("metrics", {}).get("accuracy", 0.0) or 0.0),
        context={"status": status, "reason": reason, "metrics": model.get("metrics", {}), "features": model.get("features", [])},
        lesson={"promotion_reason": reason, "trained_rows": model.get("metrics", {}).get("rows", 0)},
    )


def evaluate_pending_ml_predictions(engine: Engine | None, state: RedisState, limit: int = 500) -> int:
    if engine is None:
        return 0
    horizon_seconds = interval_to_seconds(CFG.strategy_interval) * max(int(CFG.mean_reversion_hold_bars), 1)
    rows = db_rows(
        engine,
        f"""SELECT p.idempotency_key, p.symbol, p.side, p.confidence, p.threshold, p.feature_json, p.created_at,
                  s.price AS entry_price, s.expected_reversion_bps
           FROM ml_predictions p
           JOIN signals s ON s.idempotency_key = p.idempotency_key
           WHERE p.actual_outcome IS NULL
             AND p.side IN ('BUY', 'SELL')
             AND p.created_at <= DATE_SUB(UTC_TIMESTAMP(), INTERVAL {int(horizon_seconds)} SECOND)
           ORDER BY p.created_at ASC
           LIMIT {int(limit)}""",
    )
    evaluated = 0
    for row in rows:
        try:
            symbol = str(row.get("symbol") or "")
            side = str(row.get("side") or "")
            entry_price = float(row.get("entry_price") or 0.0)
            if not symbol or side not in {"BUY", "SELL"} or entry_price <= 0:
                continue
            latest = state.get_json(f"latest_price:{symbol}") or {}
            if not fresh_payload(latest, max(CFG.market_data_ws_stale_seconds, 180)) or str(latest.get("data_quality", "")).upper() != "LIVE":
                continue
            current_price = float(latest.get("price") or 0.0)
            if current_price <= 0:
                continue
            direction = 1 if side == "BUY" else -1
            gross_return = ((current_price - entry_price) / max(entry_price, 1e-9)) * direction
            cost_return = (2 * CFG.fee_bps + CFG.slippage_bps) / 10000
            net_return = gross_return - cost_return
            label = int(net_return > 0)
            features = safe_json_dict(row.get("feature_json"))
            ts = now_utc().replace(tzinfo=None)
            key = str(row.get("idempotency_key"))
            blocked_context_rows = db_rows(
                engine,
                """SELECT context_json FROM trading_journal
                   WHERE signal_key=:key AND journal_code='SIG_BLOCKED'
                   ORDER BY id DESC LIMIT 1""",
                {"key": key},
            )
            blocked_context = safe_json_dict(blocked_context_rows[0].get("context_json")) if blocked_context_rows else {}
            blockers = blocked_context.get("blockers", [])
            if not isinstance(blockers, list):
                blockers = []
            was_blocked = bool(blockers)
            hypothetical_size = min(CFG.training_auto_approve_max_position_usdt, CFG.max_position_usdt)
            gross_pnl = gross_return * hypothetical_size
            net_pnl = net_return * hypothetical_size
            with engine.begin() as conn:
                result = conn.execute(
                    text("""UPDATE ml_predictions
                            SET actual_outcome=:label, actual_return=:ret, evaluated_at=:ts
                            WHERE idempotency_key=:key AND actual_outcome IS NULL"""),
                    {"label": label, "ret": net_return, "ts": ts, "key": key},
                )
                conn.execute(
                    text("""INSERT IGNORE INTO trade_outcomes(idempotency_key, symbol, side, label, forward_return, max_favorable_bps, max_adverse_bps, horizon_bars, outcome_json, ts)
                            VALUES(:key, :symbol, :side, :label, :ret, :mfe, :mae, :horizon, :outcome, :ts)"""),
                    {
                        "key": key,
                        "symbol": symbol,
                        "side": side,
                        "label": label,
                        "ret": net_return,
                        "mfe": max(gross_return * 10000, 0.0),
                        "mae": min(gross_return * 10000, 0.0),
                        "horizon": CFG.mean_reversion_hold_bars,
                        "outcome": json.dumps(
                            {
                                "label_rule": "live_prediction_horizon_elapsed",
                                "entry_price": entry_price,
                                "exit_price": current_price,
                                "cost_bps": 2 * CFG.fee_bps + CFG.slippage_bps,
                                "gross_return_bps": gross_return * 10000,
                                "net_return_bps": net_return * 10000,
                                "hypothetical_size_usdt": hypothetical_size,
                                "hypothetical_net_pnl": net_pnl,
                                "was_blocked": was_blocked,
                                "blockers": blockers,
                            }
                        ),
                        "ts": ts,
                    },
                )
            if result.rowcount:
                evaluated += 1
                journal_write(
                    engine,
                    state,
                    key=f"journal:{key}:{'OUTCOME_WIN' if label else 'OUTCOME_LOSS'}",
                    code="OUTCOME_WIN" if label else "OUTCOME_LOSS",
                    actor="worker-ml-live-evaluator",
                    symbol=symbol,
                    side=side,
                    signal_key=key,
                    confidence=float(row.get("confidence") or 0.0),
                    expected_reversion_bps=float(row.get("expected_reversion_bps") or 0.0),
                    actual_outcome=label,
                    actual_return=net_return,
                    features=features,
                    context={
                        "horizon_seconds": horizon_seconds,
                        "entry_price": entry_price,
                        "exit_price": current_price,
                        "gross_return_bps": gross_return * 10000,
                        "net_return_bps": net_return * 10000,
                        "hypothetical_size_usdt": hypothetical_size,
                        "hypothetical_gross_pnl": gross_pnl,
                        "hypothetical_net_pnl": net_pnl,
                        "was_blocked": was_blocked,
                        "blockers": blockers,
                    },
                    lesson={"feedback_use": "live_prediction_evaluation", "return_bps": net_return * 10000, "hypothetical_net_pnl": net_pnl},
                )
                if label and was_blocked:
                    journal_write(
                        engine,
                        state,
                        key=f"journal:{key}:MISSED_PROFITABLE_CANDIDATE",
                        code="MISSED_PROFITABLE_CANDIDATE",
                        actor="worker-ml-live-evaluator",
                        symbol=symbol,
                        side=side,
                        signal_key=key,
                        confidence=float(row.get("confidence") or 0.0),
                        expected_reversion_bps=float(row.get("expected_reversion_bps") or 0.0),
                        actual_outcome=label,
                        actual_return=net_return,
                        features=features,
                        context={
                            "blockers": blockers,
                            "entry_price": entry_price,
                            "exit_price": current_price,
                            "net_return_bps": net_return * 10000,
                            "hypothetical_size_usdt": hypothetical_size,
                            "hypothetical_net_pnl": net_pnl,
                        },
                        lesson={
                            "action": "review_blocker_for_profitable_candidate",
                            "feedback_use": "gate_correction",
                            "blockers": blockers,
                            "hypothetical_net_pnl": net_pnl,
                        },
                    )
        except Exception:
            continue
    return evaluated


def walk_forward_summary(trades: pd.DataFrame, folds: int = 5) -> dict[str, Any]:
    if trades.empty or len(trades) < folds * 3:
        return {"status": "AMBER", "train_perf": 0.0, "test_perf": 0.0, "degradation_pct": 0.0, "parameter_stability": 0.0, "overfit_warning": "Insufficient trades"}
    ordered = trades.sort_values("time").reset_index(drop=True)
    split = max(1, int(len(ordered) * 0.6))
    train = metrics_from_trades(ordered.iloc[:split])
    test = metrics_from_trades(ordered.iloc[split:])
    train_perf = float(train["expectancy"])
    test_perf = float(test["expectancy"])
    degradation = 0.0 if abs(train_perf) < 1e-9 else (train_perf - test_perf) / abs(train_perf)
    stability = max(0.0, 1.0 - abs(degradation))
    status = "GREEN" if degradation < 0.35 and test_perf > -0.002 else "AMBER" if degradation < 0.75 else "RED"
    return {
        "status": status,
        "train_perf": train_perf,
        "test_perf": test_perf,
        "degradation_pct": float(degradation * 100),
        "parameter_stability": float(stability),
        "overfit_warning": "" if status == "GREEN" else "Performance degradation detected",
    }


def monte_carlo_summary(trades: pd.DataFrame, paths: int = 250) -> dict[str, Any]:
    if trades.empty:
        return {"status": "AMBER", "median_ending_equity": CFG.starting_equity, "p5_ending_equity": CFG.starting_equity, "p95_ending_equity": CFG.starting_equity, "prob_dd_breach": 0.0, "prob_ruin": 0.0, "expected_max_dd": 0.0, "worst_path": []}
    returns = trades["ret"].dropna().to_numpy()
    rng = np.random.default_rng(42)
    endings = []
    max_dds = []
    worst_path: list[float] = []
    worst_ending = float("inf")
    for _ in range(paths):
        sample = rng.choice(returns, size=len(returns), replace=True)
        equity = CFG.starting_equity * np.cumprod(1 + sample)
        dd = equity / np.maximum.accumulate(equity) - 1
        ending = float(equity[-1])
        endings.append(ending)
        max_dds.append(float(dd.min()))
        if ending < worst_ending:
            worst_ending = ending
            worst_path = [float(v) for v in equity]
    prob_dd_breach = float(np.mean(np.array(max_dds) <= -CFG.max_portfolio_dd_pct / 100))
    prob_ruin = float(np.mean(np.array(endings) <= CFG.starting_equity * 0.7))
    status = "GREEN" if prob_dd_breach < 0.2 and prob_ruin < 0.05 else "AMBER" if prob_ruin < 0.15 else "RED"
    return {
        "status": status,
        "median_ending_equity": float(np.median(endings)),
        "p5_ending_equity": float(np.percentile(endings, 5)),
        "p95_ending_equity": float(np.percentile(endings, 95)),
        "prob_dd_breach": prob_dd_breach,
        "prob_ruin": prob_ruin,
        "expected_max_dd": float(np.mean(max_dds)),
        "worst_path": worst_path,
    }


def validation_snapshot(symbol: str, allow_live_fetch: bool = True) -> dict[str, Any]:
    state = RedisState(CFG) if allow_live_fetch else RedisState(CFG)
    frame = latest_candle_frame(state, symbol, CFG.strategy_interval, limit=CFG.validation_backtest_bars) if state.ok else None
    if frame is None and allow_live_fetch:
        frame = fetch_binance_klines(symbol, CFG.strategy_interval, limit=CFG.validation_backtest_bars)
    if frame is None:
        empty_metrics = metrics_from_trades(pd.DataFrame())
        return {
            "symbol": symbol,
            "backtest": {**empty_metrics, "performance_gate": {"profit_factor_ok": False, "expectancy_ok": False, "drawdown_ok": True, "rolling_ok": False, "enough_trades": False}},
            "rolling": empty_metrics,
            "walk_forward": walk_forward_summary(pd.DataFrame()),
            "monte_carlo": monte_carlo_summary(pd.DataFrame()),
            "validation_status": "AMBER",
            "trades": pd.DataFrame(),
            "data_quality": "UNAVAILABLE",
        }
    trades = run_backtest_for_frame(symbol, frame)
    backtest = metrics_from_trades(trades)
    rolling = metrics_from_trades(trades.tail(CFG.rolling_validation_trades)) if not trades.empty else metrics_from_trades(trades)
    walk = walk_forward_summary(trades)
    monte = monte_carlo_summary(trades)
    profit_factor_ok = backtest["profit_factor"] >= CFG.min_validation_profit_factor
    expectancy_ok = backtest["expectancy"] * 10000 >= CFG.min_validation_expectancy_bps
    drawdown_ok = abs(backtest["max_drawdown"]) * 100 <= CFG.max_validation_drawdown_pct
    rolling_ok = (
        rolling["profit_factor"] >= CFG.min_validation_profit_factor
        and rolling["expectancy"] * 10000 >= CFG.min_validation_expectancy_bps
        and abs(rolling["max_drawdown"]) * 100 <= CFG.max_validation_drawdown_pct
    )
    enough_trades = backtest["total_trades"] >= 10
    performance_ok = profit_factor_ok and expectancy_ok and drawdown_ok and rolling_ok and enough_trades
    validation_status = (
        "RED"
        if "RED" in {walk["status"], monte["status"]} or not drawdown_ok
        else "GREEN"
        if performance_ok and "AMBER" not in {walk["status"], monte["status"]}
        else "AMBER"
    )
    backtest["performance_gate"] = {
        "profit_factor_ok": profit_factor_ok,
        "expectancy_ok": expectancy_ok,
        "drawdown_ok": drawdown_ok,
        "rolling_ok": rolling_ok,
        "enough_trades": enough_trades,
    }
    return {"symbol": symbol, "backtest": backtest, "rolling": rolling, "walk_forward": walk, "monte_carlo": monte, "validation_status": validation_status, "trades": trades}


def compact_validation_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    backtest = snapshot["backtest"]
    rolling = snapshot["rolling"]
    walk = snapshot["walk_forward"]
    monte = snapshot["monte_carlo"]
    return json_safe({
        "symbol": snapshot["symbol"],
        "validation_status": snapshot["validation_status"],
        "backtest": {
            "total_trades": int(backtest.get("total_trades", 0) or 0),
            "win_rate": float(backtest.get("win_rate", 0.0) or 0.0),
            "profit_factor": float(backtest.get("profit_factor", 0.0) or 0.0),
            "expectancy": float(backtest.get("expectancy", 0.0) or 0.0),
            "avg_r": float(backtest.get("avg_r", 0.0) or 0.0),
            "max_drawdown": float(backtest.get("max_drawdown", 0.0) or 0.0),
            "sharpe_like": float(backtest.get("sharpe_like", 0.0) or 0.0),
            "largest_winner": float(backtest.get("largest_winner", 0.0) or 0.0),
            "largest_loser": float(backtest.get("largest_loser", 0.0) or 0.0),
            "consecutive_losses": int(backtest.get("consecutive_losses", 0) or 0),
            "performance_gate": backtest.get("performance_gate", {}),
            "equity_curve": backtest.get("equity_curve", [])[-60:] if CFG.validation_store_paths else [],
        },
        "rolling": {
            "total_trades": int(rolling.get("total_trades", 0) or 0),
            "win_rate": float(rolling.get("win_rate", 0.0) or 0.0),
            "profit_factor": float(rolling.get("profit_factor", 0.0) or 0.0),
            "expectancy": float(rolling.get("expectancy", 0.0) or 0.0),
            "max_drawdown": float(rolling.get("max_drawdown", 0.0) or 0.0),
        },
        "walk_forward": walk,
        "monte_carlo": {**monte, "worst_path": monte.get("worst_path", [])[-60:] if CFG.validation_store_paths else []},
        "updated_at": iso_now(),
    })


def persist_validation_snapshot(engine: Engine | None, snapshot: dict[str, Any]) -> None:
    if engine is None:
        return
    symbol = snapshot["symbol"]
    compact = compact_validation_payload(snapshot)
    backtest = {
        key: (0.0 if isinstance(value, float) and not np.isfinite(value) else value)
        for key, value in compact["backtest"].items()
        if key != "performance_gate"
    }
    rolling = compact["rolling"]
    walk = compact["walk_forward"]
    monte = compact["monte_carlo"]
    ts = now_utc().replace(tzinfo=None)
    status = compact["validation_status"]
    db_execute(
        engine,
        """INSERT INTO validation_state(symbol, status, total_trades, win_rate, profit_factor, expectancy, max_drawdown, rolling_profit_factor, rolling_expectancy, walk_status, monte_status, summary_json, updated_at)
           VALUES(:symbol, :status, :total_trades, :win_rate, :profit_factor, :expectancy, :max_drawdown, :rolling_profit_factor, :rolling_expectancy, :walk_status, :monte_status, :summary_json, :updated_at)
           ON DUPLICATE KEY UPDATE status=VALUES(status), total_trades=VALUES(total_trades), win_rate=VALUES(win_rate), profit_factor=VALUES(profit_factor), expectancy=VALUES(expectancy), max_drawdown=VALUES(max_drawdown), rolling_profit_factor=VALUES(rolling_profit_factor), rolling_expectancy=VALUES(rolling_expectancy), walk_status=VALUES(walk_status), monte_status=VALUES(monte_status), summary_json=VALUES(summary_json), updated_at=VALUES(updated_at)""",
        {
            "symbol": symbol,
            "status": status,
            "total_trades": backtest["total_trades"],
            "win_rate": backtest["win_rate"],
            "profit_factor": backtest["profit_factor"],
            "expectancy": backtest["expectancy"],
            "max_drawdown": backtest["max_drawdown"],
            "rolling_profit_factor": rolling["profit_factor"],
            "rolling_expectancy": rolling["expectancy"],
            "walk_status": walk.get("status", "AMBER"),
            "monte_status": monte.get("status", "AMBER"),
            "summary_json": json.dumps(json_safe(compact)),
            "updated_at": ts,
        },
    )
    db_execute(
        engine,
        """INSERT INTO backtest_runs(symbol, total_trades, win_rate, profit_factor, expectancy, avg_r, max_drawdown, sharpe_like, largest_winner, largest_loser, consecutive_losses, equity_curve_json, ts)
           VALUES(:symbol, :total_trades, :win_rate, :profit_factor, :expectancy, :avg_r, :max_drawdown, :sharpe_like, :largest_winner, :largest_loser, :consecutive_losses, :equity_curve_json, :ts)""",
        {**backtest, "symbol": symbol, "equity_curve_json": json.dumps(json_safe(backtest["equity_curve"] if CFG.validation_store_paths else [])), "ts": ts},
    )
    db_execute(
        engine,
        "INSERT INTO walk_forward_runs(symbol, train_perf, test_perf, degradation_pct, parameter_stability, status, overfit_warning, ts) VALUES(:symbol, :train_perf, :test_perf, :degradation_pct, :parameter_stability, :status, :overfit_warning, :ts)",
        {**walk, "symbol": symbol, "ts": ts},
    )
    db_execute(
        engine,
        "INSERT INTO monte_carlo_runs(symbol, median_ending_equity, p5_ending_equity, p95_ending_equity, prob_dd_breach, prob_ruin, expected_max_dd, worst_path_json, ts) VALUES(:symbol, :median_ending_equity, :p5_ending_equity, :p95_ending_equity, :prob_dd_breach, :prob_ruin, :expected_max_dd, :worst_path_json, :ts)",
        {**monte, "symbol": symbol, "worst_path_json": json.dumps(json_safe(monte["worst_path"] if CFG.validation_store_paths else [])), "ts": ts},
    )
    retention = max(int(CFG.validation_history_days), 1)
    for table in ["backtest_runs", "walk_forward_runs", "monte_carlo_runs"]:
        db_execute(engine, f"DELETE FROM {table} WHERE ts < DATE_SUB(UTC_TIMESTAMP(), INTERVAL {retention} DAY)", {})


def latest_validation_snapshot(state: RedisState, engine: Engine | None, symbol: str, fallback: bool = True) -> dict[str, Any]:
    cached = state.get_json(f"latest_validation:{symbol}") if state.ok else None
    if cached:
        return cached
    rows = db_rows(engine, "SELECT summary_json FROM validation_state WHERE symbol=:symbol", {"symbol": symbol})
    if rows:
        parsed = safe_json_dict(rows[0].get("summary_json"))
        if parsed:
            return parsed
    return validation_snapshot(symbol, allow_live_fetch=False) if fallback else {"symbol": symbol, "validation_status": "AMBER", "backtest": metrics_from_trades(pd.DataFrame()), "rolling": metrics_from_trades(pd.DataFrame()), "walk_forward": {}, "monte_carlo": {}}


def run_validation_cycle(state: RedisState, engine: Engine | None, use_locks: bool = True) -> dict[str, str]:
    cycle_started = now_utc()
    statuses: dict[str, str] = {}
    heartbeat(engine, state, "worker-validation", detail={"phase": "cycle_start", "interval_seconds": CFG.validation_worker_seconds})
    for symbol in CFG.symbols:
        if use_locks and not state.lock(f"lock:validation_worker:{symbol}", ttl=max(CFG.validation_worker_seconds - 5, 60)):
            statuses[symbol] = "LOCKED"
            heartbeat(engine, state, "worker-validation", detail={"phase": "lock_skipped", "symbol": symbol, "statuses": statuses, "interval_seconds": CFG.validation_worker_seconds})
            continue
        try:
            heartbeat(engine, state, "worker-validation", detail={"phase": "validating", "symbol": symbol, "statuses": statuses, "interval_seconds": CFG.validation_worker_seconds})
            snapshot = validation_snapshot(symbol, allow_live_fetch=True)
            compact = compact_validation_payload(snapshot)
            persist_validation_snapshot(engine, snapshot)
            state.set_json(f"latest_validation:{symbol}", compact, ex=max(CFG.validation_worker_seconds * 3, 1800))
            state.publish("validation_updates", compact)
            status = str(compact.get("validation_status", "AMBER"))
            statuses[symbol] = status
            code = "VALIDATION_PASS" if status == "GREEN" else "VALIDATION_FAIL" if status == "RED" else "VALIDATION_WARN"
            backtest = compact["backtest"]
            reason = (
                f"{status}: trades={backtest.get('total_trades', 0)}, "
                f"pf={format_profit_factor(backtest.get('profit_factor'))}, "
                f"expectancy={float(backtest.get('expectancy', 0.0) or 0.0) * 10000:.1f}bps"
            )
            journal_write(
                engine,
                state,
                key=f"journal:validation:{symbol}:{int(cycle_started.timestamp() // max(CFG.validation_worker_seconds, 1))}",
                code=code,
                actor="worker-validation",
                symbol=symbol,
                context={
                    "status": status,
                    "total_trades": backtest.get("total_trades", 0),
                    "profit_factor": backtest.get("profit_factor", 0.0),
                    "expectancy_bps": float(backtest.get("expectancy", 0.0) or 0.0) * 10000,
                    "max_drawdown_pct": abs(float(backtest.get("max_drawdown", 0.0) or 0.0)) * 100,
                    "walk_status": compact["walk_forward"].get("status"),
                    "monte_status": compact["monte_carlo"].get("status"),
                },
                lesson={"next_owner": "worker-signal", "use_for": "pre_entry_validation_gate"},
            )
            record_handoff(
                engine,
                state,
                key=f"handoff:validation:{symbol}:{int(cycle_started.timestamp() // max(CFG.validation_worker_seconds, 1))}",
                stage="validation",
                symbol=symbol,
                status=status,
                input_ref=f"klines:{symbol}:{CFG.strategy_interval}:{CFG.validation_backtest_bars}",
                output_ref=f"latest_validation:{symbol}",
                next_owner="worker-signal",
                reason=reason,
                metadata={"store_paths": CFG.validation_store_paths, "history_days": CFG.validation_history_days},
            )
            heartbeat(engine, state, "worker-validation", detail={"phase": "symbol_complete", "symbol": symbol, "statuses": statuses, "interval_seconds": CFG.validation_worker_seconds})
        except Exception as exc:
            statuses[symbol] = "ERROR"
            record_handoff(
                engine,
                state,
                key=f"handoff:validation-error:{symbol}:{int(time.time() // 60)}",
                stage="validation",
                symbol=symbol,
                status="ERROR",
                input_ref=f"klines:{symbol}:{CFG.strategy_interval}",
                output_ref="none",
                next_owner="worker-marketdata",
                reason=f"Validation failed: {exc}",
                metadata={},
            )
            heartbeat(engine, state, "worker-validation", status="DEGRADED", detail={"phase": "symbol_error", "symbol": symbol, "error": str(exc)[:300], "statuses": statuses, "interval_seconds": CFG.validation_worker_seconds})
    heartbeat(engine, state, "worker-validation", detail={"statuses": statuses, "interval_seconds": CFG.validation_worker_seconds})
    return statuses


def run_validation_once() -> int:
    state, engine = RedisState(CFG), db_engine(CFG)
    init_schema(engine)
    statuses = run_validation_cycle(state, engine, use_locks=False)
    print(json.dumps({"generated_at": iso_now(), "statuses": statuses}, indent=2, default=str))
    return 0 if statuses and all(status != "ERROR" for status in statuses.values()) else 1


def run_validation() -> None:
    state, engine = RedisState(CFG), db_engine(CFG)
    init_schema(engine)
    while True:
        cycle_started = now_utc()
        run_validation_cycle(state, engine, use_locks=True)
        elapsed = (now_utc() - cycle_started).total_seconds()
        sleep_until = time.monotonic() + max(30, CFG.validation_worker_seconds - elapsed)
        while True:
            remaining = max(0, int(sleep_until - time.monotonic()))
            if remaining <= 0:
                break
            heartbeat(
                engine,
                state,
                "worker-validation",
                detail={"phase": "sleeping", "next_cycle_in_seconds": remaining, "interval_seconds": CFG.validation_worker_seconds},
            )
            time.sleep(min(30, remaining))


def run_marketdata_rest_poll(state: RedisState, engine: Engine) -> None:
    for symbol in CFG.symbols:
        price, quality = fetch_binance_price(symbol)
        tick = {"symbol": symbol, "price": price, "source": CFG.binance_rest_base_url, "data_quality": quality, "ts": iso_now()}
        frame = fetch_binance_klines(symbol, "1m", limit=CFG.market_data_history_limit)
        if frame is not None and not frame.empty:
            store_candle_frames(state, symbol, frame, source=f"{CFG.binance_rest_base_url}:REST_BACKFILL", closed=True)
            price = float(frame["close"].iloc[-1])
            tick = {"symbol": symbol, "price": price, "source": f"{CFG.binance_rest_base_url}:REST_KLINE", "data_quality": "LIVE", "ts": iso_now()}
        state.set_json(f"latest_price:{symbol}", tick, ex=120)
        state.set_json(f"market_feed_status:{symbol}", {**tick, "mode": "REST_POLL"}, ex=180)
        if quality == "LIVE" or price > 0:
            state.publish("market_ticks", tick)
            db_execute(engine, "INSERT INTO market_ticks(symbol, price, source, data_quality, ts) VALUES(:symbol, :price, :source, :data_quality, :ts)", {**tick, "ts": now_utc().replace(tzinfo=None)})
        ob = fetch_orderbook(symbol)
        cross_state = cross_exchange_state(symbol, price) if price > 0 else {"symbol": symbol, "binance_price": price, "reference_price": 0.0, "reference_venues": {}, "cross_exchange_spread_bps": 0.0, "stale": True, "data_quality": "UNAVAILABLE", "ts": iso_now()}
        state.set_json(f"latest_cross_exchange:{symbol}", cross_state, ex=120)
        state.set_json(f"latest_external_prices:{symbol}", cross_state, ex=120)
        ob["cross_exchange_spread_bps"] = cross_state["cross_exchange_spread_bps"]
        ob_payload = {"symbol": symbol, **ob, "ts": iso_now()}
        state.set_json(f"latest_orderbook:{symbol}", ob_payload, ex=120)
        state.set_json(f"latest_obi:{symbol}", ob_payload, ex=120)
        state.publish("orderbook_updates", ob_payload)
        db_execute(engine, "INSERT INTO orderbook_snapshots(symbol, bid_volume, ask_volume, obi, spread_bps, liquidity_score, data_quality, ts) VALUES(:symbol, :bid_volume, :ask_volume, :obi, :spread_bps, :liquidity_score, :data_quality, :ts)", {**ob_payload, "ts": now_utc().replace(tzinfo=None)})
        funding = fetch_funding_state(symbol)
        state.set_json(f"latest_funding:{symbol}", funding, ex=300)
        db_execute(
            engine,
            "INSERT INTO funding_rates(symbol, funding_rate, percentile, data_quality, ts) VALUES(:symbol, :funding_rate, :percentile, :data_quality, :ts)",
            {**funding, "ts": now_utc().replace(tzinfo=None)},
        )
        prior_oi = state.get_json(f"latest_open_interest:{symbol}")
        oi = fetch_open_interest_state(symbol, None if not prior_oi else float(prior_oi.get("open_interest", 0.0)))
        state.set_json(f"latest_open_interest:{symbol}", oi, ex=300)
        db_execute(
            engine,
            "INSERT INTO open_interest_snapshots(symbol, open_interest, oi_change, data_quality, ts) VALUES(:symbol, :open_interest, :oi_change, :data_quality, :ts)",
            {**oi, "ts": now_utc().replace(tzinfo=None)},
        )


def seed_marketdata_history(state: RedisState, engine: Engine) -> dict[str, pd.DataFrame]:
    buffers: dict[str, pd.DataFrame] = {}
    for symbol in CFG.symbols:
        frame = fetch_binance_klines(symbol, "1m", limit=CFG.market_data_history_limit)
        if frame is None or frame.empty:
            state.set_json(
                f"market_feed_status:{symbol}",
                {"symbol": symbol, "price": 0.0, "source": f"{CFG.binance_rest_base_url}:REST_SEED", "data_quality": "UNAVAILABLE", "mode": "SEED", "ts": iso_now()},
                ex=300,
            )
            continue
        source = f"{CFG.binance_rest_base_url}:REST_SEED"
        buffers[symbol] = frame.tail(CFG.market_data_history_limit).reset_index(drop=True)
        store_candle_frames(state, symbol, buffers[symbol], source=source, closed=True)
        price = float(buffers[symbol]["close"].iloc[-1])
        quality = "LIVE"
        tick = {"symbol": symbol, "price": price, "source": source, "data_quality": quality, "ts": iso_now()}
        state.set_json(f"latest_price:{symbol}", tick, ex=180)
        state.set_json(f"market_feed_status:{symbol}", {**tick, "mode": "SEED"}, ex=300)
        db_execute(engine, "INSERT INTO market_ticks(symbol, price, source, data_quality, ts) VALUES(:symbol, :price, :source, :data_quality, :ts)", {**tick, "ts": now_utc().replace(tzinfo=None)})
    return buffers


def update_one_minute_buffer(buffer: pd.DataFrame, candle: dict[str, Any]) -> pd.DataFrame:
    row = pd.DataFrame([candle])
    if buffer is None or buffer.empty:
        frame = row
    else:
        frame = pd.concat([buffer, row], ignore_index=True)
    frame["time"] = pd.to_datetime(frame["time"], utc=True).dt.tz_convert(None)
    frame = frame.sort_values("time").drop_duplicates(subset=["time"], keep="last")
    return frame.tail(CFG.market_data_history_limit).reset_index(drop=True)


def run_marketdata_websocket(state: RedisState, engine: Engine) -> None:
    buffers = seed_marketdata_history(state, engine)
    streams = "/".join(f"{symbol.lower()}@kline_1m" for symbol in CFG.symbols)
    url = f"{CFG.binance_ws_base_url}/stream?streams={streams}"
    last_aux_refresh = 0.0
    last_message_at = {"value": time.time()}

    def refresh_auxiliary() -> None:
        nonlocal last_aux_refresh
        if time.time() - last_aux_refresh < 30:
            return
        last_aux_refresh = time.time()
        for symbol in CFG.symbols:
            latest = state.get_json(f"latest_price:{symbol}") or {}
            live_price = fresh_payload(latest, max(CFG.market_data_ws_stale_seconds, 180)) and str(latest.get("data_quality", "")).upper() == "LIVE"
            ob = fetch_orderbook(symbol)
            cross_state = cross_exchange_state(symbol, float(latest.get("price", 0.0))) if live_price else {"symbol": symbol, "binance_price": float(latest.get("price", 0.0) or 0.0), "reference_price": 0.0, "reference_venues": {}, "cross_exchange_spread_bps": 0.0, "stale": True, "data_quality": "UNAVAILABLE", "ts": iso_now()}
            state.set_json(f"latest_cross_exchange:{symbol}", cross_state, ex=120)
            ob["cross_exchange_spread_bps"] = cross_state["cross_exchange_spread_bps"]
            ob_payload = {"symbol": symbol, **ob, "ts": iso_now()}
            state.set_json(f"latest_orderbook:{symbol}", ob_payload, ex=120)
            state.set_json(f"latest_obi:{symbol}", ob_payload, ex=120)
            state.publish("orderbook_updates", ob_payload)
            db_execute(engine, "INSERT INTO orderbook_snapshots(symbol, bid_volume, ask_volume, obi, spread_bps, liquidity_score, data_quality, ts) VALUES(:symbol, :bid_volume, :ask_volume, :obi, :spread_bps, :liquidity_score, :data_quality, :ts)", {**ob_payload, "ts": now_utc().replace(tzinfo=None)})
            funding = fetch_funding_state(symbol)
            state.set_json(f"latest_funding:{symbol}", funding, ex=300)
            prior_oi = state.get_json(f"latest_open_interest:{symbol}")
            oi = fetch_open_interest_state(symbol, None if not prior_oi else float(prior_oi.get("open_interest", 0.0)))
            state.set_json(f"latest_open_interest:{symbol}", oi, ex=300)

    def on_message(_: Any, message: str) -> None:
        last_message_at["value"] = time.time()
        payload = json.loads(message)
        data = payload.get("data", payload)
        kline = data.get("k", {})
        symbol = str(kline.get("s") or data.get("s") or "").upper()
        if not symbol:
            return
        candle = {
            "time": pd.to_datetime(int(kline["t"]), unit="ms", utc=True).tz_convert(None),
            "open": float(kline["o"]),
            "high": float(kline["h"]),
            "low": float(kline["l"]),
            "close": float(kline["c"]),
            "volume": float(kline["v"]),
            "quote_volume": float(kline.get("q", 0.0) or 0.0),
            "taker_buy_quote": float(kline.get("Q", 0.0) or 0.0),
        }
        buffers[symbol] = update_one_minute_buffer(buffers.get(symbol, pd.DataFrame()), candle)
        store_candle_frames(state, symbol, buffers[symbol], source=f"{CFG.binance_ws_base_url}:WS_KLINE_1M", closed=bool(kline.get("x")))
        tick = {"symbol": symbol, "price": candle["close"], "source": f"{CFG.binance_ws_base_url}:WS_KLINE_1M", "data_quality": "LIVE", "ts": iso_now(), "closed": bool(kline.get("x"))}
        state.set_json(f"latest_price:{symbol}", tick, ex=120)
        state.set_json(f"market_feed_status:{symbol}", {**tick, "mode": "TESTNET_WS"}, ex=180)
        state.publish("market_ticks", tick)
        db_execute(engine, "INSERT INTO market_ticks(symbol, price, source, data_quality, ts) VALUES(:symbol, :price, :source, :data_quality, :ts)", {**tick, "ts": now_utc().replace(tzinfo=None)})
        refresh_auxiliary()
        heartbeat(engine, state, "worker-marketdata", detail={"mode": "TESTNET_WS", "symbols": CFG.symbols, "url": url})

    def on_error(_: Any, error: Any) -> None:
        heartbeat(engine, state, "worker-marketdata", status="DEGRADED", detail={"mode": "TESTNET_WS", "error": str(error)[:240]})

    def on_close(_: Any, status_code: Any, message: Any) -> None:
        heartbeat(engine, state, "worker-marketdata", status="DEGRADED", detail={"mode": "TESTNET_WS", "close_code": status_code, "message": str(message)[:240]})

    def on_open(_: Any) -> None:
        heartbeat(engine, state, "worker-marketdata", detail={"mode": "TESTNET_WS", "symbols": CFG.symbols, "url": url})

    while True:
        app = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
        sslopt = {} if CFG.binance_verify_tls else {"cert_reqs": ssl.CERT_NONE}
        stop_watchdog = threading.Event()

        def watchdog() -> None:
            while not stop_watchdog.wait(5):
                stale_for = time.time() - last_message_at["value"]
                if stale_for > CFG.market_data_ws_stale_seconds:
                    heartbeat(engine, state, "worker-marketdata", status="DEGRADED", detail={"mode": "TESTNET_WS", "reason": "websocket_stale", "stale_seconds": round(stale_for, 1), "reconnect_seconds": CFG.market_data_ws_reconnect_seconds})
                    app.close()
                    return

        thread = threading.Thread(target=watchdog, name="marketdata-ws-watchdog", daemon=True)
        thread.start()
        with market_proxy_environment():
            app.run_forever(ping_interval=20, ping_timeout=10, reconnect=0, sslopt=sslopt)
        stop_watchdog.set()
        run_marketdata_rest_poll(state, engine)
        heartbeat(engine, state, "worker-marketdata", status="DEGRADED", detail={"mode": "REST_AFTER_WS_DISCONNECT", "retry_seconds": CFG.market_data_ws_reconnect_seconds})
        last_message_at["value"] = time.time()
        time.sleep(CFG.market_data_ws_reconnect_seconds)


def run_marketdata() -> None:
    state, engine = RedisState(CFG), db_engine(CFG)
    init_schema(engine)
    if CFG.market_data_source in {"TESTNET_WS", "WS", "WEBSOCKET"}:
        run_marketdata_websocket(state, engine)
        return
    while True:
        run_marketdata_rest_poll(state, engine)
        heartbeat(engine, state, "worker-marketdata", detail={"mode": "REST_POLL", "symbols": CFG.symbols})
        time.sleep(5)


def market_feed_check() -> int:
    results: dict[str, Any] = {
        "generated_at": iso_now(),
        "market_data_source": CFG.market_data_source,
        "rest_base": CFG.binance_rest_base_url,
        "ws_base": CFG.binance_ws_base_url,
        "trust_env_proxy": CFG.market_data_trust_env_proxy,
        "verify_tls": CFG.binance_verify_tls,
        "symbols": {},
    }
    ok = True
    for symbol in CFG.symbols:
        item: dict[str, Any] = {}
        started = time.perf_counter()
        try:
            price = binance_get("/api/v3/ticker/price", {"symbol": symbol}, timeout=5)
            item["rest_price"] = float(price["price"])
            item["rest_ms"] = round((time.perf_counter() - started) * 1000, 1)
            item["rest_ok"] = True
        except Exception as exc:
            item["rest_ok"] = False
            item["rest_error"] = str(exc)[:240]
            if CFG.market_data_source not in {"TESTNET_WS", "WS", "WEBSOCKET"}:
                ok = False
        stream_url = f"{CFG.binance_ws_base_url}/ws/{symbol.lower()}@kline_1m"
        started = time.perf_counter()
        try:
            sslopt = {} if CFG.binance_verify_tls else {"cert_reqs": ssl.CERT_NONE}
            with market_proxy_environment():
                ws = websocket.create_connection(stream_url, timeout=8, sslopt=sslopt)
                message = json.loads(ws.recv())
                ws.close()
            kline = message.get("k", {})
            item["ws_ok"] = bool(kline)
            item["ws_price"] = float(kline.get("c", 0.0) or 0.0)
            item["ws_closed_candle"] = bool(kline.get("x"))
            item["ws_ms"] = round((time.perf_counter() - started) * 1000, 1)
            item["ws_url"] = stream_url
            if not item["ws_ok"]:
                ok = False
        except Exception as exc:
            item["ws_ok"] = False
            item["ws_error"] = str(exc)[:240]
            item["ws_url"] = stream_url
            if CFG.market_data_source in {"TESTNET_WS", "WS", "WEBSOCKET"}:
                ok = False
        results["symbols"][symbol] = item
    print(json.dumps(results, indent=2, default=str))
    return 0 if ok else 1


def run_signal() -> None:
    state, engine = RedisState(CFG), db_engine(CFG)
    init_schema(engine)
    while True:
        model = load_ml_model(engine, state) if CFG.ml_enabled else None
        drift_training = load_labeled_training_from_db(engine) if CFG.ml_drift_gate_enabled and model else pd.DataFrame()
        bucket = int(time.time() // 60)
        for symbol in CFG.symbols:
            price_payload = state.get_json(f"latest_price:{symbol}") or {}
            price_cache_ok = fresh_payload(price_payload, max(CFG.market_data_ws_stale_seconds, 180)) and str(price_payload.get("data_quality", "")).upper() == "LIVE"
            price_value = float(price_payload.get("price") or 0.0)
            ob = state.get_json(f"latest_orderbook:{symbol}") or fetch_orderbook(symbol)
            cross_state = state.get_json(f"latest_cross_exchange:{symbol}") or (cross_exchange_state(symbol, price_value) if price_cache_ok and price_value > 0 else {"cross_exchange_spread_bps": 0.0, "data_quality": "UNAVAILABLE"})
            ob["cross_exchange_spread_bps"] = cross_state["cross_exchange_spread_bps"]
            funding = state.get_json(f"latest_funding:{symbol}") or {"funding_rate": 0.0, "percentile": 0.5}
            oi = state.get_json(f"latest_open_interest:{symbol}") or {"oi_change": 0.0}
            ob["funding_pressure"] = float(np.clip(float(funding.get("funding_rate", 0.0)) * 6000, -1, 1))
            ob["open_interest_signal"] = float(np.clip(float(oi.get("oi_change", 0.0)) * 10, -1, 1))
            market_frame = latest_candle_frame(state, symbol, CFG.strategy_interval, limit=180)
            if market_frame is None:
                market_frame = fetch_binance_klines(symbol, CFG.strategy_interval, limit=180)
            htf_frame = latest_candle_frame(state, symbol, CFG.higher_timeframe_interval, limit=120)
            signal = alpha_signal(symbol, price_value, ob, CFG, market_frame=market_frame if price_cache_ok else None, htf_frame=htf_frame)
            signal["market_source"] = str(price_payload.get("source", signal.get("market_source", "")))
            signal["market_data_quality"] = str(price_payload.get("data_quality", signal.get("market_data_quality", "")))
            signal["funding_pressure"] = float(np.clip(float(funding.get("funding_rate", 0.0)) * 6000, -1, 1))
            signal["open_interest_signal"] = float(np.clip(float(oi.get("oi_change", 0.0)) * 10, -1, 1))
            prediction_side = str(signal.get("candidate_side") or signal.get("side") or "HOLD")
            key_side = prediction_side if prediction_side in {"BUY", "SELL"} else str(signal.get("side", "HOLD"))
            key = f"{symbol}:{CFG.interval}:{bucket}:{key_side}"
            if not state.lock(f"lock:signal:{key}", ttl=55):
                continue
            signal["idempotency_key"] = key
            features = signal_features(signal)
            ml_confidence, ml_version = predict_with_model(model, features) if CFG.ml_enabled else (1.0, "disabled")
            signal["ml_confidence"] = ml_confidence
            signal["ml_model_version"] = ml_version
            validation_state = latest_validation_snapshot(state, engine, symbol, fallback=False)
            validation_status = str(validation_state.get("validation_status", "AMBER"))
            signal["validation_status"] = validation_status
            if candidate_side := signal.get("candidate_side", "HOLD"):
                if candidate_side in {"BUY", "SELL"} and validation_status != "GREEN":
                    signal["deployable"] = False
                    signal.setdefault("deployment_blockers", []).append(f"validation_not_green:{validation_status}")
            drift_blockers = feature_drift_blockers(drift_training, model, features)
            if drift_blockers:
                signal["deployable"] = False
                signal.setdefault("deployment_blockers", []).extend(drift_blockers)
            if CFG.ml_confidence_gate_enabled and ml_confidence < CFG.min_ml_confidence:
                signal["deployable"] = False
                signal.setdefault("deployment_blockers", []).append("ml_confidence_below_threshold")
            state.set_json(f"latest_signal:{symbol}", signal, ex=180)
            state.publish("signal_updates", signal)
            db_execute(
                engine,
                "INSERT IGNORE INTO feature_snapshots(idempotency_key, symbol, side, strategy_interval, feature_json, source, ts) VALUES(:key, :symbol, :side, :interval, :features, 'worker-signal', :ts)",
                {"key": key, "symbol": symbol, "side": prediction_side, "interval": CFG.strategy_interval, "features": json.dumps(features), "ts": now_utc().replace(tzinfo=None)},
            )
            if prediction_side in {"BUY", "SELL"}:
                db_execute(
                    engine,
                    "INSERT IGNORE INTO ml_predictions(idempotency_key, symbol, side, model_version, confidence, threshold, feature_json, training_date, created_at) VALUES(:key, :symbol, :side, :version, :confidence, :threshold, :features, :training_date, :ts)",
                    {
                        "key": key,
                        "symbol": symbol,
                        "side": prediction_side,
                        "version": ml_version,
                        "confidence": ml_confidence,
                        "threshold": CFG.min_ml_confidence,
                        "features": json.dumps(features),
                        "training_date": utc_naive_timestamp(pd.Timestamp(model.get("trained_at"))) if model and model.get("trained_at") else None,
                        "ts": now_utc().replace(tzinfo=None),
                    },
                )
            db_execute(
                engine,
                """INSERT IGNORE INTO signals(idempotency_key, symbol, side, price, composite_score, z_score, rsi, volume_z, adx, expected_reversion_bps, ml_confidence, ml_model_version, obi, cross_exchange_spread_bps, funding_pressure, open_interest_signal, win_p_est, payoff_b, kelly_fraction, suggested_usdt, deployable, confidence, rationale, validation_status, risk_status, ts)
                   VALUES(:idempotency_key, :symbol, :side, :price, :composite_score, :z_score, :rsi, :volume_z, :adx, :expected_reversion_bps, :ml_confidence, :ml_model_version, :obi, :cross_exchange_spread_bps, :funding_pressure, :open_interest_signal, :win_p_est, :payoff_b, :kelly_fraction, :suggested_usdt, :deployable, :confidence, :rationale, :validation_status, :risk_status, :ts)""",
                {**signal, "ts": now_utc().replace(tzinfo=None)},
            )
            candidate_side = signal.get("candidate_side", "HOLD")
            signal_code = "SIG_BUY" if candidate_side == "BUY" else "SIG_SELL" if candidate_side == "SELL" else "SIG_HOLD"
            journal_write(
                engine,
                state,
                key=f"journal:{key}:{signal_code}",
                code=signal_code,
                actor="worker-signal",
                symbol=symbol,
                side=candidate_side,
                signal_key=key,
                model_version=ml_version,
                confidence=float(ml_confidence),
                expected_reversion_bps=float(signal.get("expected_reversion_bps", 0.0) or 0.0),
                features=features,
                context={
                    "price": signal.get("price"),
                    "deployable": bool(signal.get("deployable")),
                    "blockers": signal.get("deployment_blockers", []),
                    "rationale": signal.get("rationale", ""),
                    "suggested_usdt": signal.get("suggested_usdt", 0.0),
                },
                lesson={"use_for": "entry_confidence_feedback", "label_source": "trade_outcomes"},
            )
            if candidate_side in {"BUY", "SELL"} and signal.get("deployment_blockers"):
                journal_write(
                    engine,
                    state,
                    key=f"journal:{key}:SIG_BLOCKED",
                    code="SIG_BLOCKED",
                    actor="worker-signal",
                    symbol=symbol,
                    side=candidate_side,
                    signal_key=key,
                    model_version=ml_version,
                    confidence=float(ml_confidence),
                    expected_reversion_bps=float(signal.get("expected_reversion_bps", 0.0) or 0.0),
                    features=features,
                    context={"blockers": signal.get("deployment_blockers", []), "rationale": signal.get("rationale", "")},
                    lesson={"action": "do_not_train_as_execution_failure", "reason": "pre_trade_gate_block"},
                )
            if (
                CFG.system_stage == "training"
                and CFG.training_auto_approve_paper
                and CFG.auto_approve_order_mode in {"PAPER", "TESTNET"}
                and candidate_side in {"BUY", "SELL"}
                and (not CFG.deploy_symbol_whitelist or symbol in CFG.deploy_symbol_whitelist)
                and (not CFG.training_auto_requires_validation or signal.get("validation_status") == "GREEN")
                and not any(str(item).startswith("ml_feature_drift:") for item in signal.get("deployment_blockers", []))
                and ml_confidence >= CFG.training_auto_approve_min_ml_confidence
                and float(signal.get("spread_bps", 999.0)) <= 10
                and float(signal.get("model_slippage_bps", 999.0)) <= CFG.slippage_bps
            ):
                auto_mode = CFG.auto_approve_order_mode
                auto_key = f"training:auto:{auto_mode.lower()}:{symbol}:{bucket}:{candidate_side}"
                auto_payload = {
                    **signal,
                    "side": candidate_side,
                    "deployable": True,
                    "training_auto_approved": True,
                    "manual_approval": False,
                    "requested_at": iso_now(),
                    "requested_by": "worker-signal-training-auto",
                    "mode": auto_mode,
                    "suggested_usdt": min(float(signal.get("suggested_usdt", 0.0) or CFG.training_auto_approve_max_position_usdt), CFG.training_auto_approve_max_position_usdt),
                }
                db_execute(
                    engine,
                    """INSERT IGNORE INTO deployment_requests(idempotency_key, signal_key, symbol, side, requested_size_usdt, requested_price, mode, status, requested_by, request_json, created_at)
                       VALUES(:key, :signal_key, :symbol, :side, :size, :price, :mode, 'PENDING', 'worker-signal-training-auto', :payload, :ts)""",
                    {
                        "key": auto_key,
                        "signal_key": key,
                        "symbol": symbol,
                        "side": candidate_side,
                        "size": auto_payload["suggested_usdt"],
                        "price": signal["price"],
                        "mode": auto_mode,
                        "payload": json.dumps(auto_payload),
                        "ts": now_utc().replace(tzinfo=None),
                    },
                )
                journal_write(
                    engine,
                    state,
                    key=f"journal:{auto_key}:AUTO_APPROVED",
                    code="AUTO_APPROVED",
                    actor="worker-signal-training-auto",
                    symbol=symbol,
                    side=candidate_side,
                    signal_key=key,
                    model_version=ml_version,
                    confidence=float(ml_confidence),
                    expected_reversion_bps=float(signal.get("expected_reversion_bps", 0.0) or 0.0),
                    features=features,
                    context={"mode": auto_mode, "requested_size_usdt": auto_payload["suggested_usdt"], "price": signal["price"]},
                    lesson={"goal": "collect_outcome_feedback"},
                )
        heartbeat(engine, state, "worker-signal")
        time.sleep(8)


def run_risk() -> None:
    state, engine = RedisState(CFG), db_engine(CFG)
    init_schema(engine)
    while True:
        pnl = state.get_json("live_pnl") or {"daily_pnl": 0, "current_dd_pct": 0, "equity": CFG.starting_equity}
        live_expectancy = float(pnl.get("daily_pnl", 0.0)) / max(CFG.starting_equity, 1.0)
        live_drawdown = float(pnl.get("current_dd_pct", 0.0))
        drift_score = min(100.0, abs(live_expectancy) * 2500 + live_drawdown * 8)
        drift_status = "DRIFT_LOCKED" if drift_score >= 80 else "WARNING" if drift_score >= 55 else "OK"
        drift_state = {
            "status": drift_status,
            "drift_score": drift_score,
            "live_expectancy": live_expectancy,
            "modeled_slippage_bps": CFG.slippage_bps,
            "live_drawdown": live_drawdown,
            "expected_drawdown": CFG.max_portfolio_dd_pct * 0.6,
            "ts": iso_now(),
        }
        state.set_json("drift_state", drift_state, ex=90)
        locked = (
            pnl.get("daily_pnl", 0) <= -CFG.starting_equity * CFG.max_daily_loss_pct / 100
            or pnl.get("current_dd_pct", 0) >= CFG.max_portfolio_dd_pct
            or drift_status == "DRIFT_LOCKED"
        )
        risk_state = {"status": "RISK_LOCKED" if locked else "RISK_OK", "daily_pnl": pnl.get("daily_pnl", 0), "current_dd_pct": pnl.get("current_dd_pct", 0), "ts": iso_now()}
        state.set_json("risk_state", risk_state, ex=90)
        if locked:
            state.publish("risk_events", risk_state)
            db_execute(engine, "INSERT INTO risk_events(event_type, severity, message, state_json, ts) VALUES('RISK_LOCK', 'RED', 'Risk limits breached; blocking new deployments.', :state, :ts)", {"state": json.dumps(risk_state), "ts": now_utc().replace(tzinfo=None)})
            db_execute(
                engine,
                "INSERT INTO drift_snapshots(live_win_rate, backtest_win_rate, live_expectancy, backtest_expectancy, live_slippage_bps, modeled_slippage_bps, live_trade_frequency, expected_trade_frequency, live_drawdown, expected_drawdown, status, ts) VALUES(:live_win_rate, :backtest_win_rate, :live_expectancy, :backtest_expectancy, :live_slippage_bps, :modeled_slippage_bps, :live_trade_frequency, :expected_trade_frequency, :live_drawdown, :expected_drawdown, :status, :ts)",
                {
                    "live_win_rate": 0.0,
                    "backtest_win_rate": 0.578,
                    "live_expectancy": live_expectancy,
                    "backtest_expectancy": 0.0038,
                    "live_slippage_bps": CFG.slippage_bps,
                    "modeled_slippage_bps": CFG.slippage_bps,
                    "live_trade_frequency": 0.0,
                    "expected_trade_frequency": 1.0,
                    "live_drawdown": live_drawdown,
                    "expected_drawdown": drift_state["expected_drawdown"],
                    "status": drift_status,
                    "ts": now_utc().replace(tzinfo=None),
                },
            )
        heartbeat(engine, state, "worker-risk", detail=risk_state)
        time.sleep(10)


def run_pnl() -> None:
    state, engine = RedisState(CFG), db_engine(CFG)
    init_schema(engine)
    equity_peak = CFG.starting_equity
    while True:
        realized = 0.0
        unrealized = 0.0
        paper_realized = 0.0
        paper_unrealized = 0.0
        live_realized = 0.0
        live_unrealized = 0.0
        open_count = 0
        if engine is not None:
            try:
                with engine.begin() as conn:
                    positions = conn.execute(
                        text("SELECT id, symbol, side, COALESCE(venue, 'PAPER') AS venue, entry_price, size_usdt, quantity, stop_price, target_price, current_price, realized_pnl FROM positions WHERE status='OPEN'")
                    ).fetchall()
                    realized_rows = conn.execute(
                        text("""SELECT COALESCE(venue, 'PAPER') AS venue, SUM(COALESCE(realized_pnl, 0)) AS realized
                                FROM positions WHERE status='CLOSED' GROUP BY COALESCE(venue, 'PAPER')""")
                    ).fetchall()
                    paper_realized = sum(float(row.realized or 0.0) for row in realized_rows if str(row.venue).upper() != "LIVE")
                    live_realized = sum(float(row.realized or 0.0) for row in realized_rows if str(row.venue).upper() == "LIVE")
                    realized = paper_realized + live_realized
                    risk_state = state.get_json("risk_state") or {"status": "RISK_OK"}
                    paper_unrealized = 0.0
                    live_unrealized = 0.0
                    for position in positions:
                        open_count += 1
                        venue = str(position.venue or "PAPER").upper()
                        latest = state.get_json(f"latest_price:{position.symbol}") or {}
                        live_price = fresh_payload(latest, max(CFG.market_data_ws_stale_seconds, 180)) and str(latest.get("data_quality", "")).upper() == "LIVE"
                        current_price = float(latest.get("price") or position.current_price or position.entry_price)
                        if not live_price:
                            state.set_json(
                                f"live_position:{position.symbol}",
                                {"symbol": position.symbol, "side": position.side, "venue": venue, "status": "OPEN", "entry_price": position.entry_price, "current_price": current_price, "price_data_quality": "UNAVAILABLE", "unrealized_pnl": float(getattr(position, "unrealized_pnl", 0.0) or 0.0), "stop_price": position.stop_price, "target_price": position.target_price, "ts": iso_now()},
                                ex=180,
                            )
                            continue
                        direction = 1 if position.side == "BUY" else -1
                        position_unrealized = (current_price - float(position.entry_price)) * direction * float(position.quantity)
                        unrealized += position_unrealized
                        if venue == "LIVE":
                            live_unrealized += position_unrealized
                        else:
                            paper_unrealized += position_unrealized
                        stop_hit = position.side == "BUY" and current_price <= float(position.stop_price) or position.side == "SELL" and current_price >= float(position.stop_price)
                        target_hit = position.side == "BUY" and current_price >= float(position.target_price) or position.side == "SELL" and current_price <= float(position.target_price)
                        risk_exit = risk_state.get("status") == "RISK_LOCKED"
                        exit_reason = "STOP_LOSS_HIT" if stop_hit else "TAKE_PROFIT_HIT" if target_hit else "RISK_LOCK_EXIT" if risk_exit else None
                        if exit_reason:
                            realized += position_unrealized
                            conn.execute(
                                text("UPDATE positions SET current_price=:price, unrealized_pnl=0, realized_pnl=:realized, status='CLOSED', updated_at=:ts WHERE id=:id"),
                                {"price": current_price, "realized": position_unrealized, "ts": now_utc().replace(tzinfo=None), "id": position.id},
                            )
                            conn.execute(
                                text("INSERT INTO executions(order_id, venue, symbol, side, price, quantity, fee, ts) VALUES(NULL, 'PAPER_EXIT', :symbol, :side, :price, :quantity, :fee, :ts)"),
                                {"symbol": position.symbol, "side": "SELL" if position.side == "BUY" else "BUY", "price": current_price, "quantity": position.quantity, "fee": float(position.size_usdt) * CFG.fee_bps / 10000, "ts": now_utc().replace(tzinfo=None)},
                            )
                            conn.execute(
                                text("INSERT INTO audit_log(event_type, actor, symbol, message, metadata_json, ts) VALUES(:event, 'worker-pnl', :symbol, :message, :meta, :ts)"),
                                {"event": exit_reason, "symbol": position.symbol, "message": f"Paper position closed by {exit_reason}.", "meta": json.dumps({"position_id": position.id, "exit_price": current_price, "realized_pnl": position_unrealized}), "ts": now_utc().replace(tzinfo=None)},
                            )
                            state.set_json(f"live_position:{position.symbol}", {"symbol": position.symbol, "status": "CLOSED", "exit_reason": exit_reason, "exit_price": current_price, "realized_pnl": position_unrealized, "ts": iso_now()}, ex=3600)
                            state.publish_audit({"event_type": exit_reason, "symbol": position.symbol, "position_id": position.id, "ts": iso_now()})
                        else:
                            conn.execute(
                                text("UPDATE positions SET current_price=:price, unrealized_pnl=:unrealized, updated_at=:ts WHERE id=:id"),
                                {"price": current_price, "unrealized": position_unrealized, "ts": now_utc().replace(tzinfo=None), "id": position.id},
                            )
                            state.set_json(
                                f"live_position:{position.symbol}",
                                {"symbol": position.symbol, "side": position.side, "venue": venue, "status": "OPEN", "entry_price": position.entry_price, "current_price": current_price, "unrealized_pnl": position_unrealized, "stop_price": position.stop_price, "target_price": position.target_price, "ts": iso_now()},
                                ex=180,
                            )
            except Exception:
                previous = state.get_json("live_pnl") or {}
                unrealized = float(previous.get("unrealized_pnl", 0.0) or 0.0)
                realized = float(previous.get("realized_pnl", 0.0) or 0.0)
                paper_unrealized = float(previous.get("paper_unrealized_pnl", unrealized) or 0.0)
                paper_realized = float(previous.get("paper_realized_pnl", realized) or 0.0)
        else:
            previous = state.get_json("live_pnl") or {}
            unrealized = float(previous.get("unrealized_pnl", 0.0) or 0.0)
            realized = float(previous.get("realized_pnl", 0.0) or 0.0)
            paper_unrealized = float(previous.get("paper_unrealized_pnl", unrealized) or 0.0)
            paper_realized = float(previous.get("paper_realized_pnl", realized) or 0.0)
        daily = realized + unrealized * 0.25
        equity = CFG.starting_equity + realized + unrealized
        equity_peak = max(equity_peak, equity)
        dd = max(0.0, (equity_peak - equity) / equity_peak * 100)
        pnl = {
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "paper_realized_pnl": paper_realized,
            "paper_unrealized_pnl": paper_unrealized,
            "live_realized_pnl": live_realized,
            "live_unrealized_pnl": live_unrealized,
            "daily_pnl": daily,
            "equity": equity,
            "current_dd_pct": dd,
            "open_positions": open_count,
            "ts": iso_now(),
        }
        state.set_json("live_pnl", pnl, ex=90)
        state.publish("pnl_updates", pnl)
        db_execute(engine, "INSERT INTO pnl_snapshots(realized_pnl, unrealized_pnl, daily_pnl, equity, current_dd_pct, ts) VALUES(:realized_pnl, :unrealized_pnl, :daily_pnl, :equity, :current_dd_pct, :ts)", {**pnl, "ts": now_utc().replace(tzinfo=None)})
        heartbeat(engine, state, "worker-pnl", detail={"equity": equity, "dd": dd, "open_positions": open_count})
        time.sleep(7)


def order_request_is_allowed(request_payload: dict[str, Any], risk_state: dict[str, Any], drift_state: dict[str, Any], cfg: Config) -> tuple[bool, str]:
    training_auto = bool(request_payload.get("training_auto_approved")) and cfg.system_stage == "training"
    if risk_state.get("status", "RISK_OK") != "RISK_OK":
        return False, "Risk state is locked."
    if drift_state.get("status") == "DRIFT_LOCKED":
        return False, "Live-vs-backtest drift is locked."
    if request_payload.get("side") not in {"BUY", "SELL"}:
        return False, "Only BUY or SELL deployment requests are executable."
    if not bool(request_payload.get("deployable")) and not training_auto:
        return False, "Signal is not deployable."
    if bool(request_payload.get("research_only")) and not training_auto:
        return False, "Strategy is marked research-only."
    blockers = request_payload.get("deployment_blockers") or []
    if blockers and not training_auto:
        return False, f"Deployment blockers present: {', '.join(str(item) for item in blockers[:4])}."
    if training_auto and float(request_payload.get("ml_confidence", 0.0)) < cfg.training_auto_approve_min_ml_confidence:
        return False, "Training auto-approval ML confidence is below threshold."
    if CFG.ml_confidence_gate_enabled and float(request_payload.get("ml_confidence", 0.0)) < CFG.min_ml_confidence and not training_auto:
        return False, "ML confidence is below deployment threshold."
    if request_payload.get("validation_status") != "GREEN" and (not training_auto or cfg.training_auto_requires_validation):
        return False, "Validation state is not green."
    if float(request_payload.get("confidence", 0.0)) < 0.45:
        return False, "Signal confidence is below deployment threshold."
    if float(request_payload.get("suggested_usdt", 0.0)) <= 0:
        return False, "Suggested order size is zero."
    if float(request_payload.get("suggested_usdt", 0.0)) > cfg.max_position_usdt:
        return False, "Suggested order size exceeds max position cap."
    if training_auto and float(request_payload.get("suggested_usdt", 0.0)) > cfg.training_auto_approve_max_position_usdt:
        return False, "Training auto-approved paper size exceeds training cap."
    if float(request_payload.get("cross_exchange_spread_bps", 0.0)) > 50:
        return False, "Cross-exchange spread is outside sanity bounds."
    if float(request_payload.get("model_slippage_bps", 0.0)) > cfg.slippage_bps:
        return False, "Modeled order-book slippage exceeds risk limit."
    return True, "OK"


def process_deployment_request(engine: Engine, state: RedisState, request_row: Any) -> str:
    request_id = int(request_row.id)
    request_key = str(request_row.idempotency_key)
    request_payload = json.loads(request_row.request_json or "{}")
    if not state.lock(f"lock:deployment_request:{request_key}", ttl=60):
        return "LOCKED_BY_PEER"

    risk_state = state.get_json("risk_state") or {"status": "RISK_OK"}
    drift_state = state.get_json("drift_state") or {"status": "OK"}
    allowed, reason = order_request_is_allowed(request_payload, risk_state, drift_state, CFG)
    mode = str(request_row.mode or "PAPER").upper()
    if mode == "TESTNET" and (not CFG.enable_real_testnet_orders or not testnet_credentials_present()):
        allowed, reason = False, "Testnet orders require explicit enablement and credentials."
    if mode != "PAPER" and mode != "TESTNET":
        allowed, reason = False, f"Unsupported order mode {mode}."
    if allowed:
        try:
            with engine.begin() as conn:
                open_positions = int(conn.execute(text("SELECT COUNT(*) FROM positions WHERE symbol=:symbol AND status='OPEN'"), {"symbol": request_row.symbol}).scalar() or 0)
            if open_positions > 0:
                allowed, reason = False, "An open position already exists for this symbol."
        except SQLAlchemyError:
            allowed, reason = False, "Could not verify duplicate-position gate."

    ts = now_utc().replace(tzinfo=None)
    if not allowed:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE deployment_requests SET status='BLOCKED', block_reason=:reason, processed_at=:ts WHERE id=:id AND status='PENDING'"),
                {"reason": reason, "ts": ts, "id": request_id},
            )
            conn.execute(
                text("INSERT INTO risk_events(event_type, severity, message, state_json, ts) VALUES('ORDER_BLOCKED', 'AMBER', :message, :state, :ts)"),
                {"message": reason, "state": json.dumps({"request_id": request_id, "risk_state": risk_state, "drift_state": drift_state}), "ts": ts},
            )
            conn.execute(
                text("INSERT INTO audit_log(event_type, actor, symbol, message, metadata_json, ts) VALUES('ORDER_BLOCKED', 'worker-order', :symbol, :message, :meta, :ts)"),
                {"symbol": request_row.symbol, "message": reason, "meta": json.dumps(request_payload), "ts": ts},
            )
        journal_write(
            engine,
            state,
            key=f"journal:{request_key}:ORDER_BLOCKED",
            code="ORDER_BLOCKED",
            actor="worker-order",
            symbol=str(request_row.symbol),
            side=str(request_row.side),
            signal_key=str(request_row.signal_key or ""),
            model_version=str(request_payload.get("ml_model_version", "")),
            confidence=float(request_payload.get("ml_confidence", 0.0) or 0.0),
            expected_reversion_bps=float(request_payload.get("expected_reversion_bps", 0.0) or 0.0),
            features=signal_features(request_payload),
            context={"reason": reason, "request_id": request_id, "mode": mode},
            lesson={"action": "tighten_pre_order_gate" if "slippage" in reason.lower() or "spread" in reason.lower() else "review_gate_reason"},
        )
        state.publish("risk_events", {"request_id": request_id, "symbol": request_row.symbol, "status": "BLOCKED", "reason": reason, "ts": iso_now()})
        return "BLOCKED"

    price = float(request_row.requested_price or request_payload.get("price", 0.0))
    size_usdt = min(float(request_row.requested_size_usdt or request_payload.get("suggested_usdt", 0.0)), CFG.max_position_usdt)
    quantity = size_usdt / max(price, 1e-9)
    fee = size_usdt * CFG.fee_bps / 10000
    stop_multiplier = 0.985 if request_row.side == "BUY" else 1.015
    target_multiplier = 1.025 if request_row.side == "BUY" else 0.975
    order_key = f"{mode.lower()}:{request_key}"
    testnet_response: dict[str, Any] = {}
    if mode == "TESTNET":
        try:
            testnet_response = place_binance_spot_testnet_order(request_row.symbol, request_row.side, price, size_usdt)
            executed_qty = float(testnet_response.get("executedQty") or quantity)
            cummulative_quote = float(testnet_response.get("cummulativeQuoteQty") or size_usdt)
            if executed_qty > 0 and cummulative_quote > 0:
                quantity = executed_qty
                price = cummulative_quote / executed_qty
                size_usdt = cummulative_quote
                fee = size_usdt * CFG.fee_bps / 10000
        except Exception as exc:
            reason = str(exc)[:500]
            with engine.begin() as conn:
                conn.execute(
                    text("UPDATE deployment_requests SET status='BLOCKED', block_reason=:reason, processed_at=:ts WHERE id=:id AND status='PENDING'"),
                    {"reason": reason, "ts": ts, "id": request_id},
                )
                conn.execute(
                    text("INSERT INTO audit_log(event_type, actor, symbol, message, metadata_json, ts) VALUES('TESTNET_ORDER_FAILED', 'worker-order', :symbol, :message, :meta, :ts)"),
                    {"symbol": request_row.symbol, "message": reason, "meta": json.dumps(request_payload), "ts": ts},
                )
            journal_write(
                engine,
                state,
                key=f"journal:{request_key}:TESTNET_ORDER_FAILED",
                code="TESTNET_ORDER_FAILED",
                actor="worker-order",
                symbol=str(request_row.symbol),
                side=str(request_row.side),
                signal_key=str(request_row.signal_key or ""),
                model_version=str(request_payload.get("ml_model_version", "")),
                confidence=float(request_payload.get("ml_confidence", 0.0) or 0.0),
                expected_reversion_bps=float(request_payload.get("expected_reversion_bps", 0.0) or 0.0),
                features=signal_features(request_payload),
                context={"reason": reason, "request_id": request_id, "mode": mode},
                lesson={"action": "use_paper_until_testnet_execution_is_stable", "venue": "BINANCE_SPOT_TESTNET"},
            )
            state.publish("risk_events", {"request_id": request_id, "symbol": request_row.symbol, "status": "BLOCKED", "reason": reason, "ts": iso_now()})
            return "BLOCKED"

    with engine.begin() as conn:
        if mode == "PAPER":
            conn.execute(
                text("INSERT IGNORE INTO paper_orders(idempotency_key, signal_id, symbol, side, size_usdt, status, created_at) VALUES(:key, NULL, :symbol, :side, :size, 'CREATED', :ts)"),
                {"key": order_key, "symbol": request_row.symbol, "side": request_row.side, "size": size_usdt, "ts": ts},
            )
        else:
            conn.execute(
                text("INSERT IGNORE INTO testnet_orders(idempotency_key, signal_id, symbol, side, size_usdt, status, created_at) VALUES(:key, NULL, :symbol, :side, :size, 'CREATED', :ts)"),
                {"key": order_key, "symbol": request_row.symbol, "side": request_row.side, "size": size_usdt, "ts": ts},
            )
        result = conn.execute(text("SELECT id FROM paper_orders WHERE idempotency_key=:key UNION SELECT id FROM testnet_orders WHERE idempotency_key=:key LIMIT 1"), {"key": order_key})
        order_id = int(result.scalar() or 0)
        conn.execute(
            text("INSERT INTO executions(order_id, venue, symbol, side, price, quantity, fee, ts) VALUES(:order_id, :venue, :symbol, :side, :price, :quantity, :fee, :ts)"),
            {"order_id": order_id, "venue": mode, "symbol": request_row.symbol, "side": request_row.side, "price": price, "quantity": quantity, "fee": fee, "ts": ts},
        )
        conn.execute(
            text("""INSERT INTO positions(symbol, side, venue, entry_time, entry_price, size_usdt, quantity, stop_price, target_price, current_price, unrealized_pnl, realized_pnl, status, signal_id, rationale, updated_at)
                    VALUES(:symbol, :side, :venue, :ts, :entry_price, :size, :quantity, :stop_price, :target_price, :current_price, 0, 0, 'OPEN', NULL, :rationale, :ts)"""),
            {
                "symbol": request_row.symbol,
                "side": request_row.side,
                "venue": mode,
                "ts": ts,
                "entry_price": price,
                "size": size_usdt,
                "quantity": quantity,
                "stop_price": price * stop_multiplier,
                "target_price": price * target_multiplier,
                "current_price": price,
                "rationale": request_payload.get("rationale", ""),
            },
        )
        conn.execute(text("UPDATE deployment_requests SET status='EXECUTED', processed_at=:ts WHERE id=:id AND status='PENDING'"), {"ts": ts, "id": request_id})
        conn.execute(
            text("INSERT INTO audit_log(event_type, actor, symbol, message, metadata_json, ts) VALUES('ORDER_EXECUTED', 'worker-order', :symbol, :message, :meta, :ts)"),
            {"symbol": request_row.symbol, "message": f"{mode} order created by worker-order.", "meta": json.dumps({"request_id": request_id, "order_key": order_key, "size_usdt": size_usdt, "price": price, "testnet_response": testnet_response}), "ts": ts},
        )

    position_payload = {"symbol": request_row.symbol, "side": request_row.side, "size_usdt": size_usdt, "quantity": quantity, "entry_price": price, "mode": mode, "ts": iso_now()}
    state.set_json(f"live_position:{request_row.symbol}", position_payload, ex=3600)
    state.publish_audit({"event_type": "ORDER_EXECUTED", **position_payload})
    journal_write(
        engine,
        state,
        key=f"journal:{request_key}:ORDER_EXECUTED",
        code="ORDER_EXECUTED",
        actor="worker-order",
        symbol=str(request_row.symbol),
        side=str(request_row.side),
        signal_key=str(request_row.signal_key or ""),
        model_version=str(request_payload.get("ml_model_version", "")),
        confidence=float(request_payload.get("ml_confidence", 0.0) or 0.0),
        expected_reversion_bps=float(request_payload.get("expected_reversion_bps", 0.0) or 0.0),
        features=signal_features(request_payload),
        context={"request_id": request_id, "order_key": order_key, "mode": mode, "size_usdt": size_usdt, "entry_price": price},
        lesson={"watch": "actual_return_vs_expected_reversion"},
    )
    return "EXECUTED"


def run_order() -> None:
    state, engine = RedisState(CFG), db_engine(CFG)
    init_schema(engine)
    while True:
        if engine is None:
            heartbeat(engine, state, "worker-order", status="DEGRADED", detail={"reason": "MariaDB unavailable", "phase": "reconnecting"})
            time.sleep(10)
            engine = db_engine(CFG)
            init_schema(engine)
            continue
        processed = {"EXECUTED": 0, "BLOCKED": 0, "LOCKED_BY_PEER": 0, "ERROR": 0}
        try:
            with engine.begin() as conn:
                rows = conn.execute(
                    text("SELECT id, idempotency_key, signal_key, symbol, side, requested_size_usdt, requested_price, mode, request_json FROM deployment_requests WHERE status='PENDING' ORDER BY created_at ASC LIMIT 10")
                ).fetchall()
            for row in rows:
                try:
                    result = process_deployment_request(engine, state, row)
                    processed[result] = processed.get(result, 0) + 1
                except Exception as exc:
                    processed["ERROR"] += 1
                    db_execute(engine, "INSERT INTO audit_log(event_type, actor, symbol, message, metadata_json, ts) VALUES('ORDER_WORKER_ERROR', 'worker-order', :symbol, :message, :meta, :ts)", {"symbol": getattr(row, "symbol", ""), "message": str(exc)[:500], "meta": "{}", "ts": now_utc().replace(tzinfo=None)})
        except Exception as exc:
            processed["ERROR"] += 1
            if isinstance(exc, SQLAlchemyError):
                engine = None
        heartbeat(engine, state, "worker-order", detail=processed)
        time.sleep(3)


def run_ml() -> None:
    state, engine = RedisState(CFG), db_engine(CFG)
    init_schema(engine)
    while True:
        try:
            heartbeat(engine, state, "worker-ml", status="ONLINE", detail={"phase": "training"})
            evaluated_predictions = evaluate_pending_ml_predictions(engine, state)
            pieces = []
            for symbol in CFG.symbols:
                frame = latest_candle_frame(state, symbol, CFG.strategy_interval, limit=CFG.ml_training_bars)
                if frame is None:
                    frame = fetch_binance_klines(symbol, CFG.strategy_interval, limit=CFG.ml_training_bars)
                htf_frame = latest_candle_frame(state, symbol, CFG.higher_timeframe_interval, limit=max(120, CFG.ml_training_bars // 16))
                if htf_frame is None:
                    htf_frame = fetch_binance_klines(symbol, CFG.higher_timeframe_interval, limit=max(120, CFG.ml_training_bars // 16))
                if frame is None:
                    heartbeat(engine, state, "worker-ml", status="DEGRADED", detail={"phase": "training", "symbol": symbol, "reason": "live_training_frame_unavailable"})
                    continue
                candidates = training_candidates(symbol, frame, htf_frame)
                if candidates.empty:
                    continue
                pieces.append(candidates)
                heartbeat(engine, state, "worker-ml", status="ONLINE", detail={"phase": "training", "symbol": symbol, "candidate_rows": int(len(candidates))})
                for _, row in candidates.tail(250).iterrows():
                    features = {name: float(row[name]) for name in ML_FEATURES}
                    key = f"mltrain:{symbol}:{pd.Timestamp(row['time']).isoformat()}:{row['side']}"
                    ts = pd.Timestamp(row["time"]).to_pydatetime().replace(tzinfo=None)
                    db_execute(
                        engine,
                        "INSERT IGNORE INTO feature_snapshots(idempotency_key, symbol, side, strategy_interval, feature_json, source, ts) VALUES(:key, :symbol, :side, :interval, :features, 'worker-ml', :ts)",
                        {"key": key, "symbol": symbol, "side": row["side"], "interval": CFG.strategy_interval, "features": json.dumps(features), "ts": ts},
                    )
                    db_execute(
                        engine,
                        "INSERT IGNORE INTO trade_outcomes(idempotency_key, symbol, side, label, forward_return, max_favorable_bps, max_adverse_bps, horizon_bars, outcome_json, ts) VALUES(:key, :symbol, :side, :label, :ret, :mfe, :mae, :horizon, :outcome, :ts)",
                        {
                            "key": key,
                            "symbol": symbol,
                            "side": row["side"],
                            "label": int(row["label"]),
                            "ret": float(row["forward_return"]),
                            "mfe": float(row["max_favorable_bps"]),
                            "mae": float(row["max_adverse_bps"]),
                            "horizon": CFG.mean_reversion_hold_bars,
                            "outcome": json.dumps({"label_rule": "net_forward_return_gt_zero_after_costs"}),
                            "ts": ts,
                        },
                    )
                    db_execute(
                        engine,
                        """UPDATE ml_predictions
                           SET actual_outcome=:label, actual_return=:ret, evaluated_at=:evaluated_at
                           WHERE idempotency_key=:key
                             AND actual_outcome IS NULL""",
                        {"key": key, "label": int(row["label"]), "ret": float(row["forward_return"]), "evaluated_at": now_utc().replace(tzinfo=None)},
                    )
                    journal_write(
                        engine,
                        state,
                        key=f"journal:{key}:{'OUTCOME_WIN' if int(row['label']) else 'OUTCOME_LOSS'}",
                        code="OUTCOME_WIN" if int(row["label"]) else "OUTCOME_LOSS",
                        actor="worker-ml",
                        symbol=symbol,
                        side=str(row["side"]),
                        signal_key=key,
                        actual_outcome=int(row["label"]),
                        actual_return=float(row["forward_return"]),
                        expected_reversion_bps=float(row.get("expected_reversion_bps", 0.0) or 0.0),
                        features=features,
                        context={"horizon_bars": CFG.mean_reversion_hold_bars, "max_favorable_bps": float(row["max_favorable_bps"]), "max_adverse_bps": float(row["max_adverse_bps"])},
                        lesson={
                            "train_label": int(row["label"]),
                            "return_bps": float(row["forward_return"]) * 10000,
                            "feedback_use": "entry_confidence_training",
                        },
                    )
            historical_training = load_labeled_training_from_db(engine)
            combined = pieces + ([historical_training] if not historical_training.empty else [])
            training = dedupe_training_frame(pd.concat(combined, ignore_index=True)) if combined else pd.DataFrame()
            model = train_logistic_model(training)
            if model:
                active_model = latest_active_model(engine)
                promoted, reason = model_promotable(model, active_model, CFG)
                persist_model_candidate(engine, state, model, "ACTIVE" if promoted else "REJECTED", reason)
                heartbeat(engine, state, "worker-ml", status="ONLINE" if promoted else "DEGRADED", detail={"model_version": model["version"], "promotion": reason, "historical_rows": int(len(historical_training)), "evaluated_predictions": evaluated_predictions, **model["metrics"]})
            else:
                heartbeat(engine, state, "worker-ml", status="DEGRADED", detail={"reason": "insufficient_labeled_training_rows", "rows": int(len(training))})
        except Exception as exc:
            heartbeat(engine, state, "worker-ml", status="ERROR", detail={"error": str(exc)[:240]})
        slept = 0
        while slept < CFG.ml_retrain_seconds:
            nap = min(30, CFG.ml_retrain_seconds - slept)
            time.sleep(nap)
            slept += nap
            evaluated_predictions = evaluate_pending_ml_predictions(engine, state, limit=100)
            heartbeat(engine, state, "worker-ml", status="ONLINE", detail={"phase": "waiting", "next_train_seconds": max(CFG.ml_retrain_seconds - slept, 0), "evaluated_predictions": evaluated_predictions})


def color_badge(label: str, status: str, tip: str) -> str:
    colors = {"GREEN": "#118833", "AMBER": "#b7791f", "RED": "#c53030", "OK": "#118833", "RISK_OK": "#118833", "RISK_LOCKED": "#c53030"}
    return f"<span title='{tip}' style='display:inline-block;padding:5px 9px;border-radius:6px;background:{colors.get(status, '#555')};color:white;font-size:12px;font-weight:700'>{label}</span>"


def timeframe_price_change(state: RedisState, symbol: str, current_price: float, interval: str) -> dict[str, Any]:
    frame = latest_candle_frame(state, symbol, interval, limit=2) if state.ok else None
    if frame is None or frame.empty:
        return {"timeframe_change_pct": 0.0, "timeframe_change_label": f"{interval} --", "timeframe_change_status": "AMBER"}
    anchor_price = float(frame["open"].iloc[-1] or frame["close"].iloc[-1] or current_price)
    change_pct = ((float(current_price) - anchor_price) / max(anchor_price, 1e-9)) * 100
    if abs(change_pct) < 0.005:
        status = "AMBER"
    else:
        status = "GREEN" if change_pct > 0 else "RED"
    return {
        "timeframe_change_pct": float(change_pct),
        "timeframe_change_label": f"{interval} {change_pct:+.2f}%",
        "timeframe_change_status": status,
    }


def demo_rows(state: RedisState) -> list[dict[str, Any]]:
    rows = []
    for symbol in CFG.symbols:
        price_cache_ok = False
        if state.ok:
            price = state.get_json(f"latest_price:{symbol}") or {"price": 0.0, "data_quality": "UNAVAILABLE", "source": "CACHE_MISS", "ts": iso_now()}
            price_cache_ok = fresh_payload(price, max(CFG.market_data_ws_stale_seconds, 180)) and str(price.get("data_quality", "")).upper() == "LIVE"
            ob = state.get_json(f"latest_orderbook:{symbol}") or unavailable_orderbook("UNAVAILABLE")
            cross_state = state.get_json(f"latest_cross_exchange:{symbol}")
        else:
            price = {"price": 0.0, "data_quality": "UNAVAILABLE", "source": "NO_REDIS_CACHE", "ts": iso_now()}
            ob = unavailable_orderbook("UNAVAILABLE")
            cross_state = None
        if cross_state:
            ob["cross_exchange_spread_bps"] = float(cross_state.get("cross_exchange_spread_bps", 0.0) or 0.0)
        funding = state.get_json(f"latest_funding:{symbol}") if state.ok else None
        oi = state.get_json(f"latest_open_interest:{symbol}") if state.ok else None
        ob["funding_pressure"] = float(np.clip(float((funding or {}).get("funding_rate", 0.0)) * 6000, -1, 1))
        ob["open_interest_signal"] = float(np.clip(float((oi or {}).get("oi_change", 0.0)) * 10, -1, 1))
        sig = state.get_json(f"latest_signal:{symbol}") if state.ok else None
        signal_is_stale = not fresh_payload(sig, max(CFG.market_data_ws_stale_seconds, 180)) if sig else True
        if not price_cache_ok:
            sig = unavailable_signal(symbol, float(price.get("price", 0.0) or 0.0), "price_cache_unavailable")
            sig["market_source"] = str(price.get("source", "CACHE_MISS"))
        elif not sig or signal_is_stale:
            market_frame = latest_candle_frame(state, symbol, CFG.strategy_interval, limit=180) if state.ok else None
            sig = alpha_signal(symbol, float(price["price"]), ob, CFG, market_frame=market_frame, allow_live_fetch=False)
            sig["market_source"] = str(price.get("source", sig.get("market_source", "")))
            sig["market_data_quality"] = str(price.get("data_quality", sig.get("market_data_quality", "")))
        else:
            sig = dict(sig)
            sig["price"] = float(price["price"])
            sig["market_source"] = str(price.get("source", sig.get("market_source", "")))
            sig["market_data_quality"] = str(price.get("data_quality", sig.get("market_data_quality", "")))
            sig["ts"] = str(price.get("ts", sig.get("ts", iso_now())))
        sig["market_price_ts"] = str(price.get("ts", ""))
        sig["market_cache_age_seconds"] = round(age_seconds(price.get("ts")), 1)
        sig["market_cache_live"] = bool(price_cache_ok)
        sig.update(timeframe_price_change(state, symbol, float(sig.get("price", price.get("price", 0.0)) or 0.0), CFG.strategy_interval))
        rows.append(sig)
    return rows


def historical_chart(state: RedisState, symbol: str) -> go.Figure:
    frame = latest_candle_frame(state, symbol, CFG.strategy_interval, limit=120) if state.ok else None
    fig = go.Figure()
    if frame is None or frame.empty:
        fig.add_annotation(text="No live candle history available", x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False)
    else:
        idx = frame["time"]
        prices = frame["close"]
        fig.add_trace(go.Scatter(x=idx, y=prices, mode="lines", name="price", line=dict(color="#2b6cb0")))
    fig.update_layout(height=280, margin=dict(l=10, r=10, t=20, b=10), template="plotly_white", legend=dict(orientation="h"))
    return fig


def audit_rows(engine: Engine | None) -> pd.DataFrame:
    if engine is None:
        return pd.DataFrame([{"ts": iso_now(), "event_type": "LOCAL_PREVIEW", "actor": "system", "symbol": "", "message": "MariaDB unavailable; UI running in safe local preview mode."}])
    try:
        return pd.read_sql("SELECT ts, event_type, actor, symbol, message FROM audit_log ORDER BY id DESC LIMIT 20", engine)
    except Exception:
        return pd.DataFrame()


def journal_rows(engine: Engine | None, limit: int = 60) -> pd.DataFrame:
    if engine is None:
        return pd.DataFrame([{"created_at": iso_now(), "journal_code": "LOCAL_PREVIEW", "type": "SYSTEM", "severity": "INFO", "actor": "system", "symbol": "", "side": "", "confidence": 0.0, "expected_bps": 0.0, "actual_outcome": "", "actual_return_bps": "", "summary": "MariaDB unavailable; journal preview only."}])
    rows = db_rows(
        engine,
        f"""SELECT created_at, journal_code, journal_type, severity, actor, symbol, side, model_version,
                  confidence, expected_reversion_bps, actual_outcome, actual_return, context_json, lesson_json
           FROM trading_journal ORDER BY id DESC LIMIT {int(limit)}""",
    )
    parsed = []
    for row in rows:
        context = safe_json_dict(row.get("context_json"))
        lesson = safe_json_dict(row.get("lesson_json"))
        parsed.append(
            {
                "created_at": row.get("created_at"),
                "journal_code": row.get("journal_code"),
                "type": row.get("journal_type"),
                "severity": row.get("severity"),
                "actor": row.get("actor"),
                "symbol": row.get("symbol"),
                "side": row.get("side"),
                "confidence": float(row.get("confidence") or 0.0),
                "expected_bps": float(row.get("expected_reversion_bps") or 0.0),
                "actual_outcome": "" if row.get("actual_outcome") is None else int(row.get("actual_outcome")),
                "actual_return_bps": "" if row.get("actual_return") is None else f"{float(row.get('actual_return')) * 10000:.1f}",
                "summary": context.get("meaning") or context.get("reason") or lesson.get("feedback_use") or "",
            }
        )
    return pd.DataFrame(parsed)


def journal_feedback_summary(engine: Engine | None) -> pd.DataFrame:
    rows = db_rows(
        engine,
        """SELECT journal_code, journal_type, COUNT(*) events,
                  AVG(confidence) avg_confidence,
                  AVG(actual_outcome) win_rate,
                  AVG(actual_return) avg_return
           FROM trading_journal
           WHERE created_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 7 DAY)
           GROUP BY journal_code, journal_type
           ORDER BY events DESC""",
    )
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=["journal_code", "journal_type", "events", "avg_confidence", "win_rate", "avg_return_bps"])
    frame["avg_confidence"] = frame["avg_confidence"].fillna(0).astype(float).map(lambda value: f"{value:.2f}")
    frame["win_rate"] = frame["win_rate"].fillna(0).astype(float).map(lambda value: f"{value:.1%}")
    frame["avg_return_bps"] = frame["avg_return"].fillna(0).astype(float).map(lambda value: f"{value * 10000:.1f}")
    return frame.drop(columns=["avg_return"])


CONFIG_BLOCKERS = {"research_only", "symbol_not_whitelisted"}
MARKET_BLOCKERS = {
    "trend_regime_adx_high",
    "low_participation",
    "volatility_not_falling",
    "expected_move_below_cost_hurdle",
    "buy_rsi_not_oversold",
    "sell_rsi_not_overbought",
    "higher_timeframe_downtrend",
    "higher_timeframe_uptrend",
}
EXECUTION_BLOCKERS = {"spread_too_wide", "slippage_too_high", "buy_orderbook_not_confirmed", "sell_orderbook_not_confirmed", "buy_taker_flow_not_confirmed", "sell_taker_flow_not_confirmed"}
MODEL_BLOCKERS = {"ml_confidence_below_threshold", "confidence_too_low"}


def journal_blocker_class(blocker: str) -> str:
    if blocker in CONFIG_BLOCKERS:
        return "Config gate"
    if blocker in MODEL_BLOCKERS or blocker.startswith("ml_feature_drift:"):
        return "Model gate"
    if blocker in EXECUTION_BLOCKERS:
        return "Execution gate"
    if blocker in MARKET_BLOCKERS:
        return "Market filter"
    return "Other"


def journal_blocker_action(blocker_class: str, blocker: str) -> str:
    if blocker == "research_only":
        return "Keep for production; ignore when measuring live edge availability."
    if blocker == "symbol_not_whitelisted":
        return "Expand whitelist only in training/paper mode if more coverage is needed."
    if blocker == "expected_move_below_cost_hurdle":
        return "Lower the training cost multiple cautiously or wait for larger dislocations."
    if blocker == "trend_regime_adx_high":
        return "Use a separate trend-aware bucket before relaxing this for real deployment."
    if blocker == "ml_confidence_below_threshold":
        return "Collect more labeled paper outcomes before raising model authority."
    if blocker_class == "Execution gate":
        return "Do not relax until spread/slippage evidence is stable."
    return "Compare winners/losses for this feature bucket before changing thresholds."


def journal_blocker_summary_rows(engine: Engine | None, limit: int = 500) -> pd.DataFrame:
    rows = db_rows(engine, f"SELECT symbol, context_json FROM trading_journal WHERE journal_type IN ('SIGNAL','GATE') ORDER BY id DESC LIMIT {int(limit)}")
    summary: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        context = safe_json_dict(row.get("context_json"))
        blockers = context.get("blockers", [])
        if not isinstance(blockers, list):
            continue
        symbol = str(row.get("symbol") or "")
        for blocker in blockers:
            name = str(blocker)
            blocker_class = journal_blocker_class(name)
            key = (blocker_class, name)
            item = summary.setdefault(key, {"Class": blocker_class, "Blocker": name, "Events": 0, "Symbols": set(), "Suggested Action": journal_blocker_action(blocker_class, name)})
            item["Events"] += 1
            if symbol:
                item["Symbols"].add(symbol)
    output = []
    for item in summary.values():
        output.append({**item, "Symbols": ", ".join(sorted(item["Symbols"]))})
    return pd.DataFrame(output).sort_values(["Events", "Class"], ascending=[False, True]).reset_index(drop=True) if output else pd.DataFrame(columns=["Class", "Blocker", "Events", "Symbols", "Suggested Action"])


def journal_repeated_signal_rows(engine: Engine | None, limit: int = 500) -> pd.DataFrame:
    rows = db_rows(engine, f"SELECT symbol, side, context_json, COUNT(*) events, MAX(created_at) last_seen FROM trading_journal WHERE journal_code IN ('SIG_HOLD','SIG_BUY','SIG_SELL','SIG_BLOCKED') GROUP BY symbol, side, context_json ORDER BY events DESC, last_seen DESC LIMIT {int(limit)}")
    output = []
    for row in rows:
        context = safe_json_dict(row.get("context_json"))
        blockers = context.get("blockers", [])
        output.append(
            {
                "symbol": row.get("symbol", ""),
                "side": row.get("side", ""),
                "events": int(row.get("events") or 0),
                "last_seen": row.get("last_seen"),
                "blockers": ", ".join(str(item) for item in blockers[:6]) if isinstance(blockers, list) else str(blockers or ""),
            }
        )
    return pd.DataFrame(output)


def journal_freshness_rows(engine: Engine | None) -> pd.DataFrame:
    rows = db_rows(
        engine,
        """SELECT journal_type, COUNT(*) events, MAX(created_at) last_seen,
                  TIMESTAMPDIFF(SECOND, MAX(created_at), UTC_TIMESTAMP()) age_seconds
           FROM trading_journal
           GROUP BY journal_type
           ORDER BY last_seen DESC""",
    )
    output = []
    for row in rows:
        age = int(row.get("age_seconds") or 0)
        output.append(
            {
                "journal_type": row.get("journal_type", ""),
                "events": int(row.get("events") or 0),
                "last_seen": row.get("last_seen"),
                "age": f"{age}s" if age < 3600 else f"{age // 3600}h {(age % 3600) // 60}m",
                "status": "Fresh" if age <= 180 else "Quiet/deduped" if age <= 1800 else "Stale",
            }
        )
    return pd.DataFrame(output)


def journal_feedback_coverage_rows(engine: Engine | None) -> pd.DataFrame:
    rows = db_rows(
        engine,
        """SELECT symbol, COUNT(*) labels, SUM(actual_outcome=1) wins, SUM(actual_outcome=0) losses,
                  AVG(actual_return) avg_return, MAX(created_at) last_seen
           FROM trading_journal
           WHERE journal_type='FEEDBACK'
           GROUP BY symbol
           ORDER BY labels DESC""",
    )
    output = []
    for row in rows:
        labels = int(row.get("labels") or 0)
        wins = int(row.get("wins") or 0)
        output.append(
            {
                "symbol": row.get("symbol", ""),
                "labels": labels,
                "wins": wins,
                "losses": int(row.get("losses") or 0),
                "win_rate": f"{wins / max(labels, 1):.1%}",
                "avg_return_bps": f"{float(row.get('avg_return') or 0.0) * 10000:.1f}",
                "last_seen": row.get("last_seen"),
                "coverage": "Concentrated" if labels >= 20 else "Thin",
            }
        )
    missing = [symbol for symbol in CFG.symbols if symbol not in {str(row.get("symbol")) for row in rows}]
    output.extend({"symbol": symbol, "labels": 0, "wins": 0, "losses": 0, "win_rate": "0.0%", "avg_return_bps": "0.0", "last_seen": "", "coverage": "Missing"} for symbol in missing)
    return pd.DataFrame(output)


def journal_model_diagnostic_rows(engine: Engine | None) -> pd.DataFrame:
    rows = db_rows(engine, "SELECT created_at, context_json, lesson_json FROM trading_journal WHERE journal_code IN ('MODEL_REJECTED','MODEL_PROMOTED') ORDER BY id DESC LIMIT 10")
    output = []
    for row in rows:
        context = safe_json_dict(row.get("context_json"))
        metrics = safe_json_dict(context.get("metrics"))
        output.append(
            {
                "created_at": row.get("created_at"),
                "decision": context.get("status", ""),
                "reason": context.get("reason", ""),
                "rows": int(metrics.get("rows", 0) or 0),
                "accuracy": f"{float(metrics.get('accuracy', 0) or 0):.1%}",
                "precision": f"{float(metrics.get('precision', 0) or 0):.1%}",
                "recall": f"{float(metrics.get('recall', 0) or 0):.1%}",
                "positive_rate": f"{float(metrics.get('positive_rate', 0) or 0):.1%}",
                "suggested_action": "Improve labels/features before promotion" if context.get("reason") else "",
            }
        )
    return pd.DataFrame(output)


def journal_export_rows(engine: Engine | None, limit: int = 1000) -> pd.DataFrame:
    if engine is None:
        return journal_rows(engine)
    rows = db_rows(
        engine,
        f"""SELECT created_at, journal_code, journal_type, severity, actor, symbol, side, signal_key, model_version,
                  confidence, expected_reversion_bps, actual_outcome, actual_return, feature_json, context_json, lesson_json
           FROM trading_journal ORDER BY id DESC LIMIT {int(limit)}""",
    )
    export = []
    for row in rows:
        features = safe_json_dict(row.get("feature_json"))
        context = safe_json_dict(row.get("context_json"))
        lesson = safe_json_dict(row.get("lesson_json"))
        export.append(
            {
                "created_at": row.get("created_at"),
                "journal_code": row.get("journal_code"),
                "journal_type": row.get("journal_type"),
                "severity": row.get("severity"),
                "actor": row.get("actor"),
                "symbol": row.get("symbol"),
                "side": row.get("side"),
                "signal_key": row.get("signal_key"),
                "model_version": row.get("model_version"),
                "confidence": float(row.get("confidence") or 0.0),
                "expected_reversion_bps": float(row.get("expected_reversion_bps") or 0.0),
                "actual_outcome": row.get("actual_outcome"),
                "actual_return_bps": "" if row.get("actual_return") is None else float(row.get("actual_return")) * 10000,
                "reason": context.get("reason") or context.get("meaning") or context.get("rationale") or "",
                "blockers": ", ".join(str(item) for item in context.get("blockers", [])) if isinstance(context.get("blockers"), list) else str(context.get("blockers", "")),
                "lesson": lesson.get("feedback_use") or lesson.get("action") or lesson.get("goal") or lesson.get("watch") or lesson.get("promotion_reason") or "",
                "suggested_change": journal_suggested_change(str(row.get("journal_code", "")), context, lesson),
                "z_abs": features.get("z_abs", ""),
                "z_signed": features.get("z_signed", ""),
                "rsi_distance": features.get("rsi_distance", ""),
                "volume_z": features.get("volume_z", ""),
                "adx": features.get("adx", ""),
                "expected_feature_bps": features.get("expected_reversion_bps", ""),
                "obi": features.get("obi", ""),
                "spread_bps": features.get("spread_bps", ""),
                "model_slippage_bps": features.get("model_slippage_bps", ""),
                "funding_pressure": features.get("funding_pressure", ""),
                "open_interest_signal": features.get("open_interest_signal", ""),
                "context_json": json.dumps(context),
                "lesson_json": json.dumps(lesson),
            }
        )
    return pd.DataFrame(export)


def journal_suggested_change(code: str, context: dict[str, Any], lesson: dict[str, Any]) -> str:
    blockers = context.get("blockers", [])
    blockers_text = " ".join(str(item) for item in blockers) if isinstance(blockers, list) else str(blockers)
    if code == "SIG_HOLD" and ("research_only" in blockers_text or "symbol_not_whitelisted" in blockers_text):
        return "Treat as config-gated noise; separate from market/model blocker analysis."
    if code == "SIG_HOLD" and "expected_move_below_cost_hurdle" in blockers_text:
        return "More trades require a lower training hurdle or waiting for larger dislocations."
    if code == "SIG_HOLD" and "trend_regime_adx_high" in blockers_text:
        return "Avoid production relaxation until trend-regime outcomes are measured separately."
    if code == "SIG_HOLD" and "ml_confidence" in blockers_text:
        return "Collect paper labels for this setup before trusting lower-confidence entries."
    if code == "SIG_BLOCKED" and "ml_confidence" in blockers_text:
        return "Review confidence threshold or improve labels for similar setups."
    if code == "SIG_BLOCKED" and "ml_feature_drift" in blockers_text:
        return "Do not deploy; retrain or normalize drifted feature before allowing this setup."
    if code == "ORDER_BLOCKED":
        return "Move repeated final-gate failures earlier into signal/risk filters."
    if code == "TESTNET_ORDER_FAILED":
        return "Use paper mode for training until testnet venue errors are resolved."
    if code == "OUTCOME_LOSS":
        return "Compare feature values with winners and reduce weight/size for this setup bucket."
    if code == "OUTCOME_WIN":
        return "Candidate pattern worth preserving; check if feature bucket has repeatable edge."
    if code == "MISSED_PROFITABLE_CANDIDATE":
        return "Review blockers that rejected a profitable candidate; adjust only after repeated evidence by blocker bucket."
    if code == "MODEL_REJECTED":
        return "Use rejection reason to improve labeling, features, or class balance before promotion."
    if code in {"SIG_BUY", "SIG_SELL"}:
        return "Track subsequent outcome; use as evidence only after feedback label arrives."
    return lesson.get("action") or lesson.get("feedback_use") or ""


def journal_excel_bytes(frame: pd.DataFrame) -> bytes:
    html = (
        "<html><head><meta charset='utf-8'></head><body>"
        "<h2>Horizon Trading Journal Export</h2>"
        + frame.to_html(index=False, escape=True)
        + "</body></html>"
    )
    return html.encode("utf-8")


def journal_event_mix_chart(engine: Engine | None) -> go.Figure:
    rows = db_rows(
        engine,
        """SELECT journal_type, COUNT(*) events
           FROM trading_journal
           WHERE created_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 7 DAY)
           GROUP BY journal_type ORDER BY events DESC""",
    )
    frame = pd.DataFrame(rows)
    if frame.empty:
        frame = pd.DataFrame({"journal_type": ["SIGNAL", "FEEDBACK", "MODEL"], "events": [0, 0, 0]})
    fig = go.Figure(go.Bar(x=frame["journal_type"], y=frame["events"], marker_color=["#38bdf8", "#22c55e", "#facc15", "#fb7185"][: len(frame)]))
    fig.update_yaxes(title="Events")
    return dark_figure(fig, height=230)


def journal_feedback_outcome_chart(engine: Engine | None) -> go.Figure:
    rows = db_rows(
        engine,
        """SELECT journal_code, COUNT(*) events, AVG(actual_return) avg_return
           FROM trading_journal
           WHERE journal_type='FEEDBACK'
             AND created_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 30 DAY)
           GROUP BY journal_code ORDER BY journal_code""",
    )
    frame = pd.DataFrame(rows)
    if frame.empty:
        frame = pd.DataFrame({"journal_code": ["OUTCOME_WIN", "OUTCOME_LOSS"], "events": [0, 0], "avg_return": [0.0, 0.0]})
    colors = ["#22c55e" if code == "OUTCOME_WIN" else "#ef4444" for code in frame["journal_code"]]
    fig = go.Figure()
    fig.add_trace(go.Bar(x=frame["journal_code"], y=frame["events"], name="Events", marker_color=colors))
    fig.add_trace(go.Scatter(x=frame["journal_code"], y=frame["avg_return"].astype(float) * 10000, name="Avg return bps", mode="lines+markers", yaxis="y2", line=dict(color="#facc15", width=2)))
    fig.update_layout(yaxis2=dict(overlaying="y", side="right", title="Avg bps", gridcolor="rgba(0,0,0,0)"))
    return dark_figure(fig, height=230)


def journal_timeline_chart(engine: Engine | None) -> go.Figure:
    rows = db_rows(
        engine,
        """SELECT DATE(created_at) day, journal_type, COUNT(*) events
           FROM trading_journal
           WHERE created_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 14 DAY)
           GROUP BY DATE(created_at), journal_type
           ORDER BY day ASC""",
    )
    frame = pd.DataFrame(rows)
    fig = go.Figure()
    if frame.empty:
        days = pd.date_range(end=now_utc().date(), periods=7)
        frame = pd.DataFrame({"day": days, "journal_type": ["SIGNAL"] * len(days), "events": [0] * len(days)})
    for journal_type, part in frame.groupby("journal_type"):
        fig.add_trace(go.Scatter(x=part["day"], y=part["events"], mode="lines+markers", name=str(journal_type)))
    return dark_figure(fig, height=240)


def journal_reason_chart(engine: Engine | None) -> go.Figure:
    frame = journal_export_rows(engine, 500)
    if frame.empty or "suggested_change" not in frame.columns:
        frame = pd.DataFrame({"suggested_change": ["Waiting for journal entries"], "events": [0]})
    else:
        frame = frame.groupby("suggested_change", dropna=False).size().reset_index(name="events").sort_values("events", ascending=True).tail(8)
    fig = go.Figure(go.Bar(x=frame["events"], y=frame["suggested_change"], orientation="h", marker_color="#a78bfa"))
    return dark_figure(fig, height=280)


def worker_status_rows(state: RedisState) -> pd.DataFrame:
    workers = ["worker-marketdata", "worker-validation", "worker-signal", "worker-risk", "worker-ml", "worker-order", "worker-pnl"]
    rows = []
    for worker in workers:
        payload = state.get_json(f"worker_status:{worker}") or {}
        rows.append(
            {
                "worker": worker,
                "status": payload.get("status", "OFFLINE"),
                "pid": str(payload.get("pid", "")),
                "last_seen": payload.get("last_seen", ""),
            }
        )
    return pd.DataFrame(rows)


def deployment_queue_rows(engine: Engine | None) -> pd.DataFrame:
    if engine is None:
        return pd.DataFrame([{"created_at": iso_now(), "symbol": "", "side": "", "mode": "PAPER", "status": "LOCAL_PREVIEW", "block_reason": ""}])
    try:
        return pd.read_sql("SELECT created_at, symbol, side, mode, status, block_reason FROM deployment_requests ORDER BY id DESC LIMIT 10", engine)
    except Exception:
        return pd.DataFrame()


def db_scalar(engine: Engine | None, statement: str, params: dict[str, Any] | None = None, default: Any = None) -> Any:
    if engine is None:
        return default
    try:
        with engine.connect() as conn:
            value = conn.execute(text(statement), params or {}).scalar()
            return default if value is None else value
    except Exception:
        return default


def db_rows(engine: Engine | None, statement: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if engine is None:
        return []
    try:
        with engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(text(statement), params or {}).fetchall()]
    except Exception:
        return []


def position_pnl_rows(engine: Engine | None, limit: int = 20) -> pd.DataFrame:
    rows = db_rows(
        engine,
        f"""SELECT symbol, side, COALESCE(venue, 'PAPER') AS venue, entry_price, current_price, size_usdt, quantity,
                  unrealized_pnl, realized_pnl, status, updated_at
           FROM positions ORDER BY id DESC LIMIT {int(limit)}""",
    )
    output = []
    for row in rows:
        entry = float(row.get("entry_price") or 0.0)
        current = float(row.get("current_price") or entry)
        side = str(row.get("side") or "")
        direction = 1 if side == "BUY" else -1
        price_change_bps = ((current - entry) / max(entry, 1e-9)) * direction * 10000
        size = float(row.get("size_usdt") or 0.0)
        unrealized = float(row.get("unrealized_pnl") or 0.0)
        output.append(
            {
                "symbol": row.get("symbol", ""),
                "side": side,
                "venue": str(row.get("venue") or "PAPER").upper(),
                "status": row.get("status", ""),
                "entry_price": entry,
                "current_price": current,
                "size_usdt": size,
                "unrealized_pnl": unrealized,
                "unrealized_bps": (unrealized / max(size, 1e-9)) * 10000,
                "price_move_bps": price_change_bps,
                "realized_pnl": float(row.get("realized_pnl") or 0.0),
                "updated_at": row.get("updated_at", ""),
            }
        )
    return pd.DataFrame(output)


def position_pnl_summary(engine: Engine | None) -> dict[str, Any]:
    frame = position_pnl_rows(engine, 100)
    if frame.empty:
        return {"paper_unrealized": 0.0, "paper_realized": 0.0, "live_unrealized": 0.0, "live_realized": 0.0, "open_exposure": 0.0, "open_positions": 0, "top_open": {}}
    open_frame = frame[frame["status"].astype(str).str.upper().eq("OPEN")]
    paper_frame = frame[~frame["venue"].astype(str).str.upper().eq("LIVE")]
    live_frame = frame[frame["venue"].astype(str).str.upper().eq("LIVE")]
    top_open = open_frame.iloc[0].to_dict() if not open_frame.empty else {}
    return {
        "paper_unrealized": float(paper_frame.loc[paper_frame["status"].astype(str).str.upper().eq("OPEN"), "unrealized_pnl"].sum()),
        "paper_realized": float(paper_frame["realized_pnl"].sum()),
        "live_unrealized": float(live_frame.loc[live_frame["status"].astype(str).str.upper().eq("OPEN"), "unrealized_pnl"].sum()),
        "live_realized": float(live_frame["realized_pnl"].sum()),
        "open_exposure": float(open_frame["size_usdt"].sum()) if not open_frame.empty else 0.0,
        "open_positions": int(len(open_frame)),
        "top_open": top_open,
    }


def performance_report(state: RedisState, engine: Engine | None) -> dict[str, Any]:
    pnl = state.get_json("live_pnl") or {}
    risk = state.get_json("risk_state") or {}
    drift = state.get_json("drift_state") or {}
    workers = []
    for worker in ["worker-marketdata", "worker-validation", "worker-signal", "worker-risk", "worker-ml", "worker-order", "worker-pnl"]:
        payload = state.get_json(f"worker_status:{worker}") or {}
        workers.append({"worker": worker, "status": payload.get("status", "OFFLINE"), "last_seen": payload.get("last_seen", ""), "detail": payload.get("detail", {})})
    if engine is not None and all(row["status"] == "OFFLINE" for row in workers):
        workers = db_rows(engine, "SELECT worker_name AS worker, status, last_seen, detail_json AS detail FROM worker_heartbeat ORDER BY worker_name")

    latest_signals = db_rows(
        engine,
        """SELECT symbol, side, price, composite_score, ml_confidence, deployable, validation_status, risk_status, ts
           FROM signals ORDER BY id DESC LIMIT 10""",
    )
    recent_orders = db_rows(
        engine,
        """SELECT created_at, symbol, side, mode, status, requested_size_usdt, block_reason
           FROM deployment_requests ORDER BY id DESC LIMIT 10""",
    )
    active_model = db_rows(
        engine,
        """SELECT version, status, trained_rows, metrics_json, trained_at
           FROM model_registry WHERE status='ACTIVE' ORDER BY trained_at DESC LIMIT 1""",
    )
    recent_backtest = db_rows(
        engine,
        """SELECT symbol, total_trades, win_rate, profit_factor, expectancy, max_drawdown, updated_at AS ts
           FROM validation_state ORDER BY updated_at DESC LIMIT 10""",
    )
    recent_handoffs = db_rows(
        engine,
        """SELECT created_at, stage, symbol, status, next_owner, reason
           FROM handoff_events ORDER BY id DESC LIMIT 12""",
    )
    position_frame = position_pnl_rows(engine, 10)
    positions = position_frame.to_dict(orient="records") if not position_frame.empty else []
    position_summary = position_pnl_summary(engine)

    order_counts = {
        "pending": int(db_scalar(engine, "SELECT COUNT(*) FROM deployment_requests WHERE status='PENDING'", default=0) or 0),
        "executed": int(db_scalar(engine, "SELECT COUNT(*) FROM deployment_requests WHERE status='EXECUTED'", default=0) or 0),
        "blocked": int(db_scalar(engine, "SELECT COUNT(*) FROM deployment_requests WHERE status='BLOCKED'", default=0) or 0),
    }
    journal_counts = {
        "total_7d": int(db_scalar(engine, "SELECT COUNT(*) FROM trading_journal WHERE created_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 7 DAY)", default=0) or 0),
        "buy_signals_7d": int(db_scalar(engine, "SELECT COUNT(*) FROM trading_journal WHERE journal_code='SIG_BUY' AND created_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 7 DAY)", default=0) or 0),
        "feedback_7d": int(db_scalar(engine, "SELECT COUNT(*) FROM trading_journal WHERE journal_type='FEEDBACK' AND created_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 7 DAY)", default=0) or 0),
    }
    open_positions = int(db_scalar(engine, "SELECT COUNT(*) FROM positions WHERE status='OPEN'", default=0) or 0)
    active_model_payload = active_model[0] if active_model else {}
    if active_model_payload.get("metrics_json"):
        try:
            active_model_payload["metrics"] = json.loads(active_model_payload.pop("metrics_json") or "{}")
        except Exception:
            active_model_payload["metrics"] = {}

    return {
        "generated_at": iso_now(),
        "runtime": {"stage": CFG.system_stage, "headless_capable": True, "ui_required": False, "symbols": list(CFG.symbols), "strategy_interval": CFG.strategy_interval},
        "connections": {"redis": state.ok, "mariadb": engine is not None},
        "pnl": {
            "realized": float(pnl.get("realized_pnl", 0.0) or 0.0),
            "unrealized": float(pnl.get("unrealized_pnl", 0.0) or 0.0),
            "paper_realized": float(pnl.get("paper_realized_pnl", position_summary.get("paper_realized", 0.0)) or 0.0),
            "paper_unrealized": float(pnl.get("paper_unrealized_pnl", position_summary.get("paper_unrealized", 0.0)) or 0.0),
            "live_realized": float(pnl.get("live_realized_pnl", position_summary.get("live_realized", 0.0)) or 0.0),
            "live_unrealized": float(pnl.get("live_unrealized_pnl", position_summary.get("live_unrealized", 0.0)) or 0.0),
            "daily": float(pnl.get("daily_pnl", 0.0) or 0.0),
            "equity": float(pnl.get("equity", CFG.starting_equity) or CFG.starting_equity),
            "drawdown_pct": float(pnl.get("current_dd_pct", 0.0) or 0.0),
            "ts": pnl.get("ts", ""),
        },
        "risk": risk,
        "drift": drift,
        "workers": workers,
        "orders": order_counts,
        "journal": journal_counts,
        "open_positions": open_positions,
        "position_pnl": position_summary,
        "active_model": active_model_payload,
        "latest_signals": latest_signals,
        "recent_orders": recent_orders,
        "recent_backtests": recent_backtest,
        "recent_handoffs": recent_handoffs,
        "positions": positions,
    }


def print_performance_report(report: dict[str, Any], as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(report, indent=2, default=str))
        return
    pnl = report["pnl"]
    print("Horizon performance report")
    print(f"Generated: {report['generated_at']}")
    print(f"Stage: {report['runtime']['stage']} | Headless: yes | UI required: no")
    print(f"Connections: redis={report['connections']['redis']} mariadb={report['connections']['mariadb']}")
    print("")
    print(f"Equity: ${pnl['equity']:,.2f} | Daily P&L: ${pnl['daily']:,.2f} | Realized: ${pnl['realized']:,.2f} | Unrealized: ${pnl['unrealized']:,.2f} | DD: {pnl['drawdown_pct']:.2f}%")
    print(f"Risk: {report.get('risk', {}).get('status', 'UNKNOWN')} | Drift: {report.get('drift', {}).get('status', 'UNKNOWN')} score={float(report.get('drift', {}).get('drift_score', 0) or 0):.1f}")
    print(f"Orders: pending={report['orders']['pending']} executed={report['orders']['executed']} blocked={report['orders']['blocked']} | Open positions={report['open_positions']}")
    print(f"Journal: events_7d={report.get('journal', {}).get('total_7d', 0)} buy_signals_7d={report.get('journal', {}).get('buy_signals_7d', 0)} feedback_7d={report.get('journal', {}).get('feedback_7d', 0)}")
    model = report.get("active_model") or {}
    if model:
        metrics = model.get("metrics", {})
        print(f"Active ML: {model.get('version', '')} rows={model.get('trained_rows', 0)} accuracy={float(metrics.get('accuracy', 0) or 0):.3f} precision={float(metrics.get('precision', 0) or 0):.3f} recall={float(metrics.get('recall', 0) or 0):.3f}")
    print("")
    print("Workers")
    for row in report["workers"]:
        print(f"  {row.get('worker'):<18} {row.get('status', 'UNKNOWN'):<10} {row.get('last_seen', '')}")
    print("")
    print("Validation")
    for row in report.get("recent_backtests", [])[:6]:
        expectancy_bps = float(row.get("expectancy", 0) or 0) * 10000
        max_dd_pct = abs(float(row.get("max_drawdown", 0) or 0)) * 100
        print(f"  {row.get('symbol',''):<8} trades={int(row.get('total_trades', 0) or 0):<4} pf={format_profit_factor(row.get('profit_factor')):<9} exp={expectancy_bps:.1f}bps dd={max_dd_pct:.1f}% ts={row.get('ts','')}")
    print("")
    print("Recent handoffs")
    for row in report.get("recent_handoffs", [])[:6]:
        print(f"  {row.get('stage',''):<12} {row.get('symbol',''):<8} {row.get('status',''):<6} -> {row.get('next_owner',''):<18} {row.get('reason','')}")
    print("")
    print("Latest signals")
    for row in report["latest_signals"][:6]:
        print(f"  {row.get('symbol',''):<8} {row.get('side',''):<5} score={float(row.get('composite_score', 0) or 0):>6.2f} ml={float(row.get('ml_confidence', 0) or 0):.2f} deployable={row.get('deployable')} ts={row.get('ts','')}")


def production_progress_chart(state: RedisState, rows: list[dict[str, Any]], validation: dict[str, Any], risk: dict[str, Any], drift: dict[str, Any]) -> go.Figure:
    backtest = validation["backtest"]
    profit_factor = float(backtest.get("profit_factor", 0.0))
    profit_factor_label = format_profit_factor(profit_factor)
    expectancy_bps = float(backtest.get("expectancy", 0.0)) * 10000
    max_dd_pct = abs(float(backtest.get("max_drawdown", 0.0))) * 100
    ml_confidence = max((float(row.get("ml_confidence", 0.0)) for row in rows), default=0.0)
    worker_frame = worker_status_rows(state)
    online_workers = int(worker_frame["status"].isin(["ONLINE", "RUNNING"]).sum()) if not worker_frame.empty else 0
    worker_target = max(len(worker_frame), 1)
    risk_ok = risk.get("status") == "RISK_OK"
    drift_status = drift.get("status", "OK")
    deploy_unlocked = not CFG.mean_reversion_research_only
    deployable_signals = any(bool(row.get("deployable")) for row in rows)

    progress = [
        min(100.0, max(0.0, profit_factor / max(CFG.min_validation_profit_factor, 1e-9) * 100)),
        min(100.0, max(0.0, expectancy_bps / max(CFG.min_validation_expectancy_bps, 1e-9) * 100)),
        min(100.0, max(0.0, (1 - max_dd_pct / max(CFG.max_validation_drawdown_pct, 1e-9)) * 100)),
        min(100.0, max(0.0, ml_confidence / max(CFG.min_ml_confidence, 1e-9) * 100)),
        min(100.0, online_workers / worker_target * 100),
        100.0 if risk_ok and drift_status == "OK" else 60.0 if risk_ok and drift_status == "WARNING" else 0.0,
        100.0 if deploy_unlocked and deployable_signals else 50.0 if deploy_unlocked else 0.0,
    ]
    categories = ["PF", "Expectancy", "Drawdown", "ML Confidence", "Workers", "Risk/Drift", "Deploy Ready"]
    details = [
        f"PF {profit_factor_label} / {CFG.min_validation_profit_factor:.2f}",
        f"{expectancy_bps:.1f} bps / {CFG.min_validation_expectancy_bps:.1f} bps",
        f"{max_dd_pct:.1f}% <= {CFG.max_validation_drawdown_pct:.1f}%",
        f"{ml_confidence:.2f} / {CFG.min_ml_confidence:.2f}",
        f"{online_workers}/{worker_target} workers online",
        f"{risk.get('status', 'UNKNOWN')} / {drift_status}",
        "Unlocked with deployable signal" if deploy_unlocked and deployable_signals else "Research-only locked" if not deploy_unlocked else "No deployable signal",
    ]
    colors = ["#118833" if value >= 100 else "#b7791f" if value >= 60 else "#c53030" for value in progress]
    fig = go.Figure()
    fig.add_trace(go.Bar(name="Current Progress", x=categories, y=progress, marker_color=colors, text=[f"{v:.0f}%" for v in progress], textposition="outside", customdata=details, hovertemplate="%{x}<br>%{customdata}<br>Progress %{y:.1f}%<extra></extra>"))
    fig.add_trace(go.Scatter(name="Production Target", x=categories, y=[100] * len(categories), mode="lines+markers", line=dict(color="#111827", width=2, dash="dash"), marker=dict(size=7), hovertemplate="%{x}<br>Target 100%<extra></extra>"))
    fig.update_layout(
        height=330,
        margin=dict(l=10, r=10, t=30, b=10),
        yaxis=dict(range=[0, 115], ticksuffix="%", title="Progress to production target"),
        xaxis=dict(title=""),
        template="plotly_white",
        legend=dict(orientation="h", y=1.12, x=0),
    )
    return fig


def render_home_page(st: Any) -> None:
    st.title("Superbot Trading Lab")
    st.caption("A testnet-first trading system that collects market data, scores opportunities, checks risk, learns from outcomes, and can run without the web screen.")

    left, right = st.columns([1.2, 1])
    with left:
        st.subheader("What it does")
        st.markdown(
            """
            Superbot watches public crypto market data, looks for short-term mean-reversion setups, checks whether the idea is safe enough to test, and records every decision in MariaDB.

            The dashboard is only a window into the system. The backend workers can keep running headless on an Ubuntu droplet.
            """
        )
    with right:
        st.subheader("Current operating mode")
        st.metric("Stage", CFG.system_stage.upper())
        st.metric("Symbols", str(len(CFG.symbols)))
        st.metric("Testnet Orders", "Enabled" if CFG.enable_real_testnet_orders else "Disabled")

    st.subheader("Architecture at a glance")
    architecture_path = Path(__file__).resolve().parent / "docs" / "assets" / "architecture.svg"
    if architecture_path.exists():
        st.image(str(architecture_path), caption="Headless workers own trading operations; Streamlit renders state from Redis and MariaDB.")
    else:
        st.warning("Architecture image is missing from the runtime image. Check Dockerfile asset copy and docs/assets/architecture.svg.")

    st.subheader("Ubuntu install commands")
    st.code(
        """sudo apt-get update
sudo apt-get install -y git

git clone https://github.com/kaniampurath/superbot.git /home/myts/superbot
cd /home/myts/superbot

cp .env.production.example horizon-prod.env
nano horizon-prod.env
chmod 600 horizon-prod.env

bash scripts/install_ubuntu.sh --check --app-dir /home/myts/superbot --app-user myts --env-file horizon-prod.env
sudo bash scripts/install_ubuntu.sh --app-dir /home/myts/superbot --app-user myts --env-file horizon-prod.env

sudo systemctl start horizon-backend
sudo systemctl start horizon-ui
bash scripts/healthcheck_ubuntu.sh
bash scripts/horizonctl.sh performance""",
        language="bash",
    )

    st.subheader("What must be configured")
    st.dataframe(
        pd.DataFrame(
            [
                {"Setting": "MYSQL_PASSWORD", "Purpose": "Database password for the app user", "Required": "Yes"},
                {"Setting": "MYSQL_ROOT_PASSWORD", "Purpose": "MariaDB root password", "Required": "Yes"},
                {"Setting": "ENABLE_REAL_TESTNET_ORDERS", "Purpose": "Set false until Testnet credentials are ready", "Required": "Yes"},
                {"Setting": "testnet_key / testnet_secret", "Purpose": "Binance Spot Testnet credentials", "Required": "Only when Testnet orders are enabled"},
            ]
        ),
        width="stretch",
        hide_index=True,
    )

    st.info("For production, use Ubuntu 24.04 LTS, keep secrets outside GitHub, and run the backend headless with systemd.")


def ui_status_color(status: str) -> str:
    status = status.upper()
    if status in {"GREEN", "OK", "ONLINE", "RUNNING", "RISK_OK", "HEALTHY", "GOOD", "PASS"}:
        return "#22c55e"
    if status in {"AMBER", "WARNING", "DEGRADED", "TRAINING", "WAITING"}:
        return "#facc15"
    return "#ef4444"


def ui_card(title: str, value: str, subtitle: str = "", status: str = "GREEN", badge: str = "") -> str:
    color = ui_status_color(status)
    badge_html = f"<span class='sb-badge' style='background:{color}22;color:{color};border-color:{color}55'>{badge}</span>" if badge else ""
    return f"""
    <div class="sb-card">
      <div class="sb-card-top"><span>{title}</span>{badge_html}</div>
      <div class="sb-card-value">{value}</div>
      <div class="sb-card-sub">{subtitle}</div>
    </div>
    """


def ui_panel(title: str, body: str, extra_class: str = "") -> str:
    return f"<div class='sb-panel {extra_class}'><div class='sb-panel-title'>{title}</div>{body}</div>"


def plain_status(status: str) -> str:
    return "Healthy" if status == "GREEN" else "Watch" if status == "AMBER" else "Blocked"


def friendly_signal_reason(rows: list[dict[str, Any]], validation: dict[str, Any], risk: dict[str, Any], drift: dict[str, Any]) -> str:
    if any(row.get("deployable") for row in rows):
        return "A candidate passed the signal screen. Awaiting approval and final risk checks."
    if risk.get("status") != "RISK_OK":
        return "Risk controls are blocking new trades."
    if drift.get("status") == "DRIFT_LOCKED":
        return "Behavior drift is locked, so the system is pausing entries."
    if validation["backtest"].get("total_trades", 0) < 10:
        return "The strategy needs more validated historical trades before deployment."
    return "No setup currently has enough edge, confidence, and risk clearance."


def overview_alerts(rows: list[dict[str, Any]], validation: dict[str, Any], report: dict[str, Any], risk: dict[str, Any], drift: dict[str, Any]) -> list[dict[str, str]]:
    alerts: list[dict[str, str]] = []
    if not any(row.get("side") != "HOLD" for row in rows):
        alerts.append({"Level": "Info", "Area": "Signals", "Message": "No active trade signal right now.", "Action": "Let scanner continue collecting setups."})
    if validation["backtest"].get("total_trades", 0) < 10:
        alerts.append({"Level": "High", "Area": "Validation", "Message": "Not enough backtest trades for production judgement.", "Action": "Collect more history and labeled outcomes."})
    model = report.get("active_model") or {}
    if not model:
        alerts.append({"Level": "Medium", "Area": "Model", "Message": "No active trained ML model in registry.", "Action": "Allow worker-ml to gather labels and retrain."})
    offline = [row.get("worker") for row in report.get("workers", []) if row.get("status") not in {"ONLINE", "RUNNING"}]
    if offline:
        alerts.append({"Level": "Medium", "Area": "Workers", "Message": f"{len(offline)} worker(s) not online.", "Action": "Check horizonctl troubleshoot logs."})
    if risk.get("status") != "RISK_OK":
        alerts.append({"Level": "High", "Area": "Risk", "Message": "Risk gate is locked.", "Action": "Review risk events before approving trades."})
    if drift.get("status") != "OK":
        alerts.append({"Level": "Medium", "Area": "Drift", "Message": "Live behavior differs from validation.", "Action": "Wait for stabilization or retrain."})
    return alerts[:6]


def dark_figure(fig: go.Figure, height: int = 260) -> go.Figure:
    fig.update_layout(
        height=height,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#dbeafe", size=11),
        margin=dict(l=18, r=16, t=28, b=24),
        legend=dict(orientation="h", y=1.14, x=0),
        xaxis=dict(gridcolor="rgba(148,163,184,.16)", zerolinecolor="rgba(148,163,184,.2)"),
        yaxis=dict(gridcolor="rgba(148,163,184,.16)", zerolinecolor="rgba(148,163,184,.2)"),
    )
    return fig


def model_history_rows(engine: Engine | None) -> pd.DataFrame:
    rows = db_rows(
        engine,
        """SELECT version, status, trained_rows, metrics_json, trained_at
           FROM model_registry ORDER BY trained_at ASC LIMIT 30""",
    )
    parsed = []
    for row in rows:
        metrics = {}
        try:
            metrics = json.loads(row.get("metrics_json") or "{}")
        except Exception:
            metrics = {}
        parsed.append(
            {
                "trained_at": row.get("trained_at"),
                "version": row.get("version", ""),
                "status": row.get("status", ""),
                "trained_rows": int(row.get("trained_rows") or 0),
                "accuracy": float(metrics.get("accuracy", 0) or 0),
                "precision": float(metrics.get("precision", 0) or 0),
                "recall": float(metrics.get("recall", 0) or 0),
                "positive_rate": float(metrics.get("positive_rate", 0) or 0),
                "reason": metrics.get("promotion_reason", ""),
            }
        )
    return pd.DataFrame(parsed)


def model_feature_rows() -> pd.DataFrame:
    return pd.DataFrame(
        [{"Feature": name, "Meaning": ML_FEATURE_DESCRIPTIONS.get(name, ""), "Used": "Yes"} for name in ML_FEATURES]
    )


def model_learning_chart(engine: Engine | None, rows: list[dict[str, Any]]) -> go.Figure:
    history = model_history_rows(engine)
    fig = go.Figure()
    if history.empty:
        now = utc_naive_timestamp(pd.Timestamp.now(tz=UTC))
        confidence = max((float(row.get("ml_confidence", 0.0)) for row in rows), default=0.0)
        history = pd.DataFrame(
            {
                "trained_at": [now - pd.Timedelta(hours=2), now],
                "accuracy": [0.0, 0.0],
                "precision": [0.0, 0.0],
                "recall": [0.0, 0.0],
                "trained_rows": [0, 0],
                "confidence": [confidence, confidence],
            }
        )
    else:
        history["confidence"] = history[["accuracy", "precision", "recall"]].replace(0, np.nan).mean(axis=1).fillna(0)
    x = history["trained_at"]
    fig.add_trace(go.Scatter(x=x, y=history["accuracy"] * 100, mode="lines+markers", name="Accuracy", line=dict(color="#3b82f6", width=2)))
    fig.add_trace(go.Scatter(x=x, y=history["precision"] * 100, mode="lines+markers", name="Precision", line=dict(color="#22c55e", width=2)))
    fig.add_trace(go.Scatter(x=x, y=history["recall"] * 100, mode="lines+markers", name="Recall", line=dict(color="#e879f9", width=2)))
    fig.add_trace(go.Scatter(x=x, y=history["confidence"] * 100, mode="lines+markers", name="Confidence", line=dict(color="#facc15", width=2)))
    fig.update_yaxes(range=[0, 105], ticksuffix="%")
    return dark_figure(fig, height=250)


def training_growth_chart(engine: Engine | None) -> go.Figure:
    feature_rows = db_rows(engine, "SELECT DATE(ts) day, COUNT(*) rows_count FROM feature_snapshots GROUP BY DATE(ts) ORDER BY day DESC LIMIT 14")
    outcome_rows = db_rows(engine, "SELECT DATE(ts) day, COUNT(*) rows_count FROM trade_outcomes GROUP BY DATE(ts) ORDER BY day DESC LIMIT 14")
    feature_df = pd.DataFrame(feature_rows).sort_values("day") if feature_rows else pd.DataFrame(columns=["day", "rows_count"])
    outcome_df = pd.DataFrame(outcome_rows).sort_values("day") if outcome_rows else pd.DataFrame(columns=["day", "rows_count"])
    if feature_df.empty and outcome_df.empty:
        days = pd.date_range(end=now_utc().date(), periods=7)
        feature_df = pd.DataFrame({"day": days, "rows_count": [0] * len(days)})
        outcome_df = pd.DataFrame({"day": days, "rows_count": [0] * len(days)})
    fig = go.Figure()
    fig.add_trace(go.Bar(x=feature_df["day"], y=feature_df["rows_count"], name="Feature Rows", marker_color="#3b82f6"))
    fig.add_trace(go.Bar(x=outcome_df["day"], y=outcome_df["rows_count"], name="Labeled Outcomes", marker_color="#22c55e"))
    fig.update_layout(barmode="group")
    return dark_figure(fig, height=220)


def label_conversion_chart(engine: Engine | None) -> go.Figure:
    feature_rows = db_rows(engine, "SELECT DATE(ts) day, COUNT(*) feature_rows FROM feature_snapshots GROUP BY DATE(ts) ORDER BY day DESC LIMIT 14")
    outcome_rows = db_rows(engine, "SELECT DATE(ts) day, COUNT(*) labels FROM trade_outcomes GROUP BY DATE(ts) ORDER BY day DESC LIMIT 14")
    features = pd.DataFrame(feature_rows) if feature_rows else pd.DataFrame(columns=["day", "feature_rows"])
    labels = pd.DataFrame(outcome_rows) if outcome_rows else pd.DataFrame(columns=["day", "labels"])
    if features.empty and labels.empty:
        days = pd.date_range(end=now_utc().date(), periods=7)
        merged = pd.DataFrame({"day": days, "feature_rows": [0] * len(days), "labels": [0] * len(days)})
    else:
        merged = pd.merge(features, labels, on="day", how="outer").fillna(0).sort_values("day")
    merged["conversion"] = merged["labels"].astype(float) / merged["feature_rows"].astype(float).replace(0, np.nan)
    merged["conversion"] = merged["conversion"].fillna(0) * 100
    fig = go.Figure()
    fig.add_trace(go.Bar(x=merged["day"], y=merged["conversion"], name="Label conversion", marker_color="#22c55e"))
    fig.add_hline(y=10, line_dash="dot", line_color="#facc15")
    fig.update_yaxes(range=[0, max(25, float(merged["conversion"].max() if not merged.empty else 0) * 1.2)], ticksuffix="%")
    return dark_figure(fig, height=220)


def training_coverage_rows(engine: Engine | None) -> pd.DataFrame:
    rows = db_rows(
        engine,
        """SELECT DATE(ts) day, COUNT(*) labeled_outcomes, AVG(label) positive_rate,
                  AVG(forward_return) avg_forward_return
           FROM trade_outcomes
           GROUP BY DATE(ts)
           ORDER BY day DESC LIMIT 14""",
    )
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=["day", "labeled_outcomes", "positive_rate", "avg_forward_return"])
    frame = frame.sort_values("day")
    frame["positive_rate"] = frame["positive_rate"].astype(float).map(lambda value: f"{value:.1%}")
    frame["avg_forward_return"] = frame["avg_forward_return"].astype(float).map(lambda value: f"{value * 10000:.1f} bps")
    return frame


def model_learning_audit_rows(engine: Engine | None) -> pd.DataFrame:
    latest = latest_model_candidate(engine)
    metrics = latest.get("metrics", {}) if latest else {}
    active = db_rows(engine, "SELECT version, trained_rows, trained_at FROM model_registry WHERE status='ACTIVE' ORDER BY trained_at DESC LIMIT 1")
    predictions = db_rows(
        engine,
        """SELECT COUNT(*) total_predictions,
                  SUM(actual_outcome IS NOT NULL) evaluated_predictions,
                  AVG(confidence) avg_confidence,
                  MAX(created_at) last_prediction
           FROM ml_predictions
           WHERE side IN ('BUY', 'SELL')""",
    )
    labels = db_rows(
        engine,
        """SELECT COUNT(*) labels,
                  COUNT(DISTINCT symbol) symbols,
                  AVG(label) positive_rate,
                  AVG(forward_return) avg_return,
                  MAX(ts) last_label
           FROM trade_outcomes""",
    )
    prediction_row = predictions[0] if predictions else {}
    label_row = labels[0] if labels else {}
    total_predictions = int(prediction_row.get("total_predictions") or 0)
    evaluated_predictions = int(prediction_row.get("evaluated_predictions") or 0)
    active_row = active[0] if active else {}
    return pd.DataFrame(
        [
            {
                "check": "Active model",
                "status": "PASS" if active_row else "BLOCKED",
                "value": str(active_row.get("version", "none")),
                "meaning": "Signals use a trained model only when a candidate is promoted to ACTIVE.",
            },
            {
                "check": "Latest candidate",
                "status": "PASS" if latest.get("status") == "ACTIVE" else "BLOCKED",
                "value": f"{latest.get('status', 'none')} / {metrics.get('promotion_reason', 'waiting')}",
                "meaning": f"Accuracy {float(metrics.get('accuracy', 0) or 0):.1%}, precision {float(metrics.get('precision', 0) or 0):.1%}, recall {float(metrics.get('recall', 0) or 0):.1%}.",
            },
            {
                "check": "Historical labels",
                "status": "PASS" if int(label_row.get("labels") or 0) >= CFG.ml_min_training_rows else "WAITING",
                "value": f"{int(label_row.get('labels') or 0)} labels / {int(label_row.get('symbols') or 0)} symbols",
                "meaning": f"Last label {label_row.get('last_label', '')}; avg return {float(label_row.get('avg_return') or 0) * 10000:.1f} bps.",
            },
            {
                "check": "Prediction evaluation",
                "status": "PASS" if evaluated_predictions > 0 else "WAITING",
                "value": f"{evaluated_predictions}/{total_predictions} evaluated",
                "meaning": "Live prediction rows are pending forward labels until the outcome horizon is reached and matched.",
            },
            {
                "check": "Current inference mode",
                "status": "WAITING" if not active_row else "PASS",
                "value": "heuristic fallback" if not active_row else "trained model",
                "meaning": "Without an ACTIVE model, confidence still exists but is heuristic, not learned authority.",
            },
        ]
    )


def prediction_coverage_rows(engine: Engine | None) -> pd.DataFrame:
    rows = db_rows(
        engine,
        """SELECT symbol,
                  COUNT(*) predictions,
                  SUM(actual_outcome IS NOT NULL) evaluated,
                  AVG(confidence) avg_confidence,
                  MAX(created_at) last_prediction
           FROM ml_predictions
           WHERE side IN ('BUY', 'SELL')
           GROUP BY symbol
           ORDER BY predictions DESC""",
    )
    output = []
    for row in rows:
        predictions = int(row.get("predictions") or 0)
        evaluated = int(row.get("evaluated") or 0)
        output.append(
            {
                "symbol": row.get("symbol", ""),
                "predictions": predictions,
                "evaluated": evaluated,
                "pending": predictions - evaluated,
                "avg_confidence": f"{float(row.get('avg_confidence') or 0):.1%}",
                "last_prediction": row.get("last_prediction", ""),
            }
        )
    return pd.DataFrame(output)


def rolling_validation_rows(engine: Engine | None) -> pd.DataFrame:
    predictions = prediction_learning_frame(engine, 5000)
    if predictions.empty:
        return pd.DataFrame(columns=["Window", "Evaluated", "Accuracy", "Precision", "Recall", "Avg Return"])
    predictions["created_at"] = pd.to_datetime(predictions["created_at"], utc=True).dt.tz_convert(None)
    predictions = predictions.dropna(subset=["actual_outcome"])
    rows = []
    now = utc_naive_timestamp(pd.Timestamp.now(tz=UTC))
    for days in [7, 14, 30]:
        window = predictions[predictions["created_at"] >= now - pd.Timedelta(days=days)].copy()
        if window.empty:
            rows.append({"Window": f"{days}D", "Evaluated": 0, "Accuracy": "0.0%", "Precision": "0.0%", "Recall": "0.0%", "Avg Return": "0.0 bps"})
            continue
        predicted = (window["confidence"].astype(float) >= window["threshold"].astype(float)).astype(int)
        actual = window["actual_outcome"].astype(int)
        positives = actual == 1
        precision = float(((predicted == 1) & positives).sum() / max((predicted == 1).sum(), 1))
        recall = float(((predicted == 1) & positives).sum() / max(positives.sum(), 1))
        accuracy = float((predicted == actual).mean())
        avg_return = float(window["actual_return"].dropna().mean() or 0.0) * 10000
        rows.append({"Window": f"{days}D", "Evaluated": len(window), "Accuracy": f"{accuracy:.1%}", "Precision": f"{precision:.1%}", "Recall": f"{recall:.1%}", "Avg Return": f"{avg_return:.1f} bps"})
    return pd.DataFrame(rows)


def safe_json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def prediction_learning_frame(engine: Engine | None, limit: int = 1000) -> pd.DataFrame:
    rows = db_rows(
        engine,
        f"""SELECT p.idempotency_key, p.symbol, p.side, p.model_version, p.confidence, p.threshold,
                  p.feature_json, p.actual_outcome, p.actual_return, p.training_date,
                  p.evaluated_at, p.created_at, o.label, o.forward_return
           FROM ml_predictions p
           LEFT JOIN trade_outcomes o ON o.idempotency_key = p.idempotency_key
           WHERE p.side IN ('BUY', 'SELL')
           ORDER BY p.created_at DESC LIMIT {int(limit)}""",
    )
    parsed = []
    for row in rows:
        features = safe_json_dict(row.get("feature_json"))
        actual = row.get("actual_outcome")
        if actual is None:
            actual = row.get("label")
        actual_return = row.get("actual_return")
        if actual_return is None:
            actual_return = row.get("forward_return")
        record = {
            "idempotency_key": row.get("idempotency_key"),
            "symbol": row.get("symbol"),
            "side": row.get("side"),
            "model_version": row.get("model_version"),
            "confidence": float(row.get("confidence") or 0.0),
            "threshold": float(row.get("threshold") or CFG.min_ml_confidence),
            "actual_outcome": None if actual is None else int(actual),
            "actual_return": None if actual_return is None else float(actual_return),
            "training_date": row.get("training_date"),
            "evaluated_at": row.get("evaluated_at"),
            "created_at": row.get("created_at"),
        }
        for feature in ML_FEATURES:
            record[feature] = float(features.get(feature, 0.0) or 0.0)
        parsed.append(record)
    return pd.DataFrame(parsed)


def model_importance_map(model: dict[str, Any], latest_candidate: dict[str, Any] | None = None) -> dict[str, float]:
    latest_candidate = latest_candidate or {}
    importance = latest_candidate.get("feature_importance") or model.get("feature_importance") or {}
    if importance:
        return {name: float(importance.get(name, 0.0) or 0.0) for name in ML_FEATURES}
    if model.get("weights"):
        weights = [abs(float(value)) for value in model.get("weights", [])]
        total = sum(weights) or 1.0
        return {name: weight / total for name, weight in zip(model.get("features", ML_FEATURES), weights)}
    return {name: 1.0 / len(ML_FEATURES) for name in ML_FEATURES}


def feature_drift_rows(engine: Engine | None, rows: list[dict[str, Any]], model: dict[str, Any] | None = None, latest_candidate: dict[str, Any] | None = None) -> pd.DataFrame:
    history = load_labeled_training_from_db(engine)
    live = pd.DataFrame([{name: float(signal_features(row).get(name, 0.0)) for name in ML_FEATURES} for row in rows])
    importance = model_importance_map(model or {}, latest_candidate)
    drift_rows = []
    for feature in ML_FEATURES:
        if history.empty or live.empty or feature not in history.columns:
            drift_value = 0.0
        else:
            hist_series = history[feature].astype(float)
            hist_std = float(hist_series.std() or 1.0)
            drift_value = abs(float(live[feature].mean()) - float(hist_series.mean())) / max(hist_std, 1e-9)
        drift_rows.append(
            {
                "feature": feature,
                "drift": min(float(drift_value), 5.0),
                "importance": float(importance.get(feature, 0.0)),
                "weighted_drift": min(float(drift_value), 5.0) * float(importance.get(feature, 0.0)),
                "status": "RED" if drift_value >= CFG.ml_drift_block_threshold else "AMBER" if drift_value >= CFG.ml_drift_warning_threshold else "GREEN",
            }
        )
    return pd.DataFrame(drift_rows).sort_values(["weighted_drift", "drift"], ascending=False)


def feature_drift_blockers(training: pd.DataFrame, model: dict[str, Any] | None, features: dict[str, float]) -> list[str]:
    if not CFG.ml_drift_gate_enabled or training.empty or not model:
        return []
    importance = model_importance_map(model)
    top_features = sorted(ML_FEATURES, key=lambda name: importance.get(name, 0.0), reverse=True)[:5]
    blockers = []
    for feature in top_features:
        if feature not in training.columns:
            continue
        hist_series = training[feature].astype(float)
        hist_std = float(hist_series.std() or 1.0)
        drift = abs(float(features.get(feature, 0.0)) - float(hist_series.mean())) / max(hist_std, 1e-9)
        if drift >= CFG.ml_drift_block_threshold:
            blockers.append(f"ml_feature_drift:{feature}:{drift:.1f}")
    return blockers


def learning_health_summary(engine: Engine | None, rows: list[dict[str, Any]], model: dict[str, Any], latest_candidate: dict[str, Any]) -> dict[str, Any]:
    feature_rows = db_rows(engine, "SELECT COUNT(*) rows_count FROM feature_snapshots WHERE ts >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 24 HOUR)")
    label_rows = db_rows(engine, "SELECT COUNT(*) rows_count, AVG(label) positive_rate, AVG(forward_return) avg_return FROM trade_outcomes WHERE ts >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 24 HOUR)")
    predictions = prediction_learning_frame(engine, 500)
    evaluated = predictions.dropna(subset=["actual_outcome"]) if not predictions.empty else predictions
    prediction_counts = db_rows(engine, "SELECT COUNT(*) total_predictions, SUM(actual_outcome IS NOT NULL) evaluated_predictions FROM ml_predictions WHERE side IN ('BUY', 'SELL')")
    total_predictions = int((prediction_counts[0].get("total_predictions") if prediction_counts else 0) or 0)
    evaluated_predictions = int((prediction_counts[0].get("evaluated_predictions") if prediction_counts else 0) or 0)
    feature_count = int((feature_rows[0].get("rows_count") if feature_rows else 0) or 0)
    label_count = int((label_rows[0].get("rows_count") if label_rows else 0) or 0)
    conversion = label_count / max(feature_count, 1)
    latest_metrics = latest_candidate.get("metrics", {}) if latest_candidate else {}
    drift = feature_drift_rows(engine, rows, model, latest_candidate)
    top_drift = float(drift.head(5)["drift"].max()) if not drift.empty else 0.0
    evaluated_accuracy = float((evaluated["actual_outcome"].astype(int) == (evaluated["confidence"].astype(float) >= evaluated["threshold"].astype(float)).astype(int)).mean()) if not evaluated.empty else 0.0
    status = "GREEN"
    reasons: list[str] = []
    if not model:
        status = "RED"
        reasons.append("no active promoted model; signals are using heuristic confidence")
    if float(latest_metrics.get("accuracy", 0) or 0) < CFG.ml_min_accuracy:
        status = "RED"
        reasons.append("accuracy below promotion threshold")
    if total_predictions > 0 and evaluated_predictions == 0:
        status = "RED"
        reasons.append("live predictions are not yet evaluated against forward outcomes")
    if conversion < 0.10:
        status = "RED"
        reasons.append("feature snapshots are outpacing completed labels")
    if top_drift >= CFG.ml_drift_block_threshold:
        status = "RED"
        reasons.append("important live features drifted from training")
    elif top_drift >= CFG.ml_drift_warning_threshold and status != "RED":
        status = "AMBER"
        reasons.append("feature drift is elevated")
    if not reasons:
        reasons.append("learning loop is healthy enough for continued testnet evaluation")
    return {
        "status": status,
        "feature_rows_24h": feature_count,
        "labels_24h": label_count,
        "label_conversion": conversion,
        "total_predictions": total_predictions,
        "evaluated_predictions": evaluated_predictions,
        "evaluated_accuracy": evaluated_accuracy,
        "top_feature_drift": top_drift,
        "positive_rate_24h": float((label_rows[0].get("positive_rate") if label_rows else 0) or 0),
        "avg_return_24h": float((label_rows[0].get("avg_return") if label_rows else 0) or 0),
        "inference": "; ".join(reasons),
    }


def feature_importance_chart(model: dict[str, Any], latest_candidate: dict[str, Any]) -> go.Figure:
    importance = model_importance_map(model, latest_candidate)
    frame = pd.DataFrame({"feature": ML_FEATURES, "importance": [float(importance.get(name, 0.0)) * 100 for name in ML_FEATURES]})
    frame = frame.sort_values("importance", ascending=True).tail(10)
    fig = go.Figure(go.Bar(x=frame["importance"], y=frame["feature"], orientation="h", marker_color="#38bdf8"))
    fig.update_xaxes(ticksuffix="%")
    return dark_figure(fig, height=280)


def feature_drift_chart(engine: Engine | None, rows: list[dict[str, Any]]) -> go.Figure:
    drift = feature_drift_rows(engine, rows).sort_values("drift", ascending=True).tail(10)
    colors = ["#ef4444" if value >= 2 else "#facc15" if value >= 1 else "#22c55e" for value in drift["drift"]]
    fig = go.Figure(go.Bar(x=drift["drift"], y=drift["feature"], orientation="h", marker_color=colors))
    fig.add_vline(x=1, line_dash="dot", line_color="#facc15")
    fig.add_vline(x=2, line_dash="dot", line_color="#ef4444")
    return dark_figure(fig, height=280)


def prediction_confidence_trend_chart(engine: Engine | None, rows: list[dict[str, Any]]) -> go.Figure:
    frame = prediction_learning_frame(engine, 500)
    if frame.empty:
        now = utc_naive_timestamp(pd.Timestamp.now(tz=UTC))
        confidence = max((float(row.get("ml_confidence", 0.0)) for row in rows), default=0.0)
        frame = pd.DataFrame({"created_at": [now - pd.Timedelta(hours=1), now], "confidence": [confidence, confidence]})
    frame["created_at"] = pd.to_datetime(frame["created_at"])
    grouped = frame.sort_values("created_at").set_index("created_at")["confidence"].resample("1h").mean().dropna().reset_index()
    fig = go.Figure(go.Scatter(x=grouped["created_at"], y=grouped["confidence"] * 100, mode="lines+markers", name="Avg confidence", line=dict(color="#facc15", width=2)))
    fig.add_hline(y=CFG.min_ml_confidence * 100, line_dash="dot", line_color="#38bdf8")
    fig.update_yaxes(range=[0, 105], ticksuffix="%")
    return dark_figure(fig, height=250)


def hit_rate_by_feature_bucket_chart(engine: Engine | None) -> go.Figure:
    frame = prediction_learning_frame(engine, 1200)
    frame = frame.dropna(subset=["actual_outcome"]) if not frame.empty else frame
    features = ["z_abs", "expected_reversion_bps", "volume_z", "funding_pressure"]
    fig = go.Figure()
    if frame.empty:
        for feature in features:
            fig.add_trace(go.Bar(x=["Low", "Mid", "High"], y=[0, 0, 0], name=feature))
    else:
        for feature in features:
            series = frame[feature].astype(float)
            try:
                buckets = pd.qcut(series.rank(method="first"), 3, labels=["Low", "Mid", "High"])
            except Exception:
                buckets = pd.Series(["Mid"] * len(frame))
            hit_rate = frame.assign(bucket=buckets).groupby("bucket", observed=False)["actual_outcome"].mean().reindex(["Low", "Mid", "High"]).fillna(0) * 100
            fig.add_trace(go.Bar(x=hit_rate.index, y=hit_rate.values, name=feature))
    fig.update_yaxes(range=[0, 105], ticksuffix="%")
    fig.update_layout(barmode="group")
    return dark_figure(fig, height=280)


def expected_vs_actual_reversion_chart(engine: Engine | None) -> go.Figure:
    frame = prediction_learning_frame(engine, 1200)
    frame = frame.dropna(subset=["actual_return"]) if not frame.empty else frame
    if frame.empty:
        frame = pd.DataFrame({"expected_reversion_bps": [0], "actual_bps": [0], "confidence": [0]})
    else:
        frame["actual_bps"] = frame["actual_return"].astype(float) * 10000
    fig = go.Figure(go.Scatter(x=frame["expected_reversion_bps"], y=frame["actual_bps"], mode="markers", marker=dict(color=frame["confidence"] * 100, colorscale="Viridis", showscale=True, colorbar=dict(title="Conf %"))))
    fig.add_hline(y=0, line_dash="dot", line_color="#94a3b8")
    fig.update_xaxes(title="Expected reversion bps")
    fig.update_yaxes(title="Actual forward return bps")
    return dark_figure(fig, height=280)


def slippage_vs_spread_chart(engine: Engine | None) -> go.Figure:
    frame = prediction_learning_frame(engine, 1200)
    if frame.empty:
        frame = pd.DataFrame({"spread_bps": [0], "model_slippage_bps": [0], "confidence": [0]})
    fig = go.Figure(go.Scatter(x=frame["spread_bps"], y=frame["model_slippage_bps"], mode="markers", marker=dict(color=frame["confidence"] * 100, colorscale="Turbo", showscale=True, colorbar=dict(title="Conf %"))))
    fig.update_xaxes(title="Spread bps")
    fig.update_yaxes(title="Modeled slippage bps")
    return dark_figure(fig, height=250)


def funding_oi_pressure_chart(engine: Engine | None, rows: list[dict[str, Any]]) -> go.Figure:
    db_signal_rows = db_rows(engine, "SELECT ts, symbol, funding_pressure, open_interest_signal FROM signals ORDER BY ts DESC LIMIT 300")
    frame = pd.DataFrame(db_signal_rows)
    fig = go.Figure()
    if frame.empty:
        fig.add_annotation(text="No live funding/OI signal history available", x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False)
    else:
        frame["ts"] = pd.to_datetime(frame["ts"])
        grouped = frame.sort_values("ts").set_index("ts")[["funding_pressure", "open_interest_signal"]].resample("15min").mean().dropna().reset_index()
        if grouped.empty:
            fig.add_annotation(text="No resampled funding/OI history available", x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False)
        else:
            fig.add_trace(go.Scatter(x=grouped["ts"], y=grouped["funding_pressure"], mode="lines", name="Funding pressure", line=dict(color="#a78bfa", width=2)))
            fig.add_trace(go.Scatter(x=grouped["ts"], y=grouped["open_interest_signal"], mode="lines", name="OI signal", line=dict(color="#38bdf8", width=2)))
    fig.update_yaxes(range=[-1.05, 1.05])
    return dark_figure(fig, height=250)


def model_score_waterfall_chart(model: dict[str, Any], row: dict[str, Any]) -> go.Figure:
    features = signal_features(row)
    if model and model.get("weights"):
        model_features = model.get("features", ML_FEATURES)
        mean = np.array(model.get("mean", [0.0] * len(model_features)), dtype=float)
        std = np.array(model.get("std", [1.0] * len(model_features)), dtype=float)
        std[std == 0] = 1.0
        weights = np.array(model.get("weights", [0.0] * len(model_features)), dtype=float)
        values = np.array([float(features.get(name, 0.0)) for name in model_features], dtype=float)
        contributions = ((values - mean) / std) * weights
        frame = pd.DataFrame({"feature": model_features, "contribution": contributions}).sort_values("contribution", key=lambda s: s.abs(), ascending=False).head(8)
        base = float(model.get("bias", 0.0))
        title = f"Score explanation for {row.get('symbol', '-')}"
    else:
        frame = pd.DataFrame(
            {
                "feature": ["Stretch", "Expected move", "Liquidity", "Funding/OI", "Trend risk"],
                "contribution": [
                    float(features.get("z_abs", 0.0)) * 0.08,
                    float(features.get("expected_reversion_bps", 0.0)) / 250,
                    -abs(float(features.get("spread_bps", 0.0))) / 40,
                    -abs(float(features.get("funding_pressure", 0.0))) * 0.2,
                    -max(float(features.get("adx", 0.0)) - 25, 0.0) / 100,
                ],
            }
        )
        base = 0.0
        title = f"Heuristic explanation for {row.get('symbol', '-')}"
    fig = go.Figure(go.Waterfall(name="score", orientation="v", measure=["absolute"] + ["relative"] * len(frame), x=["Base"] + frame["feature"].tolist(), y=[base] + frame["contribution"].astype(float).tolist()))
    fig.update_layout(title=title)
    return dark_figure(fig, height=300)


def pnl_learning_chart(engine: Engine | None, pnl: dict[str, Any]) -> go.Figure:
    rows = db_rows(engine, "SELECT ts, equity, daily_pnl, current_dd_pct FROM pnl_snapshots ORDER BY ts ASC LIMIT 120")
    frame = pd.DataFrame(rows)
    if frame.empty:
        now = utc_naive_timestamp(pd.Timestamp.now(tz=UTC))
        frame = pd.DataFrame({"ts": [now - pd.Timedelta(hours=1), now], "equity": [CFG.starting_equity, float(pnl.get("equity", CFG.starting_equity) or CFG.starting_equity)], "current_dd_pct": [0, float(pnl.get("current_dd_pct", 0) or 0)]})
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=frame["ts"], y=frame["equity"], mode="lines", name="Equity", line=dict(color="#22c55e", width=2), fill="tozeroy", fillcolor="rgba(34,197,94,.18)"))
    fig.add_trace(go.Scatter(x=frame["ts"], y=-abs(frame["current_dd_pct"].astype(float)), mode="lines", name="Drawdown %", yaxis="y2", line=dict(color="#ef4444", width=2)))
    fig.update_layout(yaxis2=dict(overlaying="y", side="right", ticksuffix="%", gridcolor="rgba(0,0,0,0)"))
    return dark_figure(fig, height=250)


def signal_funnel_chart(report: dict[str, Any], rows: list[dict[str, Any]]) -> go.Figure:
    scanned = len(rows)
    candidates = sum(1 for row in rows if row.get("side") != "HOLD")
    deployable = sum(1 for row in rows if row.get("deployable"))
    pending = int(report.get("orders", {}).get("pending", 0) or 0)
    open_positions = int(report.get("open_positions", 0) or 0)
    fig = go.Figure(go.Funnel(y=["Scanned", "Candidates", "Risk Passed", "Queued", "Open"], x=[scanned, candidates, deployable, pending, open_positions], marker=dict(color=["#3b82f6", "#06b6d4", "#22c55e", "#facc15", "#e879f9"])))
    return dark_figure(fig, height=230)


def scanner_progress(row: dict[str, Any]) -> tuple[int, str, str]:
    checks = [
        ("Price", float(row.get("price", 0) or 0) > 0),
        ("Momentum", abs(float(row.get("z_score", 0) or 0)) >= 0.8 or float(row.get("rsi", 50) or 50) <= 35 or float(row.get("rsi", 50) or 50) >= 65),
        ("Participation", float(row.get("volume_z", 0) or 0) >= CFG.mean_reversion_min_volume_z),
        ("Order Book", abs(float(row.get("obi", 0) or 0)) <= 0.45 and abs(float(row.get("cross_exchange_spread_bps", 0) or 0)) <= 15),
        ("Model", float(row.get("ml_confidence", 0) or 0) >= CFG.min_ml_confidence or row.get("ml_model_version") in {"not_scored", "heuristic-fallback"}),
        ("Risk", bool(row.get("deployable")) or row.get("side") == "HOLD"),
    ]
    passed = sum(1 for _, ok in checks if ok)
    progress = int(round(passed / len(checks) * 100))
    next_phase = next((name for name, ok in checks if not ok), "Ready")
    blockers = row.get("deployment_blockers") or []
    if bool(row.get("deployable")):
        status = "Candidate"
    elif row.get("candidate_side") != "HOLD" and blockers:
        status = "Blocked"
    elif progress >= 80:
        status = "Watching"
    else:
        status = "Scanning"
    return progress, next_phase, status


def scanner_status_color(status: str) -> str:
    return {
        "Candidate": "#22c55e",
        "Watching": "#38bdf8",
        "Scanning": "#facc15",
        "Blocked": "#ef4444",
    }.get(status, "#94a3b8")


def scanner_reason(row: dict[str, Any]) -> str:
    blockers = row.get("deployment_blockers") or []
    if bool(row.get("deployable")):
        return "Passed signal, cost, liquidity, model, and risk screens."
    if blockers:
        friendly = {
            "research_only": "research-only mode",
            "symbol_not_whitelisted": "symbol not approved for deployment",
            "buy_rsi_not_oversold": "RSI not oversold enough",
            "sell_rsi_not_overbought": "RSI not overbought enough",
            "low_participation": "volume participation is low",
            "trend_regime_adx_high": "trend is too strong for mean reversion",
            "expected_move_below_cost_hurdle": "expected move is below fees/slippage",
            "spread_too_wide": "spread is too wide",
            "slippage_too_high": "estimated slippage is too high",
            "confidence_too_low": "confidence is below gate",
        }
        return "; ".join(friendly.get(str(item), str(item).replace("_", " ")) for item in blockers[:3])
    return "No trade: signal edge is not strong enough yet."


def scanner_radar_html(rows: list[dict[str, Any]]) -> str:
    cards = []
    for row in rows:
        progress, next_phase, status = scanner_progress(row)
        color = scanner_status_color(status)
        symbol = html_lib.escape(str(row.get("symbol", "")))
        side = html_lib.escape(str(row.get("candidate_side", row.get("side", "HOLD"))))
        reason = html_lib.escape(scanner_reason(row))
        feed = html_lib.escape(str(row.get("market_data_quality", "UNKNOWN")))
        source = html_lib.escape(str(row.get("market_source", "")).replace("wss://", "").replace("https://", "")[:42])
        cache_age = float(row.get("market_cache_age_seconds", 999999.0) or 999999.0)
        age_label = "no cache" if cache_age > 9999 else f"{cache_age:.0f}s old"
        price_value = float(row.get("price", 0) or 0)
        price_text = f"${price_value:,.4f}" if price_value > 0 else "Awaiting cache"
        change_status = str(row.get("timeframe_change_status", "AMBER"))
        change_color = ui_status_color(change_status)
        change_label = html_lib.escape(str(row.get("timeframe_change_label", f"{CFG.strategy_interval} --")))
        cards.append(
            f"<div class='scan-tile'>"
            f"<div class='scan-top'><b>{symbol}</b><span style='color:{color};border-color:{color}66;background:{color}1f'>{status}</span></div>"
            f"<div class='scan-price-row'><div class='scan-price'>{html_lib.escape(price_text)}</div><div class='scan-change' style='color:{change_color};background:{change_color}1f;border-color:{change_color}55'>{change_label}</div></div>"
            f"<div class='scan-meta'><span>Bias: {side}</span><span>ML {float(row.get('ml_confidence', 0) or 0):.2f}</span></div>"
            f"<div class='scan-meta'><span>Feed: {feed}</span><span>{html_lib.escape(age_label)}</span></div>"
            f"<div class='scan-source'>{source}</div>"
            f"<div class='scan-bar'><div style='width:{progress}%;background:{color}'></div></div>"
            f"<div class='scan-meta'><span>{progress}% scanned</span><span>Next: {html_lib.escape(next_phase)}</span></div>"
            f"<div class='scan-reason'>{reason}</div>"
            f"</div>"
        )
    return f"<div class='scan-grid'>{''.join(cards)}</div>"


def scanner_timeline_rows(rows: list[dict[str, Any]]) -> pd.DataFrame:
    timeline = []
    now = datetime.now().strftime("%H:%M:%S")
    for row in sorted(rows, key=lambda item: abs(float(item.get("composite_score", 0) or 0)), reverse=True):
        progress, next_phase, status = scanner_progress(row)
        timeline.append(
            {
                "Time": now,
                "Symbol": row.get("symbol", ""),
                "Phase": next_phase,
                "Status": status,
                "Progress": f"{progress}%",
                "Signal": row.get("candidate_side", row.get("side", "HOLD")),
                "Confidence": f"{float(row.get('ml_confidence', 0) or 0):.2f}",
                "Reason": scanner_reason(row),
            }
        )
    return pd.DataFrame(timeline)


def scanner_strength_chart(rows: list[dict[str, Any]]) -> go.Figure:
    frame = pd.DataFrame(
        [
            {
                "symbol": row.get("symbol", ""),
                "opportunity": min(100, abs(float(row.get("composite_score", 0) or 0)) * 100),
                "progress": scanner_progress(row)[0],
                "confidence": float(row.get("ml_confidence", 0) or 0) * 100,
            }
            for row in rows
        ]
    )
    fig = go.Figure()
    fig.add_trace(go.Bar(x=frame["symbol"], y=frame["progress"], name="Scan Progress", marker_color="#38bdf8"))
    fig.add_trace(go.Scatter(x=frame["symbol"], y=frame["opportunity"], mode="lines+markers", name="Opportunity", line=dict(color="#facc15", width=2)))
    fig.add_trace(go.Scatter(x=frame["symbol"], y=frame["confidence"], mode="lines+markers", name="ML Confidence", line=dict(color="#22c55e", width=2)))
    fig.update_yaxes(range=[0, 105], ticksuffix="%")
    return dark_figure(fig, height=250)


def render_scanner_command_center(st: Any, rows: list[dict[str, Any]], compact: bool = False) -> None:
    scanned = len(rows)
    candidates = sum(1 for row in rows if row.get("candidate_side") != "HOLD")
    deployable = sum(1 for row in rows if row.get("deployable"))
    avg_progress = int(round(np.mean([scanner_progress(row)[0] for row in rows]))) if rows else 0
    best_row = max(rows, key=lambda row: abs(float(row.get("composite_score", 0) or 0))) if rows else {}
    st.markdown(
        "<div class='sb-panel'>"
        "<div class='sb-panel-title'>Active Scanner</div>"
        "<div class='scan-pulse-row'>"
        f"<div><span class='scan-pulse'></span><b>Scanning {scanned} symbols</b><div class='sb-small'>Price -> momentum -> volume -> order book -> model -> risk</div></div>"
        f"<div class='scan-summary'><b>{avg_progress}%</b><span>coverage</span></div>"
        f"<div class='scan-summary'><b>{candidates}</b><span>candidates</span></div>"
        f"<div class='scan-summary'><b>{deployable}</b><span>deployable</span></div>"
        f"<div class='scan-summary'><b>{html_lib.escape(str(best_row.get('symbol', '-')))}</b><span>strongest watch</span></div>"
        "</div>"
        f"{scanner_radar_html(rows)}"
        "</div>",
        unsafe_allow_html=True,
    )
    if not compact:
        chart_col, table_col = st.columns([1.05, 1.25])
        with chart_col:
            st.plotly_chart(scanner_strength_chart(rows), width="stretch")
        with table_col:
            render_detail_table(st, "Scanner Timeline", scanner_timeline_rows(rows))


def render_live_overview_scanner(st: Any, state: RedisState) -> None:
    def render() -> None:
        render_scanner_command_center(st, demo_rows(state), compact=True)

    fragment = getattr(st, "fragment", None)
    if callable(fragment):
        @fragment(run_every="2s")
        def live_scanner_fragment() -> None:
            render()

        live_scanner_fragment()
    else:
        render()


def dataframe_display_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, default=str, sort_keys=True)
    missing = pd.isna(value)
    if isinstance(missing, (bool, np.bool_)) and missing:
        return ""
    return str(value)


def render_detail_table(st: Any, title: str, frame: pd.DataFrame) -> None:
    st.markdown(f"<div class='sb-panel-title'>{title}</div>", unsafe_allow_html=True)
    display_frame = frame.copy()
    for column in display_frame.select_dtypes(include=["object"]).columns:
        display_frame[column] = display_frame[column].map(dataframe_display_value)
    st.dataframe(display_frame, width="stretch", hide_index=True)


def render_ui() -> None:
    import streamlit as st

    st.set_page_config(page_title="Horizon Institutional Lab", layout="wide", initial_sidebar_state="expanded")
    state, engine = RedisState(CFG), db_engine(CFG)
    init_schema(engine)
    rows = demo_rows(state)
    selected = st.session_state.get("selected_symbol", CFG.symbols[0])
    validation = latest_validation_snapshot(state, engine, selected, fallback=True)
    backtest = validation["backtest"]
    walk = validation["walk_forward"]
    monte = validation["monte_carlo"]
    pnl = state.get_json("live_pnl") or {"realized_pnl": 0.0, "unrealized_pnl": 0.0, "daily_pnl": 0.0, "equity": CFG.starting_equity, "current_dd_pct": 0.0, "ts": iso_now()}
    risk = state.get_json("risk_state") or {"status": "RISK_OK", "ts": iso_now()}
    drift = state.get_json("drift_state") or {"status": "OK", "drift_score": 0, "ts": iso_now()}
    socket_status = "GREEN" if state.ok else "AMBER"
    best = max(rows, key=lambda r: abs(r["composite_score"]))
    report = performance_report(state, engine)
    worker_frame = worker_status_rows(state)
    online_workers = int(worker_frame["status"].isin(["ONLINE", "RUNNING"]).sum()) if not worker_frame.empty else 0
    total_workers = max(len(worker_frame), 1)
    model = report.get("active_model") or {}
    model_metrics = model.get("metrics", {}) if model else {}
    latest_candidate = latest_model_candidate(engine)
    latest_candidate_metrics = latest_candidate.get("metrics", {}) if latest_candidate else {}
    model_conf = max((float(row.get("ml_confidence", 0.0)) for row in rows), default=0.0)
    signals_now = sum(1 for r in rows if r.get("side") != "HOLD")
    deploy_ready = any(bool(row.get("deployable")) for row in rows) and risk.get("status") == "RISK_OK" and drift.get("status") != "DRIFT_LOCKED" and not CFG.mean_reversion_research_only
    validation_trades = int(backtest.get("total_trades", 0) or 0)
    if risk.get("status") != "RISK_OK" or drift.get("status") == "DRIFT_LOCKED":
        readiness_status = "RED"
        readiness_title = "Blocked by Safety Controls"
    elif validation_trades < 10 or not model:
        readiness_status = "AMBER"
        readiness_title = "Training Safely"
    else:
        readiness_status = "GREEN" if deploy_ready else "AMBER"
        readiness_title = "Ready for Test Review" if deploy_ready else "Healthy, Waiting for Edge"
    signal_reason = friendly_signal_reason(rows, validation, risk, drift)

    st.markdown(
        """
        <style>
        :root{--sb-bg:#070d18;--sb-panel:#101b2b;--sb-panel2:#132238;--sb-border:#24344d;--sb-text:#e5edf8;--sb-muted:#94a3b8;--sb-blue:#2563eb;--sb-green:#22c55e;--sb-yellow:#facc15;--sb-red:#ef4444}
        .stApp{background:radial-gradient(circle at top left,#0b1f36 0,#070d18 36%,#050913 100%);color:var(--sb-text)}
        .block-container{padding-top:1.2rem;padding-bottom:1.2rem;max-width:1700px}
        section[data-testid="stSidebar"]{background:linear-gradient(180deg,#0c1728,#07111f);border-right:1px solid #1e293b}
        section[data-testid="stSidebar"] *{color:#dbeafe}
        div[data-testid="stRadio"] label{padding:.42rem .55rem;border-radius:8px}
        div[data-testid="stRadio"] label:has(input:checked){background:#0f3b83}
        h1,h2,h3{letter-spacing:0;color:#f8fafc}
        p,span,div{letter-spacing:0}
        .sb-brand{display:flex;gap:.75rem;align-items:center;margin:.4rem 0 1.2rem}
        .sb-logo{width:34px;height:34px;border-radius:8px;background:#facc15;color:#0f172a;font-weight:900;display:flex;align-items:center;justify-content:center}
        .sb-brand-title{font-weight:800;font-size:1.05rem}
        .sb-brand-sub{color:#94a3b8;font-size:.78rem}
        .sb-hero{border:1px solid #24344d;background:linear-gradient(135deg,rgba(37,99,235,.18),rgba(34,197,94,.08));border-radius:8px;padding:14px 16px;margin:.4rem 0 1rem}
        .sb-hero-row{display:flex;align-items:center;justify-content:space-between;gap:14px}
        .sb-hero-title{font-size:1.15rem;font-weight:800}
        .sb-hero-sub{color:#b6c6dc;font-size:.88rem;margin-top:3px}
        .sb-dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px}
        .sb-card,.sb-panel{border:1px solid #24344d;background:linear-gradient(180deg,rgba(19,34,56,.95),rgba(12,24,40,.95));border-radius:8px;box-shadow:0 12px 30px rgba(0,0,0,.18)}
        .sb-card{min-height:96px;padding:14px 14px}
        .sb-card-top{display:flex;align-items:center;justify-content:space-between;color:#b6c6dc;font-size:.82rem}
        .sb-card-value{font-size:1.72rem;font-weight:800;color:#f8fafc;margin-top:7px;line-height:1.05}
        .sb-card-sub{font-size:.78rem;color:#94a3b8;margin-top:7px;line-height:1.25}
        .sb-badge{border:1px solid;border-radius:6px;padding:2px 7px;font-size:.72rem;font-weight:800}
        .sb-panel{padding:14px;margin-bottom:.8rem}
        .sb-panel-title{font-weight:800;color:#f8fafc;margin-bottom:10px}
        .sb-small{font-size:.78rem;color:#94a3b8}
        .sb-health-row{display:grid;grid-template-columns:22px 1fr auto;gap:10px;align-items:center;border:1px solid #263750;border-radius:8px;padding:9px 10px;margin:7px 0;background:rgba(15,23,42,.45)}
        .sb-health-title{font-weight:700;color:#f8fafc}
        .sb-health-sub{font-size:.76rem;color:#94a3b8}
        .sb-side-box{border:1px solid #24344d;background:#101b2b;border-radius:8px;padding:12px;margin:.7rem 0}
        .sb-side-title{font-size:.8rem;color:#94a3b8}
        .sb-side-value{font-weight:800;color:#f8fafc;margin-top:3px}
        div[data-testid="stMetric"]{background:#101b2b;border:1px solid #24344d;border-radius:8px;padding:10px}
        div[data-testid="stMetric"] *{color:#e5edf8!important}
        div[data-testid="stDataFrame"]{border:1px solid #24344d;border-radius:8px;overflow:hidden}
        th,td{font-size:12px!important}
        .stButton button{border-radius:8px;border:1px solid #2b4c7e;background:#12356a;color:#eff6ff}
        .stButton button:hover{border-color:#60a5fa;color:white}
        .scan-pulse-row{display:grid;grid-template-columns:1.8fr repeat(4,.72fr);gap:10px;align-items:center;margin-bottom:12px}
        .scan-pulse{display:inline-block;width:11px;height:11px;border-radius:50%;background:#22c55e;margin-right:8px;box-shadow:0 0 0 0 rgba(34,197,94,.8);animation:scanPulse 1.5s infinite}
        @keyframes scanPulse{0%{box-shadow:0 0 0 0 rgba(34,197,94,.8)}70%{box-shadow:0 0 0 12px rgba(34,197,94,0)}100%{box-shadow:0 0 0 0 rgba(34,197,94,0)}}
        .scan-summary{border:1px solid #263750;border-radius:8px;background:rgba(15,23,42,.55);padding:9px 10px;text-align:center}
        .scan-summary b{display:block;color:#f8fafc;font-size:1.15rem}
        .scan-summary span{display:block;color:#94a3b8;font-size:.72rem}
        .scan-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px}
        .scan-tile{border:1px solid #263750;border-radius:8px;background:linear-gradient(180deg,rgba(15,31,53,.92),rgba(10,21,36,.92));padding:12px;min-height:150px}
        .scan-top{display:flex;justify-content:space-between;align-items:center;gap:8px}
        .scan-top span{border:1px solid;border-radius:6px;padding:2px 7px;font-size:.72rem;font-weight:800}
        .scan-price-row{display:flex;align-items:flex-end;justify-content:space-between;gap:8px;margin-top:8px;min-height:32px}
        .scan-price{font-size:1.35rem;font-weight:800;color:#f8fafc;line-height:1}
        .scan-change{border:1px solid;border-radius:6px;padding:3px 7px;font-size:.72rem;font-weight:800;white-space:nowrap;line-height:1.05}
        .scan-meta{display:flex;justify-content:space-between;gap:8px;color:#b6c6dc;font-size:.76rem;margin-top:6px}
        .scan-source{color:#64748b;font-size:.68rem;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
        .scan-bar{height:7px;background:#111827;border-radius:999px;margin-top:10px;overflow:hidden;border:1px solid #263750}
        .scan-bar div{height:100%;border-radius:999px}
        .scan-reason{color:#94a3b8;font-size:.75rem;line-height:1.25;margin-top:9px;min-height:34px}
        @media (max-width:1100px){.scan-pulse-row{grid-template-columns:1fr 1fr}.scan-summary{text-align:left}}
        </style>
        """,
        unsafe_allow_html=True,
    )
    with st.sidebar:
        st.markdown(
            """
            <div class="sb-brand">
              <div class="sb-logo">B</div>
              <div><div class="sb-brand-title">HORIZON LAB</div><div class="sb-brand-sub">Crypto Mispricing Lab</div></div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        page = st.radio(
            "View",
            ["Overview", "System Health", "Signals", "Performance", "Model Learning", "Journal", "Trades", "Risk Monitor", "Funding & PnL", "Scanners", "Alerts", "Reports", "Settings", "Home"],
            index=0,
        )
        st.markdown(
            f"""
            <div class="sb-side-box"><div class="sb-side-title">System Status</div><div class="sb-side-value"><span class="sb-dot" style="background:{ui_status_color(readiness_status)}"></span>{plain_status(readiness_status)}</div><div class="sb-small">{readiness_title}</div></div>
            <div class="sb-side-box"><div class="sb-side-title">Workers</div><div class="sb-side-value">{online_workers}/{total_workers} online</div><div class="sb-small">Backend runs headless</div></div>
            """,
            unsafe_allow_html=True,
        )
    if page == "Home":
        render_home_page(st)
        return

    df = pd.DataFrame(rows)
    scanner_cols = ["symbol", "candidate_side", "side", "price", "timeframe_change_pct", "timeframe_change_label", "market_data_quality", "market_source", "market_cache_age_seconds", "strategy_interval", "z_score", "rsi", "volume_z", "adx", "expected_reversion_bps", "ml_confidence", "ml_model_version", "obi", "cross_exchange_spread_bps", "model_slippage_bps", "funding_pressure", "open_interest_signal", "win_p_est", "payoff_b", "kelly_fraction", "suggested_usdt", "research_only", "deployable", "deployment_blockers", "rationale"]
    alerts = overview_alerts(rows, validation, report, risk, drift)

    header_left, header_right = st.columns([2.4, 1])
    with header_left:
        st.title("Horizon Institutional Crypto Mispricing Lab")
        st.caption("AI-powered mispricing detection, risk-managed execution, and continuous learning.")
    with header_right:
        h1, h2, h3 = st.columns(3)
        h1.markdown(ui_card("Environment", CFG.system_stage.title(), "Current operating mode", "GREEN" if CFG.system_stage == "production" else "AMBER"), unsafe_allow_html=True)
        h2.markdown(ui_card("Time (IST)", datetime.now().strftime("%H:%M:%S"), "Dashboard refresh", "GREEN"), unsafe_allow_html=True)
        h3.markdown(ui_card("Latency", "Local", "UI render path", socket_status), unsafe_allow_html=True)

    st.markdown(
        f"""
        <div class="sb-hero">
          <div class="sb-hero-row">
            <div><div class="sb-hero-title"><span class="sb-dot" style="background:{ui_status_color(readiness_status)}"></span>{readiness_title}</div><div class="sb-hero-sub">{signal_reason}</div></div>
            <div class="sb-badge" style="background:{ui_status_color(readiness_status)}22;color:{ui_status_color(readiness_status)};border-color:{ui_status_color(readiness_status)}55">Deploy Ready: {'Yes' if deploy_ready else 'No'}</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if page == "Overview":
        st.markdown("<div class='sb-panel-title'>At a Glance</div>", unsafe_allow_html=True)
        card_cols = st.columns(7)
        win_text = "Need data" if validation_trades < 10 else f"{backtest['win_rate']:.2%}"
        card_specs = [
            ("Trade Opportunity", f"{best['composite_score']:.2f}", "Weak" if abs(best["composite_score"]) < 1 else "Strong setup", "AMBER" if abs(best["composite_score"]) < 1 else "GREEN", "Low" if abs(best["composite_score"]) < 1 else "High"),
            ("Active Signals", str(signals_now), "No active signals" if signals_now == 0 else "Review candidates", "AMBER" if signals_now == 0 else "GREEN", ""),
            ("Win Rate", win_text, f"{validation_trades} validated trades", "AMBER" if validation_trades < 10 else "GREEN", ""),
            ("Live P&L", f"${float(pnl.get('daily_pnl', 0) or 0):,.0f}", "Today / paper", "GREEN" if float(pnl.get("daily_pnl", 0) or 0) >= 0 else "RED", ""),
            ("Market Crowding", f"{best['funding_pressure']:.2f}", "Funding pressure", "AMBER" if abs(best["funding_pressure"]) > 0.4 else "GREEN", ""),
            ("Price Gap", f"{best['cross_exchange_spread_bps']:.1f} bps", "Cross-exchange", "GREEN" if abs(best["cross_exchange_spread_bps"]) >= 5 else "AMBER", ""),
            ("Behavior Change", f"{float(drift.get('drift_score', 0) or 0):.0f}/100", "Drift budget used", "GREEN" if drift.get("status") == "OK" else "AMBER", ""),
        ]
        for col, spec in zip(card_cols, card_specs):
            col.markdown(ui_card(*spec), unsafe_allow_html=True)

        render_live_overview_scanner(st, state)

        left, mid, right = st.columns([1.05, 1.35, .72])
        with left:
            live_feed_count = sum(1 for row in rows if row.get("market_cache_live"))
            feed_status = "GREEN" if live_feed_count == len(rows) else ("AMBER" if live_feed_count else "RED")
            feed_label = f"{live_feed_count}/{len(rows)} symbols live from websocket cache" if rows else "No symbols configured"
            health_rows = [
                ("Data Ingestion", feed_label, feed_status),
                ("Model & Signals", "Model active" if model else "Learning or fallback mode", "GREEN" if model else "AMBER"),
                ("Execution", "No order issues", "GREEN"),
                ("Risk Controls", risk.get("status", "UNKNOWN"), "GREEN" if risk.get("status") == "RISK_OK" else "RED"),
                ("Infrastructure", f"{online_workers}/{total_workers} workers online", "GREEN" if online_workers == total_workers else "AMBER"),
            ]
            body = "".join(
                f"<div class='sb-health-row'><span class='sb-dot' style='background:{ui_status_color(status)}'></span><div><div class='sb-health-title'>{name}</div><div class='sb-health-sub'>{sub}</div></div><div class='sb-small'>{plain_status(status)}</div></div>"
                for name, sub, status in health_rows
            )
            body += f"<div class='sb-side-box'><div class='sb-side-title'>Overall Status</div><div class='sb-side-value' style='color:{ui_status_color(readiness_status)}'>{plain_status(readiness_status)}</div><div class='sb-small'>Last update {datetime.now().strftime('%H:%M:%S')}</div></div>"
            st.markdown(ui_panel("System Health", body), unsafe_allow_html=True)
        with mid:
            st.markdown("<div class='sb-panel'><div class='sb-panel-title'>Production Progress</div>", unsafe_allow_html=True)
            st.plotly_chart(dark_figure(production_progress_chart(state, rows, validation, risk, drift), height=275), width="stretch")
            st.markdown("</div>", unsafe_allow_html=True)
        with right:
            perf_body = f"""
            <div class='sb-small'>Trading Performance</div>
            <div class='sb-health-row'><div></div><div>Backtest Trades</div><b>{validation_trades}</b></div>
            <div class='sb-health-row'><div></div><div>Expected Profit / Trade</div><b>{backtest['expectancy'] * 10000:.1f} bps</b></div>
            <div class='sb-health-row'><div></div><div>Profit Factor</div><b>{format_profit_factor(backtest.get('profit_factor'))}</b></div>
            <div class='sb-small' style='margin-top:10px'>Model Metrics</div>
            <div class='sb-health-row'><div></div><div>Model Confidence</div><b>{model_conf:.2f}</b></div>
            <div class='sb-health-row'><div></div><div>Training Rows</div><b>{model.get('trained_rows', 0) if model else 0}</b></div>
            <div class='sb-health-row'><div></div><div>Accuracy</div><b>{float(model_metrics.get('accuracy', 0) or 0):.2%}</b></div>
            """
            st.markdown(ui_panel("Key Metrics", perf_body), unsafe_allow_html=True)

        learn_left, learn_right = st.columns([1.05, 1.25])
        with learn_left:
            body = f"""
            <div class='sb-health-row'><span class='sb-dot' style='background:{ui_status_color('GREEN' if model else 'AMBER')}'></span><div><div class='sb-health-title'>Active Model</div><div class='sb-health-sub'>{model.get('version', 'No trained model yet')}</div></div><b>{model.get('status', 'WAITING')}</b></div>
            <div class='sb-health-row'><span class='sb-dot' style='background:{ui_status_color('GREEN' if model_conf >= CFG.min_ml_confidence else 'AMBER')}'></span><div><div class='sb-health-title'>Entry Confidence</div><div class='sb-health-sub'>Minimum target {CFG.min_ml_confidence:.2f}</div></div><b>{model_conf:.2f}</b></div>
            <div class='sb-health-row'><span class='sb-dot' style='background:{ui_status_color('GREEN' if validation_trades >= 10 else 'AMBER')}'></span><div><div class='sb-health-title'>Learning Evidence</div><div class='sb-health-sub'>Validated trades and labels</div></div><b>{validation_trades}</b></div>
            """
            st.markdown(ui_panel("Model Learning", body), unsafe_allow_html=True)
            st.plotly_chart(training_growth_chart(engine), width="stretch")
        with learn_right:
            st.markdown("<div class='sb-panel'><div class='sb-panel-title'>Model Performance Over Time</div>", unsafe_allow_html=True)
            st.plotly_chart(model_learning_chart(engine, rows), width="stretch")
            st.markdown("</div>", unsafe_allow_html=True)

        pnl_left, pnl_right = st.columns([1.1, 1])
        with pnl_left:
            st.markdown("<div class='sb-panel'><div class='sb-panel-title'>P&L and Drawdown</div>", unsafe_allow_html=True)
            st.plotly_chart(pnl_learning_chart(engine, pnl), width="stretch")
            st.markdown("</div>", unsafe_allow_html=True)
        with pnl_right:
            st.markdown("<div class='sb-panel'><div class='sb-panel-title'>Signal Lifecycle Funnel</div>", unsafe_allow_html=True)
            st.plotly_chart(signal_funnel_chart(report, rows), width="stretch")
            st.markdown("</div>", unsafe_allow_html=True)

        act_left, act_right = st.columns([1.05, 1])
        with act_left:
            render_detail_table(st, "Recent Activity", audit_rows(engine).head(6))
        with act_right:
            render_detail_table(st, "Alerts and Next Actions", pd.DataFrame(alerts))

    elif page == "System Health":
        st.subheader("System Health")
        render_detail_table(st, "Worker Status", worker_frame)
        render_detail_table(st, "Audit", audit_rows(engine))
        st.plotly_chart(signal_funnel_chart(report, rows), width="stretch")

    elif page == "Signals":
        st.subheader("Signals")
        st.markdown(f"Current interpretation: **{signal_reason}**")
        render_detail_table(st, "Latest Scanner Signals", df[scanner_cols])
        st.plotly_chart(historical_chart(state, selected), width="stretch")

    elif page == "Performance":
        st.subheader("Performance")
        report_pnl = report.get("pnl", {})
        position_summary = report.get("position_pnl", {})
        top_open = position_summary.get("top_open") or {}
        live_cols = st.columns(5)
        live_specs = [
            ("Paper Open P&L", format_money(report_pnl.get("paper_unrealized", position_summary.get("paper_unrealized", 0.0))), "Simulated open positions", pnl_status(report_pnl.get("paper_unrealized", 0.0))),
            ("Paper Realized", format_money(report_pnl.get("paper_realized", position_summary.get("paper_realized", 0.0))), "Closed paper/testnet positions", pnl_status(report_pnl.get("paper_realized", 0.0))),
            ("Live Open P&L", format_money(report_pnl.get("live_unrealized", position_summary.get("live_unrealized", 0.0))), "Real exchange positions only", pnl_status(report_pnl.get("live_unrealized", 0.0))),
            ("Open Exposure", format_money(position_summary.get("open_exposure", 0.0)), f"{int(position_summary.get('open_positions', 0) or 0)} open position(s)", "GREEN" if float(position_summary.get("open_exposure", 0.0) or 0.0) <= CFG.max_position_usdt * max(int(position_summary.get("open_positions", 0) or 0), 1) else "AMBER"),
            ("Focus Position", str(top_open.get("symbol", "None")), f"{top_open.get('venue', '')} {format_money(top_open.get('unrealized_pnl', 0.0))} / {float(top_open.get('unrealized_bps', 0.0) or 0.0):.1f} bps" if top_open else "No open position", pnl_status(top_open.get("unrealized_pnl", 0.0) if top_open else 0.0)),
        ]
        for col, spec in zip(live_cols, live_specs):
            col.markdown(ui_card(*spec), unsafe_allow_html=True)
        render_detail_table(st, "Current Position P&L", pd.DataFrame(report.get("positions", [])))
        perf_cols = st.columns(5)
        for col, (label, value) in zip(
            perf_cols,
            [
                ("Backtest Trades", str(validation_trades)),
                ("Win Rate", "Need data" if validation_trades < 10 else f"{backtest['win_rate']:.2%}"),
                ("Profit Factor", format_profit_factor(backtest.get("profit_factor"))),
                ("Expectancy", f"{backtest['expectancy'] * 10000:.1f} bps"),
                ("Max Drawdown", f"{abs(backtest['max_drawdown']) * 100:.2f}%"),
            ],
        ):
            col.markdown(ui_card(label, value, "Strategy validation", "AMBER" if validation_trades < 10 else "GREEN"), unsafe_allow_html=True)
        st.plotly_chart(pnl_learning_chart(engine, pnl), width="stretch")
        st.plotly_chart(dark_figure(production_progress_chart(state, rows, validation, risk, drift), height=300), width="stretch")

    elif page == "Model Learning":
        st.subheader("Model Learning")
        st.markdown("This page shows whether the model is collecting labels, improving, and safe enough to influence entries.")
        latest_rows = int(latest_candidate.get("trained_rows", 0) or 0) if latest_candidate else 0
        latest_accuracy = float(latest_candidate_metrics.get("accuracy", 0) or 0)
        latest_reason = str(latest_candidate_metrics.get("promotion_reason", "")) or "waiting_for_training"
        learning = learning_health_summary(engine, rows, model, latest_candidate)
        drift_rows = feature_drift_rows(engine, rows, model, latest_candidate)
        active_model_label = str(model.get("version", "none")) if model else "none"
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.markdown(ui_card("Learning State", plain_status(learning["status"]), learning["inference"], learning["status"]), unsafe_allow_html=True)
        c2.markdown(ui_card("Active Model", active_model_label, "Promoted model in use" if model else "Heuristic fallback only", "GREEN" if model else "RED"), unsafe_allow_html=True)
        c3.markdown(ui_card("Latest Accuracy", f"{latest_accuracy:.1%}", f"Target {CFG.ml_min_accuracy:.0%}", "GREEN" if latest_accuracy >= CFG.ml_min_accuracy else "RED"), unsafe_allow_html=True)
        c4.markdown(ui_card("Prediction Labels", f"{learning['evaluated_predictions']}/{learning['total_predictions']}", "Evaluated live predictions", "GREEN" if learning["evaluated_predictions"] > 0 else "RED"), unsafe_allow_html=True)
        c5.markdown(ui_card("Top Drift", f"{learning['top_feature_drift']:.1f}", f"Block at {CFG.ml_drift_block_threshold:.1f}", "GREEN" if learning["top_feature_drift"] < CFG.ml_drift_warning_threshold else "RED" if learning["top_feature_drift"] >= CFG.ml_drift_block_threshold else "AMBER"), unsafe_allow_html=True)
        st.markdown(
            ui_panel(
                "Current Inference",
                f"<div class='sb-health-row'><span class='sb-dot' style='background:{ui_status_color(learning['status'])}'></span>"
                f"<div><div class='sb-health-title'>{html_lib.escape(learning['inference'].capitalize())}</div>"
                f"<div class='sb-health-sub'>Latest model decision: {html_lib.escape(latest_reason)} | positive rate 24h {learning['positive_rate_24h']:.1%} | avg outcome {learning['avg_return_24h'] * 10000:.1f} bps</div></div>"
                f"<b>{'Use only in training/testnet' if learning['status'] != 'GREEN' else 'Eligible for cautious testnet sizing'}</b></div>",
            ),
            unsafe_allow_html=True,
        )
        if latest_candidate:
            st.markdown(
                ui_panel(
                    "Latest Candidate Decision",
                    f"<div class='sb-health-row'><span class='sb-dot' style='background:{ui_status_color('GREEN' if latest_candidate.get('status') == 'ACTIVE' else 'AMBER')}'></span>"
                    f"<div><div class='sb-health-title'>{html_lib.escape(str(latest_candidate.get('version', '')))}</div>"
                    f"<div class='sb-health-sub'>Status {html_lib.escape(str(latest_candidate.get('status', '')))} | rows {latest_rows} | precision {float(latest_candidate_metrics.get('precision', 0) or 0):.2%} | recall {float(latest_candidate_metrics.get('recall', 0) or 0):.2%} | positive rate {float(latest_candidate_metrics.get('positive_rate', 0) or 0):.2%}</div></div>"
                    f"<b>{html_lib.escape(latest_reason)}</b></div>",
                ),
                unsafe_allow_html=True,
            )
        audit_left, audit_right = st.columns([1.15, 1])
        with audit_left:
            render_detail_table(st, "Learning Usefulness Audit", model_learning_audit_rows(engine))
        with audit_right:
            render_detail_table(st, "Prediction Coverage by Symbol", prediction_coverage_rows(engine))
        l1, l2, l3 = st.columns([1.05, 1, 1])
        with l1:
            st.plotly_chart(model_learning_chart(engine, rows), width="stretch")
        with l2:
            st.plotly_chart(training_growth_chart(engine), width="stretch")
        with l3:
            st.plotly_chart(label_conversion_chart(engine), width="stretch")
        q1, q2 = st.columns([1, 1])
        with q1:
            st.markdown("<div class='sb-panel-title'>Feature Importance</div>", unsafe_allow_html=True)
            st.plotly_chart(feature_importance_chart(model, latest_candidate), width="stretch")
        with q2:
            st.markdown("<div class='sb-panel-title'>Feature Drift</div>", unsafe_allow_html=True)
            st.plotly_chart(feature_drift_chart(engine, rows), width="stretch")
        q3, q4, q5 = st.columns([1, 1, 1])
        with q3:
            st.markdown("<div class='sb-panel-title'>Prediction Confidence Trend</div>", unsafe_allow_html=True)
            st.plotly_chart(prediction_confidence_trend_chart(engine, rows), width="stretch")
        with q4:
            st.markdown("<div class='sb-panel-title'>Hit Rate by Feature Bucket</div>", unsafe_allow_html=True)
            st.plotly_chart(hit_rate_by_feature_bucket_chart(engine), width="stretch")
        with q5:
            render_detail_table(st, "Rolling Validation", rolling_validation_rows(engine))
        q6, q7 = st.columns([1, 1])
        with q6:
            st.markdown("<div class='sb-panel-title'>Expected vs Actual Reversion</div>", unsafe_allow_html=True)
            st.plotly_chart(expected_vs_actual_reversion_chart(engine), width="stretch")
        with q7:
            st.markdown("<div class='sb-panel-title'>Slippage vs Spread</div>", unsafe_allow_html=True)
            st.plotly_chart(slippage_vs_spread_chart(engine), width="stretch")
        q8, q9 = st.columns([1, 1])
        with q8:
            st.markdown("<div class='sb-panel-title'>Funding / Open Interest Pressure</div>", unsafe_allow_html=True)
            st.plotly_chart(funding_oi_pressure_chart(engine, rows), width="stretch")
        with q9:
            st.markdown("<div class='sb-panel-title'>Model Score Waterfall</div>", unsafe_allow_html=True)
            st.plotly_chart(model_score_waterfall_chart(model, best), width="stretch")
        cov_left, cov_right = st.columns([1.05, 1])
        with cov_left:
            render_detail_table(st, "Daily Labeled Outcome Coverage", training_coverage_rows(engine))
        with cov_right:
            render_detail_table(st, "Top Drift Drivers", drift_rows.head(8)[["feature", "drift", "importance", "status"]])
            render_detail_table(st, "Features Used By Entry Confidence Model", model_feature_rows())
        render_detail_table(st, "Model Registry History", model_history_rows(engine))

    elif page == "Journal":
        st.subheader("Journal and Auto Feedback")
        st.markdown("Structured entries explain what the system saw, why it acted or blocked, and what later outcomes taught the model.")
        journal = report.get("journal", {})
        export_frame = journal_export_rows(engine)
        blocker_frame = journal_blocker_summary_rows(engine)
        freshness_frame = journal_freshness_rows(engine)
        coverage_frame = journal_feedback_coverage_rows(engine)
        model_diag_frame = journal_model_diagnostic_rows(engine)
        approved_actions_frame = approved_journal_action_rows(engine)
        config_noise = int(blocker_frame.loc[blocker_frame["Class"].eq("Config gate"), "Events"].sum()) if not blocker_frame.empty else 0
        market_filters = int(blocker_frame.loc[blocker_frame["Class"].eq("Market filter"), "Events"].sum()) if not blocker_frame.empty else 0
        missing_feedback = int((coverage_frame["coverage"] == "Missing").sum()) if not coverage_frame.empty and "coverage" in coverage_frame.columns else 0
        jc = st.columns(6)
        jc[0].markdown(ui_card("Journal Events", str(journal.get("total_7d", 0)), "Last 7 days", "GREEN" if int(journal.get("total_7d", 0) or 0) > 0 else "AMBER"), unsafe_allow_html=True)
        jc[1].markdown(ui_card("Buy Signals", str(journal.get("buy_signals_7d", 0)), "Auto SIG_BUY entries", "GREEN" if int(journal.get("buy_signals_7d", 0) or 0) > 0 else "AMBER"), unsafe_allow_html=True)
        jc[2].markdown(ui_card("Outcome Feedback", str(journal.get("feedback_7d", 0)), "Win/loss feedback labels", "GREEN" if int(journal.get("feedback_7d", 0) or 0) > 0 else "AMBER"), unsafe_allow_html=True)
        jc[3].markdown(ui_card("Config Noise", str(config_noise), "Research/whitelist gates", "AMBER" if config_noise else "GREEN"), unsafe_allow_html=True)
        jc[4].markdown(ui_card("Market Filters", str(market_filters), "ADX/volume/cost gates", "AMBER" if market_filters else "GREEN"), unsafe_allow_html=True)
        jc[5].markdown(ui_card("Missing Labels", str(missing_feedback), "Symbols without feedback", "AMBER" if missing_feedback else "GREEN"), unsafe_allow_html=True)
        st.download_button(
            "Download Journal for Excel",
            data=journal_excel_bytes(export_frame),
            file_name=f"horizon_trading_journal_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xls",
            mime="application/vnd.ms-excel",
        )
        d1, d2 = st.columns([1.15, 1])
        with d1:
            render_detail_table(st, "Blocker Taxonomy", blocker_frame.head(15))
            render_detail_table(st, "Repeated Signal States", journal_repeated_signal_rows(engine, 20))
        with d2:
            render_detail_table(st, "Journal Freshness", freshness_frame)
            render_detail_table(st, "Feedback Coverage by Symbol", coverage_frame)
            render_detail_table(st, "Model Decision Diagnostics", model_diag_frame)
        render_detail_table(st, "Approved Journal Actions", approved_actions_frame)
        j1, j2, j3 = st.columns([1, 1, 1.15])
        with j1:
            st.markdown("<div class='sb-panel-title'>What Is Being Journaled</div>", unsafe_allow_html=True)
            st.plotly_chart(journal_event_mix_chart(engine), width="stretch")
        with j2:
            st.markdown("<div class='sb-panel-title'>Feedback Outcome Mix</div>", unsafe_allow_html=True)
            st.plotly_chart(journal_feedback_outcome_chart(engine), width="stretch")
        with j3:
            st.markdown("<div class='sb-panel-title'>Improvement Themes</div>", unsafe_allow_html=True)
            st.plotly_chart(journal_reason_chart(engine), width="stretch")
        st.markdown("<div class='sb-panel-title'>Journal Activity Over Time</div>", unsafe_allow_html=True)
        st.plotly_chart(journal_timeline_chart(engine), width="stretch")
        left, right = st.columns([1.25, 1])
        with left:
            render_detail_table(st, "Recent Journal Entries", journal_rows(engine))
            render_detail_table(st, "Excel Export Preview", export_frame.head(30))
        with right:
            render_detail_table(st, "Auto Feedback Summary", journal_feedback_summary(engine))
            render_detail_table(st, "Journal Code Glossary", journal_code_rows())

    elif page == "Trades":
        st.subheader("Trades and Deployment")
        selected = st.selectbox("Replay Symbol", CFG.symbols, key="selected_symbol_trades")
        candidate = next((r for r in rows if r["symbol"] == selected), rows[0])
        approval = st.checkbox("Manual approval for paper deployment")
        can_deploy = approval and candidate["deployable"] and validation["validation_status"] == "GREEN" and risk.get("status") == "RISK_OK" and drift.get("status") != "DRIFT_LOCKED"
        deploy_cols = st.columns(3)
        deploy_cols[0].markdown(ui_card("Selected Symbol", selected, candidate.get("side", "HOLD"), "GREEN" if candidate.get("deployable") else "AMBER"), unsafe_allow_html=True)
        deploy_cols[1].markdown(ui_card("Suggested Size", f"${float(candidate.get('suggested_usdt', 0)):,.0f}", "Risk capped", "GREEN"), unsafe_allow_html=True)
        deploy_cols[2].markdown(ui_card("Deploy Gate", "Open" if can_deploy else "Closed", "Needs signal, validation, approval", "GREEN" if can_deploy else "AMBER"), unsafe_allow_html=True)
        if st.button("Deploy Paper Position", disabled=not can_deploy):
            request_key = f"deploy:paper:{candidate['symbol']}:{int(time.time())}:{uuid4().hex[:8]}"
            request_payload = {**candidate, "manual_approval": True, "requested_at": iso_now(), "requested_by": "human", "validation_status": validation["validation_status"]}
            db_execute(
                engine,
                """INSERT IGNORE INTO deployment_requests(idempotency_key, signal_key, symbol, side, requested_size_usdt, requested_price, mode, status, requested_by, request_json, created_at)
                   VALUES(:key, :signal_key, :symbol, :side, :size, :price, 'PAPER', 'PENDING', 'human', :payload, :ts)""",
                {"key": request_key, "signal_key": candidate.get("idempotency_key", ""), "symbol": candidate["symbol"], "side": candidate["side"], "size": candidate["suggested_usdt"], "price": candidate["price"], "payload": json.dumps(request_payload), "ts": now_utc().replace(tzinfo=None)},
            )
            state.set_json("latest_deployment_request", {"idempotency_key": request_key, **request_payload}, ex=600)
            st.success("Deployment request queued. worker-order will re-check risk and create the paper order.")
        st.checkbox("Enable real Spot Testnet order confirmation", disabled=not CFG.enable_real_testnet_orders)
        st.button("Place Tiny Testnet Order", disabled=not (CFG.enable_real_testnet_orders and testnet_credentials_present() and can_deploy))
        render_detail_table(st, "Deployment Queue", deployment_queue_rows(engine))
        render_detail_table(st, "Recent Positions", pd.DataFrame(report.get("positions", [])))

    elif page == "Risk Monitor":
        st.subheader("Risk Monitor")
        c1, c2, c3 = st.columns(3)
        c1.markdown(ui_card("Risk State", risk.get("status", "UNKNOWN"), "Deployment safety gate", "GREEN" if risk.get("status") == "RISK_OK" else "RED"), unsafe_allow_html=True)
        c2.markdown(ui_card("Drift State", drift.get("status", "UNKNOWN"), f"Score {float(drift.get('drift_score', 0) or 0):.0f}/100", "GREEN" if drift.get("status") == "OK" else "AMBER"), unsafe_allow_html=True)
        c3.markdown(ui_card("Current DD", f"{float(pnl.get('current_dd_pct', 0) or 0):.2f}%", "Portfolio drawdown", "GREEN" if float(pnl.get("current_dd_pct", 0) or 0) <= CFG.max_portfolio_dd_pct else "RED"), unsafe_allow_html=True)
        render_detail_table(st, "Alerts", pd.DataFrame(alerts))

    elif page == "Funding & PnL":
        st.subheader("Funding and P&L")
        report_pnl = report.get("pnl", {})
        p = st.columns(5)
        for col, (label, value, status) in zip(
            p,
            [
                ("Paper P&L", format_money(report_pnl.get("paper_unrealized", pnl.get("unrealized_pnl", 0.0))), pnl_status(report_pnl.get("paper_unrealized", pnl.get("unrealized_pnl", 0.0)))),
                ("Live P&L", format_money(report_pnl.get("live_unrealized", 0.0)), pnl_status(report_pnl.get("live_unrealized", 0.0))),
                ("Daily P&L", format_money(report_pnl.get("daily", pnl.get("daily_pnl", 0.0))), pnl_status(report_pnl.get("daily", pnl.get("daily_pnl", 0.0)))),
                ("Equity", format_money(report_pnl.get("equity", pnl.get("equity", CFG.starting_equity)), 2), "GREEN"),
                ("Funding Pressure", f"{best['funding_pressure']:.2f}", "AMBER" if abs(best["funding_pressure"]) > 0.4 else "GREEN"),
            ],
        ):
            col.markdown(ui_card(label, value, "Latest state", status), unsafe_allow_html=True)
        st.plotly_chart(pnl_learning_chart(engine, pnl), width="stretch")

    elif page == "Scanners":
        st.subheader("Scanners")
        render_scanner_command_center(st, rows, compact=False)
        render_detail_table(st, "Market Scanner", df[scanner_cols])

    elif page == "Alerts":
        st.subheader("Alerts")
        render_detail_table(st, "Alerts and Recommended Actions", pd.DataFrame(alerts))
        render_detail_table(st, "Audit", audit_rows(engine))

    elif page == "Reports":
        st.subheader("Reports")
        st.json(report)

    elif page == "Settings":
        st.subheader("Settings")
        settings = pd.DataFrame(
            [
                {"Setting": "SYSTEM_STAGE", "Value": CFG.system_stage, "Meaning": "Training, testnet, or production operating stage"},
                {"Setting": "ENABLE_REAL_TESTNET_ORDERS", "Value": str(CFG.enable_real_testnet_orders), "Meaning": "Whether tiny Spot Testnet orders are allowed"},
                {"Setting": "MEAN_REVERSION_RESEARCH_ONLY", "Value": str(CFG.mean_reversion_research_only), "Meaning": "Blocks production deployment when true"},
                {"Setting": "MIN_ML_CONFIDENCE", "Value": f"{CFG.min_ml_confidence:.2f}", "Meaning": "Minimum model confidence before deployment"},
                {"Setting": "MAX_POSITION_USDT", "Value": f"{CFG.max_position_usdt:.0f}", "Meaning": "Maximum position size"},
            ]
        )
        render_detail_table(st, "Runtime Settings", settings)

    st.caption(f"pipeline_time=local | cost_per_hypothesis=paper | deploy_rate=manual | pid={os.getpid()} | seed=dynamic | runtime={CFG.run_mode}")


def main() -> None:
    cli_modes = {"ui", "marketdata", "market-check", "validation", "validation-once", "signal", "risk", "order", "ml", "pnl", "report", "migrate-db"}
    mode = sys.argv[1].strip().lower() if len(sys.argv) > 1 and sys.argv[1].strip().lower() in cli_modes else CFG.run_mode
    if mode == "ui":
        if "streamlit" not in sys.modules:
            os.execvp("streamlit", ["streamlit", "run", __file__, "--server.address=0.0.0.0", "--server.port=8501"])
        render_ui()
    elif mode == "marketdata":
        run_marketdata()
    elif mode == "market-check":
        raise SystemExit(market_feed_check())
    elif mode == "validation":
        run_validation()
    elif mode == "validation-once":
        raise SystemExit(run_validation_once())
    elif mode == "signal":
        run_signal()
    elif mode == "risk":
        run_risk()
    elif mode == "order":
        run_order()
    elif mode == "ml":
        run_ml()
    elif mode == "pnl":
        run_pnl()
    elif mode == "report":
        state, engine = RedisState(CFG), db_engine(CFG)
        print_performance_report(performance_report(state, engine), as_json="--json" in sys.argv)
    elif mode == "migrate-db":
        raise SystemExit(migrate_database(db_engine(CFG)))
    else:
        raise SystemExit(f"Unknown RUN_MODE={CFG.run_mode}")


if __name__ == "__main__":
    main()
