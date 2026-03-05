#!/usr/bin/env python3
"""PhaseTracker — Tracks session phase for context-aware rule injection.

Phases: exploration → implementation → testing → debugging
Transitions based on tool-call patterns and prompt keywords.
"""

import time

# Phase definitions
EXPLORATION = "exploration"
IMPLEMENTATION = "implementation"
TESTING = "testing"
DEBUGGING = "debugging"

# Keywords that signal phase transitions (from prompts)
PROMPT_PHASE_SIGNALS = {
    EXPLORATION: [
        "explore", "research", "understand", "investigate", "analyze",
        "erkunde", "recherche", "versteh", "analysier", "schauen",
        "what is", "how does", "wo ist", "was macht",
    ],
    IMPLEMENTATION: [
        "implement", "build", "create", "write", "add", "refactor",
        "implementier", "bauen", "erstell", "schreib", "hinzufueg",
        "change", "modify", "update", "aender",
    ],
    TESTING: [
        "test", "verify", "check", "validate", "run tests",
        "testen", "pruefen", "validier", "pytest", "unittest",
    ],
    DEBUGGING: [
        "debug", "fix", "error", "bug", "broken", "failing",
        "fehler", "kaputt", "funktioniert nicht", "crash",
    ],
}

# Tool patterns that signal phase transitions
TOOL_PHASE_SIGNALS = {
    EXPLORATION: {"Read", "Glob", "Grep", "Agent", "WebSearch", "WebFetch"},
    IMPLEMENTATION: {"Edit", "Write"},
    TESTING: set(),  # Detected via Bash commands containing "test"/"pytest"
    DEBUGGING: set(),  # Detected via error patterns
}


def _log(msg: str) -> None:
    try:
        with open("/tmp/claude-sidecar.log", "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [phase_tracker] {msg}\n")
    except Exception:
        pass


class PhaseTracker:
    """Tracks the current session phase based on signals."""

    def __init__(self):
        self.current_phase = EXPLORATION
        self.phase_history: list[tuple[str, float]] = [(EXPLORATION, time.time())]
        self.tool_window: list[str] = []  # Last N tools
        self.window_size = 10

    def update_from_prompt(self, prompt: str) -> str:
        """Update phase based on prompt keywords. Returns current phase."""
        lower = prompt.lower()

        # Score each phase by keyword matches
        scores = {}
        for phase, keywords in PROMPT_PHASE_SIGNALS.items():
            score = sum(1 for kw in keywords if kw in lower)
            if score > 0:
                scores[phase] = score

        if scores:
            best_phase = max(scores, key=scores.get)
            if best_phase != self.current_phase:
                self._transition(best_phase, f"prompt keywords: {list(scores.keys())}")

        return self.current_phase

    def update_from_tool(self, tool_name: str, tool_input: dict, had_error: bool) -> str:
        """Update phase based on tool usage patterns. Returns current phase."""
        self.tool_window.append(tool_name)
        if len(self.tool_window) > self.window_size:
            self.tool_window = self.tool_window[-self.window_size:]

        # Check for testing phase via Bash test commands
        if tool_name == "Bash":
            cmd = tool_input.get("command", "").lower() if isinstance(tool_input, dict) else ""
            if any(t in cmd for t in ["pytest", "unittest", "npm test", "cargo test", "go test"]):
                if self.current_phase != TESTING:
                    self._transition(TESTING, f"test command: {cmd[:40]}")
                return self.current_phase

        # Check for debugging phase via errors
        if had_error and self.current_phase == IMPLEMENTATION:
            error_count = sum(1 for t in self.tool_window[-5:] if t == tool_name)
            if error_count >= 2:
                self._transition(DEBUGGING, "repeated errors during implementation")
                return self.current_phase

        # Check tool distribution in window
        if len(self.tool_window) >= 5:
            recent = self.tool_window[-5:]
            read_tools = sum(1 for t in recent if t in TOOL_PHASE_SIGNALS[EXPLORATION])
            write_tools = sum(1 for t in recent if t in TOOL_PHASE_SIGNALS[IMPLEMENTATION])

            if read_tools >= 4 and self.current_phase != EXPLORATION:
                self._transition(EXPLORATION, f"tool window: {read_tools}/5 read ops")
            elif write_tools >= 3 and self.current_phase != IMPLEMENTATION:
                self._transition(IMPLEMENTATION, f"tool window: {write_tools}/5 write ops")

        return self.current_phase

    def _transition(self, new_phase: str, reason: str) -> None:
        old = self.current_phase
        self.current_phase = new_phase
        self.phase_history.append((new_phase, time.time()))
        # Keep history bounded
        if len(self.phase_history) > 20:
            self.phase_history = self.phase_history[-20:]
        _log(f"Phase transition: {old} → {new_phase} (reason: {reason})")

    def to_dict(self) -> dict:
        """Serialize for session state persistence."""
        return {
            "current_phase": self.current_phase,
            "tool_window": self.tool_window,
            "phase_history": [(p, t) for p, t in self.phase_history[-10:]],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PhaseTracker":
        """Restore from serialized state."""
        tracker = cls()
        if data:
            tracker.current_phase = data.get("current_phase", EXPLORATION)
            tracker.tool_window = data.get("tool_window", [])
            history = data.get("phase_history", [])
            if history:
                tracker.phase_history = [(p, t) for p, t in history]
        return tracker
