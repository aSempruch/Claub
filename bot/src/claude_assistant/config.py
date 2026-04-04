from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentConfig:
    channel_id: str
    display_name: str | None = None
    avatar_url: str | None = None
    allowed_tools_additional: list[str] = field(default_factory=list)
    allowed_skills: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AssistantConfig:
    agents: dict[str, AgentConfig] = field(default_factory=dict)
    allowed_user_ids: set[str] = field(default_factory=set)
    model: str | None = None
    allowed_skills: list[str] = field(default_factory=list)


def load_config(path: Path) -> AssistantConfig:
    with open(path) as f:
        raw = yaml.safe_load(f)

    agents: dict[str, AgentConfig] = {}
    for name, agent_raw in (raw.get("agents") or {}).items():
        channel_id = (agent_raw or {}).get("channel_id")
        if not channel_id:
            raise ValueError(f"agents.{name}.channel_id is required")
        agents[name] = AgentConfig(
            channel_id=channel_id,
            display_name=(agent_raw or {}).get("display_name"),
            avatar_url=(agent_raw or {}).get("avatar_url"),
            allowed_tools_additional=(agent_raw or {}).get("allowed_tools_additional") or [],
            allowed_skills=(agent_raw or {}).get("allowed_skills") or [],
        )

    if "main" not in agents:
        raise ValueError("agents.main is required")

    allowed_user_ids = set(raw.get("allowed_user_ids") or [])
    model = raw.get("model")
    allowed_skills = raw.get("allowed_skills") or []

    return AssistantConfig(agents=agents, allowed_user_ids=allowed_user_ids, model=model, allowed_skills=allowed_skills)


def parse_agent_file(path: Path) -> dict[str, str]:
    """Parse an agent .md file into a dict with 'name', 'description', and 'prompt'.

    Expects YAML frontmatter (--- delimited) with name/description fields,
    followed by the prompt body.
    """
    text = path.read_text()
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)", text, re.DOTALL)
    if not match:
        raise ValueError(f"Agent file missing YAML frontmatter: {path}")
    frontmatter = yaml.safe_load(match.group(1))
    if not isinstance(frontmatter, dict) or not frontmatter.get("name"):
        raise ValueError(f"Agent file missing 'name' in frontmatter: {path}")
    return {
        "name": frontmatter["name"],
        "description": frontmatter.get("description", ""),
        "prompt": match.group(2).strip(),
    }


def discover_skills(skills_dir: Path) -> list[str]:
    """Scan a skills directory and return all skill names.

    Each skill is a subdirectory containing a SKILL.md file. The skill name
    is taken from the ``name`` field in the YAML frontmatter, falling back
    to the directory name if not specified.
    """
    names: list[str] = []
    if not skills_dir.is_dir():
        return names
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        folder_name = skill_md.parent.name
        name = folder_name
        try:
            text = skill_md.read_text()
            match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
            if match:
                frontmatter = yaml.safe_load(match.group(1))
                if isinstance(frontmatter, dict) and frontmatter.get("name"):
                    name = frontmatter["name"]
        except Exception:
            log.warning("Failed to parse skill frontmatter: %s", skill_md)
        names.append(name)
    log.info("Discovered %d skill(s): %s", len(names), names)
    return names
