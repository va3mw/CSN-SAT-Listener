# CSN-SAT-Listener

# SAT UDP AOS Listener (Windows)

A lightweight Windows 10/11 utility that listens for **satellite AOS (Acquisition of Signal)**
UDP messages and provides **real-time audible and visual alerts**.

Designed for amateur radio satellite operations and automation.

---

## Features

- Listens on **UDP port 9932**
- Supports SAT FAOS messages in the format:



- Alerts when a satellite is **1 minute (or less) from AOS**
- **Voice alert** using Windows built-in speech (SAPI)
- **Popup alert** that auto-closes after 10 seconds
- Message wording:  
**“RS-44 Rising”**
- Ignores **queued / stale messages** after PC sleep or suspend
- Anti-spam: alerts **once per satellite per pass**
- Clean shutdown methods:
- `Ctrl+C`
- `Q` or `Ctrl+Q` in console
- Remote UDP command (`SAT,QUIT`)
- Automatic restart on unexpected errors
- Can be packaged into a **single Windows EXE**

---

## Supported Message Types

Currently supported:

| Message | Purpose |
|------|-------|
| `SAT,FAOS,...` | Future AOS (countdown to rise) |
| `SAT,QUIT` or `QUIT` | Remotely terminate listener |

---

## Requirements

### Operating System
- Windows 10 or Windows 11

### Python
- Python **3.9+** recommended  
- Tested with Python **3.11 / 3.12 / 3.13**

### Python Dependencies
This project uses **only the Python standard library**.

No third-party Python packages are required.

### External Tools (Built-in)
- **PowerShell** (included with Windows)
- **System.Speech (SAPI)** for voice alerts

---

## Installation

Clone the repository:

```bash
git clone https://github.com/YOUR_USERNAME/sat-udp-aos-listener.git
cd sat-udp-aos-listener

python sat_udp_popup.py

Real-Time Message Protection (Important)

This program intentionally ignores stale or queued messages, which can occur if:

The PC sleeps overnight

The sender queues packets and releases them all at resume

How it works

The program checks that TIMETOGO decreases in a way that matches real wall-clock time

It requires two consecutive time-consistent packets before allowing alerts

This prevents false alerts when the system resumes.

Testing note

When testing with manual packets, send at least two packets a second or two apart.

Firewall Notes

If packets are not received:

Allow inbound UDP traffic on port 9932

Or allow the executable when prompted by Windows Firewall

Known Limitations

Toast notifications use MessageBox (modal) rather than Notification Center

Voice relies on PowerShell / SAPI (standard on Windows)

No Linux or macOS support (by design)
