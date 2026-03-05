# Sidecar — FOR_SMLFLG.md

Samuels Referenz-Dokument fuer das Sidecar-Projekt. Stand: 04. Maerz 2026.

---

## Was ist Sidecar?

Ein persistenter Daemon, der Claude Code in Echtzeit ueberwacht und situativ Kontext injiziert. Zwei Hauptfunktionen:

1. **Pattern Detection** — Erkennt Anti-Patterns, Loops, Stalls, Error-Cascades etc. waehrend Claude arbeitet
2. **Context Injection** — Schiebt situativ passende Regeln, Warnungen und Skill-Vorschlaege in Claude's Kontext (via `additionalContext`)

Dazu: **Voice Bridge** — STT-Daemon der Sprache via Whisper in Text umwandelt und per wtype ins Terminal tippt.

---

## Architektur-Ueberblick

```
                    Claude Code Hooks
                         |
              +----------+----------+
              |                     |
         PostToolUse          UserPromptSubmit
         (sidecar_bridge.py)  (userpromptsubmit_bridge.py)
              |                     |
              +------> Unix Socket <------+
                    /tmp/claude-sidecar.sock
                           |
                    +------+------+
                    |  sidecar.py |  (asyncio Server, 1255 LOC)
                    +------+------+
                           |
          +--------+-------+-------+--------+
          |        |       |       |        |
     RuleRegistry  |  PluginLoader |  PhaseTracker
     (22 Rules)    |  (3 Plugins)  |  (4 Phasen)
          |        |       |       |        |
     6 Gates    9 Detektoren  rules/*.py  exploration
     (PreToolUse    (PostToolUse)          implementation
      blockiert)                           testing
                                           debugging
```

Parallel dazu:

```
     Named Pipe /tmp/voice-bridge-trigger
              |
       voice_bridge.py  (366 LOC)
              |
     arecord → Whisper HTTP API → wtype/ydotool → Terminal
```

---

## Dateien und was sie tun

| Datei | LOC | Funktion |
|-------|-----|----------|
| `sidecar.py` | 1255 | Kern-Daemon. Asyncio Unix Socket Server, 9 Detektoren, RuleRegistry (22 Rules), Session State (file-based JSON), Gate-Blocks, Companion File Parsing, ACP Command Handler |
| `phase_tracker.py` | 143 | Trackt Session-Phase (exploration/implementation/testing/debugging) basierend auf Tool-Patterns und Prompt-Keywords. Bilingual (DE+EN) |
| `plugin_loader.py` | 95 | Laedt RulePlugin-Subclasses aus `rules/` via importlib. Hot-Reload via SIGHUP |
| `rule_plugin.py` | 28 | ABC Base Class fuer Rule Plugins. `match()` + `inject()` Interface |
| `voice_bridge.py` | 366 | STT Daemon. Named Pipe Listener, arecord Audio-Aufnahme, Whisper HTTP API, wtype/ydotool Output. Chime-Support |
| `sidecar-ctl` | 305 | CLI Tool (ACP). 10 Subcommands: status, rules, enable, disable, snooze, sessions, session, findings, reload, log |
| `rules/research_first.py` | 40 | Plugin: Warnt wenn Implementation ohne Research gestartet wird |
| `rules/delegation_check.py` | 37 | Plugin: Warnt wenn direkt gecoded wird statt zu delegieren |
| `rules/git_safety.py` | 37 | Plugin: Injiziert Safety-Reminder bei gefaehrlichen Git-Ops |
| `services/sidecar.service` | 15 | systemd User Service fuer Sidecar Daemon |
| `services/voice-bridge.service` | 17 | systemd User Service fuer Voice Bridge |
| `SETUP.md` | 117 | Installations- und Rollback-Anleitung |
| `.gitignore` | 16 | Standard Python + Runtime Excludes |
| **Gesamt** | **~2307** | |

---

## Die 9 Detektoren (PostToolUse)

| # | Name | Was es erkennt | Schwelle |
|---|------|---------------|----------|
| 1 | LOOP | Gleicher Tool+Input wiederholt sich | 3x in letzten 6 Calls |
| 2 | READ-STORM | Nur Lesen ohne Aktion | 6+ konsekutive Read/Glob/Grep |
| 3 | ERROR-CASCADE | Fehler-Haeufung | 2+ Fehler in letzten 5 Calls |
| 4 | DRIFT | Edit in Verzeichnis ohne vorherigen Read dort | Letzte 8 Reads pruefen |
| 5 | YOLO | Edit ohne vorherigen Read der Datei | Session-History |
| 6 | THRASH | Gleiche Datei staendig editiert | 3x in letzten 6 Calls |
| 7 | STALL | Kein Edit/Write seit langem | 10+ Calls ohne Schreibop |
| 8 | ANTI-PATTERN | Match gegen WelcheFehlerVermeiden.md | 2+ Keyword-Treffer |
| 9 | SKILL-SUGGEST | Passenden Skill vorschlagen | Regelbasiert (5 hardcoded + Trigger Map) |

Dazu 5 **Enforcement-Detektoren** (Regeln aus CLAUDE.md):
- `no_cli_first` — MCP vor Shell genutzt
- `autonomous_git` — Git ohne vorherigen Status-Check
- `mcp_timeout_drift` — MCP-Call > 120s
- `reread_after_diff` — Read nach git diff (redundant)
- `gemini_not_first` — WebSearch vor Gemini

---

## Die 6 Gates (PreToolUse, blockierend)

Gates koennen Tool-Calls **blockieren** (exit 2). Severity: `block`.

| Gate | Was es blockiert |
|------|-----------------|
| `gate_rm_rf` | `rm -rf` ohne `# CONFIRMED` Kommentar |
| `gate_autonomous_git` | git commit/push/merge ohne vorherigen git status/diff/log |
| `gate_secret_exposure` | API Keys/Secrets in settings.json/.env/config Dateien |
| `gate_force_push` | `git push --force` / `git push -f` |
| `gate_opencode_vague` | OpenCode-Prompts mit "try/maybe/might/probably" |
| `gate_wrong_provider` | `providerID: "anthropic"` in OpenCode (kein API-Key konfiguriert) |

---

## Plugin-System

Neue Regeln als Python-Dateien in `rules/` ablegen:

```python
from rule_plugin import RulePlugin

class MeineRegel(RulePlugin):
    name = "meine_regel"
    description = "Was die Regel tut"

    def match(self, prompt: str, session_state: dict) -> bool:
        # session_state hat: total_calls, phase, files_touched, history
        return "keyword" in prompt.lower()

    def inject(self) -> str:
        return "[RULE: meine_regel] Kontext-Text der injiziert wird"
```

Hot-Reload: `sidecar-ctl reload` oder `kill -HUP <pid>`.

---

## sidecar-ctl (ACP CLI)

```bash
sidecar-ctl status          # Daemon-Status, Uptime, Rule/Finding Count
sidecar-ctl rules           # Alle 22 Rules mit Fires + Last-Fired
sidecar-ctl rules --group gate  # Nur Gates anzeigen
sidecar-ctl enable <name>   # Rule aktivieren
sidecar-ctl disable <name>  # Rule deaktivieren
sidecar-ctl snooze <name> 30  # Rule 30min stumm schalten
sidecar-ctl sessions        # Alle Sessions mit Call-Count
sidecar-ctl session <id>    # Session-Details (History, Files)
sidecar-ctl findings        # Letzte 50 Findings
sidecar-ctl reload          # Companion Files + Plugins neu laden
sidecar-ctl log -n 50       # Letzte 50 Log-Zeilen
```

Kommunikation: Unix Socket (`/tmp/claude-sidecar.sock`), JSON Protocol.

---

## Voice Bridge

Separater Daemon fuer Speech-to-Text:

- **Trigger:** Named Pipe `/tmp/voice-bridge-trigger` (start/stop/toggle/quit)
- **Aufnahme:** `arecord` (ALSA, keine Extra-Deps)
- **STT:** HTTP POST an Whisper.cpp Server (`localhost:2022`)
- **Output:** wtype (Wayland) → ydotool (Fallback) → Datei (Last Resort)
- **Chime:** paplay Sounds bei Start/Stop (deaktivierbar via `CHIME_ENABLED`)
- **IRON RULE:** Never crash (alle Exceptions gefangen)

Konfiguration via Umgebungsvariablen in `voice-bridge.service`.

---

## Technologie-Entscheidungen

| Entscheidung | Warum |
|-------------|-------|
| **Unix Socket** statt HTTP | Schneller (~1ms), kein Port-Konflikt, natuerliche Session-Isolation |
| **asyncio** statt threading | Nicht-blockierender I/O, sauberer Shutdown via signal |
| **File-based Session State** (JSON in /tmp) | Einfach, kein DB noetig, Daemon-Restart ueberlebt Sessions |
| **fcntl File Locking** | Race Conditions vermeiden bei parallelen Hook-Calls |
| **Plugin-System via importlib** | Hot-Reload ohne Daemon-Neustart, einfach erweiterbar |
| **Companion File Parsing** | Regeln aus bestehenden Markdown-Dateien extrahieren statt duplizieren |
| **Named Pipe** fuer Voice Bridge | Einfachster IPC — jedes Tool kann `echo start > /tmp/voice-bridge-trigger` |
| **wtype** statt xdotool | Wayland-native (Pop!_OS + COSMIC). Fallback-Kette: wtype → ydotool → Datei |
| **arecord** statt sounddevice | Keine Python-Deps, ALSA-basiert, ueberall verfuegbar |
| **Whisper HTTP API** statt Python-Import | Entkoppelt: Whisper-Server kann unabhaengig laufen/upgraden |

---

## Session State Schema

Pro Session eine JSON-Datei in `/tmp/sidecar-<id>.json`:

```json
{
  "history": [{"tool": "Read", "summary": "[Read] /path", "ts": 1709..., "had_error": false, "file_path": "/path"}],
  "last_analysis_ts": 1709...,
  "session_start_ts": 1709...,
  "files_touched": ["/path/to/file.py"],
  "total_calls": 42,
  "recap_done": false,
  "phase_tracker": {"current_phase": "implementation", "tool_window": [...], "phase_history": [...]}
}
```

Max 25 History-Eintraege, max 50 Files. Cooldown zwischen Analysen: 15 Sekunden.

---

## Lessons Learned

1. **Iron Rule: fail-open.** Sidecar darf Claude NIEMALS zum Absturz bringen. Jede Exception wird gefangen, schlimmstenfalls gibt der Daemon `{}` zurueck und Claude arbeitet weiter.

2. **Self-Detection ist Pflicht.** Ohne `SELF_MARKERS`-Check wuerden die eigenen Warnungen (`[SIDECAR]`, `[PATTERN WATCHER]`) als neue Events erkannt → Endlos-Loop.

3. **Sync-Hooks muessen schnell sein.** PostToolUse und UserPromptSubmit Hooks blockieren Claude. Timeout: 5s. Unix Socket + asyncio halten die Latenz bei ~1-3ms.

4. **Cooldown verhindert Spam.** Ohne `CONCERN_COOLDOWN_SECONDS` (15s) wuerde jeder Call eine Warnung produzieren. Claude bekommt sonst mehr Meta-Kontext als Nutz-Kontext.

5. **File Locking ist noetig.** Mehrere Hooks koennen parallel feuern (PostToolUse + PreToolUse). Ohne fcntl-Lock korrupte JSON-Dateien.

6. **Plugin-System spart CLAUDE.md Zeilen.** Statt 19 Anti-Patterns in CLAUDE.md (400+ Token Overhead pro Session) injiziert Sidecar nur die 1-2 relevanten. CLAUDE.md ging von 145 → 74 Zeilen (-45%).

7. **Phase Tracking ist bilingual.** Deutsche UND englische Keywords noetig, weil Samuel Deutsch schreibt aber Code-Prompts oft Englisch sind.

8. **Voice Bridge: wtype hat Runtime-Bugs.** Binary vorhanden heisst nicht funktional — COSMIC's Wayland-Protokoll kann inkompatibel sein. Deshalb Runtime-Test bei Start (`wtype -- ""`).

---

## Aktueller Status

- **Stabil**, laeuft als systemd User Service
- **7 Commits** auf main (chore → feat → docs)
- **2307 LOC** total (davon 1255 im Kern-Daemon)
- **22 registrierte Rules** (7 Pattern + 1 Anti-Pattern + 1 Skill-Suggest + 5 Enforcement + 2 Session-Tracking + 6 Gates)
- **3 Rule Plugins** (research_first, delegation_check, git_safety)
- **4 Session-Phasen** werden getrackt
- Rule Overrides persistent in `~/.claude/sidecar-rules.json`

---

## Bekannte Bugs / Offene Punkte

- **#14281**: `additionalContext` wird manchmal mehrfach injiziert (Claude Code Bug)
- **#15345**: PreToolUse hat KEIN `additionalContext`-Feld — Gates koennen nur blockieren, nicht Kontext injizieren
- Service-Pfade in `.service` Dateien zeigen noch auf alten Pfad (`/home/smlflg/Dokumente/Plaene/ClaudeCodeWorks/...`) — muessten auf `/home/smlflg/Projekte/Sidecar/` aktualisiert werden

---

## Quick Reference

```bash
# Status pruefen
sidecar-ctl status

# Log live mitlesen
tail -f /tmp/claude-sidecar.log

# Regel temporaer deaktivieren
sidecar-ctl snooze gate_rm_rf 60

# Plugins neu laden nach Aenderung
sidecar-ctl reload

# Daemon neustarten
systemctl --user restart claude-sidecar.service

# Voice Bridge testen
echo "toggle" > /tmp/voice-bridge-trigger
```
