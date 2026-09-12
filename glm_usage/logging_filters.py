"""Logging helpers: keep secrets out of the access log."""

from __future__ import annotations

import logging
import re

#: Query parameters whose value must never be written to a log.
SECRET_QUERY_PARAMS = ("key",)

_PATTERN = re.compile(r"([?&](?:" + "|".join(SECRET_QUERY_PARAMS) + r")=)[^&\s]*")


class RedactQueryFilter(logging.Filter):
    """Mask secret query parameters on log records.

    Sanic's access log records the full request URL (``%(request)s`` is
    ``"GET <url>"``), so ``/dashboard?key=...`` — the browser bootstrap for
    ``GLM_USAGE_API_KEY`` — would otherwise land in ``docker logs`` or journald
    in plaintext. Only the URL field is rewritten; message bodies are left alone
    so unrelated log lines are never mangled.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        request = getattr(record, "request", None)
        if isinstance(request, str) and "=" in request:
            record.request = _PATTERN.sub(r"\1***", request)
        return True


def install_secret_filter(*logger_names: str) -> None:
    """Attach :class:`RedactQueryFilter` to the given loggers (idempotently)."""
    for name in logger_names or ("sanic.access", "sanic.error", "sanic.root"):
        logger = logging.getLogger(name)
        if not any(isinstance(existing, RedactQueryFilter) for existing in logger.filters):
            logger.addFilter(RedactQueryFilter())
