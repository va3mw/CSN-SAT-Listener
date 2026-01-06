# -*- coding: utf-8 -*-
"""
sat_udp_popup.py

Version: 2.2.2

Changes in v2.2.2:
1) Adds a visual marker in the console log to show when a packet is actually
   *passed to the alert path* (i.e., it met all conditions and we attempted to
   produce a popup/voice).
   - We print an asterisk "*" at the end of the log line when the alert path is entered.
   - Additionally, we log explicit "[ALERT]" lines when we attempt popup and/or voice.

2) Adds a runtime switch to disable the popup while keeping audio:
   - --no-popup   disables the popup (voice still happens if enabled)
   - --popup      forces popup on (default)

Notes:
- This is a console program; switches are parsed from sys.argv.
- Popup auto-closes after POPUP_TIMEOUT_SECONDS seconds.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import threading
from typing import Dict, Optional, TypedDict


# -----------------------------
# Configuration (defaults)
# -----------------------------

VERSION = "2.2.2"

UDP_BIND_IP = "0.0.0.0"
UDP_PORT = 9932

ALERT_THRESHOLD_SECONDS = 60
NEW_PASS_JUMP_SECONDS = 120

# Optional: alert only on these satellites (exact match). Empty => all.
ALLOWED_SATS = set()  # e.g. {"RS-44", "ISS"}

# Default behaviors (can be overridden by CLI switches)
DEFAULT_ENABLE_POPUP = True
DEFAULT_ENABLE_VOICE = True

SPEAK_ONCE_PER_PASS = True
POPUP_TIMEOUT_SECONDS = 10

RESTART_ON_ERROR = True
RESTART_DELAY_SECONDS = 2.0
SOCKET_TIMEOUT_SECONDS = 1.0

REMOTE_QUIT_STRINGS = {"QUIT", "SAT,QUIT"}

# --- Real-time filtering knobs ---
TTG_TIME_CONSISTENCY_TOLERANCE_SECONDS = 15
GAP_REQUIRES_RESYNC_SECONDS = 5 * 60
CONSECUTIVE_GOOD_REQUIRED = 2


# -----------------------------
# State types
# -----------------------------

class SatState(TypedDict):
    last_ttg: Optional[int]
    last_seen: float
    alerted: bool
    good_count: int


# -----------------------------
# CLI switches
# -----------------------------

def parse_args(argv: list[str]) -> tuple[bool, bool]:
    """
    Returns (enable_popup, enable_voice)

    Supported switches:
      --no-popup    Disable popup (voice still enabled unless --no-voice is used)
      --popup       Enable popup (default)
      --no-voice    Disable voice
      --voice       Enable voice (default)

    Examples:
      python sat_udp_popup.py --no-popup
      python sat_udp_popup.py --no-popup --voice
      python sat_udp_popup.py --popup --no-voice
    """
    enable_popup = DEFAULT_ENABLE_POPUP
    enable_voice = DEFAULT_ENABLE_VOICE

    for a in argv[1:]:
        a = a.strip().lower()
        if a == "--no-popup":
            enable_popup = False
        elif a == "--popup":
            enable_popup = True
        elif a == "--no-voice":
            enable_voice = False
        elif a == "--voice":
            enable_voice = True

    return enable_popup, enable_voice


# -----------------------------
# Utilities
# -----------------------------

def format_mmss(seconds: int) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"


def run_powershell(command: str) -> None:
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def speak(text: str) -> None:
    """Speak text with Windows SAPI via PowerShell (azimuth intentionally not spoken)."""
    safe = text.replace('"', '`"')
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f'$s.Speak("{safe}");'
    )
    run_powershell(ps)


def popup_timeout(title: str, message: str, timeout_seconds: int) -> None:
    """
    Show a MessageBox that auto-closes after timeout_seconds.
    Falls back to a normal MessageBox if MessageBoxTimeoutW is unavailable.
    """
    import ctypes
    from ctypes import wintypes

    MB_OK = 0x00000000
    MB_SYSTEMMODAL = 0x00001000

    user32 = ctypes.windll.user32

    try:
        MessageBoxTimeoutW = user32.MessageBoxTimeoutW
        MessageBoxTimeoutW.argtypes = [
            wintypes.HWND,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.UINT,
            wintypes.WORD,
            wintypes.DWORD,
        ]
        MessageBoxTimeoutW.restype = ctypes.c_int

        MessageBoxTimeoutW(
            0,
            message,
            title,
            MB_OK | MB_SYSTEMMODAL,
            0,
            int(timeout_seconds * 1000),
        )
        return
    except Exception:
        user32.MessageBoxW(0, message, title, MB_OK | MB_SYSTEMMODAL)


def normalize_raw(raw: str) -> str:
    return raw.strip().strip("\x00").strip()


def is_remote_quit_packet(raw: str) -> bool:
    s = normalize_raw(raw).upper()
    return s in REMOTE_QUIT_STRINGS


def parse_faos_packet(raw: str) -> Optional[tuple[str, float, int]]:
    """
    Parse: SAT,FAOS,NAME,AZIMUTH,TIMETOGO
    Returns: (name, azimuth, ttg) or None
    """
    parts = [p.strip() for p in normalize_raw(raw).split(",")]
    if len(parts) < 5:
        return None

    prefix, event, name, az_str, ttg_str = parts[:5]
    if prefix != "SAT" or event != "FAOS":
        return None

    try:
        az = float(az_str)
        ttg = int(float(ttg_str))
    except ValueError:
        return None

    return name, az, ttg


# -----------------------------
# Hotkey thread (console-local)
# -----------------------------

def hotkey_watcher(stop_event: threading.Event) -> None:
    """Press Q/q or Ctrl+Q to stop."""
    try:
        import msvcrt
    except Exception:
        return

    while not stop_event.is_set():
        try:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("q", "Q", "\x11"):
                    print("[INFO] Hotkey quit received. Shutting down...")
                    stop_event.set()
                    return
            else:
                time.sleep(0.05)
        except Exception:
            time.sleep(0.2)


# -----------------------------
# Real-time consistency check
# -----------------------------

def update_realtime_good_count(st: SatState, ttg: int, now: float) -> SatState:
    if st["last_ttg"] is None:
        st["good_count"] = 1
        return st

    elapsed = now - st["last_seen"]

    if elapsed >= GAP_REQUIRES_RESYNC_SECONDS:
        st["good_count"] = 1
        return st

    delta_ttg = st["last_ttg"] - ttg
    off_by = abs(delta_ttg - elapsed)

    if off_by <= TTG_TIME_CONSISTENCY_TOLERANCE_SECONDS:
        st["good_count"] = min(CONSECUTIVE_GOOD_REQUIRED, st["good_count"] + 1)
    else:
        st["good_count"] = 1

    return st


def realtime_ok(st: SatState) -> bool:
    return st["good_count"] >= CONSECUTIVE_GOOD_REQUIRED


# -----------------------------
# One run of listener
# -----------------------------

def listener_run(stop_event: threading.Event, enable_popup: bool, enable_voice: bool) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((UDP_BIND_IP, UDP_PORT))
    sock.settimeout(SOCKET_TIMEOUT_SECONDS)

    print(f"[INFO] sat_udp_popup.py v{VERSION}")
    print(f"[INFO] Listening on UDP {UDP_BIND_IP}:{UDP_PORT}")
    print("[INFO] Quit: Q / Ctrl+Q (console) OR UDP 'SAT,QUIT' to port 9932")
    print(f"[INFO] Popup: {'ENABLED' if enable_popup else 'DISABLED'} | Voice: {'ENABLED' if enable_voice else 'DISABLED'}")
    print("[INFO] Log marker: '*' at end of line means packet entered alert path (popup/voice attempted).")

    state: Dict[str, SatState] = {}

    while not stop_event.is_set():
        try:
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        except OSError:
            raise

        raw = data.decode(errors="ignore")

        if is_remote_quit_packet(raw):
            print(f"[INFO] Remote quit received from {addr[0]}. Shutting down...")
            stop_event.set()
            break

        parsed = parse_faos_packet(raw)
        if not parsed:
            continue

        name, az, ttg = parsed

        if ALLOWED_SATS and name not in ALLOWED_SATS:
            continue

        now = time.time()
        st: SatState = state.get(
            name,
            {"last_ttg": None, "last_seen": 0.0, "alerted": False, "good_count": 0},
        )

        # New pass detection
        if st["last_ttg"] is not None and ttg > st["last_ttg"] + NEW_PASS_JUMP_SECONDS:
            st = {"last_ttg": None, "last_seen": 0.0, "alerted": False, "good_count": 0}

        # Update real-time score BEFORE overwriting last_seen/last_ttg
        st = update_realtime_good_count(st, ttg, now)

        st["last_ttg"] = ttg
        st["last_seen"] = now
        state[name] = st

        rt = "OK" if realtime_ok(st) else f"SYNC({st['good_count']}/{CONSECUTIVE_GOOD_REQUIRED})"

        # Evaluate alert gating (without side effects yet)
        would_alert = (
            realtime_ok(st)
            and ttg <= ALERT_THRESHOLD_SECONDS
            and (not SPEAK_ONCE_PER_PASS or not st["alerted"])
        )

        marker = " *" if would_alert else ""
        print(f"FAOS {name}: ttg={ttg} az={az:.1f} from {addr[0]} realtime={rt}{marker}")

        if not would_alert:
            continue

        # Mark alerted
        st["alerted"] = True
        state[name] = st

        title = f"{name} Rising"
        mmss = format_mmss(ttg)
        message = f"{name} Rising in {mmss}"
        voice_text = f"{name} rising in {ttg} seconds."

        # Attempt voice/popup; log what we attempted
        if enable_voice:
            try:
                print(f"[ALERT] Voice -> {name} (ttg={ttg})")
                speak(voice_text)
            except Exception as e:
                print(f"[WARN] Voice failed: {e}")

        if enable_popup:
            try:
                print(f"[ALERT] Popup -> {name} (ttg={ttg}) timeout={POPUP_TIMEOUT_SECONDS}s")
                popup_timeout(title, message, POPUP_TIMEOUT_SECONDS)
            except Exception as e:
                print(f"[WARN] Popup failed: {e}")

    try:
        sock.close()
    except Exception:
        pass


# -----------------------------
# Entry point with auto-restart
# -----------------------------

def main() -> int:
    enable_popup, enable_voice = parse_args(sys.argv)

    stop_event = threading.Event()
    t = threading.Thread(target=hotkey_watcher, args=(stop_event,), daemon=True)
    t.start()

    while not stop_event.is_set():
        try:
            listener_run(stop_event, enable_popup, enable_voice)
        except KeyboardInterrupt:
            print("\n[INFO] Ctrl+C received. Shutting down...")
            stop_event.set()
        except Exception as e:
            print(f"[ERROR] Listener crashed: {e!r}")
            if not RESTART_ON_ERROR:
                stop_event.set()
            else:
                print(f"[INFO] Restarting in {RESTART_DELAY_SECONDS} seconds...")
                time.sleep(RESTART_DELAY_SECONDS)

    print("[INFO] Exited cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
