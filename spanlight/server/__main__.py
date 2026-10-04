"""Run the spanlight server: python -m spanlight.server"""

import os

import uvicorn

from .app import create_app

if __name__ == "__main__":
    uvicorn.run(
        create_app(),
        host=os.environ.get("SPANLIGHT_HOST", "127.0.0.1"),
        port=int(os.environ.get("SPANLIGHT_PORT", "4318")),
    )
