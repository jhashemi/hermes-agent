#!/usr/bin/env python3
"""Pipe bridge.js --pair-json output through this to publish QR + status
into /srv/nebula-report/whatsapp-pair/ for browser scanning.

Usage (must be launched with the qrcode-enabled venv python):
  node bridge.js --pair-only --pair-json --session ... 2>&1 | \
    /home/ubuntu/.hermes/hermes-agent/venv/bin/python qr_publisher.py
"""
import json
import os
import sys
import time

import qrcode  # requires Pillow + qrcode; installed in hermes-agent venv

SERVE_DIR = "/srv/nebula-report/whatsapp-pair"
STATE = {"state": "starting", "qr_seq": 0, "ts": int(time.time() * 1000), "error": None}


def write_state():
    STATE["ts"] = int(time.time() * 1000)
    tmp = os.path.join(SERVE_DIR, "state.json.tmp")
    dst = os.path.join(SERVE_DIR, "state.json")
    with open(tmp, "w") as f:
        json.dump(STATE, f)
    os.replace(tmp, dst)


def publish_qr(payload: str):
    """Render QR payload → PNG and write it into the served dir atomically."""
    img = qrcode.make(payload, box_size=10, border=2)
    dst = os.path.join(SERVE_DIR, "qr.png")
    tmp = dst + ".tmp"
    img.save(tmp, format="PNG")
    os.replace(tmp, dst)
    STATE["qr_seq"] += 1
    STATE["state"] = "qr"
    write_state()
    print(f"[publisher] published qr seq={STATE['qr_seq']}", flush=True)


write_state()
print(f"[publisher] watching bridge.js output → {SERVE_DIR}", flush=True)

try:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        # Echo the line through so operator can see everything
        print(line, flush=True)
        if not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except Exception:
            continue
        event = evt.get("event")
        if event == "qr":
            payload = evt.get("qr", "")
            if payload:
                publish_qr(payload)
        elif event == "connected":
            STATE["state"] = "connected"
            STATE["user"] = evt.get("user")
            STATE["error"] = None
            write_state()
            print("[publisher] PAIRED — exiting", flush=True)
            break
        elif event == "error":
            STATE["state"] = "error"
            STATE["error"] = evt.get("error", "unknown")
            write_state()
        elif event == "disconnected":
            reason = evt.get("reason")
            STATE["state"] = "disconnected"
            STATE["error"] = f"disconnected (reason {reason})"
            write_state()
except KeyboardInterrupt:
    pass
