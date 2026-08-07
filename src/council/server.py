"""FastAPI server + SSE for live session viewing and seed submission."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from council.config import CouncilConfig
from council.events import Event, EventLog
from council.pipeline import STAGE_ORDER, Pipeline
from council.seed import Seed
from council.storage import SessionStore


def _safe_session_path(store: SessionStore, rel: str) -> Path:
    """Resolve `rel` inside the session dir, rejecting escapes.

    A string prefix check is not enough: sibling dirs sharing a name prefix
    (``abc`` vs ``abc-secret``) pass ``startswith`` while living outside.
    """
    root = store.path.resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise HTTPException(400, "Invalid path")
    return target


class RunRequest(BaseModel):
    title: str = "Untitled research"
    main_points: list[str] = Field(default_factory=list)
    seed_links: list[str] = Field(default_factory=list)
    goals: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    from_stage: str = "seed"


def create_app(config: CouncilConfig) -> FastAPI:
    app = FastAPI(title="Local LLM Council", version="0.1.0")
    static_dir = Path(__file__).parent / "web"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Track running tasks so we don't double-start
    running: set[str] = set()
    # Live EventLog per running session. SessionStore.open() always builds a
    # fresh EventLog, so SSE subscribers must share the exact instance the
    # pipeline emits on — otherwise they only ever see history replay.
    live_logs: dict[str, EventLog] = {}

    @app.exception_handler(FileNotFoundError)
    async def _not_found(request: Request, exc: FileNotFoundError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(ValueError)
    async def _bad_request(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        index_path = static_dir / "index.html"
        if index_path.exists():
            return HTMLResponse(index_path.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>Council UI missing</h1>")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "sessions_dir": str(config.sessions_path())}

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        members: dict[str, Any] = {}
        for stage in ("research", "critique"):
            role = config.roles.get(stage)
            if not role:
                continue
            for mid in role.participants:
                # Resolve each seat against the stage it actually sits in.
                members[f"{mid} ({stage})"] = config.member_invoke_spec(mid, stage)
        seats = {}
        for r in ("research_chairman", "draft_writer", "critique_chairman", "finalize"):
            if r in config.roles and config.roles[r].provider:
                seats[r] = config.seat_invoke_spec(r)
        return {
            "members": members,
            "seats": seats,
            "stages": config.pipeline.stages,
            "research_required": config.pipeline.research_required,
        }

    @app.get("/api/sessions")
    async def list_sessions() -> list[dict[str, Any]]:
        return SessionStore.list_sessions(config.sessions_path())

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        try:
            store = SessionStore.open(config.sessions_path(), session_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        meta = store.load_meta()
        meta["path"] = str(store.path)
        meta["events"] = store.events.read_all()
        return meta

    @app.get("/api/sessions/{session_id}/artifact")
    async def get_artifact(session_id: str, path: str) -> JSONResponse:
        store = SessionStore.open(config.sessions_path(), session_id)
        target = _safe_session_path(store, path)
        if not target.exists() or not target.is_file():
            raise HTTPException(404, f"Artifact not found: {path}")
        text = target.read_text(encoding="utf-8", errors="replace")
        return JSONResponse({"path": path, "content": text})

    @app.get("/api/sessions/{session_id}/events")
    async def stream_events(session_id: str, request: Request) -> StreamingResponse:
        store = SessionStore.open(config.sessions_path(), session_id)
        # Share the emitter's EventLog while a run is live; a fresh store's
        # log would never receive anything. For finished sessions there is no
        # live log — the client just gets the replay below.
        events = live_logs.get(session_id, store.events)

        async def gen():
            # Subscribe before replaying history so no event is lost in the
            # gap; dedup the overlap by event payload.
            q = events.subscribe()
            seen: set[str] = set()
            try:
                for ev in events.read_all():
                    seen.add(json.dumps(ev, sort_keys=True))
                    yield f"data: {json.dumps(ev)}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.wait_for(q.get(), timeout=15.0)
                    except TimeoutError:
                        yield f"data: {json.dumps({'type': 'ping'})}\n\n"
                        continue
                    if item is None:
                        break
                    payload = item.to_dict()
                    key = json.dumps(payload, sort_keys=True)
                    if key in seen:
                        continue
                    seen.add(key)
                    yield f"data: {json.dumps(payload)}\n\n"
            finally:
                events.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream")

    async def _run_pipeline(store: SessionStore, seed: Seed, from_stage: str) -> None:
        live_logs[store.session_id] = store.events
        try:
            pipe = Pipeline(config, store)
            await pipe.run(seed, from_stage=from_stage)
        except asyncio.CancelledError:
            store.update_meta(status="interrupted")
            raise
        except Exception as exc:  # noqa: BLE001
            store.update_meta(status="failed", error=str(exc))
            await store.events.emit(Event(type="stage_error", message=str(exc)))
        finally:
            running.discard(store.session_id)
            live_logs.pop(store.session_id, None)

    @app.post("/api/sessions")
    async def start_session(body: RunRequest, background: BackgroundTasks) -> dict[str, Any]:
        if not body.main_points:
            raise HTTPException(400, "main_points required")
        if body.from_stage not in STAGE_ORDER:
            raise HTTPException(400, f"Unknown stage: {body.from_stage}")
        seed = Seed(
            title=body.title,
            main_points=body.main_points,
            seed_links=body.seed_links,
            goals=body.goals,
            constraints=body.constraints,
        )
        store = SessionStore(config.sessions_path())
        # Register before scheduling: the check/add must be synchronous or two
        # rapid requests both pass the guard and run against the same session.
        running.add(store.session_id)
        background.add_task(_run_pipeline, store, seed, body.from_stage)
        return {"id": store.session_id, "status": "starting", "title": seed.title}

    @app.post("/api/sessions/{session_id}/resume")
    async def resume_session(
        session_id: str,
        background: BackgroundTasks,
        from_stage: str = "research",
    ) -> dict[str, Any]:
        if from_stage not in STAGE_ORDER:
            raise HTTPException(400, f"Unknown stage: {from_stage}")
        if session_id in running:
            raise HTTPException(409, "Session already running")
        store = SessionStore.open(config.sessions_path(), session_id)
        # load seed
        import yaml

        seed_path = store.path / "input" / "seed.yaml"
        if not seed_path.exists():
            raise HTTPException(400, "Session has no seed")
        seed = Seed.model_validate(yaml.safe_load(seed_path.read_text(encoding="utf-8")))
        running.add(session_id)
        background.add_task(_run_pipeline, store, seed, from_stage)
        return {"id": session_id, "status": "resuming", "from_stage": from_stage}

    @app.get("/api/sessions/{session_id}/file/{file_path:path}")
    async def download_file(session_id: str, file_path: str) -> FileResponse:
        store = SessionStore.open(config.sessions_path(), session_id)
        target = _safe_session_path(store, file_path)
        if not target.exists() or not target.is_file():
            raise HTTPException(404, "Not found")
        return FileResponse(target)

    @app.post("/api/sessions/{session_id}/export")
    async def export_api(session_id: str, format: str = "md") -> dict[str, Any]:
        from council.export import export_session

        store = SessionStore.open(config.sessions_path(), session_id)
        try:
            # python-docx conversion is blocking — keep it off the event loop.
            path = await asyncio.to_thread(
                export_session,
                store,
                fmt=format,
                title=store.load_meta().get("title"),
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc)) from exc
        return {
            "path": str(path),
            "relative": str(path.relative_to(store.path)),
            "format": format,
        }

    @app.post("/api/sessions/{session_id}/images")
    async def images_api(
        session_id: str,
        background: BackgroundTasks,
        count: int | None = None,
        style: str | None = None,
    ) -> dict[str, Any]:
        if session_id in running:
            raise HTTPException(409, "Session already running")
        SessionStore.open(config.sessions_path(), session_id)  # 404 if unknown

        async def _job() -> None:
            from council.images import generate_images

            store = SessionStore.open(config.sessions_path(), session_id)
            live_logs[session_id] = store.events
            try:
                await generate_images(config, store, count=count, style=style)
            except asyncio.CancelledError:
                store.update_meta(status="interrupted")
                raise
            except Exception as exc:  # noqa: BLE001
                store.update_meta(error=str(exc))
            finally:
                running.discard(session_id)
                live_logs.pop(session_id, None)

        running.add(session_id)
        background.add_task(_job)
        return {"id": session_id, "status": "images_starting"}

    return app
