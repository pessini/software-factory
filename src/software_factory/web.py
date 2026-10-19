"""Local planning web service: one user, loopback only, backed by PlanningControl."""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

STATIC = Path(__file__).with_name("web_static")
COOKIE = "factory_planning"


def _record_path(storage_root: Path) -> Path:
    return storage_root / "factory" / "web.json"


def web_link(storage_root: Path, session_id: str) -> str | None:
    """Return the running service's address for a session, with its access token."""
    try:
        record = json.loads(_record_path(storage_root).read_text())
        os.kill(int(record["pid"]), 0)
    except OSError, ValueError, KeyError, json.JSONDecodeError:
        return None
    return f"{record['url']}/planning/{session_id}?token={record['token']}"


def create_app(planning: Any, token: str, port: int) -> Any:
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response
    from starlette.routing import Mount, Route
    from starlette.staticfiles import StaticFiles

    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Assets(StaticFiles):
        # Revalidate on every load so an updated UI never mixes with cached files.
        async def get_response(self, path: str, scope: Any) -> Response:
            response = await super().get_response(path, scope)
            response.headers["cache-control"] = "no-cache"
            return response

    def authorized(request: Request) -> bool:
        supplied = request.cookies.get(COOKIE) or request.headers.get("x-factory-token", "")
        return secrets.compare_digest(supplied, token)

    def guard(
        handler: Callable[[Request], Awaitable[Any]],
    ) -> Callable[[Request], Awaitable[Response]]:
        async def wrapped(request: Request) -> Response:
            # Loopback host and same-origin checks block DNS rebinding and cross-site posts.
            if request.headers.get("host") not in allowed_hosts:
                return PlainTextResponse("unknown host", status_code=421)
            if not authorized(request):
                return JSONResponse({"error": "open the link printed by the service"}, 401)
            if request.method != "GET":
                origin = request.headers.get("origin")
                if origin and origin.split("//", 1)[-1] not in allowed_hosts:
                    return JSONResponse({"error": "cross-origin request refused"}, 403)
                if "application/json" not in request.headers.get("content-type", ""):
                    return JSONResponse({"error": "JSON body required"}, 415)
            try:
                return JSONResponse(await handler(request))
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, 409)

        return wrapped

    async def page(request: Request) -> Response:
        if request.headers.get("host") not in allowed_hosts:
            return PlainTextResponse("unknown host", status_code=421)
        supplied = request.query_params.get("token")
        if supplied and secrets.compare_digest(supplied, token):
            response = Response(status_code=303, headers={"location": request.url.path})
            response.set_cookie(COOKIE, token, httponly=True, samesite="strict")
            return response
        if not authorized(request):
            return PlainTextResponse(
                "Open the planning link printed by `software-factory web`.", status_code=401
            )
        return FileResponse(STATIC / "index.html", headers={"cache-control": "no-store"})

    async def body(request: Request) -> dict[str, Any]:
        value = await request.json()
        if not isinstance(value, dict):
            raise ValueError("a JSON object is required")
        return value

    async def sessions(request: Request) -> Any:
        if request.method == "POST":
            value = await body(request)
            return await planning.start(
                str(value.get("request", "")), request_id=str(value.get("request_id", ""))
            )
        return {"workspace": str(planning.workspace), "sessions": await planning.sessions()}

    async def session(request: Request) -> Any:
        after = int(request.query_params.get("after_event", "0"))
        return await planning.status(request.path_params["session_id"], after_event=after)

    async def answer(request: Request) -> Any:
        value = await body(request)
        answers = value.get("answers")
        if not isinstance(answers, dict):
            raise ValueError("answers must be an object")
        return await planning.answer(
            request.path_params["session_id"],
            str(value.get("interaction_id", "")),
            {str(key): str(item) for key, item in answers.items()},
        )

    async def revise(request: Request) -> Any:
        value = await body(request)
        return await planning.revise(
            request.path_params["session_id"], str(value.get("feedback", ""))
        )

    async def approve(request: Request) -> Any:
        value = await body(request)
        return await planning.approve(
            request.path_params["session_id"], str(value.get("plan_hash", ""))
        )

    async def cancel(request: Request) -> Any:
        return await planning.cancel(request.path_params["session_id"])

    async def resume(request: Request) -> Any:
        return await planning.resume(request.path_params["session_id"])

    async def report(request: Request) -> Any:
        status = await planning.status(request.path_params["session_id"])
        path = (status.get("implementation") or {}).get("final_report_path")
        root = (planning.storage_root / "factory").resolve()
        if not path or not Path(path).resolve().is_relative_to(root):
            raise ValueError("no final report is available yet")
        return {"report": Path(path).read_text()}

    prefix = "/api/sessions/{session_id}"
    return Starlette(
        routes=[
            Route("/", page),
            Route("/planning/{session_id}", page),
            Route("/api/sessions", guard(sessions), methods=["GET", "POST"]),
            Route(prefix, guard(session)),
            Route(prefix + "/answer", guard(answer), methods=["POST"]),
            Route(prefix + "/revise", guard(revise), methods=["POST"]),
            Route(prefix + "/approve", guard(approve), methods=["POST"]),
            Route(prefix + "/cancel", guard(cancel), methods=["POST"]),
            Route(prefix + "/resume", guard(resume), methods=["POST"]),
            Route(prefix + "/report", guard(report)),
            Mount("/static", Assets(directory=STATIC)),
        ]
    )


def serve_web(workspace: Path, *, storage_root: Path | None = None, port: int = 4317) -> None:
    import uvicorn

    from software_factory.planning_control import PlanningControl

    planning = PlanningControl(workspace, storage_root=storage_root)
    token = secrets.token_urlsafe(24)
    url = f"http://127.0.0.1:{port}"
    record = _record_path(planning.storage_root)
    record.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(record, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump({"url": url, "token": token, "pid": os.getpid()}, handle)
    print(f"Planning UI for {planning.workspace}: {url}/?token={token}", flush=True)
    try:
        uvicorn.run(
            create_app(planning, token, port), host="127.0.0.1", port=port, log_level="warning"
        )
    finally:
        record.unlink(missing_ok=True)
