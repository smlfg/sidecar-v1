# Sidecar + Voice Bridge Setup

## Voraussetzungen

```bash
# Python 3.10+ (beides — Sidecar und Voice Bridge sind Python)
python3 --version

# wtype (Wayland text injection — primaerer Ansatz)
sudo apt install wtype

# ydotool (Fallback falls wtype auf COSMIC nicht geht)
sudo apt install ydotool

# Whisper STT (fuer Voice Bridge)
pip3 install openai-whisper sounddevice numpy

# watchdog (fuer Sidecar Daemon — optional, inotify ist besser)
# pip3 install watchdog
```

## Installation

### 1. Sidecar Daemon

```bash
# Service installieren
mkdir -p ~/.config/systemd/user/
cp sidecar.service ~/.config/systemd/user/claude-sidecar.service
systemctl --user daemon-reload
systemctl --user enable claude-sidecar.service
systemctl --user start claude-sidecar.service

# Status pruefen
systemctl --user status claude-sidecar.service
journalctl --user -u claude-sidecar.service -f
```

### 2. Voice Bridge

```bash
# Service installieren
cp voice-bridge.service ~/.config/systemd/user/voice-bridge.service
systemctl --user daemon-reload
systemctl --user enable voice-bridge.service
systemctl --user start voice-bridge.service

# Trigger-Pipe erstellen
mkfifo /tmp/voice-bridge-trigger

# Test: Aufnahme triggern
echo "start" > /tmp/voice-bridge-trigger
```

### 3. Hook Migration (settings.json)

In `~/.claude/settings.json` den minimax_watcher Hook durch sidecar_bridge ersetzen:

**Vorher:**
```json
{
  "matcher": "Bash|Edit|Write",
  "hooks": [{
    "type": "command",
    "command": "python3 /home/smlflg/Dokumente/Pläne/ClaudeCodeWorks/plugins/claude-hook/hooks/minimax_watcher.py",
    "timeout": 15
  }]
}
```

**Nachher:**
```json
{
  "matcher": "Bash|Edit|Write",
  "hooks": [{
    "type": "command",
    "command": "python3 /home/smlflg/Dokumente/Pläne/ClaudeCodeWorks/plugins/claude-hook/hooks/sidecar_bridge.py",
    "timeout": 5
  }]
}
```

### 4. COSMIC Shortcut fuer Voice (optional)

COSMIC Settings → Keyboard → Custom Shortcuts:
- Name: "Voice Bridge Toggle"
- Command: `bash -c 'echo start > /tmp/voice-bridge-trigger'`
- Shortcut: F5 oder Super+V

## Validation

```bash
# Sidecar laeuft?
ls -la /tmp/claude-sidecar.sock
tail -f /tmp/claude-sidecar.log

# Voice Bridge laeuft?
tail -f /tmp/voice-bridge.log

# wtype funktioniert?
wtype "hello world"

# Trigger testen
echo "start" > /tmp/voice-bridge-trigger
# → Sprechen → Text erscheint im Terminal
```

## 5. sidecar-ctl installieren

```bash
# Symlink in PATH erstellen
chmod +x /home/smlflg/Projekte/Sidecar/sidecar-ctl
ln -sf /home/smlflg/Projekte/Sidecar/sidecar-ctl ~/.local/bin/sidecar-ctl
```

### sidecar-ctl Subcommands

| Command | Beschreibung | Beispiel |
|---------|-------------|---------|
| `status` | Daemon-Status (PID, Uptime, Rule Count, Plugins) | `sidecar-ctl status` |
| `rules` | Alle Rules auflisten (Name, Group, Enabled, Severity, Fires) | `sidecar-ctl rules` |
| `rules --group <g>` | Rules nach Gruppe filtern | `sidecar-ctl rules --group gates` |
| `enable <name>` | Rule aktivieren | `sidecar-ctl enable cli_first` |
| `disable <name>` | Rule deaktivieren | `sidecar-ctl disable cli_first` |
| `snooze <name> [min]` | Rule fuer N Minuten pausieren (default: 30) | `sidecar-ctl snooze cli_first 60` |
| `sessions` | Aktive Sessions auflisten | `sidecar-ctl sessions` |
| `session <id>` | Details einer Session | `sidecar-ctl session abc123` |
| `findings` | Letzte 50 Findings anzeigen | `sidecar-ctl findings` |
| `reload` | Daemon-Config und Plugins neu laden | `sidecar-ctl reload` |
| `log [-n N]` | Letzte N Log-Zeilen anzeigen (default: 20) | `sidecar-ctl log -n 50` |

## Konfigurationsdateien und Pfade

| Datei / Pfad | Beschreibung |
|-------------|-------------|
| `/tmp/claude-sidecar.sock` | Unix Socket fuer Daemon-Kommunikation |
| `/tmp/claude-sidecar.log` | Daemon-Logfile |
| `/tmp/voice-bridge-trigger` | Named Pipe zum Triggern der Voice-Aufnahme |
| `/tmp/voice-bridge.log` | Voice Bridge Logfile |
| `~/.claude/sidecar-rules.json` | Overrides fuer Rules (enable/disable/snooze — persistent) |
| `~/.config/systemd/user/claude-sidecar.service` | systemd Service fuer Sidecar |
| `~/.config/systemd/user/voice-bridge.service` | systemd Service fuer Voice Bridge |

### Environment Variables (in Service-Dateien)

**Sidecar:**
- `SIDECAR_SOCKET` — Socket-Pfad (default: `/tmp/claude-sidecar.sock`)
- `SIDECAR_LOG` — Log-Pfad (default: `/tmp/claude-sidecar.log`)

**Voice Bridge:**
- `WHISPER_MODEL` — Whisper-Modell (default: `medium`)
- `WHISPER_LANGUAGE` — Sprache (default: `de`)
- `CHIME_ENABLED` — Audio-Feedback bei Start/Stop (default: `true`)
- `VOICE_BRIDGE_PIPE` — Trigger-Pipe-Pfad (default: `/tmp/voice-bridge-trigger`)
- `VOICE_BRIDGE_LOG` — Log-Pfad (default: `/tmp/voice-bridge.log`)

## Plugin-System (Rules)

Eigene Rules koennen als Python-Plugins in `rules/` erstellt werden:

```python
# rules/my_rule.py
from rule_plugin import RulePlugin

class MyRule(RulePlugin):
    name = "my_rule"
    group = "custom"
    severity = "info"

    def match(self, event: dict) -> bool:
        # Return True wenn Rule zutrifft
        return "some_pattern" in event.get("content", "")

    def inject(self, event: dict) -> str:
        # Text der als additionalContext injiziert wird
        return "Hinweis: ..."
```

Nach dem Erstellen: `sidecar-ctl reload` um das neue Plugin zu laden.

Vorhandene Plugins: `research_first.py`, `delegation_check.py`, `git_safety.py`

## Troubleshooting

### Sidecar startet nicht

```bash
# Service-Status und Fehlerlog pruefen
systemctl --user status claude-sidecar.service
journalctl --user -u claude-sidecar.service --no-pager -n 30

# Socket bereits belegt? (alter Prozess)
ls -la /tmp/claude-sidecar.sock
# Falls ja: alten Socket loeschen und Service neu starten
rm /tmp/claude-sidecar.sock
systemctl --user restart claude-sidecar.service
```

### sidecar-ctl: "daemon is not running"

Sidecar-Daemon laeuft nicht oder Socket existiert nicht.

```bash
systemctl --user start claude-sidecar.service
ls -la /tmp/claude-sidecar.sock
```

### Voice Bridge nimmt nicht auf

```bash
# Named Pipe existiert?
ls -la /tmp/voice-bridge-trigger
# Falls nicht:
mkfifo /tmp/voice-bridge-trigger

# Mikrofon erkannt?
python3 -c "import sounddevice; print(sounddevice.query_devices())"

# Service laeuft?
systemctl --user status voice-bridge.service
```

### wtype funktioniert nicht (kein Text im Terminal)

- Nur auf Wayland — nicht unter X11
- COSMIC muss laufen (kein SSH, kein TTY)
- Manche Apps blockieren wtype — ydotool als Fallback testen:
  ```bash
  ydotool type "hello world"
  ```

### Rules werden nicht geladen

```bash
# Reload erzwingen
sidecar-ctl reload

# Log auf Import-Fehler pruefen
sidecar-ctl log -n 50 | grep -i error
```

## Rollback

```bash
# Sidecar zurueck zu minimax_watcher
# In settings.json: sidecar_bridge.py → minimax_watcher.py
systemctl --user stop claude-sidecar.service
systemctl --user disable claude-sidecar.service

# Voice Bridge stoppen
systemctl --user stop voice-bridge.service
systemctl --user disable voice-bridge.service
```
