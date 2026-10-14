import json
import time
from unittest.mock import patch, MagicMock
import pytest

from common.angelone_client import (
    get_nifty_spot,
    _get_nifty_spot_from_feed,
    _get_nifty_spot_angel,
    _NIFTY50_FEED_KEY,
)
from common.upstox_feed_reader import FeedUnavailableError


def test_nifty_spot_feed_success(tmp_path, monkeypatch):
    now_epoch = int(time.time())
    cache_file = tmp_path / "upstox_live_quotes.json"
    payload = {
        "source": "upstox_v3_websocket",
        "timestamp_utc": "2026-10-14T10:00:00Z",
        "active_instruments_count": 1,
        "quotes": {
            _NIFTY50_FEED_KEY: {
                "instrument_key": _NIFTY50_FEED_KEY,
                "symbol": "Nifty 50",
                "ltp": 25150.25,
                "open": 25080.00,
                "close": 25020.00,
                "high": 25180.00,
                "low": 25050.00,
                "change": 130.25,
                "change_pct": 0.52,
                "updated_at_epoch": now_epoch - 2,
            }
        },
    }
    cache_file.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("UPSTOX_FEED_CACHE_PATH", str(cache_file))

    with patch("requests.post") as mock_post:
        spot = get_nifty_spot()
        assert spot["ltp"] == 25150.25
        assert spot["open"] == 25080.00
        assert spot["close"] == 25020.00
        assert spot["high"] == 25180.00
        assert spot["low"] == 25050.00
        assert spot["source"] == "upstox_feed"
        # Angel One REST API was never called
        mock_post.assert_not_called()


def test_nifty_spot_feed_close_fallback_via_change_pct(tmp_path, monkeypatch):
    """If close/cp is 0, calculate previous close from ltp and change_pct."""
    now_epoch = int(time.time())
    cache_file = tmp_path / "upstox_live_quotes.json"
    # ltp = 25250.0, change_pct = 1.0% -> prev_close = 25250 / 1.01 = 25000.0 (1% drift, well within 5% gate)
    payload = {
        "source": "upstox_v3_websocket",
        "quotes": {
            _NIFTY50_FEED_KEY: {
                "ltp": 25250.0,
                "open": 25100.0,
                "close": 0.0,
                "cp": 0.0,
                "change_pct": 1.0,
                "high": 25300.0,
                "low": 25050.0,
                "updated_at_epoch": now_epoch - 1,
            }
        },
    }
    cache_file.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("UPSTOX_FEED_CACHE_PATH", str(cache_file))

    spot = get_nifty_spot()
    assert spot["ltp"] == 25250.0
    assert spot["open"] == 25100.0
    assert spot["close"] == 25000.0
    assert spot["source"] == "upstox_feed"


def test_nifty_spot_feed_implausible_close_falls_back_to_angel(tmp_path, monkeypatch):
    """If previous close drifts >5% from LTP, reject as implausible and fall back to Angel."""
    now_epoch = int(time.time())
    cache_file = tmp_path / "upstox_live_quotes.json"
    payload = {
        "source": "upstox_v3_websocket",
        "quotes": {
            _NIFTY50_FEED_KEY: {
                "ltp": 25000.0,
                "open": 24900.0,
                "close": 22000.0,  # 12% drift: impossible for NIFTY
                "high": 25100.0,
                "low": 24850.0,
                "updated_at_epoch": now_epoch - 1,
            }
        },
    }
    cache_file.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("UPSTOX_FEED_CACHE_PATH", str(cache_file))

    angel_mock_spot = {
        "ltp": 25000.0,
        "open": 24900.0,
        "close": 24850.0,
        "high": 25050.0,
        "low": 24800.0,
        "source": "angelone",
    }
    with patch("common.angelone_client._get_nifty_spot_angel", return_value=angel_mock_spot) as mock_angel:
        spot = get_nifty_spot()
        assert spot["ltp"] == 25000.0
        assert spot["close"] == 24850.0
        assert spot["source"] == "angelone"
        mock_angel.assert_called_once()


def test_nifty_spot_feed_incomplete_quote_falls_back_to_angel(tmp_path, monkeypatch):
    """If open or close <= 0 and close cannot be derived, fall back to Angel One."""
    now_epoch = int(time.time())
    cache_file = tmp_path / "upstox_live_quotes.json"
    payload = {
        "source": "upstox_v3_websocket",
        "quotes": {
            _NIFTY50_FEED_KEY: {
                "ltp": 25000.0,
                "open": 0.0,  # Invalid open
                "close": 24900.0,
                "updated_at_epoch": now_epoch - 1,
            }
        },
    }
    cache_file.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("UPSTOX_FEED_CACHE_PATH", str(cache_file))

    angel_mock_spot = {
        "ltp": 25010.0,
        "open": 24950.0,
        "close": 24900.0,
        "high": 25050.0,
        "low": 24920.0,
        "source": "angelone",
    }
    with patch("common.angelone_client._get_nifty_spot_angel", return_value=angel_mock_spot) as mock_angel:
        spot = get_nifty_spot()
        assert spot["ltp"] == 25010.0
        assert spot["source"] == "angelone"
        mock_angel.assert_called_once()


def test_nifty_spot_feed_unavailable_falls_back_to_angel(tmp_path, monkeypatch):
    """When feed file is missing, transparently fall back to Angel One."""
    missing_file = tmp_path / "does_not_exist.json"
    monkeypatch.setenv("UPSTOX_FEED_CACHE_PATH", str(missing_file))

    angel_mock_spot = {
        "ltp": 25100.0,
        "open": 25000.0,
        "close": 24950.0,
        "high": 25150.0,
        "low": 24980.0,
        "source": "angelone",
    }
    with patch("common.angelone_client._get_nifty_spot_angel", return_value=angel_mock_spot) as mock_angel:
        spot = get_nifty_spot()
        assert spot["ltp"] == 25100.0
        assert spot["source"] == "angelone"
        mock_angel.assert_called_once()


def test_nifty_spot_stale_feed_falls_back_to_angel(tmp_path, monkeypatch):
    """When feed quote is older than max_age_sec, fall back to Angel One."""
    stale_epoch = int(time.time()) - 45  # 45s old > 30s limit
    cache_file = tmp_path / "upstox_live_quotes.json"
    payload = {
        "source": "upstox_v3_websocket",
        "quotes": {
            _NIFTY50_FEED_KEY: {
                "ltp": 25100.0,
                "open": 25000.0,
                "close": 24950.0,
                "high": 25150.0,
                "low": 24980.0,
                "updated_at_epoch": stale_epoch,
            }
        },
    }
    cache_file.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("UPSTOX_FEED_CACHE_PATH", str(cache_file))
    monkeypatch.setenv("UPSTOX_SPOT_FEED_MAX_AGE_SEC", "30")

    angel_mock_spot = {
        "ltp": 25105.0,
        "open": 25000.0,
        "close": 24950.0,
        "high": 25150.0,
        "low": 24980.0,
        "source": "angelone",
    }
    with patch("common.angelone_client._get_nifty_spot_angel", return_value=angel_mock_spot) as mock_angel:
        spot = get_nifty_spot()
        assert spot["ltp"] == 25105.0
        assert spot["source"] == "angelone"
        mock_angel.assert_called_once()


def test_nifty_spot_total_failure_returns_empty_dict(tmp_path, monkeypatch):
    """When feed is missing and Angel One also fails, returns empty dict."""
    missing_file = tmp_path / "does_not_exist.json"
    monkeypatch.setenv("UPSTOX_FEED_CACHE_PATH", str(missing_file))

    with patch("common.angelone_client._get_nifty_spot_angel", return_value={}) as mock_angel:
        spot = get_nifty_spot()
        assert spot == {}
        mock_angel.assert_called_once()
