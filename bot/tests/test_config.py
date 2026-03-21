import pytest
from pathlib import Path
from claude_assistant.config import load_config, AssistantConfig


def test_load_minimal_config(tmp_path: Path) -> None:
    cfg_file = tmp_path / "agents.yaml"
    cfg_file.write_text(
        'discord:\n  main_channel_id: "123"\nagents: {}\n'
    )
    config = load_config(cfg_file)
    assert config.main_channel_id == "123"
    assert config.agents == {}


def test_load_config_with_agent(tmp_path: Path) -> None:
    cfg_file = tmp_path / "agents.yaml"
    cfg_file.write_text(
        'discord:\n  main_channel_id: "123"\n'
        "agents:\n"
        "  journalist:\n"
        '    channel_id: "456"\n'
        "    schedule:\n"
        '      - cron: "0 9 * * *"\n'
        '        prompt: "check news"\n'
    )
    config = load_config(cfg_file)
    assert "journalist" in config.agents
    agent = config.agents["journalist"]
    assert agent.channel_id == "456"
    assert len(agent.schedules) == 1
    assert agent.schedules[0].cron == "0 9 * * *"
    assert agent.schedules[0].prompt == "check news"


def test_load_config_missing_main_channel(tmp_path: Path) -> None:
    cfg_file = tmp_path / "agents.yaml"
    cfg_file.write_text("discord: {}\nagents: {}\n")
    with pytest.raises(ValueError, match="main_channel_id"):
        load_config(cfg_file)


def test_load_config_agent_missing_channel(tmp_path: Path) -> None:
    cfg_file = tmp_path / "agents.yaml"
    cfg_file.write_text(
        'discord:\n  main_channel_id: "123"\n'
        "agents:\n  journalist:\n    schedule: []\n"
    )
    with pytest.raises(ValueError, match="channel_id"):
        load_config(cfg_file)
