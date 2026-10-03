from __future__ import annotations

import hashlib
from pathlib import Path
import re


PLAYBOOK_URI = "ideaflow://moderation/playbook/current"
PLAYBOOK_PATH = Path(__file__).resolve().parents[3] / "docs" / "MODERATION_AGENT_PLAYBOOK.md"


class ModerationPlaybook:
    """Load the deployed policy once. Working responses contain metadata only."""

    def __init__(self, path: Path = PLAYBOOK_PATH) -> None:
        raw = path.read_bytes()
        self.content = raw.decode("utf-8")
        match = re.search(r"^Версия: `([^`]+)`", self.content, re.MULTILINE)
        updated = re.search(r"^Обновлено: `([^`]+)`", self.content, re.MULTILINE)
        if not match or not updated:
            raise ValueError("Playbook must declare version and updated_at")
        self.version = match.group(1)
        self.updated_at = updated.group(1)
        self.sha256 = hashlib.sha256(raw).hexdigest()
        self.version_uri = f"ideaflow://moderation/playbook/{self.version}"
        self.archives = {}
        for archive in sorted((path.parent / "moderation_playbooks").glob("*.md")):
            self.archives[archive.stem] = archive.read_bytes().decode("utf-8")
        if self.version in self.archives and self.archives[self.version] != self.content:
            raise ValueError("Released playbook version is immutable; increase its version")
        self.archives[self.version] = self.content

    def metadata(self) -> dict:
        return {"version": self.version, "sha256": self.sha256, "resource_uri": PLAYBOOK_URI}

    def response_metadata(self) -> dict:
        return {"policy_version": self.version, "policy": self.metadata()}

    def get(self, section: str = "core") -> dict:
        sections = {
            "core": {1, 2, 3, 4},
            "decisions": {5, 6, 7, 8, 10},
            "examples": {5, 6, 7, 9},
            "workflow": set(range(11, 20)),
        }
        if section == "all":
            content = self.content
        elif section in sections:
            parts = re.split(r"(?=^## \d+\.)", self.content, flags=re.MULTILINE)
            content = "\n".join(
                part for part in parts
                if (match := re.match(r"## (\d+)\.", part))
                and int(match.group(1)) in sections[section]
            )
        else:
            raise ValueError("Unknown playbook section")
        return {
            **self.response_metadata(), "version": self.version, "updated_at": self.updated_at,
            "sha256": self.sha256, "content_type": "text/markdown", "section": section,
            "content": content,
        }


playbook = ModerationPlaybook()
