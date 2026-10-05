#!/usr/bin/env bash
# Receiver helper. Channel switching happens in your player: every channel that
# locks is in Tvheadend, and picking one retunes the Airspy on demand.
#
#   ./atsc.sh scan [rf ...] [--quick]   rescan (pauses TV), then sync Tvheadend
#   ./atsc.sh sync [max-errors%]        make Tvheadend match the last scan (default 10)
#   ./atsc.sh list                      show the last scan (atsc/config/channels.json)
#   ./atsc.sh status                    what the tuner is doing, container health
#   ./atsc.sh urls                      playlist / guide / stream URLs for players on the LAN
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
CONF=atsc/config/atsc-rx.conf
SCAN=atsc/config/channels.json

conf_get() { sed -n "s/^$1=//p" "$CONF" 2>/dev/null | tail -1; }

usage() { sed -n '2,10s/^# \{0,1\}//p' "$0"; exit 2; }

scan() {
    mkdir -p atsc/config
    local running gain rc=0
    running=$(docker compose ps -q --status running atsc-rx)
    gain=$(conf_get GAIN)
    if [[ -n $running ]]; then
        echo "pausing the tuner (the scan needs the Airspy to itself)"
        docker compose stop atsc-rx >/dev/null
    fi
    docker compose run --rm atsc-scan --gain "${gain:-11}" "$@" || rc=$?
    [[ -n $running ]] && docker compose start atsc-rx >/dev/null
    (( rc == 0 )) || return $rc
    echo
    sync_tvh
}

sync_tvh() {
    [[ -f $SCAN ]] || { echo "no scan yet; run ./atsc.sh scan" >&2; exit 1; }
    python3 atsc/tvh_sync.py --max-errors "${1:-10}"
}

list() {
    [[ -f $SCAN ]] || { echo "no scan yet; run ./atsc.sh scan" >&2; exit 1; }
    python3 - "$SCAN" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1]))
print(f"scan of {d['scanned']} (gain {d['gain']}); ripple > 10 dB means multipath\n"
      f"{'RF':>3} {'MHz':>4} {'ripple':>7} {'errors':>7}  stations")
for c in d["channels"]:
    if c["kind"] != "atsc1":
        print(f"{c['rf']:3d} {c['freq_mhz']:4d} {'-':>7} {'-':>7}  signal without 8VSB pilot (ATSC 3.0?)")
        continue
    err = f"{c['errors_pct']}%" if "errors_pct" in c else "-"
    ripple = f"{c['ripple_db']:.1f}dB" if "ripple_db" in c else "-"
    names = ", ".join(f"{s['number']} {s['name']}" for s in c.get("services", []))
    if not names and "errors_pct" in c:
        names = "no lock (multipath?)" if c.get("ripple_db", 0) > 10 else "no lock"
    print(f"{c['rf']:3d} {c['freq_mhz']:4d} {ripple:>7} {err:>7}  {names}")
EOF
}

status() {
    docker compose ps --format '{{.Name}}: {{.Status}}' atsc-rx tvheadend
    local st
    if st=$(curl -sf -m 3 http://127.0.0.1:5600/status); then
        python3 - "$st" <<'EOF'
import json, sys
s = json.loads(sys.argv[1])
if s["rf"] is None:
    print("tuner: idle (nobody watching)")
else:
    lock = "locked" if s["locked"] else "NO LOCK"
    print(f"tuner: RF {s['rf']}, {s['clients']} client(s), {s['mbps']} Mbps, {lock}, for {s['tuned_for_s']}s")
EOF
    else
        echo "tuner: not reachable on 127.0.0.1:5600"
    fi
    docker logs --since 10m atsc-rx 2>&1 | grep '^atsc_rx:' | tail -4
}

urls() {
    python3 atsc/tvh_viewer.py --host "$(hostname -I | awk '{print $1}')"
}

case ${1:-} in
    scan) shift; scan "$@" ;;
    sync) shift; sync_tvh "$@" ;;
    list) list ;;
    status) status ;;
    urls) urls ;;
    *) usage ;;
esac
