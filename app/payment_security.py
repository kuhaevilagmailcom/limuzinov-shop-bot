from __future__ import annotations

import hashlib


def payment_event_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def already_processed(session, event_hash: str) -> bool:
    # Will be connected with payment_logs table migration
    return False
