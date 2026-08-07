"""Council CLI — run, resume, show, list, export, images, serve, doctor."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from council import __version__
from council.config import CouncilConfig, load_config
from council.export import export_session, normalize_format
from council.pipeline import STAGE_ORDER, Pipeline, status_table
from council.seed import build_seed
from council.storage import SessionStore

app = typer.Typer(
    name="council",
    help="Local LLM Council — research, critique, and finalize papers via Claude/Grok/Kimi CLIs.",
    add_completion=False,
    no_args_is_help=True,
)
console = Console()


def _cfg(config: Path | None = None):
    return load_config(path=config)


def _open_store(cfg: CouncilConfig, session_id: str) -> SessionStore:
    """Open an existing session, with CLI-friendly errors instead of tracebacks."""
    try:
        return SessionStore.open(cfg.sessions_path(), session_id)
    except FileNotFoundError:
        console.print(f"[red]Session not found:[/] {session_id}")
        raise typer.Exit(1) from None
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from None


def _check_stage(from_stage: str) -> None:
    if from_stage not in STAGE_ORDER:
        console.print(
            f"[red]Unknown stage:[/] {from_stage} (expected one of {', '.join(STAGE_ORDER)})"
        )
        raise typer.Exit(2)


@app.callback()
def main_callback() -> None:
    """Local LLM Council."""


@app.command()
def version() -> None:
    """Print version."""
    console.print(f"local-llm-council {__version__}")


@app.command()
def doctor(
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
) -> None:
    """Check CLIs, models, and config."""
    cfg = _cfg(config)
    console.print(f"[bold]Config[/] {cfg.config_path}")
    console.print(f"[bold]Sessions[/] {cfg.sessions_path()}")
    table = Table(title="Providers")
    table.add_column("Provider")
    table.add_column("Binary")
    table.add_column("Found")
    table.add_column("Default model")
    for name, prov in cfg.providers.items():
        path = shutil.which(prov.bin) or ""
        found = "yes" if path else "NO"
        style = "green" if path else "red"
        table.add_row(name, prov.bin, f"[{style}]{found}[/]", prov.default_model or "—")
    console.print(table)

    mtable = Table(title="Members / roles (resolved models)")
    mtable.add_column("Seat")
    mtable.add_column("Stage")
    mtable.add_column("Provider")
    mtable.add_column("Model")
    mtable.add_column("Tools")
    for stage in ("research", "critique"):
        role = cfg.roles.get(stage)
        if not role:
            continue
        for mid in role.participants:
            # Show each seat with the tools/timeout of the stage it sits in.
            spec = cfg.member_invoke_spec(mid, stage)
            mtable.add_row(mid, stage, spec["provider"], str(spec["model"]), spec["tools"])
    for role_name in ("research_chairman", "draft_writer", "critique_chairman", "finalize"):
        if role_name in cfg.roles and cfg.roles[role_name].provider:
            spec = cfg.seat_invoke_spec(role_name)
            mtable.add_row(role_name, "—", spec["provider"], str(spec["model"]), spec["tools"])
    console.print(mtable)

    prompts = cfg.project_root / "prompts"
    console.print(f"Prompts dir: {prompts} ({'ok' if prompts.exists() else 'MISSING'})")


@app.command()
def run(
    seed_file: Path | None = typer.Argument(None, help="seed.yaml or seed.md"),
    points: Path | None = typer.Option(None, "--points", help="Markdown/text main points file"),
    links: str | None = typer.Option(None, "--links", help="Comma-separated seed URLs"),
    links_file: Path | None = typer.Option(None, "--links-file", help="File with one URL per line"),
    title: str | None = typer.Option(None, "--title", "-t"),
    goal: list[str] | None = typer.Option(None, "--goal", "-g", help="Repeatable goal/constraint"),
    from_stage: str = typer.Option(
        "seed",
        "--from",
        help="Start stage: seed|research|draft|critique|finalize",
    ),
    config: Path | None = typer.Option(None, "--config", "-c"),
    session_id: str | None = typer.Option(None, "--session", help="Reuse session id"),
) -> None:
    """Run the full council pipeline (research is always required)."""
    cfg = _cfg(config)
    _check_stage(from_stage)
    link_list = [x.strip() for x in links.split(",") if x.strip()] if links else None
    seed = build_seed(
        title=title,
        points=None,
        links=link_list,
        goals=list(goal) if goal else None,
        seed_file=seed_file,
        points_file=points,
        links_file=links_file,
    )
    if not seed.main_points:
        # Resuming an existing session past the seed stage can rely on the
        # seed already stored on disk — don't demand points again.
        resumed = (
            session_id
            and from_stage != "seed"
            and (cfg.sessions_path() / session_id / "input" / "seed.yaml").exists()
        )
        if not resumed:
            console.print("[red]Provide main points via seed file, --points, or seed.yaml[/]")
            raise typer.Exit(2)
    if not seed.seed_links:
        console.print(
            "[yellow]Warning: no seed links — researchers will rely on search only.[/]"
        )

    try:
        store = SessionStore(cfg.sessions_path(), session_id=session_id)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from None
    console.print(f"[bold green]Session[/] {store.session_id}")
    console.print(f"[bold]Title[/] {seed.title}")
    console.print(
        f"[bold]Points[/] {len(seed.main_points)}  [bold]Links[/] {len(seed.seed_links)}"
    )
    console.print(f"[dim]{store.path}[/]\n")

    pipe = Pipeline(cfg, store, console=console)
    try:
        asyncio.run(pipe.run(seed, from_stage=from_stage))
    except KeyboardInterrupt:
        store.update_meta(status="interrupted")
        console.print("\n[yellow]Interrupted — session marked as interrupted.[/]")
        raise typer.Exit(130) from None
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Pipeline failed:[/] {exc}")
        store.update_meta(status="failed", error=str(exc))
        raise typer.Exit(1) from exc

    console.print()
    console.print(status_table(store))
    final = store.path / "final" / "paper_final.md"
    if final.exists():
        console.print(f"\n[bold green]Final paper (md):[/] {final}")
        console.print(
            f"Export Word: [cyan]council export {store.session_id} --format word[/]"
        )
        console.print(
            f"Generate images: [cyan]council images {store.session_id}[/]"
        )
    console.print(f"\nOpen Web UI: [cyan]council serve[/] → session {store.session_id}")
    console.print(f"Inspect: [cyan]council show {store.session_id}[/]")


@app.command()
def resume(
    session_id: str = typer.Argument(..., help="Session id"),
    from_stage: str = typer.Option("research", "--from", help="Stage to resume from"),
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Resume a session from a given stage."""
    cfg = _cfg(config)
    _check_stage(from_stage)
    store = _open_store(cfg, session_id)
    console.print(f"[bold]Resuming[/] {session_id} from {from_stage}")
    pipe = Pipeline(cfg, store, console=console)
    try:
        asyncio.run(pipe.resume(from_stage))
    except KeyboardInterrupt:
        store.update_meta(status="interrupted")
        console.print("\n[yellow]Interrupted — session marked as interrupted.[/]")
        raise typer.Exit(130) from None
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Failed:[/] {exc}")
        store.update_meta(status="failed", error=str(exc))
        raise typer.Exit(1) from exc
    console.print(status_table(store))


@app.command("list")
def list_sessions(
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """List sessions."""
    cfg = _cfg(config)
    sessions = SessionStore.list_sessions(cfg.sessions_path())
    if not sessions:
        console.print("No sessions yet.")
        return
    table = Table(title="Sessions")
    table.add_column("ID")
    table.add_column("Title")
    table.add_column("Status")
    table.add_column("Stage")
    table.add_column("Updated")
    for s in sessions:
        table.add_row(
            s.get("id", ""),
            (s.get("title") or "")[:40],
            s.get("status", ""),
            s.get("stage", ""),
            (s.get("updated_at") or "")[:19],
        )
    console.print(table)


@app.command()
def show(
    session_id: str = typer.Argument(...),
    artifact: str | None = typer.Option(
        None, "--artifact", "-a", help="Relative path under session"
    ),
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Show session status or an artifact."""
    cfg = _cfg(config)
    store = _open_store(cfg, session_id)
    console.print(status_table(store))
    meta = store.load_meta()
    arts = meta.get("artifacts") or {}
    if arts:
        console.print("\n[bold]Artifacts[/]")
        for k, v in arts.items():
            console.print(f"  {k}: {v}")
    if artifact:
        root = store.path.resolve()
        path = (root / artifact).resolve()
        if root != path and root not in path.parents:
            console.print(f"[red]Invalid path:[/] {artifact}")
            raise typer.Exit(1)
        if not path.exists():
            console.print(f"[red]Missing[/] {artifact}")
            raise typer.Exit(1)
        console.print(f"\n[bold]{artifact}[/]\n")
        console.print(Markdown(path.read_text(encoding="utf-8")[:50000]))


@app.command()
def export(
    session_id: str = typer.Argument(..., help="Session id"),
    format: str = typer.Option(
        "md",
        "--format",
        "-f",
        help="Output format: md (default) | docx | word",
    ),
    out: Path | None = typer.Option(
        None, "--out", "-o", help="Output file path (optional)"
    ),
    with_images: bool = typer.Option(
        False,
        "--with-images",
        help="Use paper_with_figures.md and embed PNGs (Word)",
    ),
    generate_images: bool = typer.Option(
        False,
        "--generate-images",
        help="If figures missing, run image generation first (implies --with-images)",
    ),
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Export the final paper as Markdown or Word (.docx)."""
    cfg = _cfg(config)
    store = _open_store(cfg, session_id)
    use_images = with_images or generate_images
    try:
        fmt = normalize_format(format)
        path = export_session(
            store,
            fmt=fmt,
            out=out,
            title=store.load_meta().get("title"),
            with_images=use_images,
            ensure_images=generate_images,
            config=cfg if generate_images else None,
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Export failed:[/] {exc}")
        raise typer.Exit(1) from exc
    console.print(f"[bold green]Exported[/] ({fmt}): {path}")


@app.command()
def word(
    session_id: str = typer.Argument(..., help="Session id"),
    out: Path | None = typer.Option(None, "--out", "-o", help="Output .docx path"),
    with_images: bool = typer.Option(
        False,
        "--with-images",
        help="Embed generated figures into the Word doc",
    ),
    generate_images: bool = typer.Option(
        False,
        "--generate-images",
        help="Generate figures first if missing (implies --with-images)",
    ),
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Export the final paper to Word (.docx). Use --with-images to embed figures."""
    cfg = _cfg(config)
    store = _open_store(cfg, session_id)
    use_images = with_images or generate_images
    try:
        if use_images:
            console.print(
                "[bold cyan]Word + images[/] "
                f"({'generate if needed' if generate_images else 'existing figures'})"
            )
        path = export_session(
            store,
            fmt="docx",
            out=out,
            title=store.load_meta().get("title"),
            with_images=use_images,
            ensure_images=generate_images,
            config=cfg if generate_images else None,
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Word export failed:[/] {exc}")
        raise typer.Exit(1) from exc
    console.print(f"[bold green]Word document:[/] {path}")


@app.command()
def images(
    session_id: str = typer.Argument(..., help="Session id (must have final paper)"),
    count: int | None = typer.Option(
        None, "--count", "-n", help="Number of figures (default from config)"
    ),
    style: str | None = typer.Option(
        None, "--style", help="Illustration style guidance"
    ),
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Generate figures/images for a finalized paper (separate post-step)."""
    from council.images import generate_images

    cfg = _cfg(config)
    store = _open_store(cfg, session_id)
    console.print(f"[bold]Images[/] for session {session_id}")
    try:
        result = asyncio.run(
            generate_images(cfg, store, count=count, style=style, console=console)
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Image generation failed:[/] {exc}")
        raise typer.Exit(1) from exc
    console.print(
        f"\n[bold green]Generated {result['count']} figures[/] in {result['dir']}"
    )
    console.print(f"Index: {store.path / result['index']}")
    console.print(
        f"Re-export Word with figures embedded: [cyan]council word {session_id} --with-images[/]"
    )


@app.command()
def serve(
    host: str | None = typer.Option(None, "--host"),
    port: int | None = typer.Option(None, "--port", "-p"),
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Start the Web UI (FastAPI + SSE)."""
    import uvicorn

    from council.server import create_app

    cfg = _cfg(config)
    h = host or cfg.server.host
    p = port or cfg.server.port
    console.print(f"[bold green]Council UI[/] http://{h}:{p}")
    app_ = create_app(cfg)
    uvicorn.run(app_, host=h, port=p, log_level="info")


# Typer needs a callable for console_scripts sometimes as app()
# hatch entry: council.cli:app — Typer app is fine.

if __name__ == "__main__":
    app()
