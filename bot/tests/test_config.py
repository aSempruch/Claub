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
        "    schedule:\n"
        '      - cron: "0 9 * * *"\n'
        '        prompt: "check news"\n'
    )
    config = load_config(cfg_file)
    assert "main" in config.agents
    assert "journalist" in config.agents
    agent = config.agents["journalist"]
    assert agent.channel_id == "456"
    assert len(agent.schedules) == 1
    assert agent.schedules[0].cron == "0 9 * * *"
    assert agent.schedules[0].prompt == "check news"


def test_load_config_main_with_schedule(tmp_path: Path) -> None:
    cfg_file = tmp_path / "agents.yaml"
    cfg_file.write_text(
        "agents:\n"
        "  main:\n"
        '    channel_id: "123"\n'
        "    schedule:\n"
        '      - cron: "0 8 * * *"\n'
        '        prompt: "morning review"\n'
    )
    config = load_config(cfg_file)
    assert len(config.agents["main"].schedules) == 1
    assert config.agents["main"].schedules[0].prompt == "morning review"


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
        "    schedule: []\n"
    )
    with pytest.raises(ValueError, match="channel_id"):
        load_config(cfg_file)
