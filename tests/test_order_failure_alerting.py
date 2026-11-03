"""
Unit tests for Order Management Failure Alerting & Anti-Spam Redis Cooldown Protection.
Covers:
1. HTTP dispatch and alert embed formatting.
2. Anti-spam Redis cooldown throttling.
3. Delivery failure: Redis cooldown is NOT claimed if Discord delivery fails.
4. Cooldown clearance upon position flatten and replacement protection confirmed active.
5. In-flight delayed broker rejections for MARKET and SL-M orders (Tuesday & Thursday).
6. BUY failure alert deduplication on UNWIND_FAILED.
"""
import importlib.util
import json
import os
import sqlite3
import sys
from unittest.mock import MagicMock, patch, call
import pytest

from common.entry_order_manager import (
    dispatch_order_failure_discord,
    dispatch_order_failure_with_retry,
    clear_order_failure_cooldown,
    dispatch_unwind_failure_alert,
    dispatch_unwind_failure_alert_with_retry,
    retry_pending_order_alerts,
    rollback_partial_entry,
)

# Dynamically import Tuesday and Thursday exit scripts
scripts_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))

tue_spec = importlib.util.spec_from_file_location(
    "tuesday_fno_exit_orders",
    os.path.join(scripts_dir, "Tuesday-Fno-Exit-Orders-V1.py")
)
tue_module = importlib.util.module_from_spec(tue_spec)
tue_spec.loader.exec_module(tue_module)

thu_spec = importlib.util.spec_from_file_location(
    "thursday_fno_exit_orders",
    os.path.join(scripts_dir, "Thursday-Fno-Exit-Orders-V1.py")
)
thu_module = importlib.util.module_from_spec(thu_spec)
thu_spec.loader.exec_module(thu_module)


class TestOrderFailureAlerting:
    """Test suite for order failure Discord dispatching and cooldown mechanisms."""

    @patch("requests.post")
    def test_dispatch_order_failure_without_redis(self, mock_post):
        """When no Redis is provided, sends alert directly without throttling."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        with patch.dict("os.environ", {"DISCORD_URL": "https://discord.com/api/webhooks/test"}):
            sent = dispatch_order_failure_discord(
                broker="upstox",
                action="PAIRED_BUY",
                track="5day_strategy",
                symbol="NIFTY 23500CE / 23700PE",
                quantity=65,
                error_reason="Insufficient margin in trading account",
            )
            assert sent is True
            assert mock_post.call_count == 1
            payload = mock_post.call_args[1]["json"]
            assert "🚨" in payload["content"]
            embed = payload["embeds"][0]
            assert "PAIRED MARKET BUY ORDER REJECTED" in embed["title"]
            assert embed["color"] == 0xFF0000

    @patch("requests.post")
    def test_redis_cooldown_suppression(self, mock_post):
        """First failure dispatches alert; second failure within cooldown is suppressed."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        mock_redis = MagicMock()
        # First call: key doesn't exist -> returns None -> alert sent, set(ex=600) called
        # Second call: key exists -> returns "1" -> alert suppressed
        mock_redis.get.side_effect = [None, "1"]

        with patch.dict("os.environ", {"DISCORD_URL": "https://discord.com/api/webhooks/test"}):
            sent_1 = dispatch_order_failure_discord(
                broker="upstox",
                action="SELL_MARKET",
                track="5day_strategy",
                symbol="NIFTY26NOV23500CE",
                quantity=65,
                error_reason="Upstox API 500 error",
                redis_client=mock_redis,
                cooldown_seconds=600,
            )
            assert sent_1 is True
            assert mock_post.call_count == 1
            assert mock_redis.set.call_count == 1

            sent_2 = dispatch_order_failure_discord(
                broker="upstox",
                action="SELL_MARKET",
                track="5day_strategy",
                symbol="NIFTY26NOV23500CE",
                quantity=65,
                error_reason="Upstox API 500 error",
                redis_client=mock_redis,
                cooldown_seconds=600,
            )
            # Suppressed!
            assert sent_2 is False
            # Still only 1 call to Discord
            assert mock_post.call_count == 1

    @patch("requests.post")
    def test_delivery_failure_does_not_claim_cooldown(self, mock_post):
        """If Discord webhook POST fails (e.g. 500 or network error), Redis cooldown is NOT set."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_post.return_value = mock_resp

        mock_redis = MagicMock()
        mock_redis.get.return_value = None

        with patch.dict("os.environ", {"DISCORD_URL": "https://discord.com/api/webhooks/test"}):
            sent = dispatch_order_failure_discord(
                broker="upstox",
                action="SELL_MARKET",
                track="5day_strategy",
                symbol="NIFTY26NOV23500CE",
                quantity=65,
                error_reason="Test error",
                redis_client=mock_redis,
                cooldown_seconds=600,
            )
            assert sent is False
            assert mock_post.call_count == 1
            # Cooldown must NOT be claimed on failed delivery
            assert mock_redis.set.call_count == 0

    @patch("requests.post")
    def test_clear_order_failure_cooldown(self, mock_post):
        """Clearing cooldown allows subsequent alerts to fire immediately."""
        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = ["fno:alert:fail:upstox:5day_strategy:NIFTY26NOV23500CE:SELL_MARKET"]

        clear_order_failure_cooldown(
            redis_client=mock_redis,
            broker="upstox",
            track="5day_strategy",
            symbol="NIFTY26NOV23500CE",
            action="SELL_MARKET",
        )
        assert mock_redis.delete.call_count == 1

    def test_missing_webhook_returns_false(self):
        """When no webhook is configured, returns False gracefully."""
        with patch.dict("os.environ", {"DISCORD_URL": "", "DISCORD_EXIT_5DAY": ""}, clear=True):
            sent = dispatch_order_failure_discord(
                broker="upstox",
                action="SELL_MARKET",
                track="5day_strategy",
                symbol="NIFTY26NOV23500CE",
                quantity=65,
                error_reason="Test error",
            )
            assert sent is False


class TestUnwindAlertingAndDeduplication:
    """Test suite for UNWIND_FAILED critical alert and duplicate BUY failure suppression."""

    @patch("requests.post")
    def test_unwind_alert_includes_original_buy_failure_details(self, mock_post):
        """Emergency unwind alert must include the original BUY failure details."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        with patch.dict("os.environ", {"DISCORD_URL": "https://discord.com/api/webhooks/test"}):
            sent = dispatch_unwind_failure_alert(
                broker="upstox",
                track="5day_strategy",
                details={
                    "call_filled": 65,
                    "put_filled": 0,
                    "error": "Recovery SELL failed",
                    "buy_failure_reason": "Verification failed: PUT order rejected due to margin shortfall",
                },
            )
            assert sent is True
            assert mock_post.call_count == 1
            payload = mock_post.call_args[1]["json"]
            embed = payload["embeds"][0]
            field_names = [f["name"] for f in embed["fields"]]
            assert "Original BUY Failure Details" in field_names
            detail_field = next(f for f in embed["fields"] if f["name"] == "Original BUY Failure Details")
            assert "margin shortfall" in detail_field["value"]

    def test_failed_buy_alert_is_queued_and_retried_without_order_execution(self, tmp_path):
        """A failed BUY notification is retried from SQLite; no broker executor is involved."""
        outbox_path = tmp_path / "pending_order_alerts.db"
        alert_kwargs = {
            "broker": "upstox",
            "action": "PAIRED_BUY",
            "track": "5day_strategy",
            "symbol": "CALL 23500CE / PUT 23700PE",
            "quantity": 65,
            "error_reason": "[FAILED_ROLLED_BACK] Broker rejected one leg",
        }
        with patch.dict(os.environ, {"ORDER_ALERT_OUTBOX_DB_PATH": str(outbox_path)}):
            with patch("common.entry_order_manager.dispatch_order_failure_discord", side_effect=[False, True]) as send_alert:
                sent_first = dispatch_order_failure_with_retry(
                    notification_id="paired-buy:CYCLE-20261103",
                    **alert_kwargs,
                )
                assert sent_first is False

                with sqlite3.connect(outbox_path) as conn:
                    pending = conn.execute(
                        "SELECT track, alert_type FROM pending_order_alerts"
                    ).fetchone()
                assert pending == ("5day_strategy", "order_failure")

                assert retry_pending_order_alerts(track="5day_strategy") == 1
                assert send_alert.call_count == 2
                assert send_alert.call_args.kwargs == alert_kwargs

                with sqlite3.connect(outbox_path) as conn:
                    assert conn.execute("SELECT COUNT(*) FROM pending_order_alerts").fetchone()[0] == 0

    def test_failed_unwind_alert_is_queued_and_retried(self, tmp_path):
        """An UNWIND_FAILED emergency notification remains pending until Discord accepts it."""
        outbox_path = tmp_path / "pending_order_alerts.db"
        details = {
            "call_filled": 65,
            "put_filled": 0,
            "error": "Recovery SELL rejected",
            "buy_failure_reason": "PUT BUY was rejected",
        }
        with patch.dict(os.environ, {"ORDER_ALERT_OUTBOX_DB_PATH": str(outbox_path)}):
            with patch("common.entry_order_manager.dispatch_unwind_failure_alert", side_effect=[False, True]) as send_alert:
                assert dispatch_unwind_failure_alert_with_retry(
                    broker="angelone",
                    track="THU-BUY",
                    details=details,
                ) is False
                assert retry_pending_order_alerts(track="3day_strategy") == 1
                assert send_alert.call_count == 2
                assert send_alert.call_args.kwargs == {
                    "broker": "angelone",
                    "track": "THU-BUY",
                    "details": details,
                }
                with sqlite3.connect(outbox_path) as conn:
                    assert conn.execute("SELECT COUNT(*) FROM pending_order_alerts").fetchone()[0] == 0

    def test_collectors_retry_pending_alerts_before_operating_day_gates(self):
        """Pending alerts get a retry opportunity even when the collector later skips the day."""
        import strategies.weekly_option_collector as weekly_collector
        import strategies.thursday_option_collector as thursday_collector

        with patch.object(weekly_collector, "retry_pending_order_alerts") as weekly_retry:
            with patch.object(weekly_collector, "check_nse_holiday", return_value=True):
                assert weekly_collector.collect_once() is True
            weekly_retry.assert_called_once_with(track="5day_strategy")

        with patch.object(thursday_collector, "retry_pending_order_alerts") as thursday_retry:
            with patch.object(thursday_collector, "check_nse_holiday", return_value=True):
                assert thursday_collector.collect_once() is True
            thursday_retry.assert_called_once_with(track="3day_strategy")


class TestTerminalAlertTitles:
    @pytest.mark.parametrize(
        ("action", "status", "expected_title", "expected_headline"),
        [
            (
                "SELL_MARKET_TERMINAL_REJECTED",
                "rejected",
                "IN-FLIGHT MARKET EXIT REJECTED BY BROKER",
                "BROKER ORDER REJECTED",
            ),
            (
                "SELL_MARKET_TERMINAL_REJECTED",
                "cancelled",
                "IN-FLIGHT MARKET EXIT CANCELLED WITHOUT FILL",
                "BROKER ORDER TERMINATED WITHOUT FILL",
            ),
            (
                "SELL_MARKET_TERMINAL_REJECTED",
                "expired",
                "IN-FLIGHT MARKET EXIT EXPIRED WITHOUT FILL",
                "BROKER ORDER TERMINATED WITHOUT FILL",
            ),
            (
                "SELL_SLM_TERMINAL_REJECTED",
                "cancelled",
                "IN-FLIGHT SL-M PROTECTION CANCELLED WITHOUT FILL",
                "BROKER ORDER TERMINATED WITHOUT FILL",
            ),
            (
                "SELL_SLM_TERMINAL_REJECTED",
                "expired",
                "IN-FLIGHT SL-M PROTECTION EXPIRED WITHOUT FILL",
                "BROKER ORDER TERMINATED WITHOUT FILL",
            ),
        ],
    )
    @patch("requests.post")
    def test_terminal_title_matches_broker_status(
        self, mock_post, action, status, expected_title, expected_headline
    ):
        response = MagicMock(status_code=204)
        mock_post.return_value = response
        with patch.dict(
            os.environ,
            {"DISCORD_EXIT_5DAY": "https://discord.com/api/webhooks/test"},
        ):
            assert dispatch_order_failure_discord(
                broker="upstox",
                action=action,
                track="5day_strategy",
                symbol="NIFTY26NOV23500CE",
                quantity=65,
                error_reason=f"Order terminal status: {status}",
                details={"broker_status": status},
            ) is True

        payload = mock_post.call_args.kwargs["json"]
        assert expected_title in payload["embeds"][0]["title"]
        assert expected_headline in payload["content"]
        if status in ("cancelled", "expired"):
            assert "RMS REJECTION" not in payload["embeds"][0]["title"].upper()


class TestTerminalOrderBookRejections:
    """Test suite for in-flight / delayed broker rejections in order book."""

    def test_tuesday_delayed_market_order_terminal_rejection(self):
        """Immediate market order previously accepted later rejected by Upstox RMS triggers alert."""
        pos = {
            "tradingsymbol": "NIFTY03NOV2622650CE",
            "quantity": 65,
            "instrument_token": "NSE_FO|53750",
            "product": "I",
            "_redis_target_qty": 65,
        }
        orders = [
            {
                "order_id": "MKT_REJ_123",
                "status": "rejected",
                "filled_quantity": 0,
                "status_message": "RMS: Margin shortfall",
                "transaction_type": "SELL",
                "order_type": "MARKET",
                "tag": "TUE-SL",
                "instrument_token": "NSE_FO|53750",
            }
        ]
        mock_redis = MagicMock()
        mock_redis.get.side_effect = lambda k: {
            "fno:upstox:trail:tue:NIFTY:03NOV26:22650:CE:NIFTY03NOV2622650CE:NSE_FO_53750:mode": "immediate",
            "fno:upstox:trail:tue:NIFTY:03NOV26:22650:CE:NIFTY03NOV2622650CE:NSE_FO_53750:market_order_id": "MKT_REJ_123",
        }.get(k, None)

        with patch.object(tue_module, "dispatch_order_failure_discord") as mock_dispatch:
            res = tue_module.manage_tuesday_exit_orders(
                token="test_token",
                positions=[pos],
                orders=orders,
                target_lots=1,
                force=True,
                redis_client=mock_redis,
                exit_immediately=True,
                execute=True,
            )
            assert mock_dispatch.call_count == 1
            kwargs = mock_dispatch.call_args[1]
            assert kwargs["action"] == "SELL_MARKET_TERMINAL_REJECTED"
            assert kwargs["broker"] == "upstox"
            assert kwargs["quantity"] == 65
            assert "MKT_REJ_123" in kwargs["error_reason"]
            assert "Margin shortfall" in kwargs["error_reason"]

    def test_tuesday_delayed_slm_order_terminal_rejection(self):
        """Trailing SL-M order previously accepted later rejected by Upstox RMS triggers alert."""
        pos = {
            "tradingsymbol": "NIFTY03NOV2622650CE",
            "quantity": 65,
            "average_price": 240.0,
            "instrument_token": "NSE_FO|53750",
            "product": "I",
            "_redis_target_qty": 65,
        }
        orders = [
            {
                "order_id": "SLM_REJ_456",
                "status": "cancelled",
                "filled_quantity": 0,
                "status_message": "RMS: Price circuit breached",
                "transaction_type": "SELL",
                "order_type": "SL-M",
                "tag": "TUE-SL",
                "instrument_token": "NSE_FO|53750",
            }
        ]
        mock_redis = MagicMock()
        mock_redis.get.side_effect = lambda k: {
            "fno:upstox:trail:tue:NIFTY:03NOV26:22650:CE:NIFTY03NOV2622650CE:NSE_FO_53750:order_id": "SLM_REJ_456",
            "fno:upstox:trail:tue:NIFTY:03NOV26:22650:CE:NIFTY03NOV2622650CE:NSE_FO_53750:max": "260.0",
            "fno:upstox:trail:tue:NIFTY:03NOV26:22650:CE:NIFTY03NOV2622650CE:NSE_FO_53750:sl": "220.0",
        }.get(k, None)

        with patch.object(tue_module, "fetch_ltp", return_value=250.0):
            with patch.object(tue_module, "dispatch_order_failure_discord") as mock_dispatch:
                res = tue_module.manage_tuesday_exit_orders(
                    token="test_token",
                    positions=[pos],
                    orders=orders,
                    target_lots=1,
                    force=True,
                    redis_client=mock_redis,
                    exit_immediately=False,
                    execute=False,  # dry-run still detects terminal rejection
                )
                assert mock_dispatch.call_count == 1
                kwargs = mock_dispatch.call_args[1]
                assert kwargs["action"] == "SELL_SLM_TERMINAL_REJECTED"
                assert kwargs["broker"] == "upstox"
                assert kwargs["quantity"] == 65
                assert "SLM_REJ_456" in kwargs["error_reason"]

    def test_thursday_delayed_market_order_terminal_rejection(self):
        """Immediate market order rejected by AngelOne triggers alert."""
        pos = {
            "tradingsymbol": "NIFTY07NOV2424500CE",
            "quantity": 65,
            "instrument_token": "53751",
            "product": "CARRYFORWARD",
            "_redis_target_qty": 65,
        }
        orders = [
            {
                "orderid": "THU_MKT_REJ",
                "status": "rejected",
                "filled_quantity": 0,
                "text": "RMS: Margin unavailable",
                "transaction_type": "SELL",
                "order_type": "MARKET",
                "tag": "THU-SL",
                "instrument_token": "53751",
            }
        ]
        mock_redis = MagicMock()
        mock_redis.get.side_effect = lambda k: {
            "fno:angelone:trail:thu:NIFTY:07NOV24:24500:CE:NIFTY07NOV2424500CE:53751:mode": "immediate",
            "fno:angelone:trail:thu:NIFTY:07NOV24:24500:CE:NIFTY07NOV2424500CE:53751:market_order_id": "THU_MKT_REJ",
        }.get(k, None)

        with patch.object(thu_module, "dispatch_order_failure_discord") as mock_dispatch:
            res = thu_module.manage_thursday_exit_orders(
                token="test_token",
                positions=[pos],
                orders=orders,
                target_lots=1,
                force=True,
                redis_client=mock_redis,
                exit_immediately=True,
                execute=True,
            )
            assert mock_dispatch.call_count == 1
            kwargs = mock_dispatch.call_args[1]
            assert kwargs["action"] == "SELL_MARKET_TERMINAL_REJECTED"
            assert kwargs["broker"] == "angelone"
            assert kwargs["quantity"] == 65
            assert "THU_MKT_REJ" in kwargs["error_reason"]

    def test_thursday_delayed_slm_order_terminal_rejection(self):
        """Trailing SL-M order cancelled by AngelOne exchange triggers alert."""
        pos = {
            "tradingsymbol": "NIFTY07NOV2424500CE",
            "quantity": 65,
            "average_price": 240.0,
            "instrument_token": "53751",
            "product": "CARRYFORWARD",
            "_redis_target_qty": 65,
        }
        orders = [
            {
                "orderid": "THU_SLM_CANC",
                "status": "cancelled",
                "filled_quantity": 0,
                "text": "Exchange cancelled order",
                "transaction_type": "SELL",
                "order_type": "STOPLOSS_MARKET",
                "tag": "THU-SL",
                "instrument_token": "53751",
            }
        ]
        mock_redis = MagicMock()
        mock_redis.get.side_effect = lambda k: {
            "fno:angelone:trail:thu:NIFTY:07NOV24:24500:CE:NIFTY07NOV2424500CE:53751:order_id": "THU_SLM_CANC",
            "fno:angelone:trail:thu:NIFTY:07NOV24:24500:CE:NIFTY07NOV2424500CE:53751:max": "260.0",
            "fno:angelone:trail:thu:NIFTY:07NOV24:24500:CE:NIFTY07NOV2424500CE:53751:sl": "220.0",
        }.get(k, None)

        with patch.object(thu_module, "fetch_ltp", return_value=250.0):
            with patch.object(thu_module, "dispatch_order_failure_discord") as mock_dispatch:
                res = thu_module.manage_thursday_exit_orders(
                    token="test_token",
                    positions=[pos],
                    orders=orders,
                    target_lots=1,
                    force=True,
                    redis_client=mock_redis,
                    exit_immediately=False,
                    execute=False,
                )
                assert mock_dispatch.call_count == 1
                kwargs = mock_dispatch.call_args[1]
                assert kwargs["action"] == "SELL_SLM_TERMINAL_REJECTED"
                assert kwargs["broker"] == "angelone"
                assert kwargs["quantity"] == 65
                assert "THU_SLM_CANC" in kwargs["error_reason"]


class TestImmediateExitBlockedAlerting:
    """Test that blocked immediate exits (e.g. SL-M cancel failed or missing) dispatch Discord alerts."""

    def test_tuesday_cancel_slm_failed_dispatches_alert(self):
        """When Upstox cancel_order fails during immediate exit, alert is dispatched."""
        pos = {
            "tradingsymbol": "NIFTY03NOV2622650CE",
            "quantity": 65,
            "instrument_token": "NSE_FO|53750",
            "product": "I",
            "_redis_target_qty": 65,
        }
        orders = [
            {
                "order_id": "SLM_OPEN_111",
                "status": "trigger pending",
                "filled_quantity": 0,
                "transaction_type": "SELL",
                "order_type": "SL-M",
                "tag": "TUE-SL",
                "instrument_token": "NSE_FO|53750",
            }
        ]
        mock_redis = MagicMock()
        mock_redis.get.side_effect = lambda k: {
            "fno:upstox:trail:tue:NIFTY:03NOV26:22650:CE:NIFTY03NOV2622650CE:NSE_FO_53750:mode": "immediate",
            "fno:upstox:trail:tue:NIFTY:03NOV26:22650:CE:NIFTY03NOV2622650CE:NSE_FO_53750:order_id": "SLM_OPEN_111",
        }.get(k, None)

        with patch.object(tue_module, "cancel_order", return_value={"success": False, "error": "Order cannot be cancelled"}):
            with patch.object(tue_module, "dispatch_order_failure_discord") as mock_dispatch:
                res = tue_module.manage_tuesday_exit_orders(
                    token="test_token",
                    positions=[pos],
                    orders=orders,
                    target_lots=1,
                    force=True,
                    redis_client=mock_redis,
                    exit_immediately=True,
                    execute=True,
                )
                assert mock_dispatch.call_count == 1
                kwargs = mock_dispatch.call_args[1]
                assert kwargs["action"] == "SELL_SLM_CANCEL_FAILED"
                assert kwargs["broker"] == "upstox"
                assert "SLM_OPEN_111" in kwargs["error_reason"]
                assert "cannot be cancelled" in kwargs["error_reason"]

    def test_tuesday_cancel_slm_missing_dispatches_alert(self):
        """When cancelled SL-M is missing from follow-up order book, alert is dispatched."""
        pos = {
            "tradingsymbol": "NIFTY03NOV2622650CE",
            "quantity": 65,
            "instrument_token": "NSE_FO|53750",
            "product": "I",
            "_redis_target_qty": 65,
        }
        orders = [
            {
                "order_id": "SLM_OPEN_222",
                "status": "trigger pending",
                "filled_quantity": 0,
                "transaction_type": "SELL",
                "order_type": "SL-M",
                "tag": "TUE-SL",
                "instrument_token": "NSE_FO|53750",
            }
        ]
        mock_redis = MagicMock()
        mock_redis.get.side_effect = lambda k: {
            "fno:upstox:trail:tue:NIFTY:03NOV26:22650:CE:NIFTY03NOV2622650CE:NSE_FO_53750:mode": "immediate",
            "fno:upstox:trail:tue:NIFTY:03NOV26:22650:CE:NIFTY03NOV2622650CE:NSE_FO_53750:order_id": "SLM_OPEN_222",
        }.get(k, None)

        with patch.object(tue_module, "cancel_order", return_value={"success": True}):
            with patch.object(tue_module, "fetch_order_book", return_value=[]):  # Missing after cancel
                with patch.object(tue_module, "dispatch_order_failure_discord") as mock_dispatch:
                    res = tue_module.manage_tuesday_exit_orders(
                        token="test_token",
                        positions=[pos],
                        orders=orders,
                        target_lots=1,
                        force=True,
                        redis_client=mock_redis,
                        exit_immediately=True,
                        execute=True,
                    )
                    assert mock_dispatch.call_count == 1
                    kwargs = mock_dispatch.call_args[1]
                    assert kwargs["action"] == "IMMEDIATE_EXIT_BLOCKED"
                    assert kwargs["broker"] == "upstox"
                    assert "SLM_OPEN_222" in kwargs["error_reason"]

    def test_thursday_cancel_slm_failed_dispatches_alert(self):
        """When AngelOne cancel_order fails during immediate exit, alert is dispatched."""
        pos = {
            "tradingsymbol": "NIFTY07NOV2424500CE",
            "quantity": 65,
            "instrument_token": "53751",
            "product": "CARRYFORWARD",
            "_redis_target_qty": 65,
        }
        orders = [
            {
                "orderid": "THU_SLM_333",
                "status": "trigger pending",
                "filled_quantity": 0,
                "transaction_type": "SELL",
                "order_type": "STOPLOSS_MARKET",
                "tag": "THU-SL",
                "instrument_token": "53751",
            }
        ]
        mock_redis = MagicMock()
        mock_redis.get.side_effect = lambda k: {
            "fno:angelone:trail:thu:NIFTY:07NOV24:24500:CE:NIFTY07NOV2424500CE:53751:mode": "immediate",
            "fno:angelone:trail:thu:NIFTY:07NOV24:24500:CE:NIFTY07NOV2424500CE:53751:order_id": "THU_SLM_333",
        }.get(k, None)

        with patch.object(thu_module, "cancel_order", return_value={"success": False, "error": "RMS: Cancel rejected"}):
            with patch.object(thu_module, "dispatch_order_failure_discord") as mock_dispatch:
                res = thu_module.manage_thursday_exit_orders(
                    token="test_token",
                    positions=[pos],
                    orders=orders,
                    target_lots=1,
                    force=True,
                    redis_client=mock_redis,
                    exit_immediately=True,
                    execute=True,
                )
                assert mock_dispatch.call_count == 1
                kwargs = mock_dispatch.call_args[1]
                assert kwargs["action"] == "SELL_SLM_CANCEL_FAILED"
                assert kwargs["broker"] == "angelone"
                assert "THU_SLM_333" in kwargs["error_reason"]

    def test_thursday_cancel_slm_missing_dispatches_alert(self):
        """When cancelled SL-M is missing from AngelOne order book, alert is dispatched."""
        pos = {
            "tradingsymbol": "NIFTY07NOV2424500CE",
            "quantity": 65,
            "instrument_token": "53751",
            "product": "CARRYFORWARD",
            "_redis_target_qty": 65,
        }
        orders = [
            {
                "orderid": "THU_SLM_444",
                "status": "trigger pending",
                "filled_quantity": 0,
                "transaction_type": "SELL",
                "order_type": "STOPLOSS_MARKET",
                "tag": "THU-SL",
                "instrument_token": "53751",
            }
        ]
        mock_redis = MagicMock()
        mock_redis.get.side_effect = lambda k: {
            "fno:angelone:trail:thu:NIFTY:07NOV24:24500:CE:NIFTY07NOV2424500CE:53751:mode": "immediate",
            "fno:angelone:trail:thu:NIFTY:07NOV24:24500:CE:NIFTY07NOV2424500CE:53751:order_id": "THU_SLM_444",
        }.get(k, None)

        with patch.object(thu_module, "cancel_order", return_value={"success": True}):
            with patch.object(thu_module, "fetch_order_book", return_value=[]):  # Missing after cancel
                with patch.object(thu_module, "dispatch_order_failure_discord") as mock_dispatch:
                    res = thu_module.manage_thursday_exit_orders(
                        token="test_token",
                        positions=[pos],
                        orders=orders,
                        target_lots=1,
                        force=True,
                        redis_client=mock_redis,
                        exit_immediately=True,
                        execute=True,
                    )
                    assert mock_dispatch.call_count == 1
                    kwargs = mock_dispatch.call_args[1]
                    assert kwargs["action"] == "IMMEDIATE_EXIT_BLOCKED"
                    assert kwargs["broker"] == "angelone"
                    assert "THU_SLM_444" in kwargs["error_reason"]


class TestCooldownRecovery:
    """Test that failure alert cooldowns are cleared upon recovery or flat position."""

    def test_cleanup_tuesday_clears_cooldown(self):
        """When Tuesday position is closed / flat, _cleanup_tuesday_leg_state clears cooldown."""
        mock_redis = MagicMock()
        keys = tue_module.build_trail_redis_keys("upstox", "NIFTY03NOV2622650CE", "NSE_FO|53750")
        with patch.object(tue_module, "clear_order_failure_cooldown") as mock_clear:
            tue_module._cleanup_tuesday_leg_state(mock_redis, keys, broker="upstox", symbol="NIFTY03NOV2622650CE")
            assert mock_clear.call_count == 1
            assert mock_clear.call_args[1]["broker"] == "upstox"
            assert mock_clear.call_args[1]["symbol"] == "NIFTY03NOV2622650CE"

    def test_cleanup_thursday_clears_cooldown(self):
        """When Thursday position is closed / flat, _cleanup_thursday_leg_state clears cooldown."""
        mock_redis = MagicMock()
        keys = thu_module.build_trail_redis_keys("angelone", "NIFTY07NOV2424500CE", "53751")
        with patch.object(thu_module, "clear_order_failure_cooldown") as mock_clear:
            thu_module._cleanup_thursday_leg_state(mock_redis, keys, broker="angelone", symbol="NIFTY07NOV2424500CE")
            assert mock_clear.call_count == 1
            assert mock_clear.call_args[1]["broker"] == "angelone"
            assert mock_clear.call_args[1]["symbol"] == "NIFTY07NOV2424500CE"

    def test_tuesday_unchanged_confirmed_active_clears_cooldown(self):
        """When active SL-M protection is confirmed open with trigger meeting stop, cooldown is cleared."""
        pos = {
            "tradingsymbol": "NIFTY03NOV2622650CE",
            "quantity": 65,
            "average_price": 200.0,
            "instrument_token": "NSE_FO|53750",
            "product": "I",
            "_redis_target_qty": 65,
        }
        orders = [
            {
                "order_id": "SLM_HEALTHY_555",
                "status": "trigger pending",
                "filled_quantity": 0,
                "pending_quantity": 65,
                "trigger_price": 210.0,  # Matches or exceeds required stop
                "transaction_type": "SELL",
                "order_type": "SL-M",
                "tag": "TUE-SL",
                "instrument_token": "NSE_FO|53750",
            }
        ]
        mock_redis = MagicMock()
        mock_redis.get.side_effect = lambda k: {
            "fno:upstox:trail:tue:NIFTY:03NOV26:22650:CE:NIFTY03NOV2622650CE:NSE_FO_53750:order_id": "SLM_HEALTHY_555",
            "fno:upstox:trail:tue:NIFTY:03NOV26:22650:CE:NIFTY03NOV2622650CE:NSE_FO_53750:max": "220.0",
            "fno:upstox:trail:tue:NIFTY:03NOV26:22650:CE:NIFTY03NOV2622650CE:NSE_FO_53750:sl": "210.0",
        }.get(k, None)

        with patch.object(tue_module, "fetch_ltp", return_value=220.0):
            with patch.object(tue_module, "clear_order_failure_cooldown") as mock_clear:
                res = tue_module.manage_tuesday_exit_orders(
                    token="test_token",
                    positions=[pos],
                    orders=orders,
                    target_lots=1,
                    force=True,
                    redis_client=mock_redis,
                    exit_immediately=False,
                    execute=True,
                )
                assert res["actions"][0]["status"] == "unchanged"
                assert mock_clear.call_count == 1
                assert mock_clear.call_args[1]["broker"] == "upstox"
                assert mock_clear.call_args[1]["symbol"] == "NIFTY03NOV2622650CE"

    def test_thursday_unchanged_confirmed_active_clears_cooldown(self):
        """When active AngelOne SL-M protection is confirmed open with trigger meeting stop, cooldown is cleared."""
        pos = {
            "tradingsymbol": "NIFTY07NOV2424500CE",
            "quantity": 65,
            "average_price": 200.0,
            "instrument_token": "53751",
            "product": "CARRYFORWARD",
            "_redis_target_qty": 65,
        }
        orders = [
            {
                "orderid": "THU_HEALTHY_666",
                "status": "trigger pending",
                "filled_quantity": 0,
                "pending_quantity": 65,
                "trigger_price": 210.0,
                "transaction_type": "SELL",
                "order_type": "STOPLOSS_MARKET",
                "tag": "THU-SL",
                "instrument_token": "53751",
            }
        ]
        mock_redis = MagicMock()
        mock_redis.get.side_effect = lambda k: {
            "fno:angelone:trail:thu:NIFTY:07NOV24:24500:CE:NIFTY07NOV2424500CE:53751:order_id": "THU_HEALTHY_666",
            "fno:angelone:trail:thu:NIFTY:07NOV24:24500:CE:NIFTY07NOV2424500CE:53751:max": "220.0",
            "fno:angelone:trail:thu:NIFTY:07NOV24:24500:CE:NIFTY07NOV2424500CE:53751:sl": "210.0",
        }.get(k, None)

        with patch.object(thu_module, "fetch_ltp", return_value=220.0):
            with patch.object(thu_module, "clear_order_failure_cooldown") as mock_clear:
                res = thu_module.manage_thursday_exit_orders(
                    token="test_token",
                    positions=[pos],
                    orders=orders,
                    target_lots=1,
                    force=True,
                    redis_client=mock_redis,
                    exit_immediately=False,
                    execute=True,
                )
                assert res["actions"][0]["status"] == "unchanged"
                assert mock_clear.call_count == 1
                assert mock_clear.call_args[1]["broker"] == "angelone"
                assert mock_clear.call_args[1]["symbol"] == "NIFTY07NOV2424500CE"
