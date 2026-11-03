"""Tests for the job listing filter (issue #24)."""

from ump.api.job_queries import (
    DEFAULT_LIST_STATUSES,
    JOB_ACCESS_CONDITION,
    build_job_filter,
)


def _has_top_level_or(sql):
    """Return True if an OR appears outside of any parentheses."""
    depth = 0
    for i, char in enumerate(sql):
        depth += {"(": 1, ")": -1}.get(char, 0)
        if depth == 0 and sql.startswith(" OR ", i):
            return True
    return False


def test_build_job_filter_restricts_to_user_without_interpolation():
    conditions, params = build_job_filter({}, "user-123")

    assert conditions[0] == JOB_ACCESS_CONDITION
    assert params["user_id"] == "user-123"
    assert "user-123" not in " AND ".join(conditions)


def test_build_job_filter_keeps_access_or_inside_parentheses_with_filters():
    args = {"processID": ["provider:model-a"], "status": ["accepted"]}

    conditions, _ = build_job_filter(args, "user-123")

    # DBHandler.run_query joins conditions with AND.
    assert len(conditions) == 4
    assert not _has_top_level_or(" AND ".join(conditions))


def test_build_job_filter_uses_default_statuses_when_none_given():
    _, params = build_job_filter({}, "user-123")

    assert params["status"] == DEFAULT_LIST_STATUSES


def test_build_job_filter_splits_process_id_and_provider_prefix():
    args = {"processID": ["provider:model-a", "provider:model-b"]}

    _, params = build_job_filter(args, "user-123")

    assert params["process_id"] == ("model-a", "model-b")
    assert params["provider_prefix"] == "provider"
