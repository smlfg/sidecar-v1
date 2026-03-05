#!/usr/bin/env python3
"""git_safety — Context injection for git-related prompts."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rule_plugin import RulePlugin


GIT_DANGER_KEYWORDS = [
    "git push", "force push", "git reset", "git rebase",
    "branch loeschen", "delete branch", "git clean",
    "deploy", "release", "merge to main", "push to main",
]

GIT_SAFE_KEYWORDS = [
    "git status", "git log", "git diff", "git branch -a",
    "git stash",
]


class GitSafetyRule(RulePlugin):
    name = "git_safety"
    description = "Injects safety reminders for dangerous git operations"

    def match(self, prompt: str, session_state: dict) -> bool:
        lower = prompt.lower()
        has_danger = any(kw in lower for kw in GIT_DANGER_KEYWORDS)
        only_safe = all(kw in lower for kw in GIT_SAFE_KEYWORDS if kw in lower) and not has_danger
        return has_danger and not only_safe

    def inject(self) -> str:
        return (
            "[RULE: git_safety] GIT SAFETY — Erst `git status` + `git log --oneline` pruefen. "
            "Branching-Strategie mit Samuel bestaetigen. "
            "NIEMALS force-push ohne explizite Freigabe."
        )
