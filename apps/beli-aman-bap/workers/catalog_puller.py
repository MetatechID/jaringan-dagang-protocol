"""Periodic /search worker — safety net for missed push events.

Every PULL_INTERVAL_SECS this worker sends a Beckn /search to each known BPP.
The BPP responds via /on_search (handled in routers/beckn.py), which upserts
the mirror_* tables.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from sqlalchemy import select

# Make the beckn-protocol package importable
_proto_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "packages", "beckn-protocol")
)
if _proto_path not in sys.path:
    sys.path.insert(0, _proto_path)

from beckn.outbound import build_ondc_context, send_beckn_request  # type: ignore  # noqa: E402
from database import async_session  # noqa: E402
from models.mirror import MirrorStore  # noqa: E402

logger = logging.getLogger(__name__)

PULL_INTERVAL_SECS = int(os.environ.get("BECKN_PULL_INTERVAL_SECS", "300"))

# Default seed when the mirror is empty — pulls catalog from the local seller process.
DEFAULT_BPP_ID = os.environ.get("DEFAULT_BPP_ID", "bpp.jaringan-dagang.id")
DEFAULT_BPP_URL = os.environ.get("DEFAULT_BPP_URL", "http://localhost:8001/beckn")


def _build_search_envelope(bpp_id: str, bpp_uri: str) -> dict:
    return {
        "context": build_ondc_context(
            action="search",
            bpp_id=bpp_id,
            bpp_uri=bpp_uri,
        ),
        "message": {"intent": {}},
    }


async def pull_once() -> int:
    """One sweep: pull from every known BPP. Returns count of /search sent."""
    sent = 0
    async with async_session() as db:
        stores = (await db.execute(select(MirrorStore))).scalars().all()
        targets: list[tuple[str, str]] = []
        if stores:
            for s in stores:
                if s.bpp_uri:
                    targets.append((s.bpp_id, s.bpp_uri))
        else:
            # Cold start: nothing mirrored yet. Seed with default.
            targets.append((DEFAULT_BPP_ID, DEFAULT_BPP_URL))

    for bpp_id, bpp_uri in targets:
        env = _build_search_envelope(bpp_id, bpp_uri)
        ok = await send_beckn_request(
            bpp_id=bpp_id, action="search", body=env,
            target_url=f"{bpp_uri.rstrip('/')}/search",
        )
        sent += 1
        logger.info("/search -> %s : %s", bpp_uri, "ok" if ok else "fail")
    return sent


async def run_forever() -> None:
    logger.info(
        "catalog puller started (interval=%ss, default_bpp=%s)",
        PULL_INTERVAL_SECS, DEFAULT_BPP_ID,
    )
    while True:
        try:
            await pull_once()
        except Exception:
            logger.exception("pull_once failed")
        await asyncio.sleep(PULL_INTERVAL_SECS)
