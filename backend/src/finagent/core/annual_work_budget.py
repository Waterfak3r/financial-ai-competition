"""Finite wall-clock and provider-attempt allowances for annual analysis."""

from __future__ import annotations

import math

MAIN_REPORT_BUDGET_SECONDS = 300
FINALIZATION_RESERVE_SECONDS = 15
WORK_DEADLINE_OFFSET_SECONDS = MAIN_REPORT_BUDGET_SECONDS - FINALIZATION_RESERVE_SECONDS

DISCOVERY_BUDGET_SECONDS = 150
FIXED_INVESTIGATION_BUDGET_SECONDS = 60
ISSUE_GROUPING_RESERVED_SECONDS = 35
FINAL_REVIEW_BUDGET_SECONDS = 25
UNALLOCATED_WORK_RESERVE_SECONDS = (
    WORK_DEADLINE_OFFSET_SECONDS
    - DISCOVERY_BUDGET_SECONDS
    - FIXED_INVESTIGATION_BUDGET_SECONDS
    - ISSUE_GROUPING_RESERVED_SECONDS
    - FINAL_REVIEW_BUDGET_SECONDS
)

NEW_RUN_ATTEMPT_BUDGET = 30
RESUME_ATTEMPT_BUDGET = 20
DISCOVERY_ATTEMPT_BUDGET = 18
FIXED_INVESTIGATION_ATTEMPT_BUDGET = 2
ISSUE_GROUPING_ATTEMPT_BUDGET = 8
FINAL_REVIEW_ATTEMPT_BUDGET = 2

NEW_RUN_STAGE_ATTEMPT_BUDGETS = {
    "annual_discovery": DISCOVERY_ATTEMPT_BUDGET,
    "annual_investigation": FIXED_INVESTIGATION_ATTEMPT_BUDGET,
    "annual_issue_grouping": ISSUE_GROUPING_ATTEMPT_BUDGET,
    "annual_review": FINAL_REVIEW_ATTEMPT_BUDGET,
}
RESUME_STAGE_ATTEMPT_BUDGETS = {
    "annual_discovery": 14,
    "annual_investigation": 2,
    "annual_issue_grouping": 2,
    "annual_review": 2,
}

# Immutable values written by archived_budget_contract_v1. Keep these literal
# archive values separate from runtime defaults so a later policy update does
# not make already sealed execution_v4 reports unreadable.
_ARCHIVED_BUDGET_V1_COMMON = {
    "main_report_limit_seconds": 300,
    "work_deadline_seconds": 285,
    "finalization_reserve_seconds": 15,
    "shared_goal_limit": None,
    "shared_goal_remaining_at_start": None,
    "stage_time_limits_seconds": {
        "annual_discovery": 150,
        "annual_investigation": 60,
        "annual_issue_grouping": 35,
        "annual_review": 25,
    },
}
_ARCHIVED_BUDGET_V1_SCOPE = {
    "new_run": {
        "task_provider_attempt_limit": 30,
        "stage_provider_attempt_limits": {
            "annual_discovery": 18,
            "annual_investigation": 2,
            "annual_issue_grouping": 8,
            "annual_review": 2,
        },
    },
    "resume": {
        "task_provider_attempt_limit": 20,
        "stage_provider_attempt_limits": {
            "annual_discovery": 14,
            "annual_investigation": 2,
            "annual_issue_grouping": 2,
            "annual_review": 2,
        },
    },
}


def runtime_stage_time_limits(main_report_limit_seconds: int) -> dict[str, int]:
    """Fit default stage ceilings inside a shorter API-owned task budget."""

    if type(main_report_limit_seconds) is not int or not 60 <= main_report_limit_seconds <= MAIN_REPORT_BUDGET_SECONDS:
        raise ValueError("annual_main_report_limit_invalid")
    available_work = main_report_limit_seconds - FINALIZATION_RESERVE_SECONDS
    configured = {
        "annual_discovery": DISCOVERY_BUDGET_SECONDS,
        "annual_investigation": FIXED_INVESTIGATION_BUDGET_SECONDS,
        "annual_issue_grouping": ISSUE_GROUPING_RESERVED_SECONDS,
        "annual_review": FINAL_REVIEW_BUDGET_SECONDS,
    }
    configured_total = sum(configured.values())
    if available_work >= configured_total:
        return configured

    exact = {
        name: limit * available_work / configured_total
        for name, limit in configured.items()
    }
    rounded = {name: math.floor(seconds) for name, seconds in exact.items()}
    remaining = available_work - sum(rounded.values())
    order = sorted(configured, key=lambda name: (exact[name] - rounded[name], -list(configured).index(name)), reverse=True)
    for name in order[:remaining]:
        rounded[name] += 1
    return rounded


def archived_budget_contract_v1(*, scope: str) -> dict[str, object]:
    """Return the immutable v1 contract stored in execution_v4 archives.

    This is independent of current runtime defaults so later policy changes do
    not make already sealed execution_v4 reports unreadable. V1 records no
    shared-goal quota; runtime-known shared fields belong in the live job view.
    """

    if scope not in {"new_run", "resume"}:
        raise ValueError("annual_budget_scope_invalid")
    scope_values = _ARCHIVED_BUDGET_V1_SCOPE[scope]
    return {
        "schema_version": "annual_analysis_budget_v1",
        "scope": scope,
        **{
            key: dict(value) if isinstance(value, dict) else value
            for key, value in _ARCHIVED_BUDGET_V1_COMMON.items()
        },
        "task_provider_attempt_limit": scope_values["task_provider_attempt_limit"],
        "stage_provider_attempt_limits": dict(scope_values["stage_provider_attempt_limits"]),
        "stage_provider_attempt_limits_status": "configured",
    }


def public_budget_contract(
    *, resumed: bool = False, shared_goal_remaining: int | None = None
) -> dict[str, object]:
    """Return the archived v1 default embedded in execution_v4 snapshots.

    The API job's live budget is a separate, owner-adjusted projection.
    """

    del shared_goal_remaining
    return archived_budget_contract_v1(scope="resume" if resumed else "new_run")
