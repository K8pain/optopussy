#!/usr/bin/env python3
"""Monolithic workflow to normalize Deribit snapshots, store in Postgres, and backtest iron condors.

Usage:
  python samples/deribit_ironcondor_monolith.py \
      --input /path/to/deribit_snapshot.csv \
      --postgres-url postgresql+psycopg2://user:pass@localhost:5432/deribit \
      --schema market
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

import numpy as np
import pandas as pd

import optopsy as op


def _safe_float(value):
    if pd.isna(value):
        return np.nan
    if isinstance(value, str):
        raw = value.strip().lower()
        if raw in {"", "nan", "none", "null"}:
            return np.nan
    try:
        return float(value)
    except Exception:
        return np.nan


def _best_price_from_ladder(raw_ladder, index: int) -> float:
    if pd.isna(raw_ladder) or raw_ladder in ("[]", ""):
        return np.nan
    try:
        ladder = ast.literal_eval(raw_ladder) if isinstance(raw_ladder, str) else raw_ladder
        if isinstance(ladder, list) and ladder:
            return _safe_float(ladder[0][index])
    except Exception:
        return np.nan
    return np.nan


def _split_instrument(instrument_name: str):
    # Example: ETH-13AUG25-3800-C
    parts = instrument_name.split("-")
    if len(parts) != 4:
        return None, None, None, None
    symbol, expiry_raw, strike_raw, side_raw = parts
    try:
        expiration = pd.to_datetime(expiry_raw, format="%d%b%y")
    except Exception:
        expiration = pd.NaT
    option_type = "call" if side_raw.upper() == "C" else "put"
    return symbol, expiration, _safe_float(strike_raw), option_type


def normalize_deribit_csv(input_path: Path) -> pd.DataFrame:
    src = pd.read_csv(input_path)

    parsed = src["instrument_name"].apply(_split_instrument)
    src["underlying_symbol"] = parsed.apply(lambda x: x[0])
    src["expiration"] = parsed.apply(lambda x: x[1])
    src["strike"] = parsed.apply(lambda x: x[2])
    src["option_type"] = parsed.apply(lambda x: x[3])

    src["quote_date"] = pd.to_datetime(src["timestamp"], unit="ms", errors="coerce")

    src["bid"] = src["best_bid_price"].apply(_safe_float)
    src["ask"] = src["best_ask_price"].apply(_safe_float)

    bid_missing = src["bid"].isna() | (src["bid"] <= 0)
    ask_missing = src["ask"].isna() | (src["ask"] <= 0)

    src.loc[bid_missing, "bid"] = src.loc[bid_missing, "bids"].apply(lambda x: _best_price_from_ladder(x, 0))
    src.loc[ask_missing, "ask"] = src.loc[ask_missing, "asks"].apply(lambda x: _best_price_from_ladder(x, 0))

    src["delta"] = src.get("greeks.delta", np.nan).apply(_safe_float)
    src["volume"] = src.get("stats.volume", np.nan).apply(_safe_float)

    numeric_cols = [
        "underlying_price",
        "index_price",
        "mark_price",
        "open_interest",
        "mark_iv",
        "bid_iv",
        "ask_iv",
    ]
    for col in numeric_cols:
        if col in src.columns:
            src[col] = src[col].apply(_safe_float)

    normalized = src[
        [
            "timestamp",
            "change_id",
            "underlying_symbol",
            "underlying_price",
            "index_price",
            "instrument_name",
            "option_type",
            "expiration",
            "quote_date",
            "strike",
            "bid",
            "ask",
            "best_bid_price",
            "best_ask_price",
            "best_bid_amount",
            "best_ask_amount",
            "mark_price",
            "last_price",
            "open_interest",
            "mark_iv",
            "bid_iv",
            "ask_iv",
            "delta",
            "volume",
            "settlement_period",
        ]
    ].copy()

    normalized = normalized.dropna(subset=["underlying_symbol", "expiration", "quote_date", "strike", "option_type"])
    normalized = normalized[(normalized["bid"] > 0) & (normalized["ask"] > 0)]

    return normalized


def run_iron_condor_backtest(df: pd.DataFrame):
    grouped = op.iron_condor(
        df,
        dte_interval=7,
        max_entry_dte=60,
        exit_dte=7,
        otm_pct_interval=0.02,
        max_otm_pct=0.25,
        min_bid_ask=0.0001,
        slippage="liquidity",
        fill_ratio=0.45,
        reference_volume=500,
    ).sort_values(["count", "mean"], ascending=[False, False])

    raw = op.iron_condor(
        df,
        raw=True,
        max_entry_dte=60,
        exit_dte=7,
        max_otm_pct=0.25,
        min_bid_ask=0.0001,
        slippage="liquidity",
        fill_ratio=0.45,
        reference_volume=500,
    )

    return grouped, raw


def persist_to_postgres(df: pd.DataFrame, grouped: pd.DataFrame, raw: pd.DataFrame, postgres_url: str, schema: str):
    from sqlalchemy import create_engine, text

    engine = create_engine(postgres_url)

    with engine.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))

    df.to_sql("deribit_option_ticks", engine, schema=schema, if_exists="append", index=False, chunksize=5000, method="multi")
    grouped.to_sql("iron_condor_stats", engine, schema=schema, if_exists="replace", index=False)
    raw.to_sql("iron_condor_trades", engine, schema=schema, if_exists="replace", index=False)

    superset_sql = f"""
    CREATE OR REPLACE VIEW {schema}.v_iron_condor_daily AS
    SELECT
      date_trunc('day', quote_date) AS day,
      underlying_symbol,
      COUNT(*) AS ticks,
      AVG(mark_iv) AS avg_mark_iv,
      AVG(open_interest) AS avg_open_interest,
      AVG(underlying_price) AS avg_underlying_price
    FROM {schema}.deribit_option_ticks
    GROUP BY 1, 2;

    CREATE OR REPLACE VIEW {schema}.v_iron_condor_backtest AS
    SELECT
      dte_range,
      otm_pct_range_leg1,
      otm_pct_range_leg2,
      otm_pct_range_leg3,
      otm_pct_range_leg4,
      count,
      mean,
      std,
      min,
      max
    FROM {schema}.iron_condor_stats;
    """

    with engine.begin() as conn:
        for stmt in [s.strip() for s in superset_sql.split(";") if s.strip()]:
            conn.execute(text(stmt))


def main():
    parser = argparse.ArgumentParser(description="Deribit -> PostgreSQL -> Iron Condor backtest pipeline")
    parser.add_argument("--input", required=True, type=Path, help="CSV exported from Deribit downloader")
    parser.add_argument("--postgres-url", default=None, help="SQLAlchemy URL for PostgreSQL")
    parser.add_argument("--schema", default="market", help="Destination schema for PostgreSQL")
    parser.add_argument("--output-dir", default=Path("artifacts"), type=Path, help="Local folder for outputs")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    normalized = normalize_deribit_csv(args.input)
    grouped, raw = run_iron_condor_backtest(normalized)

    normalized_path = args.output_dir / "normalized_deribit.parquet"
    normalized_csv_path = args.output_dir / "normalized_deribit.csv"
    grouped_path = args.output_dir / "iron_condor_stats.csv"
    raw_path = args.output_dir / "iron_condor_trades.csv"

    try:
        normalized.to_parquet(normalized_path, index=False)
    except Exception:
        normalized.to_csv(normalized_csv_path, index=False)
    grouped.to_csv(grouped_path, index=False)
    raw.to_csv(raw_path, index=False)

    if args.postgres_url:
        persist_to_postgres(normalized, grouped, raw, args.postgres_url, args.schema)

    preview = {
        "normalized_rows": int(len(normalized)),
        "grouped_rows": int(len(grouped)),
        "raw_rows": int(len(raw)),
        "grouped_preview": grouped.head(5).to_dict(orient="records"),
    }
    print(json.dumps(preview, default=str, indent=2))


if __name__ == "__main__":
    main()
