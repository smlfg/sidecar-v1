#!/usr/bin/env python3
"""RulePlugin ABC — Base class for context-injection rules.

Each plugin implements match() to detect when it should fire,
and inject() to return the context string to add.
"""

from abc import ABC, abstractmethod


class RulePlugin(ABC):
    """Base class for situational context-injection rules."""

    name: str = "unnamed"
    description: str = ""

    @abstractmethod
    def match(self, prompt: str, session_state: dict) -> bool:
        """Return True if this rule should fire for the given prompt/state."""
        ...

    @abstractmethod
    def inject(self) -> str:
        """Return the context string to inject into additionalContext."""
        ...

    def __repr__(self) -> str:
        return f"<RulePlugin: {self.name}>"
