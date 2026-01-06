# -*- coding: utf-8 -*-
"""
sat_udp_popup.py

Version: 2.2.1
VA3MW - January 2025
Works with:  http://www.csntechnologies.net/sat

Windows 11 UDP listener for SAT "FAOS" packets on port 9932.

Expected UDP packet format:
    SAT,FAOS,NAME,AZIMUTH,TIMETOGO

Example:
    SAT,FAOS,RS-44,151.1,55

Changes in v2.2.1:
1) Voice no longer reads the azimuth (not required).
2) Visual popup auto-closes after 10 seconds (no manual OK click).
   - Implemented via a MessageBox timeout using MessageBoxTimeoutW.
3) Retains the "real-time only" filter to ignore queued/burst packets after sleep.
4) Keeps hotkey quit (Q / Ctrl+Q), remote quit (SAT,QUIT), and auto-restart.

Notes on the popup timeout:
- MessageBoxTimeoutW is a Windows API function. It is available on modern Windows.
- If the timeout call fails for any reason, we fall back to a normal MessageBox.
"""

from __future__ import annotations

import socket
import subprocess
import time
import threading
from typing import Dict, Optional, TypedDict


# -----------------------------
# Configuration
# -----------------------------

VERSION = "2.2.1"

UDP_BIND_IP = "0.0.0.0"
UDP_PORT = 9932

# Trigger when <= this many seconds remain
ALERT_THRESHOLD_SECONDS = 60

# If TTG jumps upward by this much vs last packet, assume a new pass and re-arm
NEW_PASS_JUMP_SECONDS = 120

# Optional: alert only on these satellites (exact match). Empty => all.
ALLOWED_SATS = set()  # e.g. {"RS-44", "ISS"}

# Visual popup enabled?
ENABLE_POPUP = True

# Voice enabled?
ENABLE_VOICE = True

# Speak only once per pass
SPEAK_ONCE_PER_PASS = True

# Popup auto-close (seconds)
POPUP_TIMEOUT_SECONDS = 10

# Auto-restart parameters
RESTART_ON_ERROR = True
RESTART_DELAY_SECONDS = 2.0

# Socket timeout so we can periodically check stop_event
SOCKET_TIMEOUT_SECONDS = 1.0

# Remote quit packets (case-insensitive)
REMOTE_QUIT_STRINGS = {"QUIT", "SAT,QUIT"}

# --- Real-time filtering knobs ---
# TTG should drop ~1 second per second in real time.
# Accept some tolerance because packets may arrive about once per minute.
TTG_TIME_CONSISTENCY_TOLERANCE_SECONDS = 15

# If we haven't heard from a satellite for this long, we treat the next packet as "not trusted"
# and require re-sync before allowing alerts (helps after sleep/resume).
GAP_REQUIRES_RESYNC_SECONDS = 5 * 60  # 5 minutes

# Require N consecutive "consistent" packets before alerts are allowed for that satellite.
CONSECUTIVE_GOOD_REQUIRED = 2


# -----------------------------
# State types
# -----------------------------

class SatState(TypedDict):
    last_ttg: Optional[int]
    last_seen: float
    alerted: bool
    good_count: int  # consecutive time-consistent packets


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
    """
    Speak text with Windows SAPI via PowerShell.
    Note: Azimuth is intentionally NOT included (per request).
    """
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

    Uses the undocumented-but-common MessageBoxTimeoutW signature:
      int MessageBoxTimeoutW(HWND, LPCWSTR, LPCWSTR, UINT, WORD, DWORD)
    """
    import ctypes
    from ctypes import wintypes

    MB_OK = 0x00000000
    MB_SYSTEMMODAL = 0x00001000

    user32 = ctypes.windll.user32

    # Try MessageBoxTimeoutW first
    try:
        MessageBoxTimeoutW = user32.MessageBoxTimeoutW
        MessageBoxTimeoutW.argtypes = [
            wintypes.HWND,       # hWnd
            wintypes.LPCWSTR,    # lpText
            wintypes.LPCWSTR,    # lpCaption
            wintypes.UINT,       # uType
            wintypes.WORD,       # wLanguageId
            wintypes.DWORD,      # dwMilliseconds
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
        # Fallback: regular MessageBox (requires user click)
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
        az = float(az_str)  # still parsed for completeness (not spoken)
        ttg = int(float(ttg_str))
    except ValueError:
        return None

    return name, az, ttg


# -----------------------------
# Hotkey thread (console-local)
# -----------------------------

def hotkey_watcher(stop_event: threading.Event) -> None:
    """
    Console-local hotkey watcher:
    - Press Q/q or Ctrl+Q to stop.
    """
    try:
        import msvcrt
    except Exception:
        return

    while not stop_event.is_set():
        try:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("q", "Q", "\x11"):  # Ctrl+Q = \x11
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
    """
    Update st.good_count based on whether TTG progression matches wall clock time.

    If PC slept and then a burst of queued packets arrives, TTG will not align with
    elapsed wall-clock time and good_count will not reach the required threshold.
    """
    if st["last_ttg"] is None:
        st["good_count"] = 1
        return st

    elapsed = now - st["last_seen"]

    # Long silence => require re-sync (sleep/resume / sender paused)
    if elapsed >= GAP_REQUIRES_RESYNC_SECONDS:
        st["good_count"] = 1
        return st

    # Expected: TTG decreases ~ elapsed seconds
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

def listener_run(stop_event: threading.Event) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((UDP_BIND_IP, UDP_PORT))
    sock.settimeout(SOCKET_TIMEOUT_SECONDS)

    print(f"[INFO] sat_udp_popup.py v{VERSION}")
    print(f"[INFO] Listening on UDP {UDP_BIND_IP}:{UDP_PORT}")
    print("[INFO] Quit: Q / Ctrl+Q (console) OR UDP 'SAT,QUIT' to port 9932")

    state: Dict[str, SatState] = {}

    while not stop_event.is_set():
        try:
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        except OSError:
            raise

        raw = data.decode(errors="ignore")

        # Remote quit
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

        # New pass detection (TTG jumps UP)
        if st["last_ttg"] is not None and ttg > st["last_ttg"] + NEW_PASS_JUMP_SECONDS:
            st = {"last_ttg": None, "last_seen": 0.0, "alerted": False, "good_count": 0}

        # Update time-consistency score BEFORE overwriting last_seen/last_ttg
        st = update_realtime_good_count(st, ttg, now)

        # Store last packet info
        st["last_ttg"] = ttg
        st["last_seen"] = now
        state[name] = st

        rt = "OK" if realtime_ok(st) else f"SYNC({st['good_count']}/{CONSECUTIVE_GOOD_REQUIRED})"
        print(f"FAOS {name}: ttg={ttg} az={az:.1f} from {addr[0]} realtime={rt}")

        # Only alert on real-time stream
        if not realtime_ok(st):
            continue

        if ttg > ALERT_THRESHOLD_SECONDS:
            continue

        if SPEAK_ONCE_PER_PASS and st["alerted"]:
            continue

        st["alerted"] = True
        state[name] = st

        # Requested wording: "<NAME> Rising"
        title = f"{name} Rising"
        mmss = format_mmss(ttg)
        message = f"{name} Rising in {mmss}"

        # Voice: do NOT read azimuth
        voice_text = f"{name} rising in {ttg} seconds."

        if ENABLE_VOICE:
            try:
                speak(voice_text)
            except Exception as e:
                print(f"[WARN] Voice failed: {e}")

        if ENABLE_POPUP:
            try:
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
    stop_event = threading.Event()

    t = threading.Thread(target=hotkey_watcher, args=(stop_event,), daemon=True)
    t.start()

    while not stop_event.is_set():
        try:
            listener_run(stop_event)
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
