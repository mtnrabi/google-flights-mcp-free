"""Entry point: python -m src"""

from __future__ import annotations

import asyncio
import logging
import sys

from .server import build_server
from .settings import load_settings


def _configure_logging() -> None:
    # stderr only. MCP over stdio owns stdout, and the backend's habit of
    # print()-ing everything (backend/src/app.py:278 and friends) is the one
    # convention from this repo that must not be carried over here.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> None:
    _configure_logging()
    logger = logging.getLogger("mcp_server")

    settings = load_settings()
    server = build_server(settings)

    # Load OpenAI's connector egress ranges before serving. A failure here is
    # logged and tolerated: policy.decide() fails open while the ranges are
    # unloaded rather than blocking every ChatGPT caller.
    loaded = asyncio.run(server.classifier.refresh_openai_ranges())  # type: ignore[attr-defined]
    logger.info("loaded %d OpenAI connector prefixes", loaded)

    if settings.public_url.startswith("http://localhost"):
        logger.warning(
            "MCP_PUBLIC_URL is still the localhost default. The Lulu widget "
            "derives Claude's _meta.ui.domain from this value and silently "
            "renders nothing when it does not match the real connector URL -- "
            "which means zero CPM with no error. Set it before going live."
        )

    # Print the derived widget domain at startup. It is a hash of
    # MCP_PUBLIC_URL, and a mismatch is the one failure in this system that
    # produces no error anywhere -- the card just never renders and CPM stays
    # at zero. Having the value in the logs makes that debuggable.
    if settings.ads_enabled:
        try:
            from lulu_ads.widget import claude_apps_domain

            logger.info(
                "sponsored widget domain for %s -> %s",
                settings.public_url,
                claude_apps_domain(settings.public_url),
            )
        except Exception as exc:  # noqa: BLE001 - diagnostic only
            logger.warning("could not derive widget domain: %s", exc)

    logger.info(
        "serving on %s:%s (enforcement=%s, cap=%d/call)",
        settings.host,
        settings.port,
        settings.enforcement_mode,
        settings.max_backend_calls_per_tool_call,
    )
    server.run(transport="http", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
