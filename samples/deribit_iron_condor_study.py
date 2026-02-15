"""Monolithic study script for Deribit options data + Iron Condor backtesting.

Usage:
    python samples/deribit_iron_condor_study.py --input deribit_snapshot.csv
"""

from __future__ import annotations

import argparse
import ast
import re
from typing import Any, Optional

import numpy as np
import optopsy as op
import pandas as pd

INSTRUMENT_RE = re.compile(
    r"^(?P<underlying>[A-Z]+)-(?P<expiration>\d{1,2}[A-Z]{3}\d{2})-(?P<strike>[0-9]+(?:\.[0-9]+)?)-(?P<option>[CP])$"
)


def _parse_l1_from_book(value: Any) -> Optional[float]:
    """Extract first-level price from Deribit bids/asks JSON-like value."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None

    if isinstance(value, list):
        book = value
    else:
        raw = str(value).strip()
        if not raw or raw == "[]" or raw.lower() == "nan":
            return None
        try:
            book = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            return None

    if not book:
        return None

    first = book[0]
    if isinstance(first, (list, tuple)) and len(first) >= 1:
        return float(first[0])
    return None


def _to_optopsy_frame(raw: pd.DataFrame) -> pd.DataFrame:
    df = raw.copy()

    instrument_parts = df["instrument_name"].str.extract(INSTRUMENT_RE)
    df = pd.concat([df, instrument_parts], axis=1)

    # Drop malformed instruments (e.g. futures/perpetual rows)
    df = df.dropna(subset=["underlying", "expiration", "strike", "option"])

    df["quote_date"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True).dt.tz_localize(None)
    df["expiration"] = pd.to_datetime(df["expiration"], format="%d%b%y", errors="coerce")
    df = df.dropna(subset=["expiration"])

    df["option_type"] = df["option"].map({"C": "call", "P": "put"})
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")

    df["bid"] = pd.to_numeric(df.get("best_bid_price"), errors="coerce")
    df["ask"] = pd.to_numeric(df.get("best_ask_price"), errors="coerce")

    # Fallback to the first level in bids/asks arrays if top-of-book columns are empty
    df.loc[df["bid"].isna() | (df["bid"] <= 0), "bid"] = (
        df.loc[df["bid"].isna() | (df["bid"] <= 0), "bids"].apply(_parse_l1_from_book)
    )
    df.loc[df["ask"].isna() | (df["ask"] <= 0), "ask"] = (
        df.loc[df["ask"].isna() | (df["ask"] <= 0), "asks"].apply(_parse_l1_from_book)
    )

    df["underlying_price"] = pd.to_numeric(df.get("underlying_price"), errors="coerce")
    df["underlying_price"] = df["underlying_price"].fillna(pd.to_numeric(df.get("index_price"), errors="coerce"))

    # Optional enrichments used by Optopsy filters/slippage
    df["delta"] = pd.to_numeric(df.get("greeks.delta"), errors="coerce")
    df["volume"] = pd.to_numeric(df.get("stats.volume"), errors="coerce")
    df["open_interest"] = pd.to_numeric(df.get("open_interest"), errors="coerce")

    df = df[
        [
            "underlying",
            "underlying_price",
            "option_type",
            "expiration",
            "quote_date",
            "strike",
            "bid",
            "ask",
            "delta",
            "volume",
            "open_interest",
        ]
    ].rename(columns={"underlying": "underlying_symbol"})

    df = df.dropna(subset=["underlying_symbol", "underlying_price", "strike", "bid", "ask"])
    df = df[(df["bid"] > 0) & (df["ask"] > 0)]

    return df.sort_values(["quote_date", "expiration", "strike", "option_type"]).reset_index(drop=True)


def run_study(input_csv: str) -> None:
    raw = pd.read_csv(input_csv)
    data = _to_optopsy_frame(raw)

    if data.empty:
        raise ValueError("No valid option rows after Deribit -> Optopsy normalization.")

    print("=" * 90)
    print("DERIBIT IRON CONDOR STUDY")
    print("=" * 90)
    print(f"Input rows: {len(raw):,} | Normalized rows: {len(data):,}")
    print(f"Date range: {data['quote_date'].min()} -> {data['quote_date'].max()}")
    print(f"Expirations: {data['expiration'].nunique()} | Underlyings: {data['underlying_symbol'].nunique()}")

    # Base scenario: midpoint fills
    base = op.iron_condor(
        data,
        dte_interval=7,
        max_entry_dte=45,
        exit_dte=0,
        otm_pct_interval=0.05,
        max_otm_pct=0.30,
        min_bid_ask=0.0001,
        slippage="mid",
    ).sort_values("mean", ascending=False)

    # Stress scenario: full-spread fills
    stress = op.iron_condor(
        data,
        dte_interval=7,
        max_entry_dte=45,
        exit_dte=0,
        otm_pct_interval=0.05,
        max_otm_pct=0.30,
        min_bid_ask=0.0001,
        slippage="spread",
    ).sort_values("mean", ascending=False)

    # Raw trades to inspect PnL tails
    raw_trades = op.iron_condor(
        data,
        max_entry_dte=45,
        max_otm_pct=0.30,
        min_bid_ask=0.0001,
        raw=True,
    )

    print("\n--- Example result A: best grouped buckets (mid fills) ---")
    print(base.head(8).round(4).to_string(index=False))

    print("\n--- Example result B: best grouped buckets (spread fills) ---")
    print(stress.head(8).round(4).to_string(index=False))

    print("\n--- Trade distribution snapshot ---")
    print(raw_trades["pct_change"].describe(percentiles=[0.05, 0.25, 0.5, 0.75, 0.95]).round(4).to_string())

    top3 = raw_trades.nlargest(3, "pct_change")
    worst3 = raw_trades.nsmallest(3, "pct_change")

    cols = [
        "underlying_symbol",
        "expiration",
        "dte_entry",
        "strike_leg1",
        "strike_leg2",
        "strike_leg3",
        "strike_leg4",
        "pct_change",
    ]

    print("\n--- Top 3 trades ---")
    print(top3[cols].round(4).to_string(index=False))
    print("\n--- Worst 3 trades ---")
    print(worst3[cols].round(4).to_string(index=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="Deribit Iron Condor monolithic study script")
    parser.add_argument("--input", required=True, help="Path to CSV with Deribit downloader columns")
    args = parser.parse_args()
    run_study(args.input)


if __name__ == "__main__":
    main()
