"""
Per-host `_meta.ui.domain` on the sponsored widgets.

WHY THIS EXISTS
---------------
MCP Apps hosts validate the widget's `ui.domain`. claude.ai computes
`sha256(<the connector URL the user added>)[:32] + ".claudemcpcontent.com"`
and refuses to render the frame when the resource advertises anything else.
On 2026-09-15 that cost us every rendered impression on Claude since launch,
because `MCP_PUBLIC_URL` was the bare origin and the hash was one path
segment out (`settings._canonical_public_url` is where that story is told).

The same validation is what makes a RENAME dangerous. From 2026-09-15 this
deployment answers on two hostnames -- `free-trial.flightpowers.com` for
everyone new, `google-flights-lulu.flightpowers.com` for the connectors that
were added before it and that we promised would keep working. A connector
added on the old host hashes the OLD URL, so a single baked-in domain would
put exactly one of the two populations back into "Unable to reach
FlightPowers" with a $0 CPM behind it.

`lulu_ads.widget.register_sponsored_widget` and our own
`server._register_result_widget` both bake the domain in at REGISTRATION
time, once per process. So the rewrite happens on the way out, per request,
here.

WHAT IT TOUCHES
---------------
Only `meta["ui"]["domain"]`, and only when the value already looks like an
MCP Apps content domain (`*.claudemcpcontent.com`). Anything else in the
registry -- a resource with no `ui` meta, a domain somebody set by hand --
is handed back untouched, and the resource objects themselves are never
mutated: `model_copy` gives each response its own view, because the registry
is shared by every concurrent request on the instance.

Both `resources/list` AND `resources/read` carry the meta (verified against
fastmcp 3.4.7), so both are covered -- the read arrives as a `ResourceResult`
rather than a list, and claude.ai validates the domain on the READ, so a
middleware that only handled the list would leave the red chip exactly where
it was. A request with no HTTP scope -- an
in-memory client in the test suite, stdio -- gets the canonical domain,
which is what it got before this module existed.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp.server.middleware import Middleware

logger = logging.getLogger(__name__)

#: The suffix every MCP Apps content domain carries. A `ui.domain` that does
#: not end in it was not produced by `claude_apps_domain`, so it is not ours
#: to rewrite.
APPS_DOMAIN_SUFFIX = ".claudemcpcontent.com"


def _request_host() -> str:
    """The `Host` of the live HTTP request, or "" when there is not one."""
    try:
        from fastmcp.server.dependencies import (  # noqa: PLC0415
            get_http_request,
        )

        return (get_http_request().headers.get("host") or "").strip()
    except Exception:  # noqa: BLE001 - no HTTP request is a normal state
        return ""


class HostWidgetDomainMiddleware(Middleware):
    """Rewrites `ui.domain` to match the hostname the caller connected to.

    `settings` is the whole Settings object rather than a pair of strings so
    that `public_url_for_host` stays the ONE place that decides which
    hostname is which -- the OAuth gate and `/health` ask the same question
    of the same method.
    """

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._cache: dict[str, str] = {}

    # ── the hash ─────────────────────────────────────────────────────────

    def domain_for_host(self, host: str) -> str:
        """`claude_apps_domain` of the connector URL for `host`.

        Cached per host: it is a sha256 of a short string, but it is on the
        path of every `resources/list`, and the set of hosts is two.
        """
        url = self._settings.public_url_for_host(host)
        cached = self._cache.get(url)
        if cached is None:
            from lulu_ads.widget import claude_apps_domain  # noqa: PLC0415

            cached = claude_apps_domain(url)
            self._cache[url] = cached
        return cached

    # ── the rewrite ──────────────────────────────────────────────────────

    def _retarget(self, meta: Any, domain: str) -> dict | None:
        """A copy of `meta` with our `ui.domain` replaced, or None for no change."""
        if not isinstance(meta, dict):
            return None
        ui = meta.get("ui")
        if not isinstance(ui, dict):
            return None
        current = ui.get("domain")
        if not isinstance(current, str) or not current.endswith(APPS_DOMAIN_SUFFIX):
            return None
        if current == domain:
            return None
        out = dict(meta)
        out["ui"] = {**ui, "domain": domain}
        return out

    async def on_list_resources(self, context, call_next):
        resources = await call_next(context)
        domain = self.domain_for_host(_request_host())
        out = []
        for resource in resources:
            meta = self._retarget(getattr(resource, "meta", None), domain)
            out.append(resource if meta is None else resource.model_copy(update={"meta": meta}))
        return out

    def _retarget_blocks(self, blocks, domain: str):
        """`(rewritten blocks, changed?)`, leaving untouched blocks as they are."""
        out = []
        changed = False
        for block in blocks:
            meta = self._retarget(getattr(block, "meta", None), domain)
            if meta is None:
                out.append(block)
                continue
            changed = True
            out.append(block.model_copy(update={"meta": meta}))
        return out, changed

    async def on_read_resource(self, context, call_next):
        """`resources/read` carries the meta too -- and it is the call that
        matters most.

        fastmcp 3.4.7 hands this hook a `ResourceResult` (a pydantic model
        with `contents: list[ResourceContent]` and its own `meta`), NOT a
        list. An earlier version of this middleware early-returned on
        anything that was not a list, so `resources/list` was rewritten and
        `resources/read` was not: an alias-hostname caller got the canonical
        domain on the read and claude.ai refused the frame -- the #487
        incident all over again, for exactly the population this change
        exists to protect. Both shapes are handled now, and the list one is
        kept because nothing guarantees the hook's type across a version.
        """
        result = await call_next(context)
        domain = self.domain_for_host(_request_host())

        contents = getattr(result, "contents", None)
        if contents is not None and not isinstance(result, (list, tuple)):
            blocks, changed = self._retarget_blocks(contents, domain)
            meta = self._retarget(getattr(result, "meta", None), domain)
            if not changed and meta is None:
                return result
            update: dict[str, Any] = {"contents": blocks}
            if meta is not None:
                update["meta"] = meta
            return result.model_copy(update=update)

        if isinstance(result, (list, tuple)):
            blocks, changed = self._retarget_blocks(result, domain)
            if not changed:
                return result
            return tuple(blocks) if isinstance(result, tuple) else blocks

        return result
