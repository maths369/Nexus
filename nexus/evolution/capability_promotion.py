from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import re
from typing import Any

from nexus.agent.types import Run, RunEvent


_PERMANENCE_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"以后",
        r"长期",
        r"永久",
        r"固定",
        r"内建",
        r"默认支持",
        r"正式能力",
        r"注册(?:成)?capability",
        r"promote",
    )
]

_INSTALL_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bpip\s+install\b",
        r"\buv\s+pip\s+install\b",
        r"\bnpm\s+install\b",
        r"\bapt(?:-get)?\s+install\b",
        r"\bbrew\s+install\b",
        r"\bgit\s+clone\b",
        r"\bpython(?:3)?\s+.+\.py\b",
    )
]


@dataclass(frozen=True)
class CapabilityPromotionSuggestion:
    capability_id: str
    reason: str
    evidence: list[str]
    proposed_packages: list[str]
    proposed_imports: list[str]
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CapabilityPromotionAdvisor:
    """Suggest when a temporary expansion should become a formal capability."""

    def __init__(self, *, half_life_days: float = 30.0):
        self._half_life_days = max(1.0, float(half_life_days))

    def suggest(self, *, run: Run, events: list[RunEvent]) -> CapabilityPromotionSuggestion | None:
        if run.status.value != "succeeded":
            return None

        task_text = (run.task or "").strip()
        if not self._has_permanence_intent(task_text):
            return None

        system_runs = [
            event for event in events
            if event.event_type == "tool_call"
            and str(event.data.get("tool") or "") == "system_run"
        ]
        if not system_runs:
            return None

        evidence: list[str] = []
        proposed_packages: list[str] = []
        proposed_imports: list[str] = []
        install_like = False

        for event in system_runs:
            command = str((event.data.get("arguments") or {}).get("command") or "").strip()
            if not command:
                continue
            if any(pattern.search(command) for pattern in _INSTALL_PATTERNS):
                install_like = True
                evidence.append(command)
                proposed_packages.extend(self._extract_packages(command))
                proposed_imports.extend(self._infer_imports(command))

        if not install_like:
            return None

        capability_id = self._propose_capability_id(task_text, evidence)
        confidence = min(0.99, 0.55 + 0.1 * len(evidence))
        if run.created_at and run.updated_at:
            age_days = max(0.0, (run.updated_at - run.created_at).total_seconds() / 86400.0)
            confidence *= math.exp(-math.log(2) / self._half_life_days * age_days)

        return CapabilityPromotionSuggestion(
            capability_id=capability_id,
            reason="任务带有长期化意图，且执行中通过 system_run 安装/引导了新能力依赖。",
            evidence=evidence[:5],
            proposed_packages=sorted({item for item in proposed_packages if item}),
            proposed_imports=sorted({item for item in proposed_imports if item}),
            confidence=round(confidence, 2),
        )

    @staticmethod
    def _has_permanence_intent(task_text: str) -> bool:
        if not task_text:
            return False
        compact = re.sub(r"\s+", "", task_text)
        return any(pattern.search(compact) for pattern in _PERMANENCE_PATTERNS)

    @staticmethod
    def _extract_packages(command: str) -> list[str]:
        parts = command.split()
        collected: list[str] = []
        install_seen = False
        for part in parts:
            token = part.strip()
            if token in {"install", "add"}:
                install_seen = True
                continue
            if not install_seen:
                continue
            if token.startswith("-"):
                continue
            if token in {"&&", "||", ";"}:
                break
            collected.append(token)
        return collected

    @staticmethod
    def _infer_imports(command: str) -> list[str]:
        imports: list[str] = []
        if "openpyxl" in command:
            imports.append("openpyxl")
        if "pandas" in command:
            imports.append("pandas")
        if "xlrd" in command:
            imports.append("xlrd")
        if "python-pptx" in command:
            imports.append("pptx")
        if "pymupdf" in command:
            imports.append("fitz")
        return imports

    @staticmethod
    def _propose_capability_id(task_text: str, evidence: list[str]) -> str:
        compact = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "_", task_text.lower()).strip("_")
        if "excel" in compact:
            return "excel_processing"
        if "pdf" in compact:
            return "pdf_processing"
        if "ppt" in compact or "powerpoint" in compact:
            return "ppt_processing"
        if "image" in compact or "ocr" in compact:
            return "image_processing"
        if "web" in compact or "scrap" in compact:
            return "web_scraping"
        if evidence:
            normalized = re.sub(r"[^a-z0-9]+", "_", evidence[0].lower()).strip("_")
            if normalized:
                return normalized[:48].strip("_") or "task_extension"
        return "task_extension"
