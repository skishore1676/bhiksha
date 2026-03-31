"""Runtime issue classification helpers."""

from __future__ import annotations


def classify_runtime_issue_category(*, error: str | None, event_type: str | None = None) -> str:
    haystack = " ".join(part for part in (event_type or "", error or "") if part).lower()
    if any(token in haystack for token in ("auth", "token", "401", "403", "unauthorized", "forbidden", "expired")):
        return "auth"
    if any(token in haystack for token in ("reconciliation", "portfolio", "sync", "broker_reconciliation")):
        return "reconciliation"
    if any(token in haystack for token in ("quote", "chain", "bar", "provider", "polygon", "schwab", "price", "warm_start", "feature")):
        return "data"
    if any(token in haystack for token in ("order", "preflight", "cancel", "fill", "target", "stop", "square_off")):
        return "order"
    return "exception"
