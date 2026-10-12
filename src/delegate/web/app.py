"""FastAPI app for the dashboard. Every route opens a short-lived DB connection and
delegates to ``TaskService`` as the ``human`` actor; no business rules live here.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs

import anyio
import uvicorn
from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from delegate.core import (
    HUMAN,
    ConflictError,
    CycleError,
    DelegateError,
    ForbiddenError,
    IdentityRequiredError,
    InvalidTransitionError,
    LinkKind,
    NewTask,
    NotFoundError,
    NotHolderError,
    NoteKind,
    Project,
    ProjectClosedError,
    ProjectState,
    Settings,
    Status,
    TaskService,
    TaskType,
    TaskUpdate,
    ValidationError,
    connect,
    load_settings,
)
from delegate.web import views

WEB_DIR = Path(__file__).parent
POLL_SECONDS = 1.0
PING_SECONDS = 15.0

STATUS_FOR_ERROR: tuple[tuple[type[DelegateError], int], ...] = (
    (IdentityRequiredError, 401),
    (ForbiddenError, 403),
    (NotFoundError, 404),
    (ValidationError, 400),
    (CycleError, 400),
    (InvalidTransitionError, 400),
    (ConflictError, 409),
    (NotHolderError, 409),
    (ProjectClosedError, 409),
)


def _http_status(exc: DelegateError) -> int:
    for cls, code in STATUS_FOR_ERROR:
        if isinstance(exc, cls):
            return code
    return 400


# ---------------------------------------------------------------------- dependencies


def get_settings(request: Request) -> Settings:
    return request.app.state.settings  # pyright: ignore[reportAny]


def get_service(request: Request) -> Iterator[TaskService]:
    """One connection per request, closed afterwards."""
    settings = get_settings(request)
    conn = connect(settings.db_path)
    try:
        yield TaskService(conn, HUMAN, lease_minutes=settings.lease_minutes)
    finally:
        conn.close()


async def get_form(request: Request) -> dict[str, str]:
    """Parse an urlencoded body without python-multipart. Last value wins."""
    raw = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    return {k: v[-1] for k, v in raw.items()}


Svc = Annotated[TaskService, Depends(get_service)]
Form = Annotated[dict[str, str], Depends(get_form)]


def _parse_states(values: list[str] | None) -> list[ProjectState] | None:
    """Turn ``?state=`` values into enums. None (no values) means the default listing."""
    if not values:
        return None
    try:
        return [ProjectState(v) for v in values]
    except ValueError as exc:
        allowed = ", ".join(s.value for s in ProjectState)
        raise ValidationError(f"Invalid project state in {values!r}; use one of {allowed}") from exc


def _project(svc: TaskService, key: str) -> Project:
    return svc.get_project(key.upper())


# ---------------------------------------------------------------------- templating


def _make_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))  # autoescape on for .html
    env = templates.env
    env.filters["ago"] = views.ago  # pyright: ignore[reportUnknownMemberType]
    env.filters["event_text"] = views.describe_event  # pyright: ignore[reportUnknownMemberType]
    globals_: dict[str, Any] = {
        "COLUMN_LABELS": views.COLUMN_LABELS,
        "STATUSES": [s.value for s in Status],
        "STATE_LABELS": views.STATE_LABELS,
        "STATE_BANNERS": views.STATE_BANNERS,
        "PROJECT_STATES": [s.value for s in ProjectState],
        "NOTE_KINDS": [k.value for k in NoteKind if k is not NoteKind.SUMMARY],
        "LINK_KINDS": [k.value for k in LinkKind],
        "TASK_TYPES": [t.value for t in TaskType],
        "TYPE_LABELS": views.TYPE_LABELS,
        "TYPE_ICONS": views.TYPE_ICONS,
    }
    env.globals.update(globals_)  # pyright: ignore[reportUnknownMemberType]
    return templates


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


# ---------------------------------------------------------------------- SSE


def _fetch_events(settings: Settings, project_id: int, after: int) -> list[dict[str, Any]]:
    conn = connect(settings.db_path)
    try:
        svc = TaskService(conn, HUMAN, lease_minutes=settings.lease_minutes)
        return [e.model_dump(mode="json") for e in svc.events_since(after, project_id=project_id)]
    finally:
        conn.close()


async def event_stream(
    settings: Settings,
    project_id: int,
    after: int,
    is_disconnected: Callable[[], Awaitable[bool]],
    *,
    poll_seconds: float = POLL_SECONDS,
    ping_seconds: float = PING_SECONDS,
) -> AsyncIterator[str]:
    """Yield SSE frames for new events of one project until the client disconnects.

    Each poll opens and closes its own connection in a worker thread. A ``: ping``
    comment goes out when nothing was sent for ``ping_seconds``.
    """
    yield ": connected\n\n"
    last_sent = time.monotonic()
    while not await is_disconnected():
        events = await run_in_threadpool(_fetch_events, settings, project_id, after)
        for event in events:
            after = max(after, int(event["id"]))
            yield f"event: delegate\ndata: {json.dumps(event)}\n\n"
            last_sent = time.monotonic()
        if time.monotonic() - last_sent >= ping_seconds:
            yield ": ping\n\n"
            last_sent = time.monotonic()
        await anyio.sleep(poll_seconds)


# ---------------------------------------------------------------------- app factory


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg: Settings = settings or load_settings()
    app = FastAPI(title="delegate", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = cfg
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
    templates = _make_templates()

    def render(
        request: Request,
        name: str,
        svc: TaskService,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        **ctx: Any,
    ) -> HTMLResponse:
        context: dict[str, Any] = {
            "projects": svc.list_projects(),
            "now": datetime.now(UTC),
            **ctx,
        }
        return templates.TemplateResponse(
            request, name, context, status_code=status_code, headers=headers
        )

    # ---- errors

    @app.exception_handler(DelegateError)
    async def on_error(request: Request, exc: DelegateError) -> Response:  # pyright: ignore[reportUnusedFunction]
        status = _http_status(exc)
        message = str(exc)
        if _is_htmx(request):
            # htmx ignores 4xx bodies by default; app.js opts these in. The retarget
            # sends the fragment to the toast area instead of the original target.
            return templates.TemplateResponse(
                request,
                "_toast.html",
                {"message": message, "code": exc.code},
                status_code=status,
                headers={"HX-Retarget": "#toasts", "HX-Reswap": "beforeend"},
            )
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": exc.code, "message": message}, status_code=status)
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": message, "code": exc.code, "status": status, "projects": []},
            status_code=status,
        )

    # ---- pages

    @app.get("/", response_class=HTMLResponse)
    def overview(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        svc: Svc,
        q: str = "",
        state: Annotated[list[str] | None, Query()] = None,
    ) -> HTMLResponse:
        states = _parse_states(state)
        cards = views.project_cards(svc, states, q)
        # htmx history restore asks for the full page, so it must not get the partial.
        partial = _is_htmx(request) and request.headers.get("HX-History-Restore-Request") != "true"
        return render(
            request,
            "_projects.html" if partial else "overview.html",
            svc,
            headers={"Vary": "HX-Request"},  # same URL, two bodies: keep caches apart
            cards=cards,
            project=None,
            q=q.strip(),
            selected=[s.value for s in states or []],
        )

    def board_ctx(svc: TaskService, key: str, opts: views.BoardOptions) -> dict[str, Any]:
        project = _project(svc, key)
        return {
            "project": project,
            "columns": views.board_columns(svc, project, opts),
            "opts": opts,
        }

    def side_ctx(svc: TaskService, key: str) -> dict[str, Any]:
        project = _project(svc, key)
        return {
            "project": project,
            "agents": views.active_agents(svc, project),
            "events": views.recent_events(svc, project.id),
        }

    @app.get("/p/{key}", response_class=HTMLResponse)
    def board(  # pyright: ignore[reportUnusedFunction]
        request: Request, key: str, svc: Svc, show_done: int = 0, show_cancelled: int = 0
    ) -> HTMLResponse:
        opts = views.BoardOptions(bool(show_done), bool(show_cancelled))
        ctx = {**board_ctx(svc, key, opts), **side_ctx(svc, key)}
        return render(request, "board.html", svc, drawer=None, **ctx)

    @app.get("/p/{key}/board", response_class=HTMLResponse)
    def board_fragment(  # pyright: ignore[reportUnusedFunction]
        request: Request, key: str, svc: Svc, show_done: int = 0, show_cancelled: int = 0
    ) -> HTMLResponse:
        opts = views.BoardOptions(bool(show_done), bool(show_cancelled))
        return render(request, "_board.html", svc, **board_ctx(svc, key, opts))

    @app.get("/p/{key}/side", response_class=HTMLResponse)
    def side_fragment(request: Request, key: str, svc: Svc) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        return render(request, "_side.html", svc, **side_ctx(svc, key))

    def drawer_response(
        request: Request,
        svc: TaskService,
        key: str,
        ref: str,
        *,
        changed: bool = False,
        full_page: bool = False,
    ) -> HTMLResponse:
        project = _project(svc, key)
        detail = svc.get_task_detail(ref, project.id)
        if detail.task.project_id != project.id:
            raise NotFoundError(f"Task {ref} not found in {project.key}")
        headers = {"HX-Trigger": "boardChanged"} if changed else None
        if full_page:
            ctx = {**board_ctx(svc, key, views.BoardOptions()), **side_ctx(svc, key)}
            return render(request, "board.html", svc, drawer=detail, **ctx)
        return render(request, "_drawer.html", svc, headers=headers, project=project, d=detail)

    @app.get("/p/{key}/t/{ref}", response_class=HTMLResponse)
    def task_page(request: Request, key: str, ref: str, svc: Svc) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        return drawer_response(request, svc, key, ref, full_page=not _is_htmx(request))

    @app.post("/p/{key}/state", response_class=HTMLResponse)
    def set_state(request: Request, key: str, svc: Svc, form: Form) -> Response:  # pyright: ignore[reportUnusedFunction]
        project = svc.set_project_state(_project(svc, key).id, form.get("state", ""))
        on_board = form.get("view") == "board"
        if not _is_htmx(request):  # plain form post: go back to where the form was
            return RedirectResponse(f"/p/{project.key}" if on_board else "/", status_code=303)
        headers = {"HX-Trigger": "boardChanged"}
        if on_board:
            return render(request, "_pstate.html", svc, headers=headers, project=project)
        card = views.project_card(svc, project)
        return render(request, "_project_card.html", svc, headers=headers, c=card)

    # ---- human actions (each re-renders the drawer and asks the board to refresh)

    @app.post("/p/{key}/t/{ref}/status", response_class=HTMLResponse)
    def set_status(request: Request, key: str, ref: str, svc: Svc, form: Form) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        try:
            status = Status(form.get("status", ""))
        except ValueError as exc:
            raise ValidationError(f"Invalid status {form.get('status', '')!r}") from exc
        project = _project(svc, key)
        svc.update_task(project.id, ref, TaskUpdate(status=status))
        return drawer_response(request, svc, key, ref, changed=True)

    @app.post("/p/{key}/t/{ref}/priority", response_class=HTMLResponse)
    def set_priority(request: Request, key: str, ref: str, svc: Svc, form: Form) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        project = _project(svc, key)
        svc.update_task(project.id, ref, TaskUpdate(priority=form.get("priority", "")))
        return drawer_response(request, svc, key, ref, changed=True)

    @app.post("/p/{key}/t/{ref}/type", response_class=HTMLResponse)
    def set_type(request: Request, key: str, ref: str, svc: Svc, form: Form) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        project = _project(svc, key)
        svc.update_task(project.id, ref, TaskUpdate(type=form.get("type", "")))
        return drawer_response(request, svc, key, ref, changed=True)

    @app.post("/p/{key}/t/{ref}/edit", response_class=HTMLResponse)
    def edit_task(request: Request, key: str, ref: str, svc: Svc, form: Form) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        project = _project(svc, key)
        version = form.get("version", "")
        svc.update_task(
            project.id,
            ref,
            TaskUpdate(
                title=form.get("title"),
                description=form.get("description"),
                acceptance=form.get("acceptance"),
            ),
            expected_version=int(version) if version.isdigit() else None,
        )
        return drawer_response(request, svc, key, ref, changed=True)

    @app.post("/p/{key}/t/{ref}/note", response_class=HTMLResponse)
    def add_note(request: Request, key: str, ref: str, svc: Svc, form: Form) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        project = _project(svc, key)
        svc.add_note(project.id, ref, form.get("kind", "progress"), form.get("body", ""))
        return drawer_response(request, svc, key, ref, changed=True)

    @app.post("/p/{key}/t/{ref}/link", response_class=HTMLResponse)
    def add_link(request: Request, key: str, ref: str, svc: Svc, form: Form) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        project = _project(svc, key)
        svc.add_links(project.id, ref, [(form.get("kind", ""), form.get("ref", ""))])
        return drawer_response(request, svc, key, ref, changed=True)

    @app.post("/p/{key}/tasks", response_class=HTMLResponse)
    def create_task(request: Request, key: str, svc: Svc, form: Form) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        project = _project(svc, key)
        deps = [d.strip() for d in form.get("depends_on", "").split(",") if d.strip()]
        created = svc.create_tasks(
            project.id,
            [
                NewTask(
                    title=form.get("title", "").strip() or "",
                    description=form.get("description", ""),
                    priority=form.get("priority") or 2,
                    type=form.get("type") or TaskType.UNKNOWN,
                    parent=form.get("parent", "").strip() or None,
                    depends_on=deps,
                )
            ],
        )
        return render(
            request,
            "_new_task.html",
            svc,
            headers={"HX-Trigger": "boardChanged"},
            project=project,
            created=created[0].ref,
        )

    # ---- SSE

    def _stream_start(key: str, after: int | None) -> tuple[int, int]:
        conn = connect(cfg.db_path)
        try:
            svc = TaskService(conn, HUMAN)
            project = _project(svc, key)
            return project.id, svc.latest_event_id() if after is None else after
        finally:
            conn.close()

    @app.get("/p/{key}/events/stream")
    async def stream(request: Request, key: str, after: int | None = None) -> StreamingResponse:  # pyright: ignore[reportUnusedFunction]
        project_id, start = await run_in_threadpool(_stream_start, key, after)
        return StreamingResponse(
            event_stream(cfg, project_id, start, request.is_disconnected),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ---- JSON API

    @app.get("/api/projects")
    def api_projects(  # pyright: ignore[reportUnusedFunction]
        svc: Svc, q: str | None = None, state: Annotated[list[str] | None, Query()] = None
    ) -> list[dict[str, Any]]:
        projects = svc.list_projects(states=_parse_states(state), query=q)
        return [p.model_dump(mode="json") for p in projects]

    @app.get("/api/p/{key}/status")
    def api_status(key: str, svc: Svc) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        return svc.project_status(_project(svc, key).id).model_dump(mode="json")

    @app.get("/api/p/{key}/tasks")
    def api_tasks(  # pyright: ignore[reportUnusedFunction]
        key: str,
        svc: Svc,
        status: Annotated[list[Status] | None, Query()] = None,
        assignee: str | None = None,
        parent: str | None = None,
        type: TaskType | None = None,
        ready_only: bool = False,
        query: str | None = None,
        include_closed: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        tasks = svc.list_tasks(
            _project(svc, key).id,
            statuses=status,
            assignee=assignee,
            parent=parent,
            task_type=type,
            ready_only=ready_only,
            query=query,
            include_closed=include_closed,
            limit=limit,
        )
        return [t.model_dump(mode="json") for t in tasks]

    @app.get("/api/p/{key}/tasks/{ref}")
    def api_task(key: str, ref: str, svc: Svc) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        project = _project(svc, key)
        detail = svc.get_task_detail(ref, project.id)
        if detail.task.project_id != project.id:
            raise NotFoundError(f"Task {ref} not found in {project.key}")
        return detail.model_dump(mode="json")

    @app.get("/api/events")
    def api_events(  # pyright: ignore[reportUnusedFunction]
        svc: Svc, after: int = 0, project: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        project_id = _project(svc, project).id if project else None
        events = svc.events_since(after, project_id=project_id, limit=limit)
        return [e.model_dump(mode="json") for e in events]

    return app


def run(host: str | None = None, port: int | None = None) -> None:
    """Serve the dashboard with uvicorn. Defaults come from the settings."""
    settings = load_settings()
    uvicorn.run(
        create_app(settings),
        host=host or settings.dashboard_host,
        port=port or settings.dashboard_port,
        log_level="info",
    )
