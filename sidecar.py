#!/usr/bin/env python3
"""Claude Code Sidecar Daemon.

Persistenter Daemon, der auf Unix Socket lauscht, Companion Files laedt,
und Tool-Call Events mit 9 Detektoren analysiert.

Starten: python3 sidecar.py
Socket: /tmp/claude-sidecar.sock
Log: /tmp/claude-sidecar.log
"""

import asyncio
import fcntl
import json
import os
import signal
import sys
import tempfile
import time

# Plugin system
from plugin_loader import PluginLoader
from phase_tracker import PhaseTracker

# --- Config ---
SOCKET_PATH = "/tmp/claude-sidecar.sock"
LOG_PATH = "/tmp/claude-sidecar.log"
STATE_DIR = "/tmp"
MAX_HISTORY = 25
CONCERN_COOLDOWN_SECONDS = 15

COMPANION_FILES = {
    "fehler": os.path.expanduser("~/.claude/WelcheFehlerVermeiden.md"),
    "skills": os.path.expanduser("~/.claude/Skilluebersicht.md"),
}

RULES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules")

SOFT_ERROR_PATTERNS = [
    "Traceback (most recent call last)",
    "Error:", "FAILED", "SyntaxError", "TypeError", "NameError",
    "ValueError", "AttributeError", "ImportError", "ModuleNotFoundError",
    "KeyError", "IndexError", "RuntimeError", "fatal:", "panic:",
    "Exception:", "segfault", "core dumped",
]

SELF_MARKERS = ["[PATTERN WATCHER]", "[MINIMAX WATCHER]", "[CODEX ZWEITMEINUNG]", "[SIDECAR]"]


# --- Logging ---

def _log(msg: str) -> None:
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


# --- Companion File Parsing ---

def _parse_anti_patterns(content: str) -> list:
    """Extrahiere Anti-Pattern Rules aus WelcheFehlerVermeiden.md."""
    patterns = []
    try:
        # Bekannte Top-10 Fehler mit Keywords
        known = [
            {
                "name": "Task Incompletion Loop",
                "keywords": ["session", "parallel", "multi-task", "adhs", "74 sessions"],
                "advice": "EIN Ziel pro Session. Frag: Was ist das EINE Ergebnis?",
            },
            {
                "name": "Zombie-Prozesse",
                "keywords": ["opencode_fire", "background", "fire-and-forget", "zombie"],
                "advice": "Nach opencode_fire: opencode_check nach 5min. Freitags aufraeumen.",
            },
            {
                "name": "BUILD_SPECS ohne Execution",
                "keywords": ["spec", "docs first", "planning", "BUILD_SPEC"],
                "advice": "Code First, Docs Second. 48h Regel: Spec → implementieren oder loeschen.",
            },
            {
                "name": "JWT Token Exposed",
                "keywords": ["api_key", "jwt", "token", "secret", "settings.json"],
                "advice": "Secrets NIEMALS in settings.json → ~/.env.local",
            },
            {
                "name": "Delegation ohne Context",
                "keywords": ["chef", "ohne context", "gather-context"],
                "advice": "Vor JEDEM /chef: gather-context.sh ausfuehren.",
            },
            {
                "name": "Kosten-Wissen ohne Kosten-Disziplin",
                "keywords": ["opus", "research", "subagent", "gemini first"],
                "advice": "/research (Gemini) IMMER zuerst. WebSearch NIE als erste Wahl.",
            },
            {
                "name": "Scope Creep",
                "keywords": ["und ausserdem", "eigentlich", "wir koennen auch", "scope"],
                "advice": "Claude fragt aktiv: Das wird ein zweites Projekt. Parken?",
            },
            {
                "name": "Silent Self-Execution",
                "keywords": ["mcp fehler", "statt delegation", "selbst gemacht"],
                "advice": "MCP-Fehler MELDEN, nicht still selbst machen.",
            },
            {
                "name": "Narrow-Scoped Fix",
                "keywords": ["fix", "nur hier", "subsystem", "bug"],
                "advice": "Nach JEDEM Fix: Wo gilt das gleiche Prinzip noch?",
            },
            {
                "name": "Keine Baseline",
                "keywords": ["session start", "check-state", "wo stehen wir"],
                "advice": "Session-Start: /check-state oder /baseline.",
            },
        ]

        # Einfaches Parsing: suche Sections aus Datei
        lines = content.split("\n")
        current_name = None
        for line in lines:
            if line.startswith("#### #") or (line.startswith("### ") and "KRITISCH" not in line and "HOCH" not in line):
                # Extrahiere Titel
                title = line.lstrip("#").strip()
                if ":" in title:
                    title = title.split(":", 1)[1].strip()
                current_name = title
            elif line.startswith("- **Was:**") and current_name:
                desc = line.replace("- **Was:**", "").strip()
                # Finde ob wir diesen bereits kennen
                matched = False
                for p in known:
                    if p["name"].lower() in current_name.lower() or current_name.lower() in p["name"].lower():
                        matched = True
                        break
                if not matched:
                    known.append({
                        "name": current_name,
                        "keywords": [w.lower() for w in current_name.split()],
                        "advice": desc[:100],
                    })

        patterns = known
        _log(f"Parsed {len(patterns)} anti-patterns from WelcheFehlerVermeiden.md")
    except Exception as e:
        _log(f"Error parsing anti-patterns: {e}")
    return patterns


def _parse_skill_triggers(content: str) -> list:
    """Extrahiere Skill Trigger Map aus Skilluebersicht.md."""
    skills = []
    try:
        in_trigger_section = False
        lines = content.split("\n")
        for line in lines:
            if "Skill Trigger Map" in line:
                in_trigger_section = True
                continue
            if in_trigger_section and line.startswith("## ") and "Skill Trigger" not in line:
                # End of trigger section if we hit a new top-level section
                break
            if in_trigger_section and line.startswith("|") and "Trigger" not in line and "---" not in line:
                parts = [p.strip() for p in line.split("|") if p.strip()]
                if len(parts) >= 3:
                    trigger_text = parts[0]
                    skill_name = parts[1]
                    reason = parts[2] if len(parts) > 2 else ""
                    # Keywords aus Trigger-Text
                    keywords = []
                    raw = trigger_text.lower()
                    # Strip markdown backticks and quotes
                    for ch in ["`", '"', "'"]:
                        raw = raw.replace(ch, "")
                    keywords = [w for w in raw.split() if len(w) > 3]
                    skills.append({
                        "skill_name": skill_name,
                        "trigger_keywords": keywords,
                        "description": reason,
                        "trigger_text": trigger_text,
                    })

        _log(f"Parsed {len(skills)} skill triggers from Skilluebersicht.md")
    except Exception as e:
        _log(f"Error parsing skill triggers: {e}")
    return skills


# Skill-Suggest spezifische Regeln (ergaenzend zu Trigger Map)
SKILL_SUGGEST_RULES = [
    {
        "condition": "many_bash_errors",
        "trigger": lambda h: sum(1 for e in h[-5:] if e.get("had_error") and e.get("tool") == "Bash") >= 2,
        "suggestion": "/debug-loop",
        "reason": "2+ Bash-Fehler in letzten 5 Calls — systematisch debuggen statt Trial-and-Error",
    },
    {
        "condition": "read_storm_before_code",
        "trigger": lambda h: sum(1 for e in h[-8:] if e.get("tool") in ("Read", "Glob", "Grep")) >= 6,
        "suggestion": "/explore-first",
        "reason": "Viele Read-Ops — /explore-first fuer parallele Codebase-Erkundung",
    },
    {
        "condition": "no_research_before_new_task",
        "trigger": lambda h: len(h) <= 3 and not any(e.get("tool") == "Agent" for e in h),
        "suggestion": "/research",
        "reason": "Neue Session ohne Research — Gemini first ($0.10 statt $15)",
    },
    {
        "condition": "long_session_no_commit",
        "trigger": lambda h: len(h) >= 20 and not any("git commit" in e.get("summary", "") for e in h),
        "suggestion": "/checkpoint",
        "reason": "20+ Calls ohne Commit — /checkpoint fuer Git-Snapshot",
    },
    {
        "condition": "many_edits_no_test",
        "trigger": lambda h: sum(1 for e in h[-10:] if e.get("tool") in ("Edit", "Write")) >= 4
                            and not any("test" in e.get("summary", "").lower() or "pytest" in e.get("summary", "").lower()
                                       for e in h[-10:]),
        "suggestion": "/cli test",
        "reason": "4+ Edits ohne Test — /cli test ($0) bevor weitergebaut wird",
    },
]


ENFORCEMENT_RULES = [
    {
        "name": "no_cli_first",
        "trigger": lambda h, tn, ti: tn.startswith("mcp__opencode__") and not any(
            e.get("tool") in ("Bash", "Read", "Glob", "Grep") for e in h[-5:]
        ),
        "warning": "CLI FIRST: Shell ($0) vor MCP (~$3)",
    },
    {
        "name": "autonomous_git",
        "trigger": lambda h, tn, ti: tn == "Bash" and isinstance(ti, dict) and any(
            gc in ti.get("command", "") for gc in ["git commit", "git push", "git branch"]
        ) and not any(gc in ti.get("command", "") for gc in ["git status", "git diff", "git log", "git branch -a"]),
        "warning": "No autonomous git: Erst fragen",
    },
    {
        "name": "mcp_timeout_drift",
        "trigger": lambda h, tn, ti: tn.startswith("mcp__") and len(h) >= 2
            and h[-2].get("tool", "").startswith("mcp__")
            and (h[-1].get("ts", 0) - h[-2].get("ts", 0)) > 120,
        "warning": "120s MCP Timeout ueberschritten",
    },
    {
        "name": "reread_after_diff",
        "trigger": lambda h, tn, ti: tn == "Read" and isinstance(ti, dict) and ti.get("file_path", "") and any(
            e.get("tool") == "Bash" and "git diff" in e.get("summary", "") and ti.get("file_path", "").split("/")[-1] in e.get("summary", "")
            for e in h[-5:]
        ),
        "warning": "Trust git diff, nicht nochmal lesen",
    },
    {
        "name": "gemini_not_first",
        "trigger": lambda h, tn, ti: tn in ("WebSearch", "WebFetch") and not any(
            "mcp__gemini" in e.get("tool", "") for e in h
        ),
        "warning": "Gemini FIRST vor WebSearch",
    },
]


def _detect_enforcement(history: list, tool_name: str, tool_input: dict) -> list:
    """Detektoren 10-14: Rule enforcement basierend auf CLAUDE.md Regeln."""
    hits = []
    for rule in ENFORCEMENT_RULES:
        try:
            if rule["trigger"](history, tool_name, tool_input):
                hits.append(f"ENFORCEMENT [{rule['name']}]: {rule['warning']}")
        except Exception:
            pass
    return hits


# --- RuleRegistry ---

OVERRIDES_PATH = os.path.expanduser("~/.claude/sidecar-rules.json")


class RuleRegistry:
    """Central registry for all sidecar rules — unified schema."""

    def __init__(self):
        self.rules: dict = {}  # name -> rule dict
        self.fire_counts: dict = {}
        self.last_fired: dict = {}
        self.findings: list = []  # last 50 findings
        self.overrides: dict = {}  # loaded from OVERRIDES_PATH
        self._load_overrides()

    def _load_overrides(self):
        try:
            with open(OVERRIDES_PATH, "r") as f:
                self.overrides = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self.overrides = {}

    def save_overrides(self):
        try:
            fd, tmp = tempfile.mkstemp(prefix="sidecar-rules-")
            with os.fdopen(fd, "w") as f:
                json.dump(self.overrides, f, indent=2)
            os.replace(tmp, OVERRIDES_PATH)
        except Exception as e:
            _log(f"Error saving overrides: {e}")

    def register(self, name: str, group: str, trigger, severity: str = "warn",
                 message: str = "", cooldown_s: int = 0):
        self.rules[name] = {
            "name": name,
            "group": group,
            "enabled": True,
            "trigger": trigger,
            "severity": severity,
            "message": message,
            "cooldown_s": cooldown_s,
        }
        # Apply saved overrides
        if name in self.overrides:
            ov = self.overrides[name]
            if "enabled" in ov:
                self.rules[name]["enabled"] = ov["enabled"]
            if "cooldown_s" in ov:
                self.rules[name]["cooldown_s"] = ov["cooldown_s"]

    def is_enabled(self, name: str) -> bool:
        rule = self.rules.get(name)
        if not rule:
            return False
        if not rule["enabled"]:
            return False
        # Check snooze
        snooze_until = self.overrides.get(name, {}).get("snooze_until", 0)
        if snooze_until and time.time() < snooze_until:
            return False
        return True

    def cooldown_ok(self, name: str) -> bool:
        rule = self.rules.get(name)
        if not rule:
            return True
        cd = rule.get("cooldown_s", 0)
        if cd <= 0:
            return True
        last = self.last_fired.get(name, 0)
        return (time.time() - last) >= cd

    def fire(self, name: str, message: str = ""):
        self.fire_counts[name] = self.fire_counts.get(name, 0) + 1
        self.last_fired[name] = time.time()
        finding = {
            "rule": name,
            "message": message or self.rules.get(name, {}).get("message", ""),
            "ts": time.time(),
            "severity": self.rules.get(name, {}).get("severity", "warn"),
        }
        self.findings.append(finding)
        if len(self.findings) > 50:
            self.findings = self.findings[-50:]

    def set_override(self, name: str, **kwargs):
        if name not in self.overrides:
            self.overrides[name] = {}
        self.overrides[name].update(kwargs)
        # Apply to live rule
        if name in self.rules:
            if "enabled" in kwargs:
                self.rules[name]["enabled"] = kwargs["enabled"]
            if "cooldown_s" in kwargs:
                self.rules[name]["cooldown_s"] = kwargs["cooldown_s"]
        self.save_overrides()

    def all_rules_info(self, group_filter: str = "") -> list:
        result = []
        for name, rule in self.rules.items():
            if group_filter and rule["group"] != group_filter:
                continue
            snooze_until = self.overrides.get(name, {}).get("snooze_until", 0)
            snoozed = bool(snooze_until and time.time() < snooze_until)
            result.append({
                "name": name,
                "group": rule["group"],
                "enabled": rule["enabled"],
                "snoozed": snoozed,
                "severity": rule["severity"],
                "fires": self.fire_counts.get(name, 0),
                "last_fired": self.last_fired.get(name, 0),
            })
        return result


class SidecarState:
    """Companion Files und Daemon-State."""

    def __init__(self):
        self.anti_patterns = []
        self.skill_triggers = []
        self.plugin_loader = PluginLoader(RULES_DIR)
        self.phase_trackers: dict = {}
        self.loaded = False
        self._start_ts = time.time()
        self.registry = RuleRegistry()
        self._pending_opencode_fires: dict = {}  # session_id -> call count since last fire
        self.register_builtin_rules()

    def register_builtin_rules(self):
        """Register all built-in rules into the registry."""
        # Pattern detectors (group="pattern")
        self.registry.register("loop", "pattern",
            lambda h, tn, ti, ev: False,  # handled in _detect_concerns
            severity="warn", message="LOOP detected")
        self.registry.register("read_storm", "pattern",
            lambda h, tn, ti, ev: False,
            severity="warn", message="READ-STORM detected")
        self.registry.register("error_cascade", "pattern",
            lambda h, tn, ti, ev: False,
            severity="warn", message="ERROR-CASCADE detected")
        self.registry.register("drift", "pattern",
            lambda h, tn, ti, ev: False,
            severity="warn", message="DRIFT detected")
        self.registry.register("yolo", "pattern",
            lambda h, tn, ti, ev: False,
            severity="warn", message="YOLO detected")
        self.registry.register("thrash", "pattern",
            lambda h, tn, ti, ev: False,
            severity="warn", message="THRASH detected")
        self.registry.register("stall", "pattern",
            lambda h, tn, ti, ev: False,
            severity="warn", message="STALL detected")

        # Anti-pattern detectors (group="anti_pattern")
        self.registry.register("anti_pattern", "anti_pattern",
            lambda h, tn, ti, ev: False,
            severity="warn", message="Anti-pattern detected")

        # Skill suggestions (group="skill_suggest")
        self.registry.register("skill_suggest", "skill_suggest",
            lambda h, tn, ti, ev: False,
            severity="info", message="Skill suggestion available")

        # Enforcement rules (group="enforcement") — wrap 3-arg triggers
        for rule in ENFORCEMENT_RULES:
            name = rule["name"]
            orig_trigger = rule["trigger"]
            wrapped = (lambda t: lambda h, tn, ti, ev: t(h, tn, ti))(orig_trigger)
            self.registry.register(name, "enforcement", wrapped,
                severity="warn", message=rule["warning"])

        # New enforcement rules
        self.registry.register("session_start_recap", "enforcement",
            lambda h, tn, ti, ev: False,  # handled specially in _process_event
            severity="warn", message="Session gestartet ohne /recap + /learn")
        self.registry.register("track_opencode_fire", "enforcement",
            lambda h, tn, ti, ev: False,  # handled specially in _process_event
            severity="warn", message="opencode_fire ohne nachfolgendes opencode_check")

        # Gate rules (group="gate", severity="block")
        self.registry.register("gate_rm_rf", "gate",
            lambda h, tn, ti, ev: (
                tn == "Bash"
                and isinstance(ti, dict)
                and any(p in ti.get("command", "") for p in ["rm -rf", "rm -r"])
                and "# CONFIRMED" not in ti.get("command", "")
            ),
            severity="block",
            message="rm -rf ohne # CONFIRMED Kommentar — bitte explizit bestaetigen")

        self.registry.register("gate_autonomous_git", "gate",
            lambda h, tn, ti, ev: (
                tn == "Bash"
                and isinstance(ti, dict)
                and any(gc in ti.get("command", "") for gc in ["git commit", "git push", "git branch", "git merge"])
                and not any(
                    e.get("tool") == "Bash" and any(
                        chk in e.get("summary", "") for chk in ["git status", "git diff", "git log"]
                    )
                    for e in h[-5:]
                )
            ),
            severity="block",
            message="Autonomes git ohne vorherigen git status/diff/log — Strategie pruefen")

        self.registry.register("gate_secret_exposure", "gate",
            lambda h, tn, ti, ev: (
                tn in ("Edit", "Write")
                and isinstance(ti, dict)
                and any(kw in (ti.get("new_string", "") + ti.get("content", "")).lower()
                        for kw in ["api_key", "jwt", "bearer", "token", "secret", "password"])
                and any(sensitive in ti.get("file_path", "").lower()
                        for sensitive in ["settings.json", ".env", "config"])
            ),
            severity="block",
            message="Moegliche Secret-Exposition in sensitiver Datei — bitte pruefen")

        self.registry.register("gate_force_push", "gate",
            lambda h, tn, ti, ev: (
                tn == "Bash"
                and isinstance(ti, dict)
                and any(p in ti.get("command", "") for p in ["git push --force", "git push -f"])
            ),
            severity="block",
            message="git push --force ist destruktiv — explizite Bestaetigung erforderlich")

        self.registry.register("gate_opencode_vague", "gate",
            lambda h, tn, ti, ev: (
                isinstance(tn, str) and tn.startswith("mcp__opencode__")
                and isinstance(ti, dict)
                and any(vague in ti.get("prompt", "").lower()
                        for vague in ["try", "maybe", "might", "possibly", "probably"])
            ),
            severity="block",
            message="Vager opencode-Prompt — Fakten statt 'try/maybe'. Erst Gemini, dann OpenCode")

        self.registry.register("gate_wrong_provider", "gate",
            lambda h, tn, ti, ev: (
                isinstance(tn, str) and tn.startswith("mcp__opencode__")
                and isinstance(ti, dict)
                and 'providerID: "anthropic"' in str(ti)
            ),
            severity="block",
            message='providerID: "anthropic" in OpenCode — sollte minimax verwenden')

    def load_companion_files(self):
        try:
            with open(COMPANION_FILES["fehler"], "r") as f:
                fehler_content = f.read()
            self.anti_patterns = _parse_anti_patterns(fehler_content)
        except Exception as e:
            _log(f"Could not load WelcheFehlerVermeiden.md: {e}")
            self.anti_patterns = []

        try:
            with open(COMPANION_FILES["skills"], "r") as f:
                skills_content = f.read()
            self.skill_triggers = _parse_skill_triggers(skills_content)
        except Exception as e:
            _log(f"Could not load Skilluebersicht.md: {e}")
            self.skill_triggers = []

        # Load rule plugins
        self.plugin_loader.load()

        self.loaded = True
        _log(f"Companion files loaded: {len(self.anti_patterns)} patterns, {len(self.skill_triggers)} skill triggers, {len(self.plugin_loader.plugins)} rule plugins")

    def get_phase_tracker(self, session_id: str, session_state: dict):
        """Get or restore PhaseTracker for a session."""
        if session_id not in self.phase_trackers:
            # Try restore from session state
            pt_data = session_state.get("phase_tracker")
            if pt_data:
                self.phase_trackers[session_id] = PhaseTracker.from_dict(pt_data)
            else:
                self.phase_trackers[session_id] = PhaseTracker()
        return self.phase_trackers[session_id]


# --- Session State (file-based) ---

def _state_path(session_id: str) -> str:
    safe = session_id.replace("/", "_").replace("\\", "_")[:32]
    return os.path.join(STATE_DIR, f"sidecar-{safe}.json")


def _lock_path(session_id: str) -> str:
    safe = session_id.replace("/", "_").replace("\\", "_")[:32]
    return os.path.join(STATE_DIR, f"sidecar-{safe}.lock")


def _load_session_state(session_id: str) -> dict:
    defaults = {
        "history": [],
        "last_analysis_ts": 0,
        "session_start_ts": 0,
        "files_touched": [],
        "total_calls": 0,
        "recap_done": False,
    }
    try:
        with open(_state_path(session_id), "r") as f:
            state = json.load(f)
        for key, val in defaults.items():
            if key not in state:
                state[key] = val
        return state
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(defaults)


def _save_session_state(session_id: str, state: dict) -> None:
    path = _state_path(session_id)
    try:
        fd, tmp = tempfile.mkstemp(dir=STATE_DIR, prefix=f"sidecar-{session_id[:8]}-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(state, f)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except Exception:
        pass


# --- Tool Call Helpers ---

def _summarize_tool_call(tool_name: str, tool_input: dict) -> str:
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return f"[Bash] $ {cmd[:80]}"
    elif tool_name in ("Read", "Glob", "Grep"):
        path = tool_input.get("file_path", "") or tool_input.get("path", "")
        pattern = tool_input.get("pattern", "")
        if path:
            return f"[{tool_name}] {path}"
        elif pattern:
            return f"[{tool_name}] pattern: {pattern[:60]}"
        return f"[{tool_name}]"
    elif tool_name in ("Edit", "Write"):
        path = tool_input.get("file_path", "")
        return f"[{tool_name}] {path}"
    elif tool_name == "Agent":
        desc = tool_input.get("description", "")
        return f"[Agent] {desc[:60]}"
    elif tool_name.startswith("Task"):
        subject = tool_input.get("subject", "")
        task_id = tool_input.get("taskId", "")
        return f"[{tool_name}] {subject or task_id}"
    else:
        return f"[{tool_name}]"


def _extract_file_path(tool_name: str, tool_input: dict) -> str:
    if tool_name in ("Edit", "Write", "Read"):
        return tool_input.get("file_path", "")
    if tool_name in ("Glob", "Grep"):
        return tool_input.get("path", "")
    return ""


def _extract_response_text(tool_response) -> str:
    if isinstance(tool_response, str):
        return tool_response
    if isinstance(tool_response, dict):
        parts = []
        for key in ("stdout", "stderr", "output", "content", "text", "result"):
            val = tool_response.get(key)
            if isinstance(val, str) and val.strip():
                parts.append(val)
        return "\n".join(parts)
    if isinstance(tool_response, list):
        return " ".join(str(x) for x in tool_response if x)
    return str(tool_response) if tool_response else ""


def _has_soft_error(text: str) -> bool:
    return any(pattern in text for pattern in SOFT_ERROR_PATTERNS)


# --- Detectors ---

def _detect_concerns(history: list) -> list:
    """7 Original-Detektoren aus minimax_watcher.py."""
    concerns = []
    if len(history) < 3:
        return concerns

    last6 = history[-6:]

    # 1. LOOP: gleicher Tool+Input 3x in letzten 6 Calls
    seen = {}
    for entry in last6:
        key = entry.get("summary", "")
        seen[key] = seen.get(key, 0) + 1
    for key, count in seen.items():
        if count >= 3:
            concerns.append(f"LOOP: '{key}' {count}x in letzten {len(last6)} Calls")

    # 2. READ-STORM: 6+ konsekutive Read/Glob/Grep ohne Aktion
    read_tools = {"Read", "Glob", "Grep"}
    consecutive_reads = 0
    for entry in reversed(history):
        if entry.get("tool") in read_tools:
            consecutive_reads += 1
        else:
            break
    if consecutive_reads >= 6:
        concerns.append(f"READ-STORM: {consecutive_reads} konsekutive Lese-Operationen ohne Aktion")

    # 3. ERROR-CASCADE: 2+ Fehler in letzten 5 Calls
    last5 = history[-5:]
    error_count = sum(1 for e in last5 if e.get("had_error"))
    if error_count >= 2:
        concerns.append(f"ERROR-CASCADE: {error_count} Fehler in letzten 5 Calls")

    # 4. DRIFT: Edit in Verzeichnis ohne vorherigen Read dort
    latest = history[-1]
    if latest.get("tool") == "Edit" and latest.get("file_path"):
        edit_dir = os.path.dirname(latest["file_path"])
        recent_read_dirs = set()
        for e in history[-9:-1]:
            if e.get("tool") in read_tools and e.get("file_path"):
                recent_read_dirs.add(os.path.dirname(e["file_path"]))
        if recent_read_dirs and edit_dir not in recent_read_dirs:
            concerns.append(f"DRIFT: Edit in '{edit_dir}' ohne vorherigen Read dort")

    # 5. YOLO: Edit ohne vorherigen Read derselben Datei
    if latest.get("tool") == "Edit" and latest.get("file_path"):
        edit_path = latest["file_path"]
        was_read = any(
            e.get("tool") in ("Read", "Grep") and e.get("file_path") == edit_path
            for e in history[:-1]
        )
        if not was_read:
            concerns.append(f"YOLO: Edit an '{os.path.basename(edit_path)}' ohne vorherigen Read")

    # 6. THRASH: Gleiche Datei 3x editiert in letzten 6 Calls
    file_edit_counts: dict = {}
    for e in last6:
        if e.get("tool") in ("Edit", "Write") and e.get("file_path"):
            fp = e["file_path"]
            file_edit_counts[fp] = file_edit_counts.get(fp, 0) + 1
    for fp, count in file_edit_counts.items():
        if count >= 3:
            concerns.append(f"THRASH: '{os.path.basename(fp)}' {count}x editiert in letzten 6 Calls")

    # 7. STALL: 10+ Calls ohne Edit/Write
    action_tools = {"Edit", "Write"}
    stall_count = 0
    for e in reversed(history):
        if e.get("tool") in action_tools:
            break
        stall_count += 1
    if stall_count >= 10:
        concerns.append(f"STALL: {stall_count} Calls ohne Edit/Write")

    return concerns


def _detect_anti_pattern(history: list, anti_patterns: list, tool_name: str, tool_input: dict) -> list:
    """Detektor 8: ANTI-PATTERN — String-Match gegen WelcheFehlerVermeiden.md Rules."""
    hits = []
    if not anti_patterns:
        return hits

    # Baue Context-String aus aktuellem Call + letzten Summaries
    context_parts = [tool_name]
    if isinstance(tool_input, dict):
        for v in tool_input.values():
            if isinstance(v, str):
                context_parts.append(v[:100])
    recent_summaries = " ".join(e.get("summary", "") for e in history[-5:])
    context = (tool_name + " " + recent_summaries + " " + " ".join(context_parts)).lower()

    for pattern in anti_patterns:
        keywords = pattern.get("keywords", [])
        matches = sum(1 for kw in keywords if kw.lower() in context)
        # Mindestens 2 Keywords muessen matchen um False-Positives zu reduzieren
        if matches >= 2:
            hits.append(
                f"ANTI-PATTERN [{pattern['name']}]: {pattern['advice']}"
            )

    return hits[:2]  # Max 2 anti-pattern warnings


def _detect_skill_suggest(history: list, skill_triggers: list) -> list:
    """Detektor 9: SKILL-SUGGEST — Prueft ob ein Skill vorgeschlagen werden sollte."""
    suggestions = []

    # Hardcoded Rules (zuverlaessiger als Trigger Map Parsing)
    for rule in SKILL_SUGGEST_RULES:
        try:
            if rule["trigger"](history):
                suggestions.append(
                    f"SKILL-SUGGEST: {rule['suggestion']} — {rule['reason']}"
                )
        except Exception:
            pass

    # Zusaetzlich: Skill Trigger Map aus Companion File
    if skill_triggers and len(history) >= 2:
        recent_text = " ".join(e.get("summary", "") for e in history[-3:]).lower()
        matched_skills = set()
        for trigger in skill_triggers:
            kws = trigger.get("trigger_keywords", [])
            if len(kws) < 2:
                continue
            hits = sum(1 for kw in kws if kw in recent_text)
            if hits >= 2:
                skill = trigger.get("skill_name", "").strip()
                if skill and skill not in matched_skills:
                    matched_skills.add(skill)
                    suggestions.append(
                        f"SKILL-SUGGEST: {skill} — {trigger.get('description', '')}"
                    )
                    if len(suggestions) >= 2:
                        break

    return suggestions[:2]  # Max 2 suggestions


# --- Request Handler ---

def _process_event(event: dict, daemon_state: SidecarState) -> dict:
    """Verarbeite ein Tool-Call Event, gib Response-Dict zurueck."""
    try:
        session_id = event.get("session_id", "")
        if not session_id:
            tp = event.get("transcript_path", "")
            if tp:
                session_id = os.path.basename(tp).replace(".jsonl", "")
        if not session_id:
            session_id = "unknown"

        tool_name = event.get("tool_name", "unknown")
        tool_input = event.get("tool_input", {})
        if isinstance(tool_input, str):
            try:
                tool_input = json.loads(tool_input)
            except (json.JSONDecodeError, TypeError):
                tool_input = {}
        hook_event = event.get("hook_event_name", "PostToolUse")

        # Self-detection
        tool_response = event.get("tool_response")
        response_text = _extract_response_text(tool_response) if tool_response else ""
        if any(marker in response_text for marker in SELF_MARKERS):
            return {}

        # Load state with file lock
        lock_file = None
        result = {}

        try:
            lock_file = open(_lock_path(session_id), "w")
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            _log(f"Lock held for session {session_id[:8]} → skip")
            return {}

        try:
            state = _load_session_state(session_id)
            now_ts = int(time.time())

            if state["session_start_ts"] == 0:
                state["session_start_ts"] = now_ts
            state["total_calls"] = state.get("total_calls", 0) + 1

            summary = _summarize_tool_call(tool_name, tool_input)
            had_error = bool(event.get("error", "")) or _has_soft_error(response_text)
            file_path = _extract_file_path(tool_name, tool_input)

            entry = {
                "tool": tool_name,
                "summary": summary,
                "ts": now_ts,
                "had_error": had_error,
                "file_path": file_path,
            }
            state["history"].append(entry)

            # Update phase tracker from tool usage
            tracker = daemon_state.get_phase_tracker(session_id, state)
            tracker.update_from_tool(tool_name, tool_input, had_error)
            state["phase_tracker"] = tracker.to_dict()

            if len(state["history"]) > MAX_HISTORY:
                state["history"] = state["history"][-MAX_HISTORY:]

            if file_path and file_path not in state["files_touched"]:
                state["files_touched"].append(file_path)
                if len(state["files_touched"]) > 50:
                    state["files_touched"] = state["files_touched"][-50:]

            # --- Gate checks for PreToolUse events ---
            if hook_event == "PreToolUse":
                for name, rule in daemon_state.registry.rules.items():
                    if rule["group"] != "gate":
                        continue
                    if not daemon_state.registry.is_enabled(name):
                        continue
                    try:
                        if rule["trigger"](state["history"], tool_name, tool_input, event):
                            daemon_state.registry.fire(name, rule["message"])
                            _log(f"GATE BLOCK: {name} — {rule['message']}")
                            _save_session_state(session_id, state)
                            result = {"decision": "block", "reason": f"[SIDECAR GATE] {name}: {rule['message']}"}
                            return result
                    except Exception as e:
                        _log(f"Gate rule {name} error: {e}")

            # --- OpenCode zombie tracking (Phase 4) ---
            if tool_name.startswith("mcp__opencode__opencode_fire"):
                daemon_state._pending_opencode_fires[session_id] = 0
            elif tool_name.startswith("mcp__opencode__opencode_check"):
                daemon_state._pending_opencode_fires.pop(session_id, None)
            elif session_id in daemon_state._pending_opencode_fires:
                daemon_state._pending_opencode_fires[session_id] = daemon_state._pending_opencode_fires[session_id] + 1
                calls_since_fire = daemon_state._pending_opencode_fires[session_id]
                if calls_since_fire >= 10:
                    _log(f"ZOMBIE WARNING session={session_id[:8]}: {calls_since_fire} calls since opencode_fire without check")
                    daemon_state.registry.fire("track_opencode_fire",
                        f"opencode_fire vor {calls_since_fire} Calls ohne opencode_check — Zombie-Prozess?")

            # --- Session-start recap enforcement ---
            if not state.get("recap_done", False) and state["total_calls"] <= 2:
                if daemon_state.registry.is_enabled("session_start_recap"):
                    _log(f"session={session_id[:8]}: new session without recap")

            # Run all detectors
            concerns = _detect_concerns(state["history"])
            ap_hits = _detect_anti_pattern(state["history"], daemon_state.anti_patterns, tool_name, tool_input)
            enforcement_hits = _detect_enforcement(state["history"], tool_name, tool_input)
            skill_hints = _detect_skill_suggest(state["history"], daemon_state.skill_triggers)

            all_findings = concerns + ap_hits + enforcement_hits + skill_hints
            concern_score = len(concerns) * 2 + len(ap_hits) + len(enforcement_hits) * 2 + len(skill_hints)

            # Cooldown check
            now = int(time.time())
            last_ts = state.get("last_analysis_ts", 0)
            cooldown_ok = (now - last_ts) >= CONCERN_COOLDOWN_SECONDS
            should_output = bool(all_findings) and cooldown_ok

            _log(f"session={session_id[:8]} tool={tool_name} findings={len(all_findings)} score={concern_score} output={should_output}")

            if should_output:
                lines = ["[SIDECAR] Pattern-Analyse:"]
                for f in all_findings:
                    lines.append(f"  - {f}")
                lines.append("Strategie ueberdenken.")

                additional_context = "\n".join(lines)
                system_message = "[PATTERN WATCHER] " + "; ".join(all_findings[:3])

                result = {
                    "additionalContext": additional_context,
                    "systemMessage": system_message,
                }
                state["last_analysis_ts"] = now

            _save_session_state(session_id, state)

        finally:
            if lock_file:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
                lock_file.close()

        return result

    except Exception as e:
        _log(f"Error in _process_event: {e}")
        return {}


def _process_prompt(event: dict, daemon_state: SidecarState) -> dict:
    """Process a UserPromptSubmit event — run rule plugins, return context."""
    try:
        session_id = event.get("session_id", "")
        if not session_id:
            tp = event.get("transcript_path", "")
            if tp:
                session_id = os.path.basename(tp).replace(".jsonl", "")
        if not session_id:
            session_id = "unknown"

        prompt = event.get("prompt", "")
        if not prompt:
            return {}

        # Load session state (read-only, no lock needed for prompt processing)
        state = _load_session_state(session_id)

        # Track recap_done: if prompt contains /recap or /learn, mark done
        prompt_lower = prompt.lower()
        if "/recap" in prompt_lower or "/learn" in prompt_lower:
            state["recap_done"] = True
            # Save the recap_done flag
            lock_file = None
            try:
                lock_file = open(_lock_path(session_id), "w")
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                _save_session_state(session_id, state)
            except (BlockingIOError, OSError):
                pass
            finally:
                if lock_file:
                    try:
                        fcntl.flock(lock_file, fcntl.LOCK_UN)
                        lock_file.close()
                    except Exception:
                        pass

        # Get/update phase tracker
        tracker = daemon_state.get_phase_tracker(session_id, state)
        phase = tracker.update_from_prompt(prompt)

        # Build context for plugin matching
        plugin_context = {
            "total_calls": state.get("total_calls", 0),
            "phase": phase,
            "files_touched": state.get("files_touched", []),
            "session_start_ts": state.get("session_start_ts", 0),
            "history": state.get("history", []),
        }

        # Run plugin matcher
        injection = daemon_state.plugin_loader.match_rules(prompt, plugin_context)

        # Session-start recap reminder
        recap_reminder = ""
        if (not state.get("recap_done", False)
                and state.get("total_calls", 0) <= 1
                and daemon_state.registry.is_enabled("session_start_recap")):
            recap_reminder = (
                "\n[SIDECAR] Neue Session erkannt — bitte zuerst /recap + /learn ausfuehren, "
                "bevor du weiterarbeitest. Session-Hygiene verhindert Kontext-Verlust."
            )

        if not injection and not recap_reminder:
            _log(f"session={session_id[:8]} prompt_len={len(prompt)} phase={phase} rules_fired=0")
            return {}

        # Add phase info as prefix
        context_parts = [f"[SIDECAR-CONTEXT] Phase: {phase}"]
        if injection:
            context_parts.append(injection)
        if recap_reminder:
            context_parts.append(recap_reminder)
        context = "\n".join(context_parts)

        _log(f"session={session_id[:8]} prompt_len={len(prompt)} phase={phase} rules_fired context_len={len(context)}")

        # Persist phase tracker state
        state["phase_tracker"] = tracker.to_dict()
        # Save with lock
        lock_file = None
        try:
            lock_file = open(_lock_path(session_id), "w")
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _save_session_state(session_id, state)
        except (BlockingIOError, OSError):
            pass  # Skip save if locked
        finally:
            if lock_file:
                try:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)
                    lock_file.close()
                except Exception:
                    pass

        return {
            "additionalContext": context,
        }

    except Exception as e:
        _log(f"Error in _process_prompt: {e}")
        return {}


def _handle_command(cmd_data: dict, daemon_state: SidecarState) -> dict:
    """Handle ACP commands from sidecar-ctl."""
    cmd = cmd_data.get("cmd", "")

    if cmd == "status":
        return {
            "status": "running",
            "pid": os.getpid(),
            "uptime_s": int(time.time() - daemon_state._start_ts),
            "rule_count": len(daemon_state.registry.rules),
            "total_findings": len(daemon_state.registry.findings),
            "companions_loaded": daemon_state.loaded,
            "patterns": len(daemon_state.anti_patterns),
            "skill_triggers": len(daemon_state.skill_triggers),
            "plugins": len(daemon_state.plugin_loader.plugins),
        }

    elif cmd == "rules":
        group = cmd_data.get("group", "")
        return {"rules": daemon_state.registry.all_rules_info(group)}

    elif cmd == "enable":
        name = cmd_data.get("name", "")
        if name not in daemon_state.registry.rules:
            return {"error": f"Unknown rule: {name}"}
        daemon_state.registry.set_override(name, enabled=True)
        return {"ok": True, "name": name, "enabled": True}

    elif cmd == "disable":
        name = cmd_data.get("name", "")
        if name not in daemon_state.registry.rules:
            return {"error": f"Unknown rule: {name}"}
        daemon_state.registry.set_override(name, enabled=False)
        return {"ok": True, "name": name, "enabled": False}

    elif cmd == "snooze":
        name = cmd_data.get("name", "")
        minutes = cmd_data.get("minutes", 30)
        if name not in daemon_state.registry.rules:
            return {"error": f"Unknown rule: {name}"}
        snooze_until = time.time() + (minutes * 60)
        daemon_state.registry.set_override(name, snooze_until=snooze_until)
        return {"ok": True, "name": name, "snoozed_minutes": minutes}

    elif cmd == "sessions":
        sessions = []
        for f in os.listdir(STATE_DIR):
            if f.startswith("sidecar-") and f.endswith(".json"):
                sid = f.replace("sidecar-", "").replace(".json", "")
                try:
                    state = _load_session_state(sid)
                    sessions.append({
                        "id": sid,
                        "total_calls": state.get("total_calls", 0),
                        "files_touched": len(state.get("files_touched", [])),
                        "history_len": len(state.get("history", [])),
                    })
                except Exception:
                    pass
        return {"sessions": sessions}

    elif cmd == "session":
        sid = cmd_data.get("id", "")
        if not sid:
            return {"error": "Missing session id"}
        state = _load_session_state(sid)
        return {
            "session_id": sid,
            "total_calls": state.get("total_calls", 0),
            "files_touched": state.get("files_touched", []),
            "history": state.get("history", [])[-10:],  # last 10 entries
            "recap_done": state.get("recap_done", False),
        }

    elif cmd == "findings":
        return {"findings": daemon_state.registry.findings}

    elif cmd == "reload":
        daemon_state.load_companion_files()
        daemon_state.registry._load_overrides()
        return {"ok": True, "reloaded": True}

    else:
        return {"error": f"Unknown command: {cmd}"}


# --- Async Socket Server ---

async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, daemon_state: SidecarState):
    try:
        data = await asyncio.wait_for(reader.read(65536), timeout=5.0)
        if not data:
            return

        try:
            event = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            _log(f"Invalid JSON from client: {e}")
            writer.write(b"{}")
            await writer.drain()
            return

        # Dispatch: command vs event
        if event.get("_type") == "cmd":
            response = _handle_command(event, daemon_state)
        else:
            # Route: prompt events → _process_prompt, tool events → _process_event
            event_type = event.get("event_type", "")
            if event_type == "prompt":
                response = _process_prompt(event, daemon_state)
            else:
                response = _process_event(event, daemon_state)

        writer.write(json.dumps(response).encode("utf-8"))
        await writer.drain()

    except asyncio.TimeoutError:
        _log("Client read timeout")
    except Exception as e:
        _log(f"Error handling client: {e}")
        try:
            writer.write(b"{}")
            await writer.drain()
        except Exception:
            pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def run_server(daemon_state: SidecarState):
    # Remove stale socket
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass

    server = await asyncio.start_unix_server(
        lambda r, w: handle_client(r, w, daemon_state),
        path=SOCKET_PATH,
    )

    _log(f"Sidecar daemon started. Socket: {SOCKET_PATH}")

    async with server:
        await server.serve_forever()


def main():
    daemon_state = SidecarState()
    daemon_state.load_companion_files()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _shutdown(sig, frame):
        _log(f"Received signal {sig}, shutting down...")
        loop.call_soon_threadsafe(loop.stop)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    def _reload(sig, frame):
        _log("Received SIGHUP, reloading companion files...")
        daemon_state.load_companion_files()

    signal.signal(signal.SIGHUP, _reload)

    try:
        loop.run_until_complete(run_server(daemon_state))
    except Exception as e:
        _log(f"Server error: {e}")
    finally:
        # Cleanup socket
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass
        loop.close()
        _log("Sidecar daemon stopped.")


if __name__ == "__main__":
    main()
