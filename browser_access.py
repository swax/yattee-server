"""Dynamic browser-origin policy for cross-origin API clients."""

import logging
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import config
import settings as settings_module

logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _is_loopback_web_origin(origin: str) -> bool:
    """Return whether an HTTP(S) origin names the local machine directly."""
    try:
        parsed = urlsplit(origin)
        # Accessing .port also rejects malformed ports instead of silently accepting them.
        parsed.port
    except ValueError:
        return False
    hostname = parsed.hostname.casefold() if parsed.hostname else None
    return parsed.scheme in ("http", "https") and hostname in _LOOPBACK_HOSTS


class SettingsCORSMiddleware(CORSMiddleware):
    """Starlette CORS middleware whose trusted origins can change at runtime."""

    def is_allowed_origin(self, origin: str) -> bool:
        if super().is_allowed_origin(origin):
            return True

        try:
            current = settings_module.get_settings()
        except Exception:
            # Fail closed if the database/settings layer is unavailable during a request.
            logger.exception("Could not read browser-access settings")
            return False

        if origin in current.cors_allowed_origins:
            return True
        return current.cors_allow_localhost and _is_loopback_web_origin(origin)


def configure_cors(app: FastAPI) -> None:
    """Install CORS support with environment fallbacks and runtime-managed origins.

    The middleware is always installed so an administrator can enable an origin from
    the settings UI without restarting the process. With no configured origins its
    checks fail closed and no Access-Control-Allow-Origin header is returned.
    """
    environment_origins = [origin.strip() for origin in config.CORS_ORIGINS.split(",") if origin.strip()]
    has_specific_environment_policy = bool(environment_origins or config.CORS_ORIGIN_REGEX)
    allow_all = config.CORS_ALLOW_ALL and not has_specific_environment_policy
    origins = ["*"] if allow_all else environment_origins
    allow_credentials = config.CORS_ALLOW_CREDENTIALS and not allow_all
    origin_regex = None if allow_all else config.CORS_ORIGIN_REGEX

    if allow_all:
        logger.warning(
            "CORS configured to allow all origins from the environment. Credentials are disabled for security."
        )
    else:
        logger.info(
            "CORS enabled with %d environment origin(s), origin regex: %s, "
            "and runtime-managed browser origins; credentials: %s",
            len(environment_origins),
            origin_regex,
            allow_credentials,
        )

    app.add_middleware(
        SettingsCORSMiddleware,
        allow_origins=origins,
        allow_origin_regex=origin_regex,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )
