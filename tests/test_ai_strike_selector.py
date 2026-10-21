"""
test_ai_strike_selector.py - Unit tests for interleaved AI strike selection fallback.
"""

import os
import json
import pytest
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.ai_strike_selector import (
    get_interleaved_strike_models,
    call_openrouter_strike_selector,
    select_strikes,
)


def test_interleaved_strike_models_sequence():
    """Verify 1-to-1 interleaved candidate sequence for strike selection."""
    with patch("common.ai_strike_selector.fetch_redis_models", return_value=["or-1", "or-2"]):
        with patch("common.ai_strike_selector.fetch_cheaper_redis_models", return_value=["ci-1", "ci-2"]):
            with patch.dict(os.environ, {
                "OPENROUTER_API_KEY": "fake-or-key",
                "CHEAPERINFERENCE_API_KEY": "fake-ci-key",
            }):
                candidates = get_interleaved_strike_models()
                assert candidates == [
                    ("openrouter", "or-1"),
                    ("cheaperinference", "ci-1"),
                    ("openrouter", "or-2"),
                    ("cheaperinference", "ci-2"),
                ]


@patch("requests.post")
def test_call_strike_selector_immediate_ci_fallback(mock_post):
    """
    CRITICAL RULE 5:
    When OR-1 fails, immediately query CI-1 before OR-2!
    """
    resp_or_fail = MagicMock()
    resp_or_fail.status_code = 502
    resp_or_fail.text = "Bad Gateway"

    resp_ci_success = MagicMock()
    resp_ci_success.status_code = 200
    resp_ci_success.json.return_value = {
        "choices": [{
            "message": {
                "content": json.dumps({
                    "call_strike": 22400,
                    "put_strike": 22600,
                    "selection_rationale": "High IV skew justifies ITM long gut positioning."
                })
            }
        }]
    }

    mock_post.side_effect = [resp_or_fail, resp_ci_success]

    candidates = [
        ("openrouter", "or-model-1"),
        ("cheaperinference", "ci-model-1"),
        ("openrouter", "or-model-2"),
    ]

    with patch.dict(os.environ, {
        "OPENROUTER_API_KEY": "fake-or-key",
        "CHEAPERINFERENCE_API_KEY": "fake-ci-key",
    }):
        parsed, model_used = call_openrouter_strike_selector(
            system_prompt="sys",
            user_prompt="usr",
            interleaved_candidates=candidates
        )

        assert model_used == "cheaperinference/ci-model-1"
        assert parsed["call_strike"] == 22400
        assert parsed["put_strike"] == 22600

        # Assert sequence of calls:
        # Call 1 -> OpenRouter (or-model-1)
        # Call 2 -> CheaperInference (ci-model-1)
        assert mock_post.call_count == 2
        assert mock_post.call_args_list[0][0][0] == "https://openrouter.ai/api/v1/chat/completions"
        assert mock_post.call_args_list[0][1]["json"]["model"] == "or-model-1"
        assert mock_post.call_args_list[1][0][0] == "https://api.cheaperinference.com/v1/chat/completions"
        assert mock_post.call_args_list[1][1]["json"]["model"] == "ci-model-1"
        assert mock_post.call_args_list[1][1]["headers"]["Authorization"] == "Bearer fake-ci-key"


@patch("requests.post")
def test_select_strikes_total_failure_falls_back_to_asym_rule(mock_post):
    """When all OR and CI models fail, select_strikes falls back to deterministic asymmetric rule."""
    resp_fail = MagicMock()
    resp_fail.status_code = 500
    resp_fail.text = "Internal Server Error"
    mock_post.return_value = resp_fail

    with patch("common.ai_strike_selector.fetch_redis_models", return_value=["or-1"]):
        with patch("common.ai_strike_selector.fetch_cheaper_redis_models", return_value=["ci-1"]):
            with patch("common.ai_strike_selector.load_market_context_for_strikes", return_value={
                "iv": 0.14,
                "composite_score": 0.50,
                "directional_bias": "BULLISH",
            }):
                with patch.dict(os.environ, {
                    "OPENROUTER_API_KEY": "fake-or-key",
                    "CHEAPERINFERENCE_API_KEY": "fake-ci-key",
                }):
                    p_strike, c_strike, sp, sc, mode_tag, desc, score, bias, cd, pd = select_strikes(
                        reference_ltp=22510.0,
                        moneyness_mode="ITM"
                    )

                    # Assert fallback to asymmetric rule was executed safely
                    assert "STATIC_ASYM" in mode_tag or "ASYM" in mode_tag
                    assert p_strike > 0 and c_strike > 0


@patch("requests.post")
def test_select_strikes_disabled_guard(mock_post):
    """Verify that ENABLE_LLM_INFERENCE=false bypasses AI calls completely and uses static rules."""
    with patch("common.ai_strike_selector.load_market_context_for_strikes", return_value={
        "iv": 0.14,
        "composite_score": 0.50,
        "directional_bias": "BULLISH",
    }):
        with patch.dict(os.environ, {
            "ENABLE_LLM_INFERENCE": "false",
            "OPENROUTER_API_KEY": "fake-or-key",
            "CHEAPERINFERENCE_API_KEY": "fake-ci-key",
        }):
            p_strike, c_strike, sp, sc, mode_tag, desc, score, bias, cd, pd = select_strikes(
                reference_ltp=22510.0,
                moneyness_mode="ITM"
            )

            # Zero HTTP requests should be issued
            assert mock_post.call_count == 0
            assert "STATIC_ASYM" in mode_tag


def test_empty_cheaper_redis_models_no_hardcoded_fallback():
    """Verify that when Redis cheaperinference_llm_models is empty, no hardcoded models are injected."""
    with patch("common.ai_strike_selector.fetch_redis_models", return_value=["or-1"]):
        with patch("common.ai_strike_selector.fetch_cheaper_redis_models", return_value=[]):
            with patch.dict(os.environ, {
                "OPENROUTER_API_KEY": "fake-or-key",
                "CHEAPERINFERENCE_API_KEY": "fake-ci-key",
            }):
                candidates = get_interleaved_strike_models()
                assert candidates == [("openrouter", "or-1")]
