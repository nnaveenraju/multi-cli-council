"""Session filesystem layout and metadata."""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from council.events import EventLog, utc_now

# Session ids are generated as uuid hex (12 chars); accept that shape plus
# simple slug-like ids, but never anything that could escape the root dir.
_SESSION_ID_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{1,63}")


def _validate_session_id(session_id: str) -> str:
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError(f"Invalid session id: {session_id!r}")
    return session_id


class SessionStore:
    """One session = one directory under data/sessions/{id}/."""

    def __init__(self, root: Path, session_id: str | None = None) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.session_id = _validate_session_id(session_id) if session_id else uuid.uuid4().hex[:12]
        self.path = self.root / self.session_id
        self.path.mkdir(parents=True, exist_ok=True)
        self.events = EventLog(self.path / "events.jsonl")
        self._meta_path = self.path / "session.json"

    # --- paths ---
    def sub(self, *parts: str) -> Path:
        p = self.path.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def write_text(self, rel: str, content: str) -> Path:
        p = self.sub(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def read_text(self, rel: str) -> str:
        return (self.path / rel).read_text(encoding="utf-8")

    def exists(self, rel: str) -> bool:
        return (self.path / rel).exists()

    # --- metadata ---
    def load_meta(self) -> dict[str, Any]:
        if not self._meta_path.exists():
            return {
                "id": self.session_id,
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "status": "created",
                "stage": "seed",
                "stages_completed": [],
                "title": "",
                "error": None,
                "artifacts": {},
            }
        return json.loads(self._meta_path.read_text(encoding="utf-8"))

    def save_meta(self, meta: dict[str, Any]) -> None:
        meta["updated_at"] = utc_now()
        meta["id"] = self.session_id
        # Atomic write: a crash mid-write must not leave a truncated
        # session.json that list_sessions then reports as "corrupt".
        tmp = self._meta_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._meta_path)

    def update_meta(self, **kwargs: Any) -> dict[str, Any]:
        meta = self.load_meta()
        meta.update(kwargs)
        self.save_meta(meta)
        return meta

    def mark_artifact(self, key: str, rel_path: str) -> None:
        meta = self.load_meta()
        arts = meta.setdefault("artifacts", {})
        arts[key] = rel_path
        self.save_meta(meta)

    def complete_stage(self, stage: str) -> None:
        meta = self.load_meta()
        done = meta.setdefault("stages_completed", [])
        if stage not in done:
            done.append(stage)
        meta["stage"] = stage
        meta["status"] = "running"
        self.save_meta(meta)

    @classmethod
    def list_sessions(cls, root: Path) -> list[dict[str, Any]]:
        root.mkdir(parents=True, exist_ok=True)
        sessions: list[dict[str, Any]] = []
        for child in root.iterdir():
            if not child.is_dir():
                continue
            meta_path = child / "session.json"
            if meta_path.exists():
                try:
                    sessions.append(json.loads(meta_path.read_text(encoding="utf-8")))
                except json.JSONDecodeError:
                    sessions.append({"id": child.name, "status": "corrupt"})
            else:
                sessions.append({"id": child.name, "status": "unknown"})
        # Newest first by creation time (mtime would reshuffle on every resume).
        sessions.sort(key=lambda m: m.get("created_at") or "", reverse=True)
        return sessions

    @classmethod
    def open(cls, root: Path, session_id: str) -> SessionStore:
        _validate_session_id(session_id)
        path = root / session_id
        if not path.exists():
            raise FileNotFoundError(f"Session not found: {session_id}")
        return cls(root, session_id=session_id)
