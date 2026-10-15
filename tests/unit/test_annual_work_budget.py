from __future__ import annotations

import pytest

from finagent.core import annual_work_budget as budget
from finagent.core.annual_work_budget import (
    FINALIZATION_RESERVE_SECONDS,
    MAIN_REPORT_BUDGET_SECONDS,
    archived_budget_contract_v1,
    archived_budget_contract_v2,
    archived_budget_contract_v3,
    archived_budget_contract_v4,
    archived_runtime_stage_time_limits_v2,
    runtime_stage_time_limits,
)

_STAGE_KEYS = {
    "annual_discovery",
    "annual_investigation",
    "annual_issue_grouping",
    "annual_review",
}


def test_archived_budget_contract_v1_ignores_later_runtime_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    frozen = archived_budget_contract_v1(scope="new_run")
    monkeypatch.setattr(budget, "MAIN_REPORT_BUDGET_SECONDS", 90)
    monkeypatch.setattr(budget, "ISSUE_GROUPING_RESERVED_SECONDS", 9)
    monkeypatch.setattr(budget, "FINAL_REVIEW_BUDGET_SECONDS", 7)
    monkeypatch.setattr(budget, "REVIEW_REQUEST_LIMIT_CHARS", 1)

    assert archived_budget_contract_v1(scope="new_run") == frozen
    assert frozen["schema_version"] == "annual_analysis_budget_v1"
    assert "review_request_limit_chars" not in frozen
    assert "model_call_timeout_seconds" not in frozen



def test_runtime_stage_time_limits_scale_short_owner_inside_work_window() -> None:
    full = runtime_stage_time_limits(MAIN_REPORT_BUDGET_SECONDS)
    assert set(full) == _STAGE_KEYS
    assert all(type(value) is int and value >= 0 for value in full.values())
    assert sum(full.values()) <= MAIN_REPORT_BUDGET_SECONDS - FINALIZATION_RESERVE_SECONDS

    short = runtime_stage_time_limits(60)
    assert set(short) == _STAGE_KEYS
    assert all(type(value) is int and value >= 0 for value in short.values())
    assert sum(short.values()) == 60 - FINALIZATION_RESERVE_SECONDS
    assert short != full

    owner_below_full = runtime_stage_time_limits(300)
    assert set(owner_below_full) == _STAGE_KEYS
    assert sum(owner_below_full.values()) == 300 - FINALIZATION_RESERVE_SECONDS
    assert owner_below_full != full


@pytest.mark.parametrize("main_limit", [59, MAIN_REPORT_BUDGET_SECONDS + 1, 300.0, True])
def test_runtime_stage_time_limits_reject_out_of_range_owner(main_limit: object) -> None:
    with pytest.raises(ValueError, match="annual_main_report_limit_invalid"):
        runtime_stage_time_limits(main_limit)  # type: ignore[arg-type]


@pytest.mark.parametrize("scope", ["", "child", "NEW_RUN"])
def test_archived_budget_contracts_reject_unknown_scope(scope: str) -> None:
    with pytest.raises(ValueError, match="annual_budget_scope_invalid"):
        archived_budget_contract_v1(scope=scope)
    with pytest.raises(ValueError, match="annual_budget_scope_invalid"):
        archived_budget_contract_v2(scope=scope)
    with pytest.raises(ValueError, match="annual_budget_scope_invalid"):
        archived_budget_contract_v3(scope=scope)
    with pytest.raises(ValueError, match="annual_budget_scope_invalid"):
        archived_budget_contract_v4(scope=scope)


def test_archived_budget_contract_v2_ignores_later_runtime_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    frozen = archived_budget_contract_v2(scope="new_run")
    monkeypatch.setattr(budget, "MAIN_REPORT_BUDGET_SECONDS", 90)
    monkeypatch.setattr(budget, "ISSUE_GROUPING_RESERVED_SECONDS", 9)
    monkeypatch.setattr(budget, "MODEL_CALL_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(budget, "BUDGET_CONTRACT_VERSION", "annual_analysis_budget_v9")

    assert archived_budget_contract_v2(scope="new_run") == frozen
    assert frozen["schema_version"] == "annual_analysis_budget_v2"


def test_archived_budget_contract_v3_ignores_later_runtime_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    frozen = archived_budget_contract_v3(scope="new_run")
    monkeypatch.setattr(budget, "MAIN_REPORT_BUDGET_SECONDS", 90)
    monkeypatch.setattr(budget, "ISSUE_GROUPING_RESERVED_SECONDS", 9)
    monkeypatch.setattr(budget, "MODEL_CALL_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(budget, "REVIEW_REQUEST_LIMIT_CHARS", 1)

    assert archived_budget_contract_v3(scope="new_run") == frozen


def test_archived_budget_contract_v4_ignores_later_runtime_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    frozen = archived_budget_contract_v4(scope="resume")
    monkeypatch.setattr(budget, "MAIN_REPORT_BUDGET_SECONDS", 90)
    monkeypatch.setattr(budget, "GLOBAL_REQUEST_LIMIT_CHARS", 1)
    monkeypatch.setattr(budget, "REVIEW_REQUEST_LIMIT_CHARS", 1)
    monkeypatch.setattr(budget, "MODEL_CALL_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(budget, "BUDGET_CONTRACT_VERSION", "annual_analysis_budget_v99")

    assert archived_budget_contract_v4(scope="resume") == frozen


def test_archived_v2_stage_fit_stays_on_sealed_weights_and_owner_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archived_short = archived_runtime_stage_time_limits_v2(60)
    live_short = runtime_stage_time_limits(60)
    assert set(archived_short) == _STAGE_KEYS
    assert all(type(value) is int and value >= 0 for value in archived_short.values())
    assert sum(archived_short.values()) == 60 - FINALIZATION_RESERVE_SECONDS
    assert archived_short != live_short

    monkeypatch.setattr(budget, "ISSUE_GROUPING_RESERVED_SECONDS", 9)
    monkeypatch.setattr(budget, "FINAL_REVIEW_BUDGET_SECONDS", 7)
    assert archived_runtime_stage_time_limits_v2(60) == archived_short
    assert runtime_stage_time_limits(60) != archived_short

    with pytest.raises(ValueError, match="annual_main_report_limit_invalid"):
        archived_runtime_stage_time_limits_v2(MAIN_REPORT_BUDGET_SECONDS)
