# Sidecar v1

A persistent context daemon for Claude Code CLI. Monitors your session in real-time and injects relevant rules, patterns, and skill suggestions only when they matter — keeping your context window lean.

## Problem

Claude Code accumulates rules, skill references, anti-patterns, and coaching notes in CLAUDE.md. At scale this bloats every session with ~900 lines of context that is mostly irrelevant to the current task. Sidecar externalizes that knowledge and injects it situationally — reducing CLAUDE.md from 145 to 74 lines (-45%) while increasing rule coverage.

## Solution

Sidecar runs as a daemon and hooks into Claude Code at two points:

- **UserPromptSubmit** — injects relevant context before Claude sees your prompt
- **PostToolUse** — detects anti-patterns and bad sequences after each tool call

Rules are loaded as plugins from `rules/`. The daemon tracks your session phase (exploration / implementation / testing / debugging) and only injects what matches.

## Architecture

```
Claude Code
    |
    |-- UserPromptSubmit hook
    |       userpromptsubmit_bridge.py
    |           |
    |           v
    |       Unix Socket (/tmp/claude-sidecar.sock)
    |           |
    |           v
    |       sidecar.py (asyncio daemon)
    |           |-- RuleRegistry (22 rules)
    |           |-- PluginLoader (rules/*.py, hot-reload)
    |           |-- PhaseTracker (session phase detection)
    |           |-- SessionState (file-based JSON, /tmp/)
    |
    |-- PostToolUse hook
            posttooluse_bridge.py
                |
                v
            Unix Socket (/tmp/claude-sidecar.sock)
                |
                v
            9 Detectors + 5 Enforcement detectors
```

### Fail-open guarantee

Sidecar must never crash Claude. All exceptions are caught. Worst case: bridge scripts return `{}` (empty context injection). Claude continues unaffected.

## Features

### Detectors (PostToolUse)

| Detector | What it catches |
|---|---|
| LOOP | Same tool called repeatedly with identical args |
| READ-STORM | Excessive file reads without writes |
| ERROR-CASCADE | Repeated errors on the same file |
| DRIFT | Task scope expanding beyond original intent |
| YOLO | Writes without prior reads on the same file |
| THRASH | Alternating edits and reverts |
| STALL | Long sequences without progress |
| ANTI-PATTERN | Known bad patterns (e.g. grep instead of Grep tool) |
| SKILL-SUGGEST | Recommends a cheaper/faster delegation path |

### Enforcement detectors (PostToolUse)

- `no_cli_first` — LLM tool used when a CLI tool would suffice
- `autonomous_git` — git commit/push without user confirmation
- `mcp_timeout_drift` — MCP calls that should have been CLI calls
- `reread_after_diff` — file re-read immediately after an edit
- `gemini_not_first` — web research done without consulting Gemini first

### Gates (PreToolUse, blocking)

| Gate | Blocks |
|---|---|
| `gate_rm_rf` | `rm -rf` without explicit confirmation |
| `gate_autonomous_git` | git operations outside approved scope |
| `gate_secret_exposure` | writes that may contain credentials |
| `gate_force_push` | `git push --force` to main/master |
| `gate_opencode_vague` | vague OpenCode delegations |
| `gate_wrong_provider` | using expensive model where cheaper suffices |

### Plugin system

Rules in `rules/` are loaded at startup and hot-reloaded on SIGHUP. Each plugin is a `RulePlugin` subclass:

```python
from rule_plugin import RulePlugin

class MyRule(RulePlugin):
    name = "my_rule"
    description = "Short description shown in sidecar-ctl rules"

    def match(self, prompt: str, session_state: dict) -> bool:
        return "keyword" in prompt.lower()

    def inject(self) -> str:
        return "[SIDECAR] Contextual guidance injected here."
```

Three plugins ship with v1: `research_first`, `delegation_check`, `git_safety`.

### Session phase tracking

`PhaseTracker` infers phase from tool usage patterns and bilingual (DE/EN) keywords:

- **exploration** — lots of reads, greps, globs; no writes yet
- **implementation** — edits and writes dominate
- **testing** — bash calls with test runners, assertion keywords
- **debugging** — error keywords, repeated reads of the same files

## Components

| File | Purpose |
|---|---|
| `sidecar.py` | Core daemon, asyncio Unix Socket server, RuleRegistry, session state |
| `phase_tracker.py` | Session phase detection (143 LOC) |
| `plugin_loader.py` | importlib-based plugin loader, SIGHUP hot-reload (95 LOC) |
| `rule_plugin.py` | RulePlugin ABC base class (28 LOC) |
| `voice_bridge.py` | STT daemon using Whisper, named pipe trigger, wtype/ydotool output (366 LOC) |
| `sidecar-ctl` | CLI management tool (305 LOC) |
| `rules/` | Plugin directory (3 plugins) |
| `services/` | systemd user service files |

## Setup

### 1. Install as systemd user service

```bash
cp services/sidecar.service ~/.config/systemd/user/claude-sidecar.service
systemctl --user daemon-reload
systemctl --user enable --now claude-sidecar.service
```

### 2. Register Claude Code hooks

Add to your `~/.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [{"type": "command", "command": "python3 /path/to/userpromptsubmit_bridge.py", "timeout": 5}]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "",
        "hooks": [{"type": "command", "command": "python3 /path/to/posttooluse_bridge.py", "timeout": 5}]
      }
    ]
  }
}
```

### 3. Verify

```bash
sidecar-ctl status
tail -f /tmp/claude-sidecar.log
```

## CLI usage

```
sidecar-ctl status              Show daemon status and active session count
sidecar-ctl rules               List all 22 rules with enabled/disabled state
sidecar-ctl enable <rule>       Enable a rule by name
sidecar-ctl disable <rule>      Disable a rule by name
sidecar-ctl snooze <rule> <n>   Snooze a rule for n minutes
sidecar-ctl sessions            List active sessions
sidecar-ctl session <id>        Show full state for a session
sidecar-ctl findings            Show recent detector findings
sidecar-ctl reload              Hot-reload plugins (same as SIGHUP)
sidecar-ctl log                 Tail the daemon log
```

Rule state is persisted to `~/.claude/sidecar-rules.json`.

## Design decisions

**Unix socket over HTTP** — ~1ms latency, no port conflicts, no firewall rules. Hooks have a tight timeout budget.

**File-based session state** — Session JSON in `/tmp/sidecar-<SESSION_ID>.json` survives daemon restarts. `fcntl` locking handles parallel hook calls safely.

**Self-detection** — `SELF_MARKERS` in bridge scripts skip injection when Claude is responding to Sidecar's own output, preventing infinite loops.

**No external dependencies for core** — stdlib only for `sidecar.py`, `phase_tracker.py`, `plugin_loader.py`, and all gates.

## Requirements

- Python 3.10+
- Claude Code CLI with hooks support
- systemd (optional, recommended for auto-start)

Voice bridge only: `arecord`, Whisper HTTP API, `wtype` or `ydotool`.

## License

MIT
