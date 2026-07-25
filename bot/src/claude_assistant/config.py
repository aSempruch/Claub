from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


VALID_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def _validate_compact_pct(value: int | None, context: str) -> int | None:
    if value is not None:
        if not isinstance(value, int) or not (1 <= value <= 100):
            raise ValueError(
                f"{context}: compact_pct must be an integer between 1 and 100, got {value!r}"
            )
    return value


@dataclass(frozen=True)
class AgentConfig:
    channel_id: str
    display_name: str | None = None
    avatar_url: str | None = None
    allowed_tools_additional: list[str] = field(default_factory=list)
    allowed_skills: list[str] = field(default_factory=list)
    model: str | None = None
    effort: str | None = None
    compact_pct: int | None = None
    on_start: list[str] = field(default_factory=list)
    on_stop: list[str] = field(default_factory=list)
    can_stop: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AssistantConfig:
    agents: dict[str, AgentConfig] = field(default_factory=dict)
    allowed_user_ids: set[str] = field(default_factory=set)
    model: str | None = None
    allowed_skills: list[str] = field(default_factory=list)
    effort: str | None = None
    compact_pct: int | None = None
    on_start: list[str] = field(default_factory=list)
    on_stop: list[str] = field(default_factory=list)
    can_stop: list[str] = field(default_factory=list)
    agent_groups: dict[str, list[str]] = field(default_factory=dict)


def load_config(path: Path) -> AssistantConfig:
    with open(path) as f:
        raw = yaml.safe_load(f)

    def _validate_effort(value: str | None, context: str) -> str | None:
        if value is not None and value not in VALID_EFFORT_LEVELS:
            raise ValueError(
                f"{context}: effort must be one of {VALID_EFFORT_LEVELS}, got {value!r}"
            )
        return value

    agents: dict[str, AgentConfig] = {}
    for name, agent_raw in (raw.get("agents") or {}).items():
        # Agent names become MCP tool names (message_agent_{name}), env vars,
        # and URL path segments — keep them to a conservative character set.
        if not re.fullmatch(r"[A-Za-z0-9_-]+", str(name)):
            raise ValueError(
                f"agents.{name}: agent name must contain only letters, digits, "
                f"'-' and '_'"
            )
        channel_id = (agent_raw or {}).get("channel_id")
        if not channel_id:
            raise ValueError(f"agents.{name}.channel_id is required")
        agent_effort = _validate_effort(
            (agent_raw or {}).get("effort"), f"agents.{name}"
        )
        agent_compact_pct = _validate_compact_pct(
            (agent_raw or {}).get("compact_pct"), f"agents.{name}"
        )
        agents[name] = AgentConfig(
            channel_id=channel_id,
            display_name=(agent_raw or {}).get("display_name"),
            avatar_url=(agent_raw or {}).get("avatar_url"),
            allowed_tools_additional=(agent_raw or {}).get("allowed_tools_additional") or [],
            allowed_skills=(agent_raw or {}).get("allowed_skills") or [],
            model=(agent_raw or {}).get("model"),
            effort=agent_effort,
            compact_pct=agent_compact_pct,
            on_start=(agent_raw or {}).get("on_start") or [],
            on_stop=(agent_raw or {}).get("on_stop") or [],
            can_stop=(agent_raw or {}).get("can_stop") or [],
        )

    if "main" not in agents:
        raise ValueError("agents.main is required")

    allowed_user_ids = set(raw.get("allowed_user_ids") or [])
    model = raw.get("model")
    allowed_skills = raw.get("allowed_skills") or []
    effort = _validate_effort(raw.get("effort"), "top-level")
    compact_pct = _validate_compact_pct(raw.get("compact_pct"), "top-level")
    on_start = raw.get("on_start") or []
    on_stop = raw.get("on_stop") or []
    can_stop = raw.get("can_stop") or []

    agent_groups: dict[str, list[str]] = {}
    for gname, members in (raw.get("agent_groups") or {}).items():
        if not isinstance(members, list) or len(members) < 2:
            raise ValueError(f"agent_groups.{gname}: must list at least 2 agents")
        if len(set(members)) != len(members):
            raise ValueError(f"agent_groups.{gname}: contains duplicate members")
        unknown = [m for m in members if m not in agents]
        if unknown:
            raise ValueError(f"agent_groups.{gname}: unknown agents {unknown}")
        agent_groups[gname] = list(members)

    return AssistantConfig(
        agents=agents,
        allowed_user_ids=allowed_user_ids,
        model=model,
        allowed_skills=allowed_skills,
        effort=effort,
        compact_pct=compact_pct,
        on_start=on_start,
        on_stop=on_stop,
        can_stop=can_stop,
        agent_groups=agent_groups,
    )


def reachable_agents(agent_groups: dict[str, list[str]], name: str) -> set[str]:
    """Agents that *name* may message: union of co-members across its groups."""
    out: set[str] = set()
    for members in agent_groups.values():
        if name in members:
            out.update(members)
    out.discard(name)
    return out


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
