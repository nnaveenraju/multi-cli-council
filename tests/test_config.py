from pathlib import Path

from council.config import load_config


def test_load_default_config():
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(project_root=root)
    assert "claude" in cfg.providers
    assert cfg.pipeline.research_required is True
    assert cfg.members["researcher_claude"].model == "sonnet"
    assert cfg.members["critic_claude"].model == "opus"

    # Sonnet gathers; Opus summarizes research
    research = cfg.member_invoke_spec("researcher_claude", "research")
    assert research["tools"] == "web"
    assert research["model"] == "sonnet"
    chair = cfg.seat_invoke_spec("research_chairman")
    assert chair["provider"] == "claude"
    assert chair["model"] == "opus"

    # Critique uses Opus critic, never Sonnet researcher
    assert "critic_claude" in cfg.roles["critique"].participants
    assert "researcher_claude" not in cfg.roles["critique"].participants
    critic = cfg.member_invoke_spec("critic_claude", "critique")
    assert critic["model"] == "opus"
    critique_chair = cfg.seat_invoke_spec("critique_chairman")
    assert critique_chair["model"] == "opus"

    # Draft / finalize stay on fable
    assert cfg.seat_invoke_spec("draft_writer")["model"] == "fable"
    assert cfg.seat_invoke_spec("finalize")["model"] == "fable"
