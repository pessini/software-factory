"""Thin client for the version-1 Rust Factory control boundary.

FACTORY_CONTROL_URL opts existing CLI, ACP and MCP adapters into the same service.
The service owns the workspace and all execution; clients send IDs and JSON only.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class RemoteTransport:
    def __init__(self, url: str, token: str, workspace: Path):
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("FACTORY_CONTROL_URL must be an HTTP(S) service origin")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("non-loopback Factory connections require HTTPS")
        if not token:
            raise ValueError("FACTORY_HTTP_TOKEN is required for the Rust service")
        self.url = url.rstrip("/")
        self.token = token
        self.workspace = workspace.resolve()

    @classmethod
    def from_environment(cls, workspace: Path) -> RemoteTransport | None:
        url = os.environ.get("FACTORY_CONTROL_URL")
        return cls(url, os.environ.get("FACTORY_HTTP_TOKEN", ""), workspace) if url else None

    async def call(self, operation: str, **value: Any) -> dict[str, Any]:
        result = await asyncio.to_thread(self._call, operation, value)
        if not isinstance(result, dict):
            raise ValueError("Factory returned an invalid object response")
        return result

    async def listing(self, operation: str) -> list[dict[str, Any]]:
        result = await asyncio.to_thread(self._call, operation, {})
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise ValueError("Factory returned an invalid list response")
        return result

    def _call(self, operation: str, value: dict[str, Any]) -> Any:
        # Do not follow redirects carrying a credential to a different origin.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args: Any, **kwargs: Any) -> None:
                return None

        opener = urllib.request.build_opener(NoRedirect)
        headers = {"authorization": f"Bearer {self.token}", "content-type": "application/json"}
        try:
            check = urllib.request.Request(self.url + "/api/sessions", headers=headers)
            with opener.open(check, timeout=30) as response:
                saved = json.load(response)
            if Path(saved["workspace"]) != self.workspace:
                raise ValueError("Factory service belongs to a different workspace")
            request = urllib.request.Request(
                self.url + "/api/control/" + operation,
                data=json.dumps(value).encode(),
                headers=headers,
                method="POST",
            )
            with opener.open(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read()).get("error", str(exc))
            except ValueError:
                message = str(exc)
            raise ValueError(message) from exc
        except urllib.error.URLError as exc:
            raise ValueError(f"Factory service unavailable: {exc.reason}") from exc
