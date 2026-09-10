"""Read the local BB API where the CLI has no storage-location command."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import quote, urlparse
from urllib.request import urlopen

from software_factory.bb.client import BBError


async def read_bb(path: str) -> Any:
    base = os.environ.get("BB_SERVER_URL", "http://127.0.0.1:38886").rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise BBError(
            "the protected worker currently requires the local BB server", "configuration"
        )

    def read() -> Any:
        try:
            with urlopen(base + "/api/v1/" + path, timeout=10) as response:
                value = response.read(8 * 1024 * 1024 + 1)
                if len(value) > 8 * 1024 * 1024:
                    raise BBError("BB response exceeds the supported size", "protocol")
                return json.loads(value)
        except URLError as exc:
            raise BBError(f"BB connection failed: {exc.reason}", "connection") from exc

    return await asyncio.to_thread(read)


async def thread_storage(thread_id: str) -> Path:
    value = await read_bb(f"threads/{quote(thread_id, safe='')}/thread-storage/location")
    root = value.get("storageRootPath") if isinstance(value, dict) else None
    if not isinstance(root, str) or not Path(root).is_absolute():
        raise BBError("BB returned an invalid local storage location", "protocol")
    return Path(root)
