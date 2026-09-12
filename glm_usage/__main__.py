"""``python -m glm_usage`` / ``glm-usage`` entrypoint."""

from __future__ import annotations

from sanic import Sanic
from sanic.worker.loader import AppLoader

APP_TARGET = "glm_usage.server:app"


def main() -> None:
    """Serve through Sanic's worker manager so ``GLM_USAGE_WORKERS`` works.

    The manager rebuilds the application inside every worker process, so the
    app has to be importable by name: that is why it lives in
    ``glm_usage.server`` instead of being created here. uvloop is picked up
    automatically by Sanic when it is installed.
    """
    loader = AppLoader(module_input=APP_TARGET)
    app = loader.load()
    settings = app.ctx.settings
    app.prepare(
        host=settings.host,
        port=settings.port,
        workers=settings.workers,
        access_log=settings.access_log,
        motd=False,
    )
    Sanic.serve(primary=app, app_loader=loader)


if __name__ == "__main__":
    main()
