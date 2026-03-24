import pytest
from pathlib import Path
from claude_assistant.config import load_config, AssistantConfig


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
