"""The entrypoint is small but easy to get wrong: the app has to be importable
by name, because Sanic's worker manager rebuilds it inside every worker."""

from __future__ import annotations

import importlib

from sanic import Sanic

from glm_usage import __main__ as entry


def test_app_loader_target_resolves() -> None:
    module_name, _, attribute = entry.APP_TARGET.partition(":")

    module = importlib.import_module(module_name)

    assert isinstance(getattr(module, attribute), Sanic)


def test_server_module_exposes_the_configured_app() -> None:
    from glm_usage import server

    paths = {route.path for route in server.app.router.routes}

    assert {"healthz", "dashboard"} <= paths
    assert "api/v1/summary" in paths


def test_main_is_exposed_for_the_console_script() -> None:
    assert callable(entry.main)
