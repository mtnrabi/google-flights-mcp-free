"""Fire-and-forget tasks that actually run to the end.

Two bugs live here, and both of them were invisible:

1. **A bare `asyncio.create_task` is weakly referenced.** Nothing else
   holds the task, so the loop may collect it mid-await and the work
   simply stops, with no exception and no log line. `spawn` keeps a strong
   reference until the task is done.
2. **A serverless instance is frozen the moment it answers.** A task
   created after the response is sent gets whatever runtime is left over,
   which on Vercel is often none. `spawn_and_wait` gives the caller a
   bounded window to let the work finish INSIDE the request, and on
   timeout leaves it running (still referenced) rather than cancelling
   it -- a send that may already be in flight must never be cancelled.

Between 2026-09-13 and 09-16 three of nine real free-MCP sign-ins never
got a welcome note because of the second one.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable

logger = logging.getLogger(__name__)

#: Strong references to every in-flight background task, dropped when it
#: finishes. Module level on purpose: it has to outlive the request.
_RUNNING: set[asyncio.Task] = set()


def spawn(coro: Awaitable, *, label: str = "task") -> asyncio.Task | None:
    """Start `coro` in the background and keep it referenced.

    Returns the task, or None when there is no running loop (a test, or a
    synchronous entry point). Never raises.
    """
    try:
        task = asyncio.ensure_future(coro)
    except RuntimeError:  # pragma: no cover - no loop, e.g. a sync test
        logger.debug("no event loop for %s; skipped", label)
        return None
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)
    return task


async def spawn_and_wait(
    coro: Awaitable, *, timeout: float, label: str = "task"
) -> bool:
    """Run `coro` in the background and wait up to `timeout` for it.

    True when it finished inside the window. False when the window closed
    first -- and then the task is STILL RUNNING and still referenced:
    `asyncio.shield` is what stops the timeout from cancelling a send that
    may already have reached Resend. Whatever the caller does next must
    therefore be safe to do while the task is in flight.

    Never raises: a background job may not be the reason a request fails.
    """
    task = spawn(coro, label=label)
    if task is None:
        return False
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout)
        return True
    except asyncio.TimeoutError:
        logger.info(
            "%s did not finish in %.1fs; it keeps running and the daily "
            "retry covers it if it does not",
            label,
            timeout,
        )
        return False
    except Exception as exc:  # noqa: BLE001 - the task logs its own failures
        logger.warning("%s failed: %s", label, exc)
        return False
