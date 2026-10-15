"""Finite wall-clock and provider-attempt allowances for annual analysis."""

from __future__ import annotations

import math

MAIN_REPORT_BUDGET_SECONDS = 375
FINALIZATION_RESERVE_SECONDS = 15
WORK_DEADLINE_OFFSET_SECONDS = MAIN_REPORT_BUDGET_SECONDS - FINALIZATION_RESERVE_SECONDS

DISCOVERY_BUDGET_SECONDS = 150
FIXED_INVESTIGATION_BUDGET_SECONDS = 60
ISSUE_GROUPING_RESERVED_SECONDS = 90
FINAL_REVIEW_BUDGET_SECONDS = 45
UNALLOCATED_WORK_RESERVE_SECONDS = (
    WORK_DEADLINE_OFFSET_SECONDS
    - DISCOVERY_BUDGET_SECONDS
    - FIXED_INVESTIGATION_BUDGET_SECONDS
    - ISSUE_GROUPING_RESERVED_SECONDS
    - FINAL_REVIEW_BUDGET_SECONDS
)

GLOBAL_REQUEST_LIMIT_CHARS = 96000
REVIEW_REQUEST_LIMIT_CHARS = 128000
MODEL_CALL_TIMEOUT_SECONDS = 90.0
BUDGET_CONTRACT_VERSION = "annual_analysis_budget_v4"

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

# Immutable v2 archive defaults. Live owner-short values belong in the job
# projection, not this sealed contract.
_ARCHIVED_BUDGET_V2_COMMON = {
    "main_report_limit_seconds": 345,
    "work_deadline_seconds": 330,
    "finalization_reserve_seconds": 15,
    "shared_goal_limit": None,
    "shared_goal_remaining_at_start": None,
    "stage_time_limits_seconds": {
        "annual_discovery": 150,
        "annual_investigation": 60,
        "annual_issue_grouping": 60,
        "annual_review": 45,
    },
    "review_request_limit_chars": 40000,
    "model_call_timeout_seconds": 60.0,
}
_ARCHIVED_BUDGET_V3_COMMON = {
    "main_report_limit_seconds": 375,
    "work_deadline_seconds": 360,
    "finalization_reserve_seconds": 15,
    "shared_goal_limit": None,
    "shared_goal_remaining_at_start": None,
    "stage_time_limits_seconds": {
        "annual_discovery": 150,
        "annual_investigation": 60,
        "annual_issue_grouping": 90,
        "annual_review": 45,
    },
    "review_request_limit_chars": 40000,
    "model_call_timeout_seconds": 90.0,
}
_ARCHIVED_BUDGET_V4_COMMON = {
    "main_report_limit_seconds": 375,
    "work_deadline_seconds": 360,
    "finalization_reserve_seconds": 15,
    "shared_goal_limit": None,
    "shared_goal_remaining_at_start": None,
    "stage_time_limits_seconds": {
        "annual_discovery": 150,
        "annual_investigation": 60,
        "annual_issue_grouping": 90,
        "annual_review": 45,
    },
    "global_request_limit_chars": 96000,
    "review_request_limit_chars": 128000,
    "model_call_timeout_seconds": 90.0,
}



def _fit_stage_time_limits(
    main_report_limit_seconds: int,
    *,
    maximum_main_seconds: int,
    finalization_reserve_seconds: int,
    configured: dict[str, int],
) -> dict[str, int]:
    if type(main_report_limit_seconds) is not int or not 60 <= main_report_limit_seconds <= maximum_main_seconds:
        raise ValueError("annual_main_report_limit_invalid")
    available_work = main_report_limit_seconds - finalization_reserve_seconds
    configured_total = sum(configured.values())
    if available_work >= configured_total:
        return dict(configured)

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


def runtime_stage_time_limits(main_report_limit_seconds: int) -> dict[str, int]:
    """Fit activity stage ceilings inside a shorter API-owned task budget."""

    return _fit_stage_time_limits(
        main_report_limit_seconds,
        maximum_main_seconds=MAIN_REPORT_BUDGET_SECONDS,
        finalization_reserve_seconds=FINALIZATION_RESERVE_SECONDS,
        configured={
            "annual_discovery": DISCOVERY_BUDGET_SECONDS,
            "annual_investigation": FIXED_INVESTIGATION_BUDGET_SECONDS,
            "annual_issue_grouping": ISSUE_GROUPING_RESERVED_SECONDS,
            "annual_review": FINAL_REVIEW_BUDGET_SECONDS,
        },
    )


def archived_runtime_stage_time_limits_v1(main_report_limit_seconds: int) -> dict[str, int]:
    """Scale archived v1 default stages inside a historic 60..300 owner budget."""

    return _fit_stage_time_limits(
        main_report_limit_seconds,
        maximum_main_seconds=int(_ARCHIVED_BUDGET_V1_COMMON["main_report_limit_seconds"]),
        finalization_reserve_seconds=int(_ARCHIVED_BUDGET_V1_COMMON["finalization_reserve_seconds"]),
        configured={
            name: int(limit)
            for name, limit in dict(_ARCHIVED_BUDGET_V1_COMMON["stage_time_limits_seconds"]).items()
        },
    )


def archived_runtime_stage_time_limits_v2(main_report_limit_seconds: int) -> dict[str, int]:
    """Scale archived v2 default stages inside a historic 60..345 owner budget."""

    return _fit_stage_time_limits(
        main_report_limit_seconds,
        maximum_main_seconds=int(_ARCHIVED_BUDGET_V2_COMMON["main_report_limit_seconds"]),
        finalization_reserve_seconds=int(_ARCHIVED_BUDGET_V2_COMMON["finalization_reserve_seconds"]),
        configured={
            name: int(limit)
            for name, limit in dict(_ARCHIVED_BUDGET_V2_COMMON["stage_time_limits_seconds"]).items()
        },
    )



def archived_runtime_stage_time_limits_v3(main_report_limit_seconds: int) -> dict[str, int]:
    """Scale frozen v3 stages without borrowing activity defaults."""

    return _fit_stage_time_limits(
        main_report_limit_seconds,
        maximum_main_seconds=int(_ARCHIVED_BUDGET_V3_COMMON["main_report_limit_seconds"]),
        finalization_reserve_seconds=int(_ARCHIVED_BUDGET_V3_COMMON["finalization_reserve_seconds"]),
        configured={
            name: int(limit)
            for name, limit in dict(_ARCHIVED_BUDGET_V3_COMMON["stage_time_limits_seconds"]).items()
        },
    )


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


def archived_budget_contract_v2(*, scope: str) -> dict[str, object]:
    """Return the immutable v2 contract stored in activity execution_v6 archives.

    This is independent of later runtime defaults so sealed v6 reports stay
    readable. Short owner limits belong in the live job projection.
    """

    if scope not in {"new_run", "resume"}:
        raise ValueError("annual_budget_scope_invalid")
    scope_values = _ARCHIVED_BUDGET_V1_SCOPE[scope]
    return {
        "schema_version": "annual_analysis_budget_v2",
        "scope": scope,
        **{
            key: dict(value) if isinstance(value, dict) else value
            for key, value in _ARCHIVED_BUDGET_V2_COMMON.items()
        },
        "task_provider_attempt_limit": scope_values["task_provider_attempt_limit"],
        "stage_provider_attempt_limits": dict(scope_values["stage_provider_attempt_limits"]),
        "stage_provider_attempt_limits_status": "configured",
    }


def archived_budget_contract_v3(*, scope: str) -> dict[str, object]:
    """Return the immutable v3 contract stored in activity execution_v7 archives.

    This is independent of later runtime defaults so sealed v7 reports stay
    readable. Short owner limits belong in the live job projection.
    """

    if scope not in {"new_run", "resume"}:
        raise ValueError("annual_budget_scope_invalid")
    scope_values = _ARCHIVED_BUDGET_V1_SCOPE[scope]
    return {
        "schema_version": "annual_analysis_budget_v3",
        "scope": scope,
        **{
            key: dict(value) if isinstance(value, dict) else value
            for key, value in _ARCHIVED_BUDGET_V3_COMMON.items()
        },
        "task_provider_attempt_limit": scope_values["task_provider_attempt_limit"],
        "stage_provider_attempt_limits": dict(scope_values["stage_provider_attempt_limits"]),
        "stage_provider_attempt_limits_status": "configured",
    }


def archived_budget_contract_v4(*, scope: str) -> dict[str, object]:
    """Return frozen v9/v10 capacity policy; attempt and wall-clock limits are unchanged."""

    if scope not in {"new_run", "resume"}:
        raise ValueError("annual_budget_scope_invalid")
    scope_values = _ARCHIVED_BUDGET_V1_SCOPE[scope]
    return {
        "schema_version": "annual_analysis_budget_v4",
        "scope": scope,
        **{
            key: dict(value) if isinstance(value, dict) else value
            for key, value in _ARCHIVED_BUDGET_V4_COMMON.items()
        },
        "task_provider_attempt_limit": scope_values["task_provider_attempt_limit"],
        "stage_provider_attempt_limits": dict(scope_values["stage_provider_attempt_limits"]),
        "stage_provider_attempt_limits_status": "configured",
    }


def public_budget_contract(
    *, resumed: bool = False, shared_goal_remaining: int | None = None
) -> dict[str, object]:
    """Return the archived v4 default embedded in activity snapshots.

    The API job's live budget is a separate, owner-adjusted projection.
    """

    del shared_goal_remaining
    return archived_budget_contract_v4(scope="resume" if resumed else "new_run")
