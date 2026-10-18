from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from copper_pilot_cli.copper_api import (
    describe_reset,
    format_token_limits,
    is_usage_block,
    limits_reset_time,
    usage_error_message,
    usage_limit_message,
)

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 10, 19, 0, 57, tzinfo=IST)


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


@pytest.mark.parametrize(
    ("reset", "expected"),
    [
        ("2026-10-18T21:50:00Z", "at 3:20 AM (in 2h 23m)"),
        ("2026-10-18T19:30:00+00:00", "at 1:00 AM (in 3m)"),
        ("2026-10-18T19:27:30Z", "at 12:57 AM (in under a minute)"),
        ("2026-10-18T22:27:00Z", "at 3:57 AM (in 3h)"),
        ("2026-10-21T02:56:47Z", "on Oct 21 at 8:26 AM (in 2d 7h)"),
        ("2026-10-20T19:27:00Z", "on Oct 21 at 12:57 AM (in 2d)"),
    ],
)
def test_describe_reset_renders_local_time_and_countdown(reset, expected) -> None:
    assert describe_reset(reset, NOW) == expected


@pytest.mark.parametrize("reset", [None, "", "soon", "2026-10-18T19:00:00Z", "2026-10-19T03:20"])
def test_describe_reset_ignores_missing_past_and_naive_times(reset) -> None:
    assert describe_reset(reset, NOW) is None


def test_describe_reset_defaults_to_the_current_local_time() -> None:
    reset = (datetime.now(UTC) + timedelta(hours=2, minutes=23, seconds=30)).isoformat()
    assert describe_reset(reset) is not None
    assert describe_reset(reset).endswith("(in 2h 23m)")


def test_window_limit_message_names_the_local_reset_time() -> None:
    message = usage_limit_message(
        block_reason="window",
        resets_at="2026-10-18T21:50:00.121000Z",
        can_purchase_overage=False,
        hosted_message="You have reached the current usage window. Usage limit resets at x.",
        now=NOW,
    )
    assert message == (
        "CopperPilot usage limit reached: the current usage window is used up. "
        "It resets at 3:20 AM (in 2h 23m)."
    )


def test_weekly_limit_message_offers_additional_usage() -> None:
    message = usage_limit_message(
        block_reason="weekly",
        resets_at="2026-10-21T02:56:47Z",
        can_purchase_overage=True,
        hosted_message="",
        now=NOW,
    )
    assert message == (
        "CopperPilot weekly usage limit reached. It resets on Oct 21 at 8:26 AM (in 2d 7h). "
        "You can buy additional usage from your CopperPilot billing page."
    )


def test_limit_message_without_a_reset_time_says_so() -> None:
    message = usage_limit_message(
        block_reason="window", resets_at=None, can_purchase_overage=None, hosted_message="x"
    )
    assert message == (
        "CopperPilot usage limit reached: the current usage window is used up. "
        "Reset time unavailable; try again later."
    )


@pytest.mark.parametrize(
    ("reason", "hosted", "expected"),
    [
        (
            "cost_cap",
            "Reached usage cap. Contact team admin.",
            "Reached usage cap. Contact team admin.",
        ),
        ("billing", "  ", "CopperPilot usage limit reached."),
        ("busy", "CopperPilot is busy.", "CopperPilot is busy."),
    ],
)
def test_untimed_blocks_keep_the_hosted_copy(reason, hosted, expected) -> None:
    message = usage_limit_message(
        block_reason=reason,
        resets_at="2026-10-18T21:50:00Z",
        can_purchase_overage=True,
        hosted_message=hosted,
        now=NOW,
    )
    assert message == expected


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    [
        (402, "window", True),
        (429, "busy", True),
        (402, None, False),
        (429, None, False),
        (500, "window", False),
    ],
)
def test_is_usage_block_requires_a_block_reason(status, reason, expected) -> None:
    assert is_usage_block(status, reason) is expected


@pytest.mark.parametrize(
    ("limits", "expected"),
    [
        (
            {"block_reason": "window", "window_ends_at": "W", "week_ends_at": "K"},
            "W",
        ),
        ({"block_reason": "weekly", "window_ends_at": "W", "week_ends_at": "K"}, "K"),
        ({"block_reason": None, "window_open": True, "window_ends_at": "W"}, None),
        ({"block_reason": "cost_cap", "window_ends_at": "W"}, None),
        ({"block_reason": "window", "window_ends_at": " "}, None),
    ],
)
def test_limits_reset_time_follows_the_block_reason(limits, expected) -> None:
    assert limits_reset_time(limits) == expected


def test_usage_error_message_rewrites_only_usage_blocks() -> None:
    plan_gate = {"message": "Upgrade to a paid plan to use Intelligence.", "status": 402}
    assert usage_error_message(plan_gate) == "Upgrade to a paid plan to use Intelligence."
    assert usage_error_message("plain failure") == "plain failure"
    reset = (datetime.now(UTC) + timedelta(hours=1, minutes=5, seconds=30)).isoformat()
    window = {
        "message": f"You have reached the current usage window. Usage limit resets at {reset}.",
        "status": 402,
        "block_reason": "window",
        "resets_at": reset,
    }
    message = usage_error_message(window)
    assert message.startswith("CopperPilot usage limit reached: the current usage window")
    assert reset not in message
    assert "(in 1h 5m)" in message
