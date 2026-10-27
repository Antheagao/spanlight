"""Server settings, read once from the environment at startup.

`Settings.from_env` takes the environment as a mapping, so tests pass a dict
and a malformed value fails startup with a message naming the variable.
The ingest token is never included in any message.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4318
DEFAULT_MAX_REQUEST_BYTES = 16 * 1024 * 1024
# Connections uvicorn serves at once; the next gets a 503. Sync routes run on
# a pool of about 40 threads, so more than this only queues.
DEFAULT_MAX_CONCURRENCY = 64
MIN_TOKEN_CHARS = 32
LOOPBACK_ALLOWED_HOSTS = ("localhost", "127.0.0.1", "[::1]")


class SettingsError(ValueError):
    """A malformed or unsafe setting; the message names the variable."""


def is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _integer(env: Mapping[str, str], name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise SettingsError(f"{name} must be between {minimum} and {maximum}")
    return value


def _dev_origins(env: Mapping[str, str]) -> tuple[str, ...]:
    origins = []
    for raw in env.get("SPANLIGHT_DEV_ORIGINS", "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            parts = urlsplit(raw)
            valid = parts.scheme in ("http", "https") and parts.hostname and parts.port != 0
        except ValueError:
            valid = False
        if not valid or parts.path not in ("", "/") or parts.query or parts.fragment:
            raise SettingsError(
                "SPANLIGHT_DEV_ORIGINS entries must be origins such as http://localhost:5173"
            )
        origins.append(f"{parts.scheme}://{parts.netloc}".lower())
    return tuple(origins)


def _token(env: Mapping[str, str]) -> str | None:
    raw = env.get("SPANLIGHT_INGEST_TOKEN")
    if not raw:
        return None
    # A header value cannot carry whitespace or control characters, so such a
    # token could never be sent; refuse it instead of locking everyone out.
    if not raw.isascii() or any(c.isspace() or not c.isprintable() for c in raw):
        raise SettingsError(
            "SPANLIGHT_INGEST_TOKEN must be printable ASCII without whitespace"
        )
    if len(raw) < MIN_TOKEN_CHARS:
        raise SettingsError(f"SPANLIGHT_INGEST_TOKEN must be at least {MIN_TOKEN_CHARS} characters")
    return raw


@dataclass(frozen=True)
class Settings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    # Lower-case hostnames the Host header may name; ports are ignored.
    allowed_hosts: tuple[str, ...] = LOOPBACK_ALLOWED_HOSTS
    ingest_token: str | None = None
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    # Extra origins, besides the server's own, allowed to make cross-origin
    # requests (CORS and the write Origin check). Empty by default, because
    # trusting a port trusts every program that can serve one on this machine.
    dev_origins: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        host = env.get("SPANLIGHT_HOST", DEFAULT_HOST)
        if not host.strip():
            raise SettingsError("SPANLIGHT_HOST must not be empty")
        token = _token(env)
        if not is_loopback(host) and token is None:
            raise SettingsError(
                f"SPANLIGHT_INGEST_TOKEN is required when SPANLIGHT_HOST ({host}) is not a "
                "loopback address"
            )
        listed = env.get("SPANLIGHT_ALLOWED_HOSTS", "").split(",")
        extra = tuple(h.strip().lower() for h in listed if h.strip())
        return cls(
            host=host,
            port=_integer(env, "SPANLIGHT_PORT", DEFAULT_PORT, minimum=1, maximum=65535),
            allowed_hosts=(*LOOPBACK_ALLOWED_HOSTS, *extra),
            ingest_token=token,
            max_request_bytes=_integer(
                env,
                "SPANLIGHT_MAX_REQUEST_BYTES",
                DEFAULT_MAX_REQUEST_BYTES,
                minimum=1,
                maximum=1024**3,
            ),
            max_concurrency=_integer(
                env,
                "SPANLIGHT_MAX_CONCURRENCY",
                DEFAULT_MAX_CONCURRENCY,
                minimum=1,
                maximum=100_000,
            ),
            dev_origins=_dev_origins(env),
        )
