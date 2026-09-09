from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from agent_framework import SkillsProvider

LOGGER = logging.getLogger(__name__)


def _run_skill_script(skill: Any, script: Any, args: dict | list[str] | None = None) -> str:
    script_path = Path(getattr(script, "full_path", ""))
    if not script_path.exists():
        raise FileNotFoundError(f"Skill script not found: {script_path}")

    cmd = [sys.executable, str(script_path)]
    if isinstance(args, list):
        cmd.extend([str(item) for item in args])
    elif isinstance(args, dict):
        cmd.append(json.dumps(args))

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(script_path.parent),
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(
            f"Skill script execution failed ({script_path.name}): {stderr or f'exit code {result.returncode}'}"
        )
    return (result.stdout or "").strip()


def _build_skills_provider(skill_dir_names: list[str]) -> SkillsProvider | None:
    if not skill_dir_names:
        return None

    # parents[1] from src/skill_utils.py reaches the project root where skills/ lives
    skills_root = Path(__file__).resolve().parents[1] / "skills"
    skill_paths = [
        skills_root / skill_dir_name
        for skill_dir_name in skill_dir_names
        if (skills_root / skill_dir_name).exists()
    ]

    if not skill_paths:
        LOGGER.warning("No matching skill paths found for configured names: %s", skill_dir_names)
        return None

    try:
        return SkillsProvider.from_paths(
            skill_paths=skill_paths,
            script_runner=_run_skill_script,
        )
    except TypeError:
        if len(skill_paths) == 1:
            return SkillsProvider.from_paths(
                skill_paths=skill_paths[0],
                script_runner=_run_skill_script,
            )
        raise
    except Exception as exc:
        LOGGER.warning("Failed to initialize skills provider: %s", exc)
        return None
