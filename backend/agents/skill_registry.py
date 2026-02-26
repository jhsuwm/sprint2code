import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from log_config import logger


class SkillRegistry:
    """Loads local SKILL.md files and prepares context for planning/code generation."""

    def __init__(self):
        self.skills_dir = Path(__file__).parent.parent.parent / "skills"

    def list_local_skills(self) -> List[Dict[str, Any]]:
        if not self.skills_dir.exists():
            return []

        skills: List[Dict[str, Any]] = []
        for skill_file in self.skills_dir.rglob("SKILL.md"):
            parsed = self._parse_skill_file(skill_file)
            if not parsed:
                continue
            skills.append(
                {
                    "name": parsed["name"],
                    "description": parsed["description"],
                    "type": parsed["type"],
                    "path": str(skill_file.relative_to(self.skills_dir.parent)),
                }
            )
        return sorted(skills, key=lambda s: s["name"])

    def load_skill(self, skill_name: str) -> Optional[Dict[str, Any]]:
        if not self.skills_dir.exists():
            return None

        normalized = skill_name.strip().lower()
        for skill_file in self.skills_dir.rglob("SKILL.md"):
            parsed = self._parse_skill_file(skill_file)
            if not parsed:
                continue
            candidates = {
                parsed["name"].lower(),
                skill_file.parent.name.lower(),
                skill_file.stem.lower(),
            }
            if normalized in candidates:
                return parsed

        logger.warning(f"Skill not found: {skill_name}")
        return None

    def build_skill_context(self, skill_names: List[str]) -> Dict[str, Any]:
        technical_context: Dict[str, Any] = {"skills": []}

        for skill_name in skill_names or []:
            skill = self.load_skill(skill_name)
            if not skill:
                continue

            skill_type = skill.get("type", "full")
            # Use the Markdown body only — frontmatter is machine-readable metadata and
            # must NOT be forwarded to the AI as instructional content.
            body = skill.get("body", skill.get("content", ""))
            github_repository = skill.get("github_repository", "")

            technical_context["skills"].append(
                {
                    "name": skill["name"],
                    "description": skill.get("description", ""),
                    "type": skill_type,
                    "github_repository": github_repository,
                    "path": skill.get("path"),
                }
            )

            if skill_type in ("frontend", "backend"):
                existing = technical_context.get(skill_type, "")
                technical_context[skill_type] = f"{existing}\n\n{body}".strip()
            else:
                existing = technical_context.get("full", "")
                technical_context["full"] = f"{existing}\n\n{body}".strip()

        return technical_context

    def extract_repo_urls(self, content: str) -> List[str]:
        if not content:
            return []
        urls = re.findall(r"https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", content)
        return list(dict.fromkeys(urls))

    def _parse_skill_file(self, skill_file: Path) -> Optional[Dict[str, Any]]:
        try:
            raw = skill_file.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to read skill file {skill_file}: {e}")
            return None

        frontmatter, body = self._split_frontmatter(raw)
        name = frontmatter.get("name") or skill_file.parent.name
        description = frontmatter.get("description", "")
        skill_type = self._infer_skill_type(name, description, body, frontmatter)
        # Read github_repository from frontmatter — this is where users configure their project repo
        github_repository = frontmatter.get("github_repository", "").strip()

        return {
            "name": name,
            "description": description,
            "type": skill_type,
            "github_repository": github_repository,
            # body: human-readable Markdown instructions for the AI (no frontmatter)
            "body": body.strip(),
            # content: full raw file including frontmatter (kept for backward compatibility)
            "content": raw.strip(),
            "path": str(skill_file),
        }

    def _split_frontmatter(self, raw: str) -> tuple:
        match = re.match(r"^\s*---\s*\n(.*?)\n---\s*\n?(.*)$", raw, flags=re.DOTALL)
        if not match:
            return {}, raw

        metadata: Dict[str, str] = {}
        for line in match.group(1).splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip().strip("'\"")
        return metadata, match.group(2)

    def _infer_skill_type(self, name: str, description: str, body: str, frontmatter: Dict[str, str]) -> str:
        explicit_type = (frontmatter.get("type") or "").lower()
        if explicit_type in {"frontend", "backend", "full", "fullstack", "grouped"}:
            return "full" if explicit_type in {"fullstack", "grouped"} else explicit_type

        haystack = f"{name}\n{description}\n{body}".lower()
        if "frontend" in haystack and "backend" not in haystack:
            return "frontend"
        if "backend" in haystack and "frontend" not in haystack:
            return "backend"
        return "full"
