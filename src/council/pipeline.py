"""Full council pipeline: seed → research → draft → critique → finalize."""

from __future__ import annotations

import asyncio
import json
import random
import re
import shlex
from collections.abc import Callable, Coroutine
from typing import Any

from rich.console import Console
from rich.table import Table

from council.config import CouncilConfig
from council.events import Event
from council.models import invoke_model
from council.models.base import ModelResult
from council.prompts import render_prompt
from council.sections import split_sections
from council.seed import Seed, write_seed_artifacts
from council.storage import SessionStore

STAGE_ORDER = ["seed", "research", "draft", "critique", "finalize"]

ProgressCb = Callable[[str], None]


class Pipeline:
    def __init__(
        self,
        config: CouncilConfig,
        store: SessionStore,
        console: Console | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.console = console or Console()
        self._status: dict[str, str] = {}

    # ---------- public ----------

    async def run(
        self,
        seed: Seed,
        *,
        from_stage: str = "seed",
        only_stages: list[str] | None = None,
    ) -> dict[str, Any]:
        stages = only_stages or list(self.config.pipeline.stages)
        if "research" not in stages and self.config.pipeline.research_required:
            # Research is mandatory — inject after seed
            if "seed" in stages:
                idx = stages.index("seed") + 1
                stages.insert(idx, "research")
            else:
                stages.insert(0, "research")

        if from_stage not in STAGE_ORDER:
            raise ValueError(
                f"Unknown stage: {from_stage!r} (expected one of {', '.join(STAGE_ORDER)})"
            )
        start_idx = STAGE_ORDER.index(from_stage)
        ordered = [s for s in STAGE_ORDER if s in stages and STAGE_ORDER.index(s) >= start_idx]

        await self._emit(
            "session_start",
            f"Session {self.store.session_id}",
            data={"stages": ordered},
        )
        self.store.update_meta(
            status="running",
            title=seed.title,
            stage=ordered[0] if ordered else "seed",
        )

        if "seed" in ordered or from_stage == "seed":
            await self.stage_seed(seed)
        elif not (self.store.path / "input" / "seed.yaml").exists():
            # Starting mid-pipeline in a session with no seed on disk (e.g.
            # `council run --from draft`): persist the in-memory seed so
            # downstream stages have something to read.
            await self.stage_seed(seed)

        # Reload seed from disk if resuming
        disk_seed = self._load_seed()
        if from_stage != "seed" and "seed" not in ordered and disk_seed != seed:
            self.console.print(
                "[yellow]Session already has a seed on disk; ignoring the "
                "seed arguments passed to this run.[/]"
            )
        seed = disk_seed

        if "research" in ordered:
            await self.stage_research(seed)
        if "draft" in ordered:
            await self.stage_draft(seed)
        if "critique" in ordered:
            await self.stage_critique(seed)
        if "finalize" in ordered:
            await self.stage_finalize(seed)

        # Report the stage that actually ran last, not always "finalize".
        last_stage = ordered[-1] if ordered else from_stage
        meta = self.store.update_meta(status="completed", stage=last_stage)
        await self._emit(
            "session_complete",
            "Pipeline complete",
            data={"id": self.store.session_id},
        )
        return meta

    async def resume(self, from_stage: str) -> dict[str, Any]:
        seed = self._load_seed()
        return await self.run(seed, from_stage=from_stage)

    # ---------- stages ----------

    async def stage_seed(self, seed: Seed) -> None:
        await self._emit("stage_start", "Ingesting seed", stage="seed")
        arts = write_seed_artifacts(self.store.write_text, seed)
        for k, v in arts.items():
            self.store.mark_artifact(k, v)
        self.store.complete_stage("seed")
        self.store.update_meta(title=seed.title)
        await self._emit("stage_complete", "Seed saved", stage="seed")

    async def stage_research(self, seed: Seed) -> None:
        await self._emit("stage_start", "Research (mandatory) — web tools ON", stage="research")
        role = self.config.roles["research"]
        participants = role.participants
        results: dict[str, ModelResult] = {}

        async def one(member_id: str) -> tuple[str, ModelResult]:
            spec = self.config.member_invoke_spec(member_id, "research")
            await self._emit(
                "member_start",
                f"Researching: {spec['label']}",
                stage="research",
                member=member_id,
                provider=spec["provider"],
                model=spec["model"],
            )
            prompt = render_prompt(
                self.config.project_root,
                "research.md",
                title=seed.title,
                main_points=seed.main_points_md(),
                seed_links=seed.links_md(),
                goals=seed.goals_md(),
                role_slant=spec["role_slant"] or member_id,
                label=spec["label"],
            )
            work = self.store.sub("research", member_id)
            result = await invoke_model(
                self.config,
                provider=spec["provider"],
                model=spec["model"],
                prompt=prompt,
                system=self._system_research(spec),
                tools=spec["tools"],
                timeout_seconds=spec["timeout_seconds"],
                cwd=work,
                label=spec["label"],
                member_id=member_id,
            )
            notes_path = f"research/{member_id}/notes.md"
            raw_path = f"research/{member_id}/raw_log.txt"
            self.store.write_text(notes_path, result.text or f"# FAILED\n\n{result.error}\n")
            self.store.write_text(
                raw_path,
                f"exit={result.exit_code}\ncmd={shlex.join(result.command)}\n\n"
                f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}\n",
            )
            # crude sources extract
            sources = _extract_urls(result.text or "")
            self.store.write_text(
                f"research/{member_id}/sources.md",
                "# Sources\n\n" + "\n".join(f"- {u}" for u in sources) + "\n",
            )
            self.store.mark_artifact(f"research_{member_id}", notes_path)
            status = "Done" if result.ok else "Failed"
            secs = f"{result.duration_seconds:.0f}s"
            await self._emit(
                "member_complete" if result.ok else "member_error",
                f"{status}: {spec['label']} ({secs})",
                stage="research",
                member=member_id,
                provider=spec["provider"],
                model=spec["model"],
                artifact=notes_path,
                data={"ok": result.ok, "error": result.error},
            )
            return member_id, result

        if role.parallel:
            pairs = await asyncio.gather(*[_guard_member(one, m) for m in participants])
            results = dict(pairs)
        else:
            for m in participants:
                mid, res = await _guard_member(one, m)
                results[mid] = res

        ok_count = sum(1 for r in results.values() if r.ok)
        if ok_count == 0:
            self.store.update_meta(status="failed", error="All research members failed")
            await self._emit("stage_error", "All research members failed", stage="research")
            raise RuntimeError("Research stage failed: no successful model outputs")

        # Research chairman synthesis
        bundle_parts = []
        for mid, res in results.items():
            spec = self.config.member_invoke_spec(mid, "research")
            bundle_parts.append(f"### {spec['label']} (`{mid}`)\n\n{res.text or res.error}\n")
        bundle = "\n\n---\n\n".join(bundle_parts)
        self.store.write_text("research/bundle.md", bundle)

        seat = self.config.seat_invoke_spec("research_chairman")
        await self._emit(
            "member_start",
            f"Research chairman: {seat['label']}",
            stage="research",
            member="research_chairman",
            provider=seat["provider"],
            model=seat["model"],
        )
        synth_prompt = render_prompt(
            self.config.project_root,
            "research_chairman.md",
            title=seed.title,
            main_points=seed.main_points_md(),
            seed_links=seed.links_md(),
            goals=seed.goals_md(),
            research_bundle=bundle,
        )
        synth = await invoke_model(
            self.config,
            provider=seat["provider"],
            model=seat["model"],
            prompt=synth_prompt,
            system="You are Research Chairman. Output only the synthesis Markdown.",
            tools=seat["tools"],
            timeout_seconds=seat["timeout_seconds"],
            cwd=self.store.sub("research", "_chairman"),
            label=seat["label"],
            member_id="research_chairman",
        )
        self.store.write_text("research/synthesis.md", synth.text or f"# FAILED\n\n{synth.error}\n")
        self.store.mark_artifact("research_synthesis", "research/synthesis.md")
        if not synth.ok:
            # A failed chairman must not feed a "# FAILED" file into drafting.
            await self._emit(
                "member_error",
                f"Research chairman failed: {synth.error}",
                stage="research",
                member="research_chairman",
            )
            self.store.update_meta(status="failed", error=synth.error)
            raise RuntimeError(f"Research chairman failed: {synth.error}")

        # Source union
        all_urls: list[str] = []
        for mid in participants:
            p = self.store.path / "research" / mid / "sources.md"
            if p.exists():
                all_urls.extend(_extract_urls(p.read_text(encoding="utf-8")))
        all_urls.extend(_extract_urls(synth.text or ""))
        uniq = list(dict.fromkeys(all_urls))
        self.store.write_text(
            "research/source_union.md",
            "# Unified sources\n\n" + "\n".join(f"- {u}" for u in uniq) + "\n",
        )
        self.store.mark_artifact("source_union", "research/source_union.md")

        self.store.complete_stage("research")
        await self._emit(
            "stage_complete",
            f"Research complete ({ok_count}/{len(participants)} members + chairman)",
            stage="research",
            artifact="research/synthesis.md",
        )

    async def stage_draft(self, seed: Seed) -> None:
        await self._emit("stage_start", "Writing first draft", stage="draft")
        synthesis = self._read("research/synthesis.md")
        seat = self.config.seat_invoke_spec("draft_writer")
        await self._emit(
            "member_start",
            f"Draft writer: {seat['label']}",
            stage="draft",
            member="draft_writer",
            provider=seat["provider"],
            model=seat["model"],
        )
        prompt = render_prompt(
            self.config.project_root,
            "draft.md",
            title=seed.title,
            main_points=seed.main_points_md(),
            goals=seed.goals_md(),
            research_synthesis=synthesis,
        )
        result = await invoke_model(
            self.config,
            provider=seat["provider"],
            model=seat["model"],
            prompt=prompt,
            system="You are the Draft Writer. Output only the paper Markdown.",
            tools=seat["tools"],
            timeout_seconds=seat["timeout_seconds"],
            cwd=self.store.sub("draft"),
            label=seat["label"],
            member_id="draft_writer",
        )
        paper, claims = _split_draft(result.text or "")
        draft_body = paper or result.text or f"# FAILED\n{result.error}"
        self.store.write_text("draft/paper_v1.md", draft_body)
        if claims:
            self.store.write_text("draft/claims_trace.md", claims)
        self.store.write_text(
            "draft/raw_log.txt",
            f"exit={result.exit_code}\n\nSTDERR:\n{result.stderr}\n",
        )
        self.store.mark_artifact("paper_v1", "draft/paper_v1.md")
        if not result.ok:
            await self._emit("member_error", f"Draft failed: {result.error}", stage="draft")
            self.store.update_meta(status="failed", error=result.error)
            raise RuntimeError(f"Draft failed: {result.error}")
        self.store.complete_stage("draft")
        await self._emit(
            "stage_complete",
            "Draft complete",
            stage="draft",
            artifact="draft/paper_v1.md",
            member="draft_writer",
            provider=seat["provider"],
            model=seat["model"],
        )

    async def stage_critique(self, seed: Seed) -> None:
        await self._emit("stage_start", "Critique council", stage="critique")
        paper = self._read("draft/paper_v1.md")
        synthesis = self._read("research/synthesis.md")
        # Truncate synthesis for critique context if huge
        if len(synthesis) < 20000:
            synth_excerpt = synthesis
        else:
            synth_excerpt = synthesis[:20000] + "\n\n_[truncated]_\n"

        role = self.config.roles["critique"]
        participants = role.participants
        critiques: dict[str, str] = {}

        async def one(member_id: str) -> tuple[str, ModelResult]:
            spec = self.config.member_invoke_spec(member_id, "critique")
            await self._emit(
                "member_start",
                f"Critique: {spec['label']}",
                stage="critique",
                member=member_id,
                provider=spec["provider"],
                model=spec["model"],
            )
            prompt = render_prompt(
                self.config.project_root,
                "critique.md",
                label=spec["label"],
                role_slant=spec["role_slant"] or member_id,
                goals=seed.goals_md(),
                research_synthesis_excerpt=synth_excerpt,
                paper_draft=paper,
            )
            result = await invoke_model(
                self.config,
                provider=spec["provider"],
                model=spec["model"],
                prompt=prompt,
                system="You are an independent paper critic. Output only the critique Markdown.",
                tools=spec["tools"],
                timeout_seconds=spec["timeout_seconds"],
                cwd=self.store.sub("critique", "independent", member_id),
                label=spec["label"],
                member_id=member_id,
            )
            rel = f"critique/independent/{member_id}.md"
            self.store.write_text(rel, result.text or f"# FAILED\n{result.error}")
            self.store.mark_artifact(f"critique_{member_id}", rel)
            await self._emit(
                "member_complete" if result.ok else "member_error",
                f"{'Done' if result.ok else 'Failed'}: {spec['label']}",
                stage="critique",
                member=member_id,
                provider=spec["provider"],
                model=spec["model"],
                artifact=rel,
            )
            return member_id, result

        if role.parallel:
            pairs = await asyncio.gather(*[_guard_member(one, m) for m in participants])
        else:
            pairs = [await _guard_member(one, m) for m in participants]
        for mid, res in pairs:
            if res.ok and res.text:
                critiques[mid] = res.text

        if not critiques:
            self.store.update_meta(status="failed", error="All critique members failed")
            await self._emit("stage_error", "All critique members failed", stage="critique")
            raise RuntimeError("Critique stage failed: no successful critiques")

        # Anonymize (A..Z, then A2..Z2 if a config ever seats >26 critics)
        letters = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

        def _letter(i: int) -> str:
            suffix = str(i // 26 + 1) if i >= 26 else ""
            return letters[i % 26] + suffix

        member_ids = list(critiques.keys())
        random.shuffle(member_ids)
        anon_map = {_letter(i): member_ids[i] for i in range(len(member_ids))}
        inv_map = {v: k for k, v in anon_map.items()}
        self.store.write_text(
            "critique/anonymized_map.json",
            json.dumps({"letter_to_member": anon_map, "member_to_letter": inv_map}, indent=2),
        )

        anon_blocks = []
        for letter, mid in anon_map.items():
            anon_blocks.append(f"## Response {letter}\n\n{critiques[mid]}\n")
            self.store.write_text(f"critique/anonymized/{letter}.md", critiques[mid])
        anonymized_text = "\n".join(anon_blocks)

        # Peer reviews — each participant reviews anonymized set
        peer_parts: list[str] = []

        async def peer(member_id: str) -> tuple[str, ModelResult]:
            spec = self.config.member_invoke_spec(member_id, "critique")
            await self._emit(
                "member_start",
                f"Peer review: {spec['label']}",
                stage="critique",
                member=f"peer_{member_id}",
                provider=spec["provider"],
                model=spec["model"],
            )
            prompt = render_prompt(
                self.config.project_root,
                "peer_review.md",
                title=seed.title,
                anonymized_critiques=anonymized_text,
            )
            result = await invoke_model(
                self.config,
                provider=spec["provider"],
                model=spec["model"],
                prompt=prompt,
                system="You are peer-reviewing anonymized critiques. Be concise.",
                tools="off",
                timeout_seconds=min(spec["timeout_seconds"], 600),
                cwd=self.store.sub("critique", "peer", member_id),
                label=spec["label"],
                member_id=f"peer_{member_id}",
            )
            rel = f"critique/peer_reviews/{member_id}.md"
            self.store.write_text(rel, result.text or f"# FAILED\n{result.error}")
            await self._emit(
                "member_complete" if result.ok else "member_error",
                f"Peer review {'done' if result.ok else 'failed'}: {spec['label']}",
                stage="critique",
                member=f"peer_{member_id}",
                artifact=rel,
            )
            return member_id, result

        peer_pairs = await asyncio.gather(
            *[_guard_member(peer, m) for m in participants if m in critiques]
        )
        for mid, res in peer_pairs:
            peer_parts.append(f"### Peer review by {mid}\n\n{res.text or res.error}\n")

        # Chairman
        seat = self.config.seat_invoke_spec("critique_chairman")
        await self._emit(
            "member_start",
            f"Critique chairman: {seat['label']}",
            stage="critique",
            member="critique_chairman",
            provider=seat["provider"],
            model=seat["model"],
        )
        crit_bundle = "\n\n".join(
            f"### {mid}\n\n{critiques[mid]}" for mid in critiques
        )
        chair_prompt = render_prompt(
            self.config.project_root,
            "critique_chairman.md",
            title=seed.title,
            critiques_bundle=crit_bundle,
            peer_reviews_bundle="\n".join(peer_parts),
            anon_map=json.dumps(anon_map, indent=2),
        )
        chair = await invoke_model(
            self.config,
            provider=seat["provider"],
            model=seat["model"],
            prompt=chair_prompt,
            system="You are Critique Chairman. Output only the council report.",
            tools=seat["tools"],
            timeout_seconds=seat["timeout_seconds"],
            cwd=self.store.sub("critique", "_chairman"),
            label=seat["label"],
            member_id="critique_chairman",
        )
        self.store.write_text(
            "critique/chairman_report.md",
            chair.text or f"# FAILED\n{chair.error}",
        )
        self.store.mark_artifact("critique_report", "critique/chairman_report.md")
        if not chair.ok:
            # Finalize consumes this report — do not continue on a failure.
            await self._emit(
                "member_error",
                f"Critique chairman failed: {chair.error}",
                stage="critique",
                member="critique_chairman",
            )
            self.store.update_meta(status="failed", error=chair.error)
            raise RuntimeError(f"Critique chairman failed: {chair.error}")
        self.store.complete_stage("critique")
        await self._emit(
            "stage_complete",
            "Critique council complete",
            stage="critique",
            artifact="critique/chairman_report.md",
        )

    async def stage_finalize(self, seed: Seed) -> None:
        await self._emit("stage_start", "Finalizing revised paper", stage="finalize")
        seat = self.config.seat_invoke_spec("finalize")
        await self._emit(
            "member_start",
            f"Final editor: {seat['label']}",
            stage="finalize",
            member="finalize",
            provider=seat["provider"],
            model=seat["model"],
        )
        prompt = render_prompt(
            self.config.project_root,
            "finalize.md",
            title=seed.title,
            main_points=seed.main_points_md(),
            goals=seed.goals_md(),
            research_synthesis=self._read("research/synthesis.md"),
            paper_draft=self._read("draft/paper_v1.md"),
            critique_report=self._read("critique/chairman_report.md"),
        )
        result = await invoke_model(
            self.config,
            provider=seat["provider"],
            model=seat["model"],
            prompt=prompt,
            system="You are the Final Editor. Output only the revised paper Markdown package.",
            tools=seat["tools"],
            timeout_seconds=seat["timeout_seconds"],
            cwd=self.store.sub("final"),
            label=seat["label"],
            member_id="finalize",
        )
        # Always keep the raw output for debugging, even on failure.
        self.store.write_text("final/full_output.md", result.text or "")
        if not result.ok:
            self.store.write_text(
                "final/finalize_error.txt",
                f"exit={result.exit_code}\n{result.error}\n",
            )
            await self._emit("member_error", f"Finalize failed: {result.error}", stage="finalize")
            self.store.update_meta(status="failed", error=result.error)
            raise RuntimeError(f"Finalize failed: {result.error}")

        paper, plan, changelog = _split_final(result.text or "")
        final_body = paper or result.text or ""
        self.store.write_text("final/paper_final.md", final_body)
        if plan:
            self.store.write_text("final/revision_plan.md", plan)
        if changelog:
            self.store.write_text("final/change_log.md", changelog)
        self.store.mark_artifact("paper_final", "final/paper_final.md")

        # Auto-export formats (md default; docx if configured)
        try:
            from council.export import export_session

            for fmt in self.config.output.auto_export or ["md"]:
                path = export_session(
                    self.store,
                    fmt=fmt,
                    title=seed.title,
                )
                await self._emit(
                    "member_complete",
                    f"Exported {fmt}: {path.name}",
                    stage="finalize",
                    artifact=str(path.relative_to(self.store.path)),
                )
        except Exception as exc:  # noqa: BLE001
            await self._emit(
                "member_error",
                f"Auto-export warning: {exc}",
                stage="finalize",
            )

        self.store.complete_stage("finalize")
        await self._emit(
            "stage_complete",
            "Final paper ready (use `council word` / `council images` next)",
            stage="finalize",
            artifact="final/paper_final.md",
            provider=seat["provider"],
            model=seat["model"],
        )

    # ---------- helpers ----------

    def _load_seed(self) -> Seed:
        from council.seed import Seed as SeedModel

        path = self.store.path / "input" / "seed.yaml"
        if not path.exists():
            raise FileNotFoundError("No seed in session (input/seed.yaml missing)")
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return SeedModel.model_validate(data)

    def _read(self, rel: str) -> str:
        p = self.store.path / rel
        if not p.exists():
            # A missing prerequisite must stop the run — a placeholder string
            # would silently produce output that never saw the upstream stage.
            raise FileNotFoundError(
                f"Required artifact missing: {rel} "
                f"(resume from an earlier stage to produce it)"
            )
        return p.read_text(encoding="utf-8")

    def _system_research(self, spec: dict[str, Any]) -> str:
        return (
            f"You are {spec['label']}. Angle: {spec.get('role_slant', '')}. "
            "Use web/browser tools to open seed links and find similar sources. "
            "Never invent URLs. Output only the research notes Markdown."
        )

    async def _emit(
        self,
        type_: str,
        message: str,
        *,
        stage: str | None = None,
        member: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        artifact: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        ev = Event(
            type=type_,
            message=message,
            stage=stage,
            member=member,
            provider=provider,
            model=model,
            artifact=artifact,
            data=data or {},
        )
        await self.store.events.emit(ev)
        # Terminal feedback
        prefix = f"[bold cyan]{stage}[/]" if stage else ""
        model_s = f" [dim]{provider}:{model}[/]" if provider else ""
        self.console.print(f"  {prefix}{model_s} {message}")


def _extract_urls(text: str) -> list[str]:
    urls = re.findall(r"https?://[^\s\)\]\>\"']+", text)
    cleaned = []
    for u in urls:
        u = u.rstrip(".,;:")
        if u not in cleaned:
            cleaned.append(u)
    return cleaned


async def _guard_member(
    fn: Callable[[str], Coroutine[Any, Any, tuple[str, ModelResult]]],
    member_id: str,
) -> tuple[str, ModelResult]:
    """Turn unexpected per-member errors into a failed ModelResult.

    Without this, one member's KeyError/OSError inside asyncio.gather aborts
    the whole stage and strands the sibling invocations.
    """
    try:
        return await fn(member_id)
    except Exception as exc:  # noqa: BLE001
        return member_id, ModelResult(
            ok=False,
            text="",
            provider="",
            model=None,
            error=f"{type(exc).__name__}: {exc}",
        )


def _split_draft(text: str) -> tuple[str, str]:
    body, sections = split_sections(text)
    return body, sections.get("Claims Trace", "")


def _split_final(text: str) -> tuple[str, str, str]:
    body, sections = split_sections(text)
    plan = sections.get("Revision Plan Applied", "")
    changelog = sections.get("Change Log", "")
    return body, plan, changelog


def status_table(store: SessionStore) -> Table:
    meta = store.load_meta()
    table = Table(title=f"Session {store.session_id}")
    table.add_column("Field")
    table.add_column("Value")
    for k in ("title", "status", "stage", "created_at", "updated_at"):
        table.add_row(k, str(meta.get(k, "")))
    done = meta.get("stages_completed") or []
    table.add_row("completed", ", ".join(done))
    arts = meta.get("artifacts") or {}
    table.add_row("artifacts", str(len(arts)))
    return table
