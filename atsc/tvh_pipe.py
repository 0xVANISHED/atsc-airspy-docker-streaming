#!/usr/bin/env python3
"""Tvheadend pipe:// helper: copy one RF channel's transport stream from the
on-demand tuner to stdout.

The repo's atsc/ is mounted into the Tvheadend container at /atsc; each mux URL
is `pipe:///usr/bin/python3 /atsc/tvh_pipe.py <rf>` with Respawn on.
Tvheadend never reconnects a plain HTTP stream that ends, so this does:

  * tuner unreachable (restarting, redeploy): retry every second for a minute;
  * stream ended: if the tuner now serves another RF channel (someone switched)
    or is scanning, exit and let Tvheadend decide; otherwise reconnect.

Tvheadend kills this process when it stops the mux; exiting on a closed stdout
covers the rest.
"""
import json, shutil, sys, time, urllib.error, urllib.request

rf = int(sys.argv[1])
base = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:5600"


def note(msg):
    print(f"atsc-pipe: RF {rf}: {msg}", file=sys.stderr, flush=True)


def displaced():
    """True if the tuner moved on deliberately (other channel watched, or scanning)."""
    try:
        with urllib.request.urlopen(f"{base}/status", timeout=3) as r:
            s = json.load(r)
    except Exception:
        return False                      # tuner down: a restart, keep trying
    return s["scan"]["running"] or (s["rf"] not in (None, rf) and s["clients"] > 0)


down_since = None
while True:
    try:
        # timeout applies per read: no data for 15 s (no lock) -> treat as ended
        with urllib.request.urlopen(f"{base}/rf/{rf}", timeout=15) as r:
            down_since = None
            shutil.copyfileobj(r, sys.stdout.buffer, 65536)
        note("stream ended")
    except BrokenPipeError:
        sys.exit(0)                       # Tvheadend closed our stdout: mux stopped
    except urllib.error.HTTPError as e:
        note(f"tuner refused ({e.code})")
        if e.code == 503:                 # scanning
            sys.exit(1)
    except Exception as e:
        note(str(e))
    if displaced():
        note("tuner moved to another channel; giving up")
        sys.exit(1)
    down_since = down_since or time.monotonic()
    if time.monotonic() - down_since > 60:
        note("tuner unavailable for a minute; giving up")
        sys.exit(1)
    time.sleep(1)
