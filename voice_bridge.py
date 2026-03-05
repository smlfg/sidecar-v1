#!/usr/bin/env python3
"""Voice Bridge Daemon — STT -> wtype input for Claude Code.

Triggered via named pipe: /tmp/voice-bridge-trigger
  echo "start" > /tmp/voice-bridge-trigger  # begin recording
  echo "stop"  > /tmp/voice-bridge-trigger  # end recording, run STT, type output

Audio: arecord (ALSA, no extra deps)
STT:   HTTP API to whisper.cpp server (localhost:2022/v1/audio/transcriptions)
Output: wtype (falls back to ydotool, then file dump)

IRON RULE: Never crash.
"""

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------
WHISPER_API_URL = os.environ.get("WHISPER_API_URL", "http://127.0.0.1:2022/v1/audio/transcriptions")
WHISPER_LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "de")
PIPE_PATH = os.environ.get("VOICE_BRIDGE_PIPE", "/tmp/voice-bridge-trigger")
LOG_PATH = os.environ.get("VOICE_BRIDGE_LOG", "/tmp/voice-bridge.log")
CHIME_ENABLED = os.environ.get("CHIME_ENABLED", "true").lower() == "true"
SAMPLE_RATE = 16000
CHANNELS = 1

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("voice-bridge")

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
_shutdown = threading.Event()
_recording = threading.Event()


# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------
def _check_wtype() -> str | None:
    """Return the typing command that actually works at runtime, or None.

    Tests wtype with an empty string to catch Wayland protocol mismatches
    (e.g. text-input-v3 vs zwp_virtual_keyboard_v1) that only surface at
    runtime even when the binary is present.
    """
    if shutil.which("wtype"):
        try:
            result = subprocess.run(
                ["wtype", "--", ""],
                capture_output=True,
                timeout=3,
            )
            if result.returncode == 0:
                log.info("Typer selected: wtype (runtime test passed)")
                return "wtype"
            else:
                log.warning(
                    "wtype binary found but runtime test failed (rc=%d, stderr=%r) — trying ydotool",
                    result.returncode,
                    result.stderr.decode("utf-8", errors="replace").strip()[:200],
                )
        except Exception as exc:
            log.warning("wtype runtime test raised exception: %s — trying ydotool", exc)

    if shutil.which("ydotool"):
        log.info("Typer selected: ydotool")
        return "ydotool"

    log.warning("No typer available (wtype/ydotool). Text will be written to /tmp/voice-bridge-output.txt")
    return None


def _type_text(text: str, typer: str | None) -> None:
    text = text.strip()
    if not text:
        return
    try:
        if typer == "wtype":
            subprocess.run(["wtype", "--", text], timeout=10, check=False)
        elif typer == "ydotool":
            subprocess.run(["ydotool", "type", "--", text], timeout=10, check=False)
        else:
            # Last resort: dump to file
            dump = Path("/tmp/voice-bridge-output.txt")
            dump.write_text(text + "\n", encoding="utf-8")
            log.warning("No typer available. Text written to %s", dump)
            return
        log.info("Typed: %r", text[:80])
    except Exception as exc:
        log.error("_type_text failed: %s", exc)


def _chime(kind: str) -> None:
    """Play a short beep via paplay/aplay if available."""
    if not CHIME_ENABLED:
        return
    try:
        if kind == "start":
            subprocess.Popen(
                ["paplay", "/usr/share/sounds/freedesktop/stereo/message.oga"],
                stderr=subprocess.DEVNULL,
            )
        elif kind == "stop":
            subprocess.Popen(
                ["paplay", "/usr/share/sounds/freedesktop/stereo/complete.oga"],
                stderr=subprocess.DEVNULL,
            )
    except Exception:
        pass  # chimes are optional


# ---------------------------------------------------------------------------
# Audio recording (arecord only — no extra deps)
# ---------------------------------------------------------------------------
def _record(wav_path: str) -> bool:
    """Record via arecord until _recording is cleared. Returns True on success."""
    try:
        proc = subprocess.Popen(
            [
                "arecord",
                "-f", "S16_LE",
                "-r", str(SAMPLE_RATE),
                "-c", str(CHANNELS),
                wav_path,
            ],
            stderr=subprocess.DEVNULL,
        )
        log.info("Recording started (arecord, pid=%d)", proc.pid)
        _chime("start")

        while _recording.is_set() and not _shutdown.is_set():
            time.sleep(0.05)

        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

        log.info("arecord stopped")
        return Path(wav_path).stat().st_size > 44  # more than just WAV header
    except FileNotFoundError:
        log.error("arecord not found")
        return False
    except Exception as exc:
        log.error("arecord recording failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Whisper STT via HTTP API (whisper.cpp server on port 2022)
# ---------------------------------------------------------------------------
def _transcribe(wav_path: str) -> str:
    """Send wav to Whisper HTTP server and return transcribed text."""
    try:
        log.info("Transcribing %s via %s ...", wav_path, WHISPER_API_URL)
        boundary = "----VoiceBridge" + str(int(time.time()))
        body = bytearray()

        # multipart/form-data: file field
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="file"; filename="{Path(wav_path).name}"\r\n'.encode()
        body += b"Content-Type: audio/wav\r\n\r\n"
        body += Path(wav_path).read_bytes()
        body += b"\r\n"

        # language field
        body += f"--{boundary}\r\n".encode()
        body += b'Content-Disposition: form-data; name="language"\r\n\r\n'
        body += WHISPER_LANGUAGE.encode() + b"\r\n"

        # response_format field
        body += f"--{boundary}\r\n".encode()
        body += b'Content-Disposition: form-data; name="response_format"\r\n\r\n'
        body += b"json\r\n"

        body += f"--{boundary}--\r\n".encode()

        req = urllib.request.Request(
            WHISPER_API_URL,
            data=bytes(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        text = result.get("text", "").strip()
        log.info("Transcription: %r", text[:120])
        return text
    except urllib.error.URLError as exc:
        log.error("Whisper server unreachable (%s): %s", WHISPER_API_URL, exc)
        return ""
    except Exception as exc:
        log.error("Whisper transcription failed: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Main recording session
# ---------------------------------------------------------------------------
def _run_session(typer: str | None) -> None:
    """One full record → transcribe → type cycle."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = tmp.name

    try:
        success = _record(wav_path)
        _chime("stop")

        if not success:
            log.warning("Recording produced no audio")
            return

        text = _transcribe(wav_path)
        if text:
            _type_text(text, typer)
    finally:
        try:
            os.unlink(wav_path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Named pipe listener
# ---------------------------------------------------------------------------
def _ensure_pipe() -> None:
    if os.path.exists(PIPE_PATH):
        if not os.stat(PIPE_PATH).st_mode & 0o010000:
            os.unlink(PIPE_PATH)
    if not os.path.exists(PIPE_PATH):
        os.mkfifo(PIPE_PATH, mode=0o600)
    log.info("Named pipe ready: %s", PIPE_PATH)


def _pipe_listener(typer: str | None) -> None:
    """Read commands from the named pipe in a loop."""
    session_thread: threading.Thread | None = None

    while not _shutdown.is_set():
        try:
            fd = os.open(PIPE_PATH, os.O_RDONLY)
            try:
                data = os.read(fd, 1024).decode("utf-8", errors="replace").strip()
            finally:
                os.close(fd)

            if not data:
                continue

            for cmd in data.splitlines():
                cmd = cmd.strip().lower()
                if cmd == "start":
                    if not _recording.is_set():
                        _recording.set()
                        log.info("Command: start recording")
                        session_thread = threading.Thread(
                            target=_run_session,
                            args=(typer,),
                            daemon=True,
                        )
                        session_thread.start()
                    else:
                        log.info("Already recording — ignoring start")

                elif cmd == "stop":
                    if _recording.is_set():
                        log.info("Command: stop recording")
                        _recording.clear()
                    else:
                        log.info("Not recording — ignoring stop")

                elif cmd == "toggle":
                    if _recording.is_set():
                        log.info("Command: toggle → stop")
                        _recording.clear()
                    else:
                        log.info("Command: toggle → start")
                        _recording.set()
                        session_thread = threading.Thread(
                            target=_run_session,
                            args=(typer,),
                            daemon=True,
                        )
                        session_thread.start()

                elif cmd == "quit" or cmd == "shutdown":
                    log.info("Shutdown command received via pipe")
                    _recording.clear()
                    _shutdown.set()
                    if session_thread and session_thread.is_alive():
                        session_thread.join(timeout=5)

                else:
                    log.warning("Unknown command: %r", cmd)

        except Exception as exc:
            log.error("Pipe listener error: %s", exc)
            time.sleep(0.5)


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------
def _handle_signal(signum, frame):
    log.info("Signal %d received — shutting down", signum)
    _recording.clear()
    _shutdown.set()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    log.info("=== Voice Bridge starting ===")
    log.info("API=%s  Language=%s  Pipe=%s", WHISPER_API_URL, WHISPER_LANGUAGE, PIPE_PATH)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    typer = _check_wtype()
    if typer:
        log.info("Typer: %s", typer)
    else:
        log.warning("No typer found (wtype/ydotool). Text will be written to /tmp/voice-bridge-output.txt")

    try:
        _ensure_pipe()
    except Exception as exc:
        log.error("Could not create named pipe: %s", exc)
        sys.exit(1)

    log.info("Ready. Send commands to %s: start | stop | toggle | quit", PIPE_PATH)

    _pipe_listener(typer)

    log.info("=== Voice Bridge stopped ===")


if __name__ == "__main__":
    main()
