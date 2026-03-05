#!/usr/bin/env python3
"""research_first — Remind to research before implementing."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rule_plugin import RulePlugin


BUILD_KEYWORDS = [
    "implementier", "implement", "bauen", "build", "erstell", "create",
    "schreib", "write", "add feature", "neue funktion", "refactor",
    "entwickl", "develop", "code", "programmier",
]

RESEARCH_SIGNALS = [
    "research", "recherche", "gemini", "prior-art", "explore",
    "untersuche", "analysier", "investigate",
]


class ResearchFirstRule(RulePlugin):
    name = "research_first"
    description = "Reminds to research before jumping into implementation"

    def match(self, prompt: str, session_state: dict) -> bool:
        lower = prompt.lower()
        has_build = any(kw in lower for kw in BUILD_KEYWORDS)
        has_research = any(kw in lower for kw in RESEARCH_SIGNALS)
        # Fire if building intent detected but no research signal in prompt
        # and session is young (< 5 tool calls)
        young_session = session_state.get("total_calls", 0) < 5
        return has_build and not has_research and young_session

    def inject(self) -> str:
        return (
            "[RULE: research_first] RESEARCH FIRST — Vor Implementation: "
            "Gemini MCP ($0.10) oder /research Skill nutzen. "
            "Opus orchestriert, Sonnet recherchiert+implementiert."
        )
