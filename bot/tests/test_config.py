import pytest
from pathlib import Path
from claude_assistant.config import load_config, discover_skills, AssistantConfig


def test_load_minimal_config(tmp_path: Path) -> None:
    cfg_file = tmp_path / "agents.yaml"
    cfg_file.write_text(
        "agents:\n"
        "  main:\n"
        '    channel_id: "123"\n'
    )
    config = load_config(cfg_file)
    assert "main" in config.agents
    assert config.agents["main"].channel_id == "123"


def test_load_config_with_agents(tmp_path: Path) -> None:
    cfg_file = tmp_path / "agents.yaml"
    cfg_file.write_text(
        "agents:\n"
        "  main:\n"
        '    channel_id: "123"\n'
        "  journalist:\n"
        '    channel_id: "456"\n'
        '    display_name: "The Journalist"\n'
    )
    config = load_config(cfg_file)
    assert "main" in config.agents
    assert "journalist" in config.agents
    assert config.agents["journalist"].channel_id == "456"
    assert config.agents["journalist"].display_name == "The Journalist"


def test_load_config_missing_main(tmp_path: Path) -> None:
    cfg_file = tmp_path / "agents.yaml"
    cfg_file.write_text(
        "agents:\n"
        "  journalist:\n"
        '    channel_id: "456"\n'
    )
    with pytest.raises(ValueError, match="agents.main is required"):
        load_config(cfg_file)


def test_load_config_agent_missing_channel(tmp_path: Path) -> None:
    cfg_file = tmp_path / "agents.yaml"
    cfg_file.write_text(
        "agents:\n"
        "  main:\n"
        '    channel_id: "123"\n'
        "  journalist:\n"
        "    display_name: nope\n"
    )
    with pytest.raises(ValueError, match="channel_id"):
        load_config(cfg_file)


def test_discover_skills_from_frontmatter(tmp_path: Path) -> None:
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: custom-name\ndescription: A skill\n---\nContent here\n"
    )
    names = discover_skills(tmp_path)
    assert names == ["custom-name"]


def test_discover_skills_falls_back_to_folder_name(tmp_path: Path) -> None:
    skill_dir = tmp_path / "folder-name"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("No frontmatter here\n")
    names = discover_skills(tmp_path)
    assert names == ["folder-name"]


def test_discover_skills_empty_dir(tmp_path: Path) -> None:
    assert discover_skills(tmp_path) == []


def test_discover_skills_nonexistent_dir(tmp_path: Path) -> None:
    assert discover_skills(tmp_path / "nope") == []


def test_discover_skills_multiple(tmp_path: Path) -> None:
    for name in ["alpha", "beta"]:
        d = tmp_path / name
        d.mkdir()
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n")
    names = discover_skills(tmp_path)
    assert names == ["alpha", "beta"]


def test_effort_global_default(tmp_path: Path) -> None:
    cfg_file = tmp_path / "agents.yaml"
    cfg_file.write_text(
        "effort: high\n"
        "agents:\n"
        "  main:\n"
        '    channel_id: "123"\n'
    )
    config = load_config(cfg_file)
    assert config.effort == "high"
    assert config.agents["main"].effort is None


def test_effort_per_agent(tmp_path: Path) -> None:
    cfg_file = tmp_path / "agents.yaml"
    cfg_file.write_text(
        "agents:\n"
        "  main:\n"
        '    channel_id: "123"\n'
        "    effort: max\n"
    )
    config = load_config(cfg_file)
    assert config.effort is None
    assert config.agents["main"].effort == "max"


def test_effort_invalid_value(tmp_path: Path) -> None:
    cfg_file = tmp_path / "agents.yaml"
    cfg_file.write_text(
        "effort: turbo\n"
        "agents:\n"
        "  main:\n"
        '    channel_id: "123"\n'
    )
    with pytest.raises(ValueError, match="effort must be one of"):
        load_config(cfg_file)


def test_effort_invalid_per_agent(tmp_path: Path) -> None:
    cfg_file = tmp_path / "agents.yaml"
    cfg_file.write_text(
        "agents:\n"
        "  main:\n"
        '    channel_id: "123"\n'
        "    effort: ultra\n"
    )
    with pytest.raises(ValueError, match="agents.main"):
        load_config(cfg_file)


def test_compact_pct_global_default(tmp_path: Path) -> None:
    cfg_file = tmp_path / "agents.yaml"
    cfg_file.write_text(
        "compact_pct: 75\n"
        "agents:\n"
        "  main:\n"
        '    channel_id: "123"\n'
    )
    config = load_config(cfg_file)
    assert config.compact_pct == 75
    assert config.agents["main"].compact_pct is None


def test_compact_pct_per_agent(tmp_path: Path) -> None:
    cfg_file = tmp_path / "agents.yaml"
    cfg_file.write_text(
        "agents:\n"
        "  main:\n"
        '    channel_id: "123"\n'
        "    compact_pct: 60\n"
    )
    config = load_config(cfg_file)
    assert config.compact_pct is None
    assert config.agents["main"].compact_pct == 60


def test_compact_pct_invalid_value(tmp_path: Path) -> None:
    cfg_file = tmp_path / "agents.yaml"
    cfg_file.write_text(
        "compact_pct: 150\n"
        "agents:\n"
        "  main:\n"
        '    channel_id: "123"\n'
    )
    with pytest.raises(ValueError, match="compact_pct must be an integer between 1 and 100"):
        load_config(cfg_file)


def test_compact_pct_invalid_per_agent(tmp_path: Path) -> None:
    cfg_file = tmp_path / "agents.yaml"
    cfg_file.write_text(
        "agents:\n"
        "  main:\n"
        '    channel_id: "123"\n'
        "    compact_pct: 0\n"
    )
    with pytest.raises(ValueError, match="agents.main"):
        load_config(cfg_file)


def test_on_start_global_only(tmp_path: Path) -> None:
    cfg_file = tmp_path / "agents.yaml"
    cfg_file.write_text(
        'on_start: ["echo global-start"]\n'
        'on_stop: ["echo global-stop"]\n'
        "agents:\n"
        "  main:\n"
        '    channel_id: "123"\n'
    )
    config = load_config(cfg_file)
    assert config.on_start == ["echo global-start"]
    assert config.on_stop == ["echo global-stop"]
    assert config.agents["main"].on_start == []
    assert config.agents["main"].on_stop == []


def test_on_start_per_agent_additive(tmp_path: Path) -> None:
    cfg_file = tmp_path / "agents.yaml"
    cfg_file.write_text(
        'on_start: ["echo global"]\n'
        "agents:\n"
        "  main:\n"
        '    channel_id: "123"\n'
        '    on_start: ["echo local"]\n'
    )
    config = load_config(cfg_file)
    assert config.on_start == ["echo global"]
    assert config.agents["main"].on_start == ["echo local"]


def test_on_start_empty_defaults(tmp_path: Path) -> None:
    cfg_file = tmp_path / "agents.yaml"
    cfg_file.write_text(
        "agents:\n"
        "  main:\n"
        '    channel_id: "123"\n'
    )
    config = load_config(cfg_file)
    assert config.on_start == []
    assert config.on_stop == []
    assert config.agents["main"].on_start == []
    assert config.agents["main"].on_stop == []


def test_allowed_skills_config(tmp_path: Path) -> None:
    cfg_file = tmp_path / "agents.yaml"
    cfg_file.write_text(
        "agents:\n"
        "  main:\n"
        '    channel_id: "123"\n'
        "    allowed_skills:\n"
        "      - amazon-browse\n"
    )
    config = load_config(cfg_file)
    assert config.agents["main"].allowed_skills == ["amazon-browse"]
