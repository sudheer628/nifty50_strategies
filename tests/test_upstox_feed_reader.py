import json
import os
import time
import pytest
from common.upstox_feed_reader import read_quote, FeedUnavailableError


def test_fresh_quote_success(tmp_path):
    now_epoch = int(time.time())
    cache_file = tmp_path / "upstox_live_quotes.json"
    payload = {
        "source": "upstox_v3_websocket",
        "timestamp_utc": "2026-10-14T10:00:00Z",
        "active_instruments_count": 1,
        "quotes": {
            "NSE_INDEX|Nifty 50": {
                "instrument_key": "NSE_INDEX|Nifty 50",
                "symbol": "Nifty 50",
                "ltp": 25120.50,
                "open": 25050.00,
                "close": 25000.00,
                "high": 25150.00,
                "low": 25020.00,
                "change": 120.50,
                "change_pct": 0.48,
                "updated_at_epoch": now_epoch - 5,
            }
        },
    }
    cache_file.write_text(json.dumps(payload), encoding="utf-8")

    quote = read_quote("NSE_INDEX|Nifty 50", max_age_sec=30, cache_path=str(cache_file))
    assert quote["ltp"] == 25120.50
    assert quote["symbol"] == "Nifty 50"
    assert quote["change_pct"] == 0.48


def test_missing_file_raises(tmp_path):
    missing_file = tmp_path / "non_existent.json"
    with pytest.raises(FeedUnavailableError, match="does not exist"):
        read_quote("NSE_INDEX|Nifty 50", cache_path=str(missing_file))


def test_malformed_json_raises(tmp_path):
    cache_file = tmp_path / "corrupted.json"
    cache_file.write_text("{ incomplete json ...", encoding="utf-8")
    with pytest.raises(FeedUnavailableError, match="Failed to read/parse"):
        read_quote("NSE_INDEX|Nifty 50", cache_path=str(cache_file))


def test_instrument_absent_raises(tmp_path):
    now_epoch = int(time.time())
    cache_file = tmp_path / "upstox_live_quotes.json"
    payload = {
        "source": "upstox_v3_websocket",
        "quotes": {
            "GLOBAL_INDICATOR|USDINR": {
                "ltp": 83.25,
                "updated_at_epoch": now_epoch,
            }
        },
    }
    cache_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(FeedUnavailableError, match="not present in feed cache"):
        read_quote("NSE_INDEX|Nifty 50", cache_path=str(cache_file))


def test_stale_quote_raises(tmp_path):
    stale_epoch = int(time.time()) - 40  # 40s old (limit is 30s)
    cache_file = tmp_path / "upstox_live_quotes.json"
    payload = {
        "source": "upstox_v3_websocket",
        "quotes": {
            "NSE_INDEX|Nifty 50": {
                "ltp": 25120.50,
                "updated_at_epoch": stale_epoch,
            }
        },
    }
    cache_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(FeedUnavailableError, match="is stale"):
        read_quote("NSE_INDEX|Nifty 50", max_age_sec=30, cache_path=str(cache_file))


def test_non_positive_ltp_raises(tmp_path):
    now_epoch = int(time.time())
    cache_file = tmp_path / "upstox_live_quotes.json"
    payload = {
        "source": "upstox_v3_websocket",
        "quotes": {
            "NSE_INDEX|Nifty 50": {
                "ltp": 0.0,
                "updated_at_epoch": now_epoch,
            }
        },
    }
    cache_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(FeedUnavailableError, match="Non-positive 'ltp'"):
        read_quote("NSE_INDEX|Nifty 50", cache_path=str(cache_file))


def test_missing_quotes_dictionary_raises(tmp_path):
    cache_file = tmp_path / "upstox_live_quotes.json"
    payload = {
        "source": "upstox_v3_websocket",
        "timestamp_utc": "2026-10-14T10:00:00Z",
    }
    cache_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(FeedUnavailableError, match="missing 'quotes' dictionary"):
        read_quote("NSE_INDEX|Nifty 50", cache_path=str(cache_file))


def test_missing_updated_at_epoch_raises(tmp_path):
    cache_file = tmp_path / "upstox_live_quotes.json"
    payload = {
        "source": "upstox_v3_websocket",
        "quotes": {
            "NSE_INDEX|Nifty 50": {
                "ltp": 25120.50,
            }
        },
    }
    cache_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(FeedUnavailableError, match="missing 'updated_at_epoch'"):
        read_quote("NSE_INDEX|Nifty 50", cache_path=str(cache_file))

