from __future__ import annotations

from copper_pilot_cli.copper_api import format_token_limits


def test_token_limits_summary_uses_member_safe_percentages_and_resets() -> None:
    summary = format_token_limits(
        {
            "window_usage_percentage": 84.6,
            "window_open": True,
            "window_ends_at": "2026-10-03T12:00:00Z",
            "week_ends_at": "2026-10-07T12:00:00Z",
            "week_reset_state": "resets",
            "overage_valid": True,
            "overage_usage_percentage": 12.4,
            "block_reason": None,
            "can_purchase_overage": False,
            "public_message": "",
        }
    )

    assert "Current usage window: 85% used" in summary
    assert "Usage limit refreshes:" in summary
    assert "Weekly balance refreshes:" in summary
    assert "Additional usage: 12% used" in summary
    assert "token" not in summary.lower()


def test_token_limits_summary_does_not_treat_week_state_as_admission_block() -> None:
    summary = format_token_limits(
        {
            "window_usage_percentage": 100,
            "window_open": False,
            "week_ends_at": "2026-10-07T12:00:00Z",
            "week_reset_state": "blocked",
            "overage_valid": True,
            "overage_usage_percentage": 1,
            "block_reason": None,
            "can_purchase_overage": False,
        }
    )

    assert "Blocked until" not in summary
    assert "Weekly balance refreshes:" in summary


def test_token_limits_summary_surfaces_purchase_eligibility() -> None:
    summary = format_token_limits(
        {
            "window_usage_percentage": 100,
            "window_open": False,
            "week_ends_at": "not-a-date",
            "week_reset_state": "blocked",
            "overage_valid": False,
            "block_reason": "weekly",
            "can_purchase_overage": True,
            "public_message": "Blocked until the weekly reset.",
        }
    )

    assert "Blocked until: not-a-date" in summary
    assert "Status: Blocked until the weekly reset." in summary
    assert "Additional usage is available" in summary
