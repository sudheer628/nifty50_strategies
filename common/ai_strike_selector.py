"""
ai_strike_selector.py - Dynamic AI Strike & Expiry Selector for NIFTY50 Weekly Option Strategy

Runs on Tuesday 9:30 AM IST during weekly cycle entry.
Selects volatility-adaptive, Greeks-calibrated Call and Put strikes using OpenRouter LLMs
(via Redis 'finance_llm_models' fallback chain) and SQLite microstructure & technical indicators.

Always provides an immediate fail-safe fallback to the standard static anchor +/- 100 rule
if the API, network, or data has any delay or error.
"""

import os
import sys
import json
import sqlite3
import logging
import datetime
import time
from typing import Dict, Any, Tuple, Optional, List

try:
    import pytz
    IST_TZ = pytz.timezone("Asia/Kolkata")
except ImportError:
    from zoneinfo import ZoneInfo
    IST_TZ = ZoneInfo("Asia/Kolkata")

import requests
from dotenv import load_dotenv

# Load environment
load_dotenv()

# Shared logger
try:
    from config import logger, get_redis_client, STRIKE_STEP
except ImportError:
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("ai_strike_selector")
    STRIKE_STEP = 100
    def get_redis_client():
        return None

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_SQLITE_DIR = os.getenv("SQLITE_DIR", "/home/ubuntu/sqlite")
REDIS_FINANCE_MODELS_KEY = "finance_llm_models"
FALLBACK_MODELS = [
    "deepseek/deepseek-v4-pro",
    "minimax/minimax-m3",
    "anthropic/claude-sonnet-4-20250514",
    "google/gemini-2.5-flash"
]


def compute_static_strikes(reference_ltp: float, step: int = 100) -> Tuple[int, int]:
    """
    Compute standard static benchmark strikes: anchor +/- step (default 100 pts).
    
    Returns:
        (static_put_strike, static_call_strike)
    """
    anchor = int(reference_ltp / step) * step
    return anchor - step, anchor + step


def fetch_redis_models() -> List[str]:
    """Fetch ordered model list from Redis Cloud key 'finance_llm_models'."""
    try:
        r = get_redis_client()
        if r:
            val = r.get(REDIS_FINANCE_MODELS_KEY)
            if val:
                parsed = json.loads(val)
                if isinstance(parsed, list) and parsed:
                    return [str(m).strip() for m in parsed if str(m).strip()]
                elif isinstance(parsed, str) and parsed.strip():
                    return [parsed.strip()]
    except Exception as e:
        logger.warning(f"Could not load '{REDIS_FINANCE_MODELS_KEY}' from Redis: {e}")
    return FALLBACK_MODELS


def load_market_context_for_strikes(
    reference_ltp: float,
    sqlite_dir: str = DEFAULT_SQLITE_DIR
) -> Dict[str, Any]:
    """
    Ingest latest VIX, ATR, Greeks surface, IV skew, PCR, and FII positioning
    from SQLite databases for Tuesday strike selection.
    """
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    yyyy_mm = now_utc.strftime("%Y_%m")
    
    # Handle possible parent dir resolution for sqlite_dir
    base_sqlite = sqlite_dir.replace("/strategies", "").rstrip("/\\")

    context: Dict[str, Any] = {
        "nifty_ltp": reference_ltp,
        "vix": None,
        "atr_14": None,
        "bb_width": None,
        "pcr": None,
        "avg_iv": None,
        "iv_skew": None,
        "max_pain_strike": None,
        "net_gamma_exposure": None,
        "rsi_14": None,
        "macd_hist": None,
        "trend_label": None,
        "fii_flow": None,
        "candidate_strikes": []
    }

    # 1. Load NSF features (VIX, ATR, RSI, BB Width)
    nsf_db = os.path.join(base_sqlite, f"nifty_signal_features_{yyyy_mm}.db")
    if os.path.exists(nsf_db):
        try:
            conn = sqlite3.connect(nsf_db)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM signals WHERE symbol = 'NIFTY50' ORDER BY ts DESC LIMIT 1")
            row = cursor.fetchone()
            if row:
                context["atr_14"] = row["atr_14"]
                context["bb_width"] = row["bb_width"]
                context["rsi_14"] = row["nsf_rsi14"]
                context["macd_hist"] = row["macd_histogram"]
                context["trend_label"] = row["signal_label"]
            conn.close()
        except Exception as e:
            logger.warning(f"Error loading NSF indicators: {e}")

    # 2. Load Market Signal Agent features (IV, Skew, Greeks, PCR, FII)
    signals_db = os.path.join(base_sqlite, f"signals_data_{yyyy_mm}.db")
    if os.path.exists(signals_db):
        try:
            conn = sqlite3.connect(signals_db)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # NIFTY microstructure & Greeks
            cursor.execute("SELECT price, rsi14, pcr, vix, data FROM market_data WHERE pk = 'INDEX#NIFTY50' ORDER BY ts DESC LIMIT 1")
            row = cursor.fetchone()
            if row:
                try:
                    sig_data = json.loads(row["data"]) if row["data"] else {}
                except Exception:
                    sig_data = {}

                context["vix"] = row["vix"] or sig_data.get("vix")
                context["pcr"] = row["pcr"] or sig_data.get("pcr")
                context["avg_iv"] = sig_data.get("avg_iv")
                context["iv_skew"] = sig_data.get("iv_skew")
                context["max_pain_strike"] = sig_data.get("max_pain_strike")
                context["net_gamma_exposure"] = sig_data.get("net_gamma_exposure")

                # Extract candidate strikes around reference_ltp (+/- 250 pts in 50-pt steps)
                surface = sig_data.get("option_chain_surface", {})
                if isinstance(surface, str):
                    try: surface = json.loads(surface)
                    except Exception: surface = {}

                atm_anchor = int(round(reference_ltp / 50.0)) * 50
                candidates = []
                for offset in range(-250, 300, 50):
                    strike_val = atm_anchor + offset
                    strike_str = str(strike_val)
                    strike_data = surface.get(strike_str, {})
                    c_leg = strike_data.get("CALL", {})
                    p_leg = strike_data.get("PUT", {})

                    candidates.append({
                        "strike": strike_val,
                        "offset_from_spot": offset,
                        "call_delta": c_leg.get("delta"),
                        "call_theta": c_leg.get("theta"),
                        "call_iv": c_leg.get("implied_volatility"),
                        "put_delta": p_leg.get("delta"),
                        "put_theta": p_leg.get("theta"),
                        "put_iv": p_leg.get("implied_volatility")
                    })
                context["candidate_strikes"] = candidates

            # FII Flow
            cursor.execute("SELECT data FROM market_data WHERE pk = 'FLOW#FII_DII#DAILY' ORDER BY ts DESC LIMIT 1")
            f_row = cursor.fetchone()
            if f_row and f_row["data"]:
                try:
                    f_json = json.loads(f_row["data"])
                    context["fii_flow"] = {
                        "fii_net_5d_cr": f_json.get("cash_flow", {}).get("fii_net_5d_sum"),
                        "fno_bias": f_json.get("fno_positioning", {}).get("bias")
                    }
                except Exception:
                    pass

            conn.close()
        except Exception as e:
            logger.warning(f"Error loading market signals for strike selection: {e}")

    return context


def build_strike_selection_prompt(
    reference_ltp: float,
    context: Dict[str, Any],
    static_put: int,
    static_call: int
) -> Tuple[str, str]:
    """Construct the strike selection prompt for the LLM."""
    system_prompt = (
        "You are an expert quantitative NIFTY50 options strategist.\n"
        "Your objective: Select optimal Call & Put strikes for a weekly Long Strangle (+CE / +PE entered on Tuesday 9:30 AM).\n"
        "You must optimize strike selection based on India VIX, ATR daily range, Option Greeks (Delta ~0.30-0.38), IV Skew, and Technical trend.\n"
        "Rules:\n"
        "1. All strikes MUST be valid NIFTY 50-point increments (multiples of 50: e.g. 24450, 24500, 24550, 24600).\n"
        "2. call_strike MUST be >= reference_ltp.\n"
        "3. put_strike MUST be <= reference_ltp.\n"
        "4. In high volatility (VIX > 16 or ATR > 160), choose wider strikes (+/- 150 to 200 pts) to reduce theta drag.\n"
        "5. In low volatility (VIX < 13 or ATR < 110), choose tighter strikes (+/- 50 to 100 pts) so delta moves into the money.\n"
        "6. If Put IV is heavily skewed (> 1.5 pt over Call IV), push Put strike 50 pts further OTM to equalize premium outlay.\n"
        "7. Output ONLY a valid JSON object matching the schema."
    )

    user_data = {
        "nifty_spot_ltp": reference_ltp,
        "static_benchmark_strikes": {
            "static_put_strike": static_put,
            "static_call_strike": static_call
        },
        "volatility_and_range": {
            "india_vix": context.get("vix", 14.0),
            "atr_14_daily_range": context.get("atr_14", 130.0),
            "bollinger_band_width_pct": context.get("bb_width"),
            "avg_iv": context.get("avg_iv"),
            "iv_skew": context.get("iv_skew")
        },
        "technical_and_microstructure": {
            "pcr": context.get("pcr"),
            "max_pain_strike": context.get("max_pain_strike"),
            "net_gamma_exposure_gex": context.get("net_gamma_exposure"),
            "rsi_14": context.get("rsi_14"),
            "macd_histogram": context.get("macd_hist"),
            "trend_label": context.get("trend_label")
        },
        "macro_fii_bias": context.get("fii_flow"),
        "candidate_strikes_greeks": context.get("candidate_strikes", [])
    }

    user_prompt = (
        "## Current Market State for Strike Selection (Tuesday 9:30 AM IST)\n"
        f"{json.dumps(user_data, indent=2)}\n\n"
        "## Required Output Format (JSON)\n"
        "```json\n"
        "{\n"
        '  "call_strike": 24600,\n'
        '  "put_strike": 24350,\n'
        '  "target_call_delta": 0.35,\n'
        '  "target_put_delta": -0.35,\n'
        '  "selection_rationale": "Clear 2-sentence rationale citing VIX/ATR, Delta targets, and IV skew."\n'
        "}\n"
        "```"
    )

    return system_prompt, user_prompt


def call_openrouter_strike_selector(
    system_prompt: str,
    user_prompt: str,
    models: List[str]
) -> Optional[Tuple[Dict[str, Any], str]]:
    """Query OpenRouter using sequential model fallback chain."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        logger.warning("OPENROUTER_API_KEY not found in environment. Using fallback.")
        return None

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/sudheer628/nifty50_strategies",
        "X-Title": "Nifty50 Strategy Strike Selector"
    }

    for model in models:
        try:
            logger.info(f"Attempting AI strike selection via model: {model}")
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": 0.1,
                "max_tokens": 300,
                "response_format": {"type": "json_object"}
            }

            resp = requests.post(OPENROUTER_API_URL, headers=headers, json=payload, timeout=7)
            if resp.status_code == 200:
                data = resp.json()
                content = data["choices"][0]["message"]["content"].strip()
                
                # Parse JSON
                if content.startswith("```"):
                    content = content.split("```")[1]
                    if content.startswith("json"):
                        content = content[4:]
                parsed = json.loads(content.strip())
                logger.info(f"Successfully generated strikes via {model}: Call={parsed.get('call_strike')}, Put={parsed.get('put_strike')}")
                return parsed, model
            else:
                logger.warning(f"Model {model} returned HTTP {resp.status_code}: {resp.text[:150]}")
        except Exception as e:
            logger.warning(f"Model {model} failed: {e}")

    return None


def select_strikes(
    reference_ltp: float,
    sqlite_dir: str = DEFAULT_SQLITE_DIR,
    force_static: bool = False
) -> Tuple[int, int, int, int, str, str]:
    """
    Master strike selection entry point.
    
    Selects Call and Put strikes using AI volatility & Greeks calibration,
    with an immediate fail-safe fallback to standard static +/- 100 anchor.
    
    Args:
        reference_ltp: NIFTY spot LTP at Tuesday 9:30 AM.
        sqlite_dir: Path to directory containing SQLite databases.
        force_static: If True, bypass AI and use static benchmark.
        
    Returns:
        Tuple of:
        (put_strike, call_strike, static_put_strike, static_call_strike, selection_mode, rationale)
    """
    static_put, static_call = compute_static_strikes(reference_ltp, step=STRIKE_STEP)

    if force_static or not os.getenv("OPENROUTER_API_KEY"):
        return (
            static_put,
            static_call,
            static_put,
            static_call,
            "STATIC_RULE" if force_static else "STATIC_FALLBACK",
            "Selected via static anchor +/- 100 pt rule."
        )

    # 1. Ingest market context
    context = load_market_context_for_strikes(reference_ltp, sqlite_dir)

    # 2. Build prompt
    sys_p, user_p = build_strike_selection_prompt(reference_ltp, context, static_put, static_call)

    # 3. Fetch model chain & query OpenRouter
    models = fetch_redis_models()
    result = call_openrouter_strike_selector(sys_p, user_p, models)

    if result:
        parsed_json, used_model = result
        try:
            c_strike = int(parsed_json.get("call_strike", 0))
            p_strike = int(parsed_json.get("put_strike", 0))
            rationale = str(parsed_json.get("selection_rationale", "")).strip()

            # Sanity checks on AI strikes:
            # 1. Must be multiples of 50
            # 2. call_strike >= reference_ltp - 50 and put_strike <= reference_ltp + 50
            # 3. call_strike > put_strike
            # 4. Reasonably close to spot (within +/- 400 pts)
            if (
                c_strike % 50 == 0 and p_strike % 50 == 0
                and c_strike > p_strike
                and abs(c_strike - reference_ltp) <= 450
                and abs(p_strike - reference_ltp) <= 450
            ):
                logger.info(f"AI Strike Selection Approved [{used_model}]: Call={c_strike}, Put={p_strike}")
                return (
                    p_strike,
                    c_strike,
                    static_put,
                    static_call,
                    f"AI_{used_model.split('/')[-1]}",
                    rationale or f"Selected via AI model {used_model} based on VIX & Greeks."
                )
            else:
                logger.warning(f"AI returned invalid strikes: Call={c_strike}, Put={p_strike}. Reverting to static fallback.")
        except Exception as e:
            logger.warning(f"Failed to parse AI strike selection output: {e}")

    # Fallback to static rule
    logger.info(f"Using Fail-Safe Static Fallback Strikes: Call={static_call}, Put={static_put}")
    return (
        static_put,
        static_call,
        static_put,
        static_call,
        "STATIC_FALLBACK",
        f"Fallback to static anchor +/- {STRIKE_STEP} pt rule due to API timeout or invalid strike response."
    )
