#!/usr/bin/env python3
"""delegation_check — Remind to delegate implementation to Sonnet/OpenCode."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rule_plugin import RulePlugin


DIRECT_IMPL_KEYWORDS = [
    "schreib den code", "write the code", "implementier das direkt",
    "mach das schnell", "fix it directly", "just do it",
    "code das", "hack together", "quick fix",
]

DELEGATION_SIGNALS = [
    "opencode", "sonnet", "delegat", "agent", "/chef",
    "mcp__opencode", "subagent",
]


class DelegationCheckRule(RulePlugin):
    name = "delegation_check"
    description = "Reminds to delegate implementation instead of doing it directly"

    def match(self, prompt: str, session_state: dict) -> bool:
        lower = prompt.lower()
        has_direct = any(kw in lower for kw in DIRECT_IMPL_KEYWORDS)
        has_delegation = any(kw in lower for kw in DELEGATION_SIGNALS)
        return has_direct and not has_delegation

    def inject(self) -> str:
        return (
            "[RULE: delegation_check] DELEGATION — Opus orchestriert, Sonnet implementiert. "
            "Nutze OpenCode MCP (~$3) oder /chef Skill statt direkt zu coden. "
            "CLI FIRST ($0) wenn kein LLM noetig."
        )
