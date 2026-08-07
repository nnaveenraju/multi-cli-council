"""Regression tests for the hardening pass (failure semantics, validation, retries)."""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rich.console import Console

from council.config import load_config
from council.events import Event, EventLog
from council.models.base import ARG_MAX_SOFT, ModelResult, _detect_cli_error
from council.seed import Seed
from council.server import create_app
from council.storage import SessionStore

# ---------- storage: session id validation ----------


def test_session_id_traversal_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="Invalid session id"):
        SessionStore(tmp_path, session_id="../../evil")
    with pytest.raises(ValueError, match="Invalid session id"):
        SessionStore.open(tmp_path, "../outside")
    with pytest.raises(ValueError, match="Invalid session id"):
        SessionStore.open(tmp_path, "a/b")


def test_meta_write_is_atomic(tmp_path: Path):
    store = SessionStore(tmp_path, session_id="atomic1")
    store.update_meta(title="t")
    assert (tmp_path / "atomic1" / "session.json").exists()
    assert not (tmp_path / "atomic1" / "session.json.tmp").exists()


# ---------- events: dropped subscriber gets a sentinel ----------


def test_slow_subscriber_stream_terminates(tmp_path: Path):
    log = EventLog(tmp_path / "events.jsonl")

    async def run():
        q = log.subscribe(maxsize=1)
        await log.emit(Event(type="a"))  # fills the queue
        await log.emit(Event(type="b"))  # overflows: subscriber dropped + sentinel
        return await q.get()

    assert asyncio.run(run()) is None


# ---------- models: CLI error detection ----------


def test_model_prose_is_not_flagged_as_cli_error():
    text = "This article explains the 'unknown model' error in CLI tools."
    assert _detect_cli_error(text, "", text, 0) is None


def test_stderr_usage_error_is_flagged():
    assert _detect_cli_error("", "error: unrecognized arguments: --bogus", "", 0) is not None


def test_failed_exit_scans_stdout():
    assert _detect_cli_error("unknown model: foo", "", "unknown model: foo", 1) is not None


def test_empty_output_reports_empty_not_exit_code():
    assert _detect_cli_error("", "", "", 0) == "empty output"


# ---------- models: retry policy ----------


class _FailAdapter:
    def __init__(self, error: str):
        self.error = error
        self.calls = 0

    async def invoke(self, req) -> ModelResult:
        self.calls += 1
        return ModelResult(
            ok=False, text="", provider="claude", model="m", error=self.error
        )


def _run_invoke(monkeypatch, error: str, retries: int) -> int:
    import council.models.registry as reg

    cfg = load_config()
    cfg.invoke.retries = retries
    adapter = _FailAdapter(error)
    monkeypatch.setattr(reg, "get_adapter", lambda provider, config: adapter)

    async def _no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(reg.asyncio, "sleep", _no_sleep)
    asyncio.run(reg.invoke_model(cfg, provider="claude", model="m", prompt="hi"))
    return adapter.calls


def test_missing_binary_is_not_retried(monkeypatch):
    assert _run_invoke(monkeypatch, "Binary not found: claude", retries=3) == 1


def test_timeout_is_not_retried(monkeypatch):
    assert _run_invoke(monkeypatch, "Timed out after 5s", retries=3) == 1


def test_transient_failure_is_retried(monkeypatch):
    assert _run_invoke(monkeypatch, "rate limited", retries=3) == 4


def test_unknown_tools_mode_rejected():
    import council.models.registry as reg

    cfg = load_config()
    with pytest.raises(ValueError, match="Unknown tools mode"):
        asyncio.run(
            reg.invoke_model(cfg, provider="claude", model="m", prompt="hi", tools="wed")
        )


# ---------- claude adapter: oversized prompt with tools off uses stdin ----------


def test_tools_off_large_prompt_uses_stdin(tmp_path: Path):
    from council.models.base import InvokeRequest
    from council.models.claude import ClaudeAdapter

    adapter = ClaudeAdapter(bin_path="claude")
    big = "x" * (ARG_MAX_SOFT + 10)
    req = InvokeRequest(cwd=tmp_path, prompt=big, system="s", tools="off")
    prompt_file = adapter.prepare_prompt_file(req, tmp_path / "_invoke")
    cmd = adapter.build_command(req, prompt_file)
    assert req.prompt_via_stdin is True
    assert big not in cmd  # prompt must not hit ARG_MAX on argv


def test_web_large_prompt_uses_file_indirection(tmp_path: Path):
    from council.models.base import InvokeRequest
    from council.models.claude import ClaudeAdapter

    adapter = ClaudeAdapter(bin_path="claude")
    big = "x" * (ARG_MAX_SOFT + 10)
    req = InvokeRequest(cwd=tmp_path, prompt=big, system="s", tools="web")
    prompt_file = adapter.prepare_prompt_file(req, tmp_path / "_invoke")
    cmd = adapter.build_command(req, prompt_file)
    assert req.prompt_via_stdin is False
    assert str(prompt_file.resolve()) in cmd[-1]


# ---------- pipeline: stage validation / failure semantics ----------


def _quiet_pipeline(tmp_path: Path, session_id: str = "pipes"):
    from council.pipeline import Pipeline

    cfg = load_config()
    store = SessionStore(tmp_path, session_id=session_id)
    return Pipeline(cfg, store, console=Console(quiet=True)), store


def test_run_rejects_unknown_from_stage(tmp_path: Path):
    pipe, _ = _quiet_pipeline(tmp_path)
    seed = Seed(title="t", main_points=["p"])
    with pytest.raises(ValueError, match="Unknown stage"):
        asyncio.run(pipe.run(seed, from_stage="drft"))


def test_read_missing_artifact_raises(tmp_path: Path):
    pipe, _ = _quiet_pipeline(tmp_path)
    with pytest.raises(FileNotFoundError, match="Required artifact missing"):
        pipe._read("draft/paper_v1.md")


def test_research_chairman_failure_aborts(tmp_path: Path, monkeypatch):
    import council.pipeline as pl

    pipe, store = _quiet_pipeline(tmp_path)

    async def fake_invoke(config, **kwargs):
        ok = kwargs.get("member_id") != "research_chairman"
        return ModelResult(
            ok=ok,
            text="## Notes" if ok else "",
            provider=kwargs.get("provider", ""),
            model=kwargs.get("model"),
            error=None if ok else "chairman boom",
        )

    monkeypatch.setattr(pl, "invoke_model", fake_invoke)
    seed = Seed(title="t", main_points=["p"])
    with pytest.raises(RuntimeError, match="Research chairman failed"):
        asyncio.run(pipe.stage_research(seed))
    assert store.load_meta()["status"] == "failed"
    # A "# FAILED" synthesis must not be treated as a completed stage.
    assert "research" not in store.load_meta()["stages_completed"]


def test_member_exception_becomes_failed_result(tmp_path: Path, monkeypatch):
    """An unexpected per-member error must not abort the whole gather."""
    import council.pipeline as pl

    pipe, store = _quiet_pipeline(tmp_path)

    async def fake_invoke(config, **kwargs):
        if kwargs.get("member_id") == "researcher_grok":
            raise KeyError("boom")
        return ModelResult(
            ok=True,
            text="## Notes",
            provider=kwargs.get("provider", ""),
            model=kwargs.get("model"),
        )

    monkeypatch.setattr(pl, "invoke_model", fake_invoke)
    seed = Seed(title="t", main_points=["p"])
    asyncio.run(pipe.stage_research(seed))
    # Stage completed on the remaining members despite the broken one.
    assert "research" in store.load_meta()["stages_completed"]


# ---------- export: --out outside the session dir ----------


def test_export_outside_session_dir(tmp_path: Path):
    from council.export import export_session

    store = SessionStore(tmp_path / "sessions", session_id="outtest")
    store.write_text("final/paper_final.md", "# T\n\nBody text.\n")
    store.update_meta(title="T")
    dest = tmp_path / "Desktop" / "article.md"
    out = export_session(store, fmt="md", out=dest)
    assert out.read_text(encoding="utf-8").startswith("# T")
    recorded = store.load_meta()["artifacts"]["export_md"]
    assert recorded.endswith("article.md")


def test_export_inside_session_records_relative(tmp_path: Path):
    from council.export import export_session

    store = SessionStore(tmp_path, session_id="intest")
    store.write_text("final/paper_final.md", "# T\n\nBody text.\n")
    export_session(store, fmt="md")
    recorded = store.load_meta()["artifacts"]["export_md"]
    assert recorded == "export/paper_final.md"


# ---------- server: error mapping + stage validation ----------


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    sess = tmp_path / "aaa"
    sess.mkdir()
    (sess / "session.json").write_text(json.dumps({"id": "aaa", "title": "t"}))
    config = load_config()
    config.storage.sessions_dir = str(tmp_path)
    return TestClient(create_app(config), raise_server_exceptions=False)


def test_unknown_session_is_404_not_500(client: TestClient):
    resp = client.get("/api/sessions/nope123/artifact", params={"path": "x.md"})
    assert resp.status_code == 404


def test_invalid_session_id_is_400(client: TestClient):
    # One path segment, but characters the id validator rejects.
    resp = client.get("/api/sessions/inv@lid/artifact", params={"path": "x.md"})
    assert resp.status_code == 400
    assert "Invalid session id" in resp.text


def test_resume_rejects_unknown_stage(client: TestClient):
    resp = client.post("/api/sessions/aaa/resume", params={"from_stage": "drft"})
    assert resp.status_code == 400


def test_start_rejects_unknown_stage(client: TestClient):
    resp = client.post(
        "/api/sessions",
        json={"title": "t", "main_points": ["p"], "from_stage": "drft"},
    )
    assert resp.status_code == 400
