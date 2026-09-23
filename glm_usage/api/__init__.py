"""HTTP layer: the JSON API consumed by the dashboard and by scripts.

Importing this package registers every route on :data:`glm_usage.api.bp`.
"""

from __future__ import annotations

from . import (  # noqa: F401  (route registration)
    account,
    credentials,
    overview,
    proxy,
    proxy_meta,
    reset,
    sections,
    summary,
    system,
)
from .blueprint import bp
from .common import json_response
from .params import resolve_token
from .proxy import proxy_bp

__all__ = ["bp", "json_response", "proxy_bp", "resolve_token"]
