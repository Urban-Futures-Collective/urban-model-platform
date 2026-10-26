"""ETags for optimistic concurrency on PATCH (If-Match).

Derived from ``updated_at``, which every registry write refreshes, so an ETag
changes exactly when the stored row does.
"""

from __future__ import annotations

from datetime import datetime


def etag_for(updated_at: datetime | None) -> str:
    stamp = updated_at.isoformat() if updated_at else "new"
    return f'"{stamp}"'
