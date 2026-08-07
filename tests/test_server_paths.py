"""Regression tests for session path containment in the artifact API.

A string-prefix check let sibling directories sharing a name prefix
(`abc` vs `abc-secret`) escape the session dir and leak file contents.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from council.config import load_config
from council.server import create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    sess = tmp_path / "aaa"
    sess.mkdir()
    (sess / "session.json").write_text(json.dumps({"id": "aaa", "title": "t"}))
    (sess / "ok.md").write_text("legit artifact")
    nested = sess / "final"
    nested.mkdir()
    (nested / "paper_final.md").write_text("nested artifact")

    # Sibling dir sharing the "aaa" prefix — the original exploit.
    evil = tmp_path / "aaa-secret"
    evil.mkdir()
    (evil / "leak.txt").write_text("TOP SECRET CONTENTS")

    config = load_config()
    config.storage.sessions_dir = str(tmp_path)
    return TestClient(create_app(config))


@pytest.mark.parametrize(
    "bad_path",
    [
        "../aaa-secret/leak.txt",  # sibling prefix escape
        "../../etc/passwd",
        "/etc/passwd",
        "final/../../aaa-secret/leak.txt",
    ],
)
def test_artifact_rejects_escapes(client: TestClient, bad_path: str):
    resp = client.get("/api/sessions/aaa/artifact", params={"path": bad_path})
    assert resp.status_code == 400
    assert "SECRET" not in resp.text


def test_artifact_allows_paths_inside_session(client: TestClient):
    resp = client.get("/api/sessions/aaa/artifact", params={"path": "ok.md"})
    assert resp.status_code == 200
    assert resp.json()["content"] == "legit artifact"

    resp = client.get(
        "/api/sessions/aaa/artifact", params={"path": "final/paper_final.md"}
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "nested artifact"


def test_artifact_missing_file_is_404_not_400(client: TestClient):
    resp = client.get("/api/sessions/aaa/artifact", params={"path": "nope.md"})
    assert resp.status_code == 404


def test_download_endpoint_rejects_escape(client: TestClient):
    resp = client.get("/api/sessions/aaa/file/../aaa-secret/leak.txt")
    assert resp.status_code in (400, 404)
    assert "SECRET" not in resp.text
