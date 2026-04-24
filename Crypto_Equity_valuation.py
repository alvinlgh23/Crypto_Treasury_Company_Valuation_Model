"""
Crypto-Native Multi-Asset Equity Valuation Model
=================================================
Interactive, general-purpose version.

Usage:
    python crypto_equity_valuation.py            # interactive mode (prompts for ticker)
    python crypto_equity_valuation.py MSTR       # single ticker
    python crypto_equity_valuation.py MSTR BMNR  # multiple tickers

How it works:
    1. Fetches live BTC/ETH prices from CoinGecko
    2. Detects whether the company is a crypto treasury firm by scraping
       their BTC/ETH holdings from yfinance + public APIs
    3. If it IS a treasury company → full NAV / mNAV / yield / scoring analysis
    4. If it is NOT → "Not a crypto treasury company" with a brief explanation

Install deps:
    pip install requests yfinance rich
"""

import sys
import math
import json
import re
import requests
import yfinance as yf
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime

try:
    from rich.console import Console
    from rich.table import Table
    from rich import box
    from rich.panel import Panel
    from rich.columns import Columns

    RICH = True
    console = Console()
except ImportError:
    RICH = False
    console = None

# ─────────────────────────────────────────────────────────────
# PARAMETERS  (mirrors xlsx parameter block)
# ─────────────────────────────────────────────────────────────

ENTRY_THRESHOLD = 0.02  # minimum spread to open a position
EXIT_THRESHOLD = 0.005  # minimum |spread| to keep trade_enabled
MAX_POSITION = 0.40  # final position cap (40%)
SIZE_SCALAR = 0.20  # base position size scalar (±20%)
SIZE_CAP_MULT = 2.0  # max multiplier on spread/entry ratio
ETH_YIELD_RATE = 0.04  # baseline ETH staking yield (4%)

# Minimum crypto holdings to qualify as a treasury company
MIN_BTC_THRESHOLD = 10  # coins
MIN_ETH_THRESHOLD = 1_000  # coins


# ─────────────────────────────────────────────────────────────
# KNOWN TREASURY REGISTRY
# Hard-coded for tickers where on-chain data is authoritative.
# yfinance does not report crypto holdings — this is the
# canonical source. Add new names here as they emerge.
# ─────────────────────────────────────────────────────────────

@dataclass
class TreasuryProfile:
    btc_held: float = 0.0
    eth_held: float = 0.0
    eth_staked_pct: float = 0.0  # fraction of ETH that is staked
    operating_drag: float = 0.01  # annual opex as fraction of NAV


KNOWN_TREASURY: dict[str, TreasuryProfile] = {
    "MSTR": TreasuryProfile(btc_held=815_061, eth_held=0, eth_staked_pct=0.8, operating_drag=0.01),
    "MARA": TreasuryProfile(btc_held=47_531, eth_held=0, eth_staked_pct=0.0, operating_drag=0.015),
    "RIOT": TreasuryProfile(btc_held=19_223, eth_held=0, eth_staked_pct=0.0, operating_drag=0.02),
    "CLSK": TreasuryProfile(btc_held=10_126, eth_held=0, eth_staked_pct=0.0, operating_drag=0.02),
    "COIN": TreasuryProfile(btc_held=9_480, eth_held=0, eth_staked_pct=0.0, operating_drag=0.01),
    "BMNR": TreasuryProfile(btc_held=199, eth_held=4_976_485, eth_staked_pct=1.0, operating_drag=0.02),
    "SMLR": TreasuryProfile(btc_held=3_192, eth_held=0, eth_staked_pct=0.0, operating_drag=0.025),
    "HUT": TreasuryProfile(btc_held=10_167, eth_held=0, eth_staked_pct=0.0, operating_drag=0.02),
    "BTBT": TreasuryProfile(btc_held=1_049, eth_held=0, eth_staked_pct=0.0, operating_drag=0.03),
}


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def _isnan(v) -> bool:
    try:
        return math.isnan(v)
    except:
        return False


def _pick(live, fallback):
    if live is not None and live != 0 and not _isnan(live):
        return live
    return fallback


def _safe(v, fallback=0.0):
    return fallback if (v is None or _isnan(v)) else v


def _pct(v, d=2):
    if v is None or _isnan(v): return "n/a"
    return f"{v * 100:+.{d}f}%"


def _pct_abs(v, d=2):
    if v is None or _isnan(v): return "n/a"
    return f"{v * 100:.{d}f}%"


def _usd(v, d=2):
    if v is None or _isnan(v): return "n/a"
    if abs(v) >= 1e9: return f"${v / 1e9:,.2f}B"
    if abs(v) >= 1e6: return f"${v / 1e6:,.2f}M"
    if abs(v) >= 1e3: return f"${v / 1e3:,.1f}K"
    return f"${v:,.{d}f}"


def _mult(v, d=3):
    if v is None or _isnan(v): return "n/a"
    return f"{v:.{d}f}×"


def _coins(v):
    if v is None or _isnan(v): return "n/a"
    return f"{v:,.0f}"


# ─────────────────────────────────────────────────────────────
# 1. LIVE DATA FETCHING
# ─────────────────────────────────────────────────────────────

def fetch_crypto_prices() -> dict[str, float]:
    url = ("https://api.coingecko.com/api/v3/simple/price"
           "?ids=bitcoin,ethereum&vs_currencies=usd")
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        d = r.json()
        return {"BTC": d["bitcoin"]["usd"], "ETH": d["ethereum"]["usd"]}
    except Exception as e:
        _warn(f"CoinGecko failed: {e}. Using fallback prices.")
        return {"BTC": 79_273.43, "ETH": 2_410.44}


def fetch_equity(ticker: str) -> dict:
    """Pull shares, market_cap, cash, debt from yfinance."""
    try:
        t = yf.Ticker(ticker)
        info = t.info

        shares = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")
        market_cap = info.get("marketCap")

        cash = debt = None
        for bs in [t.quarterly_balance_sheet, t.balance_sheet]:
            if bs is None or bs.empty: continue
            for k in ["Cash And Cash Equivalents",
                      "Cash Cash Equivalents And Short Term Investments",
                      "Cash And Short Term Investments"]:
                if k in bs.index:
                    v = float(bs.loc[k].iloc[0])
                    if not _isnan(v): cash = v; break
            for k in ["Total Debt", "Long Term Debt And Capital Lease Obligation",
                      "Long Term Debt", "Current Debt And Capital Lease Obligation"]:
                if k in bs.index:
                    v = float(bs.loc[k].iloc[0])
                    if not _isnan(v): debt = v; break
            if cash is not None and debt is not None: break

        if cash is None:
            raw = info.get("totalCash") or info.get("cash")
            if raw and not _isnan(raw): cash = float(raw)
        if debt is None:
            raw = info.get("totalDebt")
            debt = float(raw) if (raw and not _isnan(raw)) else 0.0

        return {
            "shares": shares, "market_cap": market_cap,
            "cash": cash, "debt": debt,
            "name": info.get("longName") or info.get("shortName") or ticker,
            "sector": info.get("sector", ""),
            "industry": info.get("industry", ""),
            "currency": info.get("currency", "USD"),
        }
    except Exception as e:
        _warn(f"yfinance failed for {ticker}: {e}")
        return {"shares": None, "market_cap": None, "cash": None, "debt": None,
                "name": ticker, "sector": "", "industry": "", "currency": "USD"}


# ─────────────────────────────────────────────────────────────
# 2. TREASURY DETECTION
# ─────────────────────────────────────────────────────────────

def detect_treasury(ticker: str, eq: dict) -> tuple[bool, TreasuryProfile | None, str]:
    """
    Returns (is_treasury, profile, reason).

    Detection logic:
      1. Ticker is in KNOWN_TREASURY registry → definitive YES
      2. sector/industry keywords suggest crypto exposure → flag for manual review
      3. Otherwise → NOT a treasury company
    """
    ticker_up = ticker.upper()

    # ── Hard registry hit ──
    if ticker_up in KNOWN_TREASURY:
        p = KNOWN_TREASURY[ticker_up]
        if p.btc_held >= MIN_BTC_THRESHOLD or p.eth_held >= MIN_ETH_THRESHOLD:
            return True, p, "found in known treasury registry"

    # ── Heuristic: industry/sector keywords ──
    text = f"{eq.get('sector', '')} {eq.get('industry', '')}".lower()
    crypto_keywords = {"bitcoin", "crypto", "digital asset", "blockchain",
                       "mining", "ethereum", "web3", "defi"}
    matched = crypto_keywords & set(re.split(r'\W+', text))
    if matched:
        # It's crypto-adjacent but not in our registry — flag as partial
        return False, None, (
            f"crypto-adjacent industry ({', '.join(matched)}) but no verified "
            f"BTC/ETH treasury holdings on record — add to KNOWN_TREASURY if confirmed"
        )

    return False, None, "no crypto treasury holdings detected"


# ─────────────────────────────────────────────────────────────
# 3. VALUATION ENGINE
# ─────────────────────────────────────────────────────────────

@dataclass
class ValuationResult:
    ticker: str
    name: str
    is_treasury: bool
    not_applicable_reason: str = ""

    # prices
    btc_price: float = 0.0
    eth_price: float = 0.0

    # holdings
    btc_held: float = 0.0
    eth_held: float = 0.0
    btc_value: float = 0.0
    eth_value: float = 0.0
    btc_crypto_pct: float = 0.0
    eth_crypto_pct: float = 0.0

    # equity
    shares: float = 0.0
    market_cap: float = 0.0
    cash: float = 0.0
    debt: float = 0.0

    # NAV block
    total_nav: float = 0.0
    nav_per_share: float = 0.0
    mnav: float = 0.0  # market_cap / NAV  (multiple)
    adjusted_mnav: float = 0.0
    nav_discount: float = 0.0  # AG: (market_cap/NAV) - 1

    # yield module
    staked_eth: float = 0.0
    annual_yield_usd: float = 0.0
    yield_per_share: float = 0.0
    yield_yield_pct: float = 0.0
    adjusted_yield_pct: float = 0.0
    spread: float = 0.0
    real_yield_mkt_adj: float = 0.0

    # signal
    signal: str = ""
    confidence: float = 0.0
    mispricing_pct: float = 0.0

    # trade sizing
    trade_enabled: bool = False
    position_size: float = 0.0
    normalized_weight: float = 0.0
    final_position: float = 0.0

    # composite score  (AK / AL)
    valuation_score: float = 0.0  # AL = spread
    final_score: float = 0.0  # AK = weighted composite

    # recommendation
    recommendation: str = ""
    score_label: str = ""


def _unified_label(trade_enabled: bool, position: float, score: float) -> str:
    """
    Single source of truth for the rating.
    Execution gate comes first — if trade is off or position is zero → Neutral.
    Score magnitude upgrades Attractive to Strong Attractive, Expensive to Very Expensive.
    """
    if not trade_enabled or position == 0.0:
        return "NEUTRAL"
    if position > 0:
        return "VERY ATTRACTIVE" if score >= 0.04 else "ATTRACTIVE"
    else:
        return "VERY EXPENSIVE" if score <= -0.04 else "EXPENSIVE"


def _score_btc_treasury(mnav: float, nav_disc: float) -> tuple[float, str]:
    """
    Scoring for BTC-only treasuries (no ETH staking yield).

    Value driver = mNAV premium/discount to NAV.
    Investors pay a premium for the BTC exposure wrapper — that's normal.
    The question is whether the premium is excessive relative to history.

    mNAV bands (calibrated to MSTR historical range ~0.9x–3.5x):
      < 1.0   → trading at NAV discount  → STRONG BUY (rare opportunity)
      1.0–1.5 → modest premium           → BUY
      1.5–2.0 → fair premium             → HOLD
      2.0–2.5 → elevated premium         → SELL
      > 2.5   → extreme premium          → STRONG SELL

    Score is mapped to the same [-0.3, +0.3] range used by ETH branch
    so portfolio sizing stays comparable.
    """
    if _isnan(mnav) or mnav <= 0:
        return 0.0, "insufficient data to score mNAV"

    # Linear score: centered at mNAV=1.5 (reasonable fair value premium)
    # Score = 0 at mNAV=1.5, +0.3 at mNAV≤1.0, -0.3 at mNAV≥2.5
    raw_score = (1.5 - mnav) / (2.5 - 1.0) * 0.3  # clamp to ±0.3
    score = max(-0.3, min(0.3, raw_score))

    if mnav < 1.0:
        rec = f"trading at a {abs(nav_disc) * 100:.1f}% discount to BTC NAV — rare entry opportunity"
    elif mnav < 1.5:
        rec = f"mNAV {mnav:.2f}×, modest premium, reasonable BTC wrapper cost"
    elif mnav < 2.0:
        rec = f"mNAV {mnav:.2f}×, fair premium range, no strong edge either way"
    elif mnav < 2.5:
        rec = f"mNAV {mnav:.2f}×, elevated premium — cheaper to buy BTC directly"
    else:
        rec = f"mNAV {mnav:.2f}×, extreme premium — significant downside risk if BTC corrects"

    return score, rec


def _score_eth_treasury(adj_yld: float, spread: float, mispricing: float,
                        mnav: float, nav_disc: float) -> tuple[float, str]:
    """
    Scoring for ETH-yield treasuries (BMNR-style).

    Value drivers = staking yield spread + NAV discount/premium.
    Uses the original xlsx AK formula but with a proper mNAV penalty
    so high-premium ETH treasuries aren't blindly rated STRONG BUY.

    Components:
      50% — adjusted yield (cash yield quality)
      30% — spread vs benchmark ETH rate (alpha over raw staking)
      20% — mNAV discount signal (negative if trading at premium)
    """
    # mNAV component: +score when trading below NAV (discount), -score at premium
    # Same band logic as BTC branch, but softer weight
    mnav_score = max(-0.3, min(0.3, (1.5 - mnav) / (2.5 - 1.0) * 0.3)) if (not _isnan(mnav) and mnav > 0) else 0.0

    score = (adj_yld * 0.5) + (spread * 0.3) + (mnav_score * 0.2)
    score = max(-0.3, min(0.3, score))  # clamp to same range as BTC branch

    if score >= 0.04:
        rec = (f"strong staking yield spread ({spread * 100:+.2f}%) "
               f"with mNAV {mnav:.2f}×")
    elif score >= 0.01:
        rec = f"positive yield edge, mNAV {mnav:.2f}×"
    elif score >= -0.01:
        rec = f"yield spread marginal or offset by NAV premium ({mnav:.2f}×)"
    elif score >= -0.04:
        rec = (f"yield underperforms benchmark; "
               f"mNAV {mnav:.2f}× unjustified by spread ({spread * 100:+.2f}%)")
    else:
        rec = (f"deeply negative spread ({spread * 100:+.2f}%) "
               f"at {mnav:.2f}× NAV premium")

    return score, rec


def _build_recommendation(label: str, rec_detail: str) -> str:
    """Combine the unified label with the detail string into a human-readable sentence."""
    return f"{label} — {rec_detail}"


def compute(
        ticker: str,
        profile: TreasuryProfile,
        eq: dict,
        btc_price: float,
        eth_price: float,
) -> ValuationResult:
    res = ValuationResult(
        ticker=ticker.upper(),
        name=eq.get("name", ticker),
        is_treasury=True,
        btc_price=btc_price, eth_price=eth_price,
        btc_held=profile.btc_held, eth_held=profile.eth_held,
    )

    shares = _pick(eq["shares"], None)
    market_cap = _pick(eq["market_cap"], None)
    cash = _safe(eq["cash"], 0.0)
    debt = _safe(eq["debt"], 0.0)

    if not shares or not market_cap:
        res.not_applicable_reason = "could not fetch shares / market cap from yfinance"
        res.is_treasury = False
        return res

    res.shares = shares
    res.market_cap = market_cap
    res.cash = cash
    res.debt = debt

    # ── holdings ──
    btc_val = profile.btc_held * btc_price
    eth_val = profile.eth_held * eth_price
    total_crypto = btc_val + eth_val
    res.btc_value = btc_val
    res.eth_value = eth_val
    res.btc_crypto_pct = btc_val / total_crypto if total_crypto else 0.0
    res.eth_crypto_pct = eth_val / total_crypto if total_crypto else 0.0

    # ── NAV ──
    nav = btc_val + eth_val + cash - debt
    nav_ps = nav / shares
    mnav = market_cap / nav if nav else float("nan")
    adj_m = mnav / (1 + ETH_YIELD_RATE)
    nav_disc = (market_cap / nav) - 1 if nav else float("nan")  # AG

    res.total_nav = nav
    res.nav_per_share = nav_ps
    res.mnav = mnav
    res.adjusted_mnav = adj_m
    res.nav_discount = nav_disc

    # ── yield module ──
    staked = profile.eth_held * profile.eth_staked_pct
    staked_value = staked * eth_price  # USD value of staked ETH
    ann_yld = staked_value * ETH_YIELD_RATE if staked else 0.0
    yld_ps = ann_yld / shares if shares else 0.0

    # yield_pct = annual_yield / staked_value
    # Return on staked capital — by construction equals ETH_YIELD_RATE before drag.
    # The meaningful number is adj_yld (after operating drag) and spread vs benchmark.
    yld_yld = ann_yld / staked_value if staked_value else 0.0

    adj_yld = 0.0 if staked == 0 else yld_yld - profile.operating_drag
    spread = adj_yld - ETH_YIELD_RATE  # alpha vs raw staking benchmark
    real_yld = ann_yld / market_cap if market_cap else 0.0  # yield as % of mkt cap

    res.staked_eth = staked
    res.annual_yield_usd = ann_yld
    res.yield_per_share = yld_ps
    res.yield_yield_pct = yld_yld
    res.adjusted_yield_pct = adj_yld
    res.spread = spread
    res.real_yield_mkt_adj = real_yld

    # ── signal ──
    # ETH treasury: spread-driven signal (original xlsx logic)
    # BTC treasury: mNAV-driven signal (premium/discount to NAV)
    is_eth_treasury = profile.eth_held >= MIN_ETH_THRESHOLD and staked > 0
    sp = _safe(spread)

    if is_eth_treasury:
        if sp < -ENTRY_THRESHOLD:
            signal = "OVERVALUED"
        elif sp > ENTRY_THRESHOLD:
            signal = "UNDERVALUED"
        else:
            signal = "FAIR"
        confidence_val = abs(sp)
    else:
        mn = _safe(mnav, 1.5)
        if mn < 1.0:
            signal = "UNDERVALUED"  # discount to NAV
        elif mn < 1.5:
            signal = "FAIR"
        elif mn < 2.0:
            signal = "FAIR"  # modest premium, not alarm yet
        else:
            signal = "OVERVALUED"  # excessive premium
        confidence_val = abs(mn - 1.5) / 1.5  # normalised distance from fair value

    res.signal = signal
    res.confidence = confidence_val
    res.mispricing_pct = sp

    # ── trade sizing ──
    if is_eth_treasury:
        # ETH: position scales with yield spread magnitude
        if sp > ENTRY_THRESHOLD:
            pos = min(sp / ENTRY_THRESHOLD, SIZE_CAP_MULT) * SIZE_SCALAR
        elif sp < -ENTRY_THRESHOLD:
            pos = -min(abs(sp) / ENTRY_THRESHOLD, SIZE_CAP_MULT) * SIZE_SCALAR
        else:
            pos = 0.0
    else:
        # BTC: position scales with distance from mNAV fair-value band (1.0–1.5×)
        mn = _safe(mnav, 1.5)
        BTC_FAIR_HIGH = 1.5
        BTC_FAIR_LOW = 1.0
        if mn < BTC_FAIR_LOW:
            discount = (BTC_FAIR_LOW - mn) / BTC_FAIR_LOW
            pos = min(discount / 0.1, SIZE_CAP_MULT) * SIZE_SCALAR
        elif mn > BTC_FAIR_HIGH:
            premium = (mn - BTC_FAIR_HIGH) / BTC_FAIR_HIGH
            pos = -min(premium / 0.3, SIZE_CAP_MULT) * SIZE_SCALAR
        else:
            pos = 0.0

    # trade_enabled is derived from position — if no position, no trade.
    # This ensures trade_enabled and position are never contradictory.
    trade_on = pos != 0.0

    res.trade_enabled = trade_on
    res.position_size = pos

    # ── composite score — branch on treasury type ──
    #
    # BTC-only treasury  → value driver is mNAV premium/discount
    # ETH-yield treasury → value driver is staking yield spread + mNAV
    #
    is_eth_treasury = profile.eth_held >= MIN_ETH_THRESHOLD and staked > 0

    if is_eth_treasury:
        ak, rec_detail = _score_eth_treasury(
            adj_yld, spread, sp, _safe(mnav, 0.0), _safe(nav_disc, 0.0)
        )
        al = spread
    else:
        ak, rec_detail = _score_btc_treasury(
            _safe(mnav, 0.0), _safe(nav_disc, 0.0)
        )
        al = (1.0 - _safe(mnav, 1.5)) / 2.5

    # ── unified label: execution gate + score magnitude ──
    unified = _unified_label(trade_on, pos, ak)

    res.valuation_score = al
    res.final_score = ak
    res.score_label = unified
    res.recommendation = _build_recommendation(unified, rec_detail)

    return res


def apply_portfolio_sizing(results: list[ValuationResult]) -> None:
    """Cross-portfolio AH / AI / AJ pass."""
    active = [r for r in results if r.is_treasury]
    total_exp = sum(abs(r.position_size) for r in active)
    for r in active:
        if total_exp > 0:
            r.normalized_weight = r.position_size / total_exp
            r.final_position = r.normalized_weight * MAX_POSITION
        else:
            r.normalized_weight = r.final_position = 0.0


# ─────────────────────────────────────────────────────────────
# 4. DISPLAY
# ─────────────────────────────────────────────────────────────

def _signal_color(s: str) -> str:
    return {"OVERVALUED": "red", "UNDERVALUED": "green", "FAIR": "yellow"}.get(s, "white")


def _score_color(label: str) -> str:
    return {
        "VERY ATTRACTIVE": "bold green", "ATTRACTIVE": "green",
        "NEUTRAL": "yellow",
        "EXPENSIVE": "red", "VERY EXPENSIVE": "bold red",
    }.get(label, "white")


def _label_emoji(label: str) -> str:
    return {
        "VERY ATTRACTIVE": "🟢", "ATTRACTIVE": "🟢",
        "NEUTRAL": "🟡",
        "EXPENSIVE": "🔴", "VERY EXPENSIVE": "🔴",
    }.get(label, "⚪")


def _confidence_band(confidence: float) -> str:
    """Convert raw confidence number to Low / Medium / High."""
    if confidence < 0.02:  return "Low"
    if confidence < 0.06:  return "Medium"
    return "High"


def _build_why(r) -> str:
    """Generate plain-English bullet reasons from the valuation data."""
    lines = []
    mn = r.mnav if not _isnan(r.mnav) else 1.5
    nd = r.nav_discount if not _isnan(r.nav_discount) else 0.0
    sp = r.spread

    # NAV signal
    if nd < -0.05:
        lines.append(f"+ Stock is trading {abs(nd) * 100:.1f}% BELOW the value of its crypto — rare discount")
    elif nd < 0:
        lines.append(f"+ Small discount to crypto asset value ({nd * 100:.1f}%)")
    elif nd < 0.2:
        lines.append(f"~ Slight premium to asset value ({nd * 100:.1f}%) — fairly normal")
    elif nd < 0.5:
        lines.append(f"- Moderate premium to asset value ({nd * 100:.1f}%) — you're overpaying a bit")
    else:
        lines.append(f"- Large premium to asset value ({nd * 100:.1f}%) — significantly overpriced vs holdings")

    # Yield signal (ETH treasury only)
    if r.staked_eth > 0:
        if r.adjusted_yield_pct > 0.03:
            lines.append(f"+ Strong staking yield after costs ({r.adjusted_yield_pct * 100:.1f}%/yr)")
        elif r.adjusted_yield_pct > 0:
            lines.append(f"+ Positive staking yield after costs ({r.adjusted_yield_pct * 100:.1f}%/yr)")
        elif r.adjusted_yield_pct == 0:
            lines.append("- Operating costs eat all staking yield — net yield is zero")
        else:
            lines.append(f"- Operating costs exceed staking yield (net: {r.adjusted_yield_pct * 100:.1f}%/yr)")

        if sp > 0.02:
            lines.append(f"+ Yield beats the raw ETH staking benchmark by {sp * 100:.1f}%")
        elif sp > 0:
            lines.append(f"~ Yield slightly above raw ETH staking benchmark (+{sp * 100:.1f}%)")
        elif sp > -0.02:
            lines.append(f"~ Yield slightly below raw ETH staking benchmark ({sp * 100:.1f}%)")
        else:
            lines.append(f"- Yield underperforms raw ETH staking by {abs(sp) * 100:.1f}% — no yield advantage")
    else:
        # BTC treasury — value driver is mNAV
        if mn < 1.0:
            lines.append(f"+ BTC wrapper trading at discount ({mn:.2f}×) — you get $1 of BTC for less than $1")
        elif mn < 1.5:
            lines.append(f"+ Reasonable premium for BTC access ({mn:.2f}×)")
        elif mn < 2.0:
            lines.append(f"~ Premium is fair but not cheap ({mn:.2f}×)")
        elif mn < 2.5:
            lines.append(f"- High premium ({mn:.2f}×) — buying BTC directly would be cheaper")
        else:
            lines.append(f"- Very high premium ({mn:.2f}×) — major overpayment vs just buying BTC")

    # Mispricing
    if abs(r.mispricing_pct) < 0.01:
        lines.append("- No strong mispricing signal detected")
    elif r.mispricing_pct > 0.01:
        lines.append(f"+ Positive mispricing signal ({r.mispricing_pct * 100:.1f}%) — price looks low vs fair value")
    else:
        lines.append(f"- Negative mispricing signal ({r.mispricing_pct * 100:.1f}%) — price looks stretched")

    # Conclusion arrow
    label = r.score_label
    if label in ("VERY ATTRACTIVE", "ATTRACTIVE"):
        lines.append("→ Overall: Looks like a reasonable opportunity")
    elif label == "NEUTRAL":
        lines.append("→ Overall: Not a strong opportunity either way — wait and watch")
    else:
        lines.append("→ Overall: More expensive than it should be — proceed with caution")

    return "\n".join(lines)


def _build_summary(r) -> str:
    """One or two plain sentences summarising the situation."""
    mn = r.mnav if not _isnan(r.mnav) else 1.5
    nd = r.nav_discount if not _isnan(r.nav_discount) else 0.0
    label = r.score_label

    has_yield = r.staked_eth > 0

    if label in ("VERY ATTRACTIVE", "ATTRACTIVE"):
        if has_yield and nd < 0:
            return (f"The stock is trading below its crypto asset value "
                    f"AND generating staking yield. That's a double positive — "
                    f"you're getting assets at a discount while they earn income.")
        elif has_yield:
            return (f"The stock generates real staking income from its ETH holdings. "
                    f"The valuation looks reasonable for the yield it produces.")
        else:
            return (f"The stock is trading at or below the value of its BTC holdings. "
                    f"You're getting BTC exposure at a fair or better-than-fair price.")
    elif label == "NEUTRAL":
        if has_yield:
            return (f"Price is close to asset value with some yield, "
                    f"but not enough edge to make a strong call. "
                    f"Worth watching but not urgent.")
        else:
            return (f"The stock trades at a modest premium to its BTC holdings. "
                    f"Not cheap, not crazy expensive — sit on the sidelines for now.")
    else:
        if has_yield:
            return (f"The stock is priced well above its crypto asset value, "
                    f"and the staking yield doesn't justify the premium. "
                    f"You'd likely do better just buying ETH directly.")
        else:
            return (f"You're paying a big premium ({mn:.1f}× asset value) just for BTC exposure. "
                    f"Buying BTC directly would give you more for your money.")


def _build_action(r) -> str:
    """Concrete suggested next steps in plain language."""
    label = r.score_label
    has_yield = r.staked_eth > 0
    mn = r.mnav if not _isnan(r.mnav) else 1.5

    if label == "VERY ATTRACTIVE":
        steps = [
            "Consider opening or adding to a position",
            "Set a price alert if it dips further — even better entry possible",
            "Check company financials and news before committing",
        ]
    elif label == "ATTRACTIVE":
        steps = [
            "Worth researching further as a potential buy",
            "Compare with similar crypto treasury companies",
            "Consider a small starter position if conviction is high",
        ]
    elif label == "NEUTRAL":
        steps = [
            "Wait for a better entry point (lower price or better yield)",
            "Monitor changes in BTC/ETH price — that shifts the NAV quickly",
            "Check back when market conditions change",
        ]
    elif label == "EXPENSIVE":
        steps = [
            "Avoid adding new money at current prices",
            "If already holding, consider trimming",
            f"Could revisit if mNAV drops below 1.5× (currently {mn:.2f}×)",
        ]
    else:  # VERY EXPENSIVE
        steps = [
            "Current price is hard to justify based on asset value",
            "Consider reducing or exiting existing position",
            "Buying BTC/ETH directly gives better value than this wrapper",
        ]

    return "\n".join(f"  • {s}" for s in steps)


def _warn(msg: str):
    if RICH:
        console.print(f"[dim yellow][WARN][/dim yellow] {msg}")
    else:
        print(f"[WARN] {msg}")


def _info(msg: str):
    if RICH:
        console.print(f"[dim cyan][INFO][/dim cyan] {msg}")
    else:
        print(f"[INFO] {msg}")


def display_not_applicable(r: ValuationResult):
    if RICH:
        console.print(Panel(
            f"[bold]{r.ticker}[/bold]  ({r.name})\n\n"
            f"[yellow]⚠  NOT A CRYPTO TREASURY COMPANY[/yellow]\n\n"
            f"[dim]{r.not_applicable_reason}[/dim]\n\n"
            "This model only applies to companies that hold material BTC/ETH\n"
            "on their balance sheet as a treasury strategy.\n"
            "If you believe this company does hold crypto, add it to\n"
            "[cyan]KNOWN_TREASURY[/cyan] in the script with the correct holding figures.",
            title="[bold red]Not Applicable[/bold red]",
            border_style="red",
        ))
    else:
        print(f"\n{'=' * 60}")
        print(f"  {r.ticker} ({r.name})")
        print(f"  NOT A CRYPTO TREASURY COMPANY")
        print(f"  {r.not_applicable_reason}")
        print(f"{'=' * 60}\n")


def display_result(r: ValuationResult):
    if not RICH:
        _display_plain(r)
        return

    lc = _score_color(r.score_label)
    emo = _label_emoji(r.score_label)
    conf_band = _confidence_band(r.confidence)
    conf_color = {"Low": "dim yellow", "Medium": "yellow", "High": "bold yellow"}[conf_band]
    nav_disc_str = f"{r.nav_discount * 100:+.1f}%" if not _isnan(r.nav_discount) else "n/a"

    why = _build_why(r)
    summ = _build_summary(r)
    action = _build_action(r)

    panel_lines = [
        f"[bold]{r.ticker}[/bold]  [dim]{r.name}[/dim]",
        f"[dim]BTC ${r.btc_price:,.0f}  │  ETH ${r.eth_price:,.0f}  │  NAV Discount: {nav_disc_str}[/dim]",
        "",
        f"  Rating:      [{lc}]{emo} {r.score_label}[/{lc}]",
        f"  Confidence:  [{conf_color}]{conf_band}[/{conf_color}]",
        "",
        "[bold]Summary:[/bold]",
        f"  {summ}",
        "",
        "[bold]Why:[/bold]",
        why,
        "",
        "[bold]Suggested Approach:[/bold]",
        action,
        "",
        "[dim]⚠️  Risk Warning: This is a simplified model based on limited public data.[/dim]",
        "[dim]   It does NOT account for market sentiment, macro trends, or company risks.[/dim]",
        "[dim]   Use for research and learning only — not as a sole investment decision.[/dim]",
    ]
    console.print(Panel("\n".join(panel_lines),
                        border_style=lc if lc != "white" else "cyan",
                        padding=(1, 2)))

    # NAV breakdown table
    t1 = Table(title="Under the Hood — NAV & Holdings",
               box=box.SIMPLE_HEAVY, show_lines=True, title_style="dim")
    for col in ["BTC Held", "ETH Held", "BTC Value", "ETH Value",
                "Cash", "Debt", "Total NAV", "Mkt Cap", "NAV/Share", "mNAV"]:
        t1.add_column(col, justify="right", style="dim")
    nav_ps = f"${r.nav_per_share:,.4f}" if not _isnan(r.nav_per_share) else "n/a"
    t1.add_row(
        _coins(r.btc_held), _coins(r.eth_held),
        _usd(r.btc_value), _usd(r.eth_value),
        _usd(r.cash), _usd(r.debt),
        _usd(r.total_nav), _usd(r.market_cap),
        nav_ps, _mult(r.mnav),
    )
    console.print(t1)

    if r.staked_eth > 0:
        t2 = Table(title="Under the Hood — Yield Module",
                   box=box.SIMPLE_HEAVY, show_lines=True, title_style="dim")
        for col in ["Staked ETH", "Annual Yield", "Adj.Yield%",
                    "Spread vs Benchmark", "Real Yield on Mkt Cap"]:
            t2.add_column(col, justify="right", style="dim")
        t2.add_row(
            _coins(r.staked_eth), _usd(r.annual_yield_usd),
            _pct(r.adjusted_yield_pct), _pct(r.spread),
            _pct(r.real_yield_mkt_adj),
        )
        console.print(t2)


def _display_plain(r: ValuationResult):
    sep = "=" * 60
    nav_ps = f"${r.nav_per_share:,.4f}" if not _isnan(r.nav_per_share) else "n/a"
    nav_disc = f"{r.nav_discount * 100:+.1f}%" if not _isnan(r.nav_discount) else "n/a"
    emo = _label_emoji(r.score_label)
    conf = _confidence_band(r.confidence)
    why = _build_why(r)
    summ = _build_summary(r)
    action = _build_action(r)

    print(f"\n{sep}")
    print(f"  {r.ticker}  |  {r.name}")
    print(f"  BTC ${r.btc_price:,.0f}  |  ETH ${r.eth_price:,.0f}")
    print(sep)
    print(f"  Rating:     {emo} {r.score_label}")
    print(f"  Confidence: {conf}")
    print(f"  NAV Discount: {nav_disc}")
    print(f"\nSummary:")
    print(f"  {summ}")
    print(f"\nWhy:")
    print(why)
    print(f"\nSuggested Approach:")
    print(action)
    print(f"\n  --- Numbers ---")
    print(f"  BTC held: {_coins(r.btc_held)}  |  ETH held: {_coins(r.eth_held)}")
    print(f"  Total NAV: {_usd(r.total_nav)}  |  Mkt Cap: {_usd(r.market_cap)}")
    print(f"  mNAV: {_mult(r.mnav)}  |  NAV/Share: {nav_ps}")
    if r.staked_eth > 0:
        print(f"  Staked ETH: {_coins(r.staked_eth)}  |  Annual Yield: {_usd(r.annual_yield_usd)}")
        print(f"  Adj. Yield: {_pct(r.adjusted_yield_pct)}  |  Spread: {_pct(r.spread)}")
    print(f"\n  ⚠️  Disclaimer:")
    print(f"  This model is simplified and should not be used as sole investment decision.")
    print(f"{sep}\n")


def display_portfolio_summary(results: list[ValuationResult]):
    treasury = [r for r in results if r.is_treasury]
    if len(treasury) < 2 or not RICH:
        return

    console.print("\n")
    t = Table(title="Portfolio Summary", box=box.SIMPLE_HEAVY, show_lines=True)
    for col in ["Ticker", "Signal", "Rating", "Final Score",
                "Position Size", "Norm. Weight", "Final Position"]:
        t.add_column(col, justify="right")
    for r in treasury:
        lc = _score_color(r.score_label)
        emo = _label_emoji(r.score_label)
        t.add_row(
            r.ticker,
            _confidence_band(r.confidence),
            f"[{lc}]{emo} {r.score_label}[/{lc}]",
            f"{r.final_score:+.4f}",
            _pct(r.position_size),
            _pct(r.normalized_weight),
            _pct(r.final_position),
        )
    console.print(t)


def export_json(results: list[ValuationResult], path="valuation_output.json"):
    out = []
    for r in results:
        d = r.__dict__.copy()
        for k, v in d.items():
            if isinstance(v, float) and _isnan(v): d[k] = None
        out.append(d)
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    _info(f"JSON written to {path}")


# ─────────────────────────────────────────────────────────────
# 5. MAIN
# ─────────────────────────────────────────────────────────────

def run(tickers: list[str]) -> None:
    as_of = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    _info(f"Fetching live crypto prices  [{as_of}]")
    prices = fetch_crypto_prices()
    _info(f"BTC = ${prices['BTC']:,.2f}  │  ETH = ${prices['ETH']:,.2f}")

    results: list[ValuationResult] = []

    for ticker in tickers:
        ticker = ticker.upper().strip()
        _info(f"Analysing {ticker}...")

        eq = fetch_equity(ticker)
        is_t, profile, reason = detect_treasury(ticker, eq)

        if not is_t:
            r = ValuationResult(
                ticker=ticker, name=eq.get("name", ticker),
                is_treasury=False, not_applicable_reason=reason,
            )
            results.append(r)
            display_not_applicable(r)
            continue

        r = compute(ticker, profile, eq, prices["BTC"], prices["ETH"])
        results.append(r)

    # Portfolio sizing across all treasury results
    apply_portfolio_sizing(results)

    # Display treasury results
    for r in results:
        if r.is_treasury:
            display_result(r)

    if len([r for r in results if r.is_treasury]) > 1:
        display_portfolio_summary(results)

    export_json(results)


def main() -> None:
    # Accept tickers from CLI args or prompt interactively
    if len(sys.argv) > 1:
        tickers = sys.argv[1:]
    else:
        if RICH:
            console.print(
                "\n[bold cyan]Crypto-Native Multi-Asset Equity Valuation[/bold cyan]\n"
                "[dim]Enter one or more ticker symbols separated by spaces.[/dim]\n"
                "[dim]Examples:  MSTR   │   MSTR BMNR MARA   │   AAPL (non-treasury test)[/dim]\n"
            )
            raw = console.input("[bold]Ticker(s) › [/bold] ")
        else:
            print("\nCrypto Treasury Valuation Model")
            print("Enter ticker symbols (space-separated), e.g.: MSTR BMNR MARA")
            raw = input("Ticker(s) > ").strip()

        tickers = raw.upper().split()
        if not tickers:
            _warn("No tickers provided. Exiting.")
            return

    run(tickers)


if __name__ == "__main__":
    main()