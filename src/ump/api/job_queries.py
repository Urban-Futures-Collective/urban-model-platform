"""Builds the SQL filter for listing jobs.

Kept free of database access so the access rules can be unit-tested
without a running PostgreSQL instance.
"""

import re

from ump.api.models.job_status import JobStatus

DEFAULT_LIST_STATUSES = (
    JobStatus.running.value,
    JobStatus.successful.value,
    JobStatus.failed.value,
    JobStatus.dismissed.value,
)

# A job is visible to a user if they own it or it has been shared with them.
# EXISTS (instead of a JOIN on jobs_users) avoids one row per share, and the
# surrounding parentheses keep the OR from escaping the AND-joined conditions.
JOB_ACCESS_CONDITION = (
    "(j.user_id = %(user_id)s OR EXISTS ("
    "SELECT 1 FROM jobs_users u "
    "WHERE u.job_id = j.job_id AND u.user_id = %(user_id)s))"
)


def build_job_filter(
    args: dict[str, list[str]], user_id: str
) -> tuple[list[str], dict]:
    """Build WHERE conditions and bound parameters for listing a user's jobs.

    Args:
        args: Query string arguments as returned by
            ``request.args.to_dict(flat=False)``.
        user_id: Subject (``sub``) of the authenticated user.

    Returns:
        A tuple of SQL conditions (to be joined with ``AND``) and the
        parameters they reference.

    Raises:
        ValueError: If ``user_id`` is empty. Anonymous callers must not
            reach this function.
    """
    if not user_id:
        raise ValueError("user_id is required to list jobs")

    conditions = [JOB_ACCESS_CONDITION]
    query_params: dict = {"user_id": user_id}

    if args.get("processID"):
        # processID is actually "<provider_prefix>:<process_id>"; the name is
        # kept for OGC API - Processes compliance.
        process_ids = []
        provider_prefix = None
        for process_id_with_prefix in args["processID"]:
            match = re.search(r"(.*):(.*)", process_id_with_prefix)
            provider_prefix = match.group(1)
            process_ids.append(match.group(2))

        conditions.append("j.process_id IN %(process_id)s")
        query_params["process_id"] = tuple(process_ids)
        conditions.append("j.provider_prefix = %(provider_prefix)s")
        query_params["provider_prefix"] = provider_prefix

    if "status" in args:
        query_params["status"] = tuple(args["status"])
    else:
        query_params["status"] = DEFAULT_LIST_STATUSES
    conditions.append("j.status IN %(status)s")

    return conditions, query_params
