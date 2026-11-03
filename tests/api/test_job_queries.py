"""Tests for the job listing filter (issue #24)."""

import pytest

from ump.api.job_queries import (
    DEFAULT_LIST_STATUSES,
    JOB_ACCESS_CONDITION,
    build_job_filter,
)


def _where_clause(conditions):
    # Mirrors how DBHandler.run_query combines conditions.
    return " AND ".join(conditions)


def _top_level_or_count(sql):
    """Count occurrences of OR that are not nested inside parentheses."""
    depth = 0
    count = 0
    i = 0
    while i < len(sql):
        char = sql[i]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and sql.startswith(" OR ", i):
            count += 1
        i += 1
    return count


def test_build_job_filter_rejects_anonymous_user():
    with pytest.raises(ValueError):
        build_job_filter({}, None)


def test_build_job_filter_restricts_to_user_without_interpolation():
    conditions, params = build_job_filter({}, "user-123")

    assert conditions[0] == JOB_ACCESS_CONDITION
    assert params["user_id"] == "user-123"
    assert "user-123" not in _where_clause(conditions)


def test_build_job_filter_keeps_access_or_inside_parentheses_with_filters():
    args = {"processID": ["provider:model-a"], "status": ["accepted"]}

    conditions, _ = build_job_filter(args, "user-123")

    assert len(conditions) == 4
    assert _top_level_or_count(_where_clause(conditions)) == 0


def test_build_job_filter_does_not_join_shares_table():
    conditions, _ = build_job_filter({}, "user-123")

    assert "JOIN" not in _where_clause(conditions).upper()
    assert "EXISTS" in conditions[0]


def test_build_job_filter_uses_default_statuses_when_none_given():
    _, params = build_job_filter({}, "user-123")

    assert params["status"] == DEFAULT_LIST_STATUSES


def test_build_job_filter_splits_process_id_and_provider_prefix():
    args = {"processID": ["provider:model-a", "provider:model-b"]}

    _, params = build_job_filter(args, "user-123")

    assert params["process_id"] == ("model-a", "model-b")
    assert params["provider_prefix"] == "provider"
