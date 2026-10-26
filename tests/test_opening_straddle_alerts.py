"""
Unit tests for strategies.opening_straddle_alerts.
"""
from unittest.mock import MagicMock, patch
import pytest

from strategies.opening_straddle_alerts import (
    resolve_straddle_discord_webhook,
    send_daily_session_report_from_db,
    send_decoupling_alert,
    send_straddle_entry_alert,
    send_straddle_exit_summary,
)
from strategies.opening_straddle_models import (
    BiasClassification,
    ConfidenceLevel,
    ExitReason,
    PreMarketIntelligence,
    SelectedLeg,
    StrategyState,
    StraddleSessionRecord,
    VolatilityRegime,
)


@pytest.fixture
def sample_intel():
    return PreMarketIntelligence(
        cds=0.35,
        coverage=0.90,
        confidence=ConfidenceLevel.HIGH,
        bias=BiasClassification.STRONG_BULLISH,
        components={},
        vix=14.5,
        vix_regime=VolatilityRegime.NORMAL,
        timestamp_utc="2026-10-27T03:44:00Z",
    )


@pytest.fixture
def sample_session_record(sample_intel):
    return StraddleSessionRecord(
        trade_date="2026-10-27",
        expiry_date="2026-11-03",
        atm_strike=22400,
        pre_market=sample_intel,
        call_entry_price=300.0,
        put_entry_price=200.0,
        total_debit_inr=32500.0,
        state=StrategyState.CLOSED,
        selected_leg=SelectedLeg.CALL,
        decision_time="09:20:00",
        closed_leg_exit_price=170.0,
        closed_leg_pnl_inr=-1950.0,
        runner_exit_price=360.0,
        runner_pnl_inr=3900.0,
        net_pnl_inr=1800.0,
        net_roi_pct=5.54,
        peak_roi_pct=6.10,
        spot_confirmed_ticks=3,
        exit_reason=ExitReason.PROFIT_TARGET,
        status="COMPLETED",
    )


def test_resolve_straddle_discord_webhook_priority(monkeypatch):
    # Case 1: DISCORD_OPENING_STRADDLE set
    monkeypatch.setenv("DISCORD_OPENING_STRADDLE", "https://discord.com/api/webhooks/opening")
    monkeypatch.setenv("DISCORD_URL", "https://discord.com/api/webhooks/general")
    assert resolve_straddle_discord_webhook() == "https://discord.com/api/webhooks/opening"

    # Case 2: DISCORD_OPENING_STRADDLE not set -> must return empty string (strict isolation, no fallback)
    monkeypatch.delenv("DISCORD_OPENING_STRADDLE", raising=False)
    assert resolve_straddle_discord_webhook() == ""


@patch("requests.post")
def test_send_straddle_entry_alert_no_webhook(mock_post, monkeypatch, sample_intel):
    monkeypatch.delenv("DISCORD_OPENING_STRADDLE", raising=False)
    monkeypatch.delenv("DISCORD_URL", raising=False)
    monkeypatch.delenv("DISCORD_WATCHDOG_URL", raising=False)

    success = send_straddle_entry_alert(
        atm_strike=22400,
        call_entry=300.0,
        put_entry=200.0,
        total_debit_inr=32500.0,
        intel=sample_intel,
        trade_date="2026-10-27",
    )
    assert success is False
    assert not mock_post.called


@patch("requests.post")
def test_send_straddle_entry_alert_success(mock_post, monkeypatch, sample_intel):
    monkeypatch.setenv("DISCORD_OPENING_STRADDLE", "https://discord.com/api/webhooks/test")
    mock_resp = MagicMock()
    mock_resp.status_code = 204
    mock_post.return_value = mock_resp

    success = send_straddle_entry_alert(
        atm_strike=22400,
        call_entry=300.0,
        put_entry=200.0,
        total_debit_inr=32500.0,
        intel=sample_intel,
        trade_date="2026-10-27",
    )
    assert success is True
    assert mock_post.called
    kwargs = mock_post.call_args[1]
    embed = kwargs["json"]["embeds"][0]
    assert "[PAPER TRADING]" in embed["title"]
    assert "22400" in embed["description"]
    assert "300.00" in embed["fields"][0]["value"]


@patch("requests.post")
def test_send_decoupling_alert_all_branches(mock_post, monkeypatch):
    monkeypatch.setenv("DISCORD_OPENING_STRADDLE", "https://discord.com/api/webhooks/test")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_post.return_value = mock_resp

    # CALL Runner
    res_call = send_decoupling_alert(
        checkpoint_name="09:20:00",
        selected_runner=SelectedLeg.CALL,
        closed_leg=SelectedLeg.PUT,
        closed_exit_price=180.0,
        closed_pnl_inr=-1300.0,
        closed_pnl_pct=-10.0,
        live_roi_pct=-2.0,
    )
    assert res_call is True
    embed_call = mock_post.call_args[1]["json"]["embeds"][0]
    assert "Bullish Decoupling" in embed_call["title"]

    # PUT Runner
    res_put = send_decoupling_alert(
        checkpoint_name="09:20:00",
        selected_runner=SelectedLeg.PUT,
        closed_leg=SelectedLeg.CALL,
        closed_exit_price=270.0,
        closed_pnl_inr=-1950.0,
        closed_pnl_pct=-10.0,
        live_roi_pct=-3.0,
    )
    assert res_put is True
    embed_put = mock_post.call_args[1]["json"]["embeds"][0]
    assert "Bearish Decoupling" in embed_put["title"]

    # SCRATCH
    res_scratch = send_decoupling_alert(
        checkpoint_name="09:25:00",
        selected_runner=SelectedLeg.SCRATCH,
        closed_leg=SelectedLeg.SCRATCH,
        closed_exit_price=0.0,
        closed_pnl_inr=-650.0,
        closed_pnl_pct=-1.0,
        live_roi_pct=-1.0,
    )
    assert res_scratch is True
    embed_scratch = mock_post.call_args[1]["json"]["embeds"][0]
    assert "Scratch" in embed_scratch["title"]


@patch("requests.post")
def test_send_straddle_exit_summary(mock_post, monkeypatch, sample_session_record):
    monkeypatch.setenv("DISCORD_OPENING_STRADDLE", "https://discord.com/api/webhooks/test")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_post.return_value = mock_resp

    # 1. Profit Target
    sample_session_record.exit_reason = ExitReason.PROFIT_TARGET
    assert send_straddle_exit_summary(sample_session_record) is True
    assert "PROFIT_TARGET" in mock_post.call_args[1]["json"]["embeds"][0]["title"]

    # 2. Stop Loss
    sample_session_record.exit_reason = ExitReason.STOP_LOSS
    assert send_straddle_exit_summary(sample_session_record) is True
    assert "STOP_LOSS" in mock_post.call_args[1]["json"]["embeds"][0]["title"]

    # 3. Trailing Stop
    sample_session_record.exit_reason = ExitReason.TRAILING_STOP
    assert send_straddle_exit_summary(sample_session_record) is True
    assert "TRAILING_STOP" in mock_post.call_args[1]["json"]["embeds"][0]["title"]

    # 4. Scratch
    sample_session_record.exit_reason = ExitReason.SCRATCH
    assert send_straddle_exit_summary(sample_session_record) is True
    assert "SCRATCH" in mock_post.call_args[1]["json"]["embeds"][0]["title"]


@patch("requests.post", side_effect=Exception("Network Timeout"))
def test_send_alert_network_failure_handled_gracefully(mock_post, monkeypatch, sample_session_record):
    monkeypatch.setenv("DISCORD_OPENING_STRADDLE", "https://discord.com/api/webhooks/test")
    success = send_straddle_exit_summary(sample_session_record)
    assert success is False


@patch("requests.post")
def test_send_daily_session_report_from_db(mock_post, monkeypatch, tmp_path):
    monkeypatch.setenv("DISCORD_OPENING_STRADDLE", "https://discord.com/api/webhooks/test")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_post.return_value = mock_resp

    test_db = str(tmp_path / "test_sessions.db")
    import sqlite3
    from strategies.opening_straddle_engine import init_opening_straddle_db
    init_opening_straddle_db(test_db)

    with sqlite3.connect(test_db) as conn:
        conn.execute(
            """
            INSERT INTO opening_straddle_sessions (
                trade_date, expiry_date, atm_strike, composite_directional_score,
                cds_coverage, cds_confidence, bias_classification, vix_open,
                call_entry_price, put_entry_price, total_debit_inr,
                selected_leg, decision_time, closed_leg_exit_price, closed_leg_pnl_inr,
                runner_exit_price, runner_pnl_inr, net_pnl_inr, net_roi_pct,
                peak_roi_pct, spot_confirmed_ticks, exit_reason, status
            ) VALUES (
                '2026-10-26', '2026-11-03', 22400, -0.498,
                1.0, 'HIGH', 'STRONG_BEARISH', 17.89,
                307.51, 212.62, 33808.98,
                'PUT', '09:20:00', 270.0, -2438.15,
                275.0, 4054.70, 1466.55, 4.34,
                4.80, 2, 'PROFIT_TARGET', 'COMPLETED'
            )
            """
        )
        conn.commit()

    success = send_daily_session_report_from_db(db_path=test_db)
    assert success is True
    assert mock_post.call_count == 1
    embed = mock_post.call_args[1]["json"]["embeds"][0]
    assert "PROFIT_TARGET" in embed["title"]
    assert "22400" in embed["description"]

    # Second call should skip sending duplicate (call_count remains 1)
    success_duplicate = send_daily_session_report_from_db(db_path=test_db)
    assert success_duplicate is True
    assert mock_post.call_count == 1  # No duplicate request made!

