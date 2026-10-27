"""Run the spanlight server: python -m spanlight.server"""

import os

import uvicorn

from .app import create_app
from .settings import Settings, SettingsError


def main() -> None:
    try:
        settings = Settings.from_env(os.environ)
    except SettingsError as error:
        raise SystemExit(f"spanlight: {error}") from None
    uvicorn.run(
        create_app(settings=settings),
        host=settings.host,
        port=settings.port,
        limit_concurrency=settings.max_concurrency,
    )


if __name__ == "__main__":
    main()
