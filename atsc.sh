#!/usr/bin/env bash
# Receiver helper.
#
#   ./atsc.sh scan [rf ...] [--quick]   find receivable channels (pauses the receiver while scanning)
#   ./atsc.sh list                      show the last scan (atsc/config/channels.json)
#   ./atsc.sh tune <rf> [gain]          receive this RF channel and update Tvheadend
#   ./atsc.sh best                      print the strongest clean RF channel from the last scan
#   ./atsc.sh status                    configured channel, containers, stream rate
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
        echo "pausing atsc-rx (the Airspy can only be used by one process)"
        docker compose stop atsc-rx >/dev/null
    fi
    docker compose run --rm atsc-scan --gain "${gain:-11}" "$@" || rc=$?
    [[ -n $running ]] && docker compose start atsc-rx >/dev/null
    return $rc
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
    names = ", ".join(f"{s['number']} {s['name']}" for s in c.get("services", []))
    if not names and "errors_pct" in c:
        names = "no lock (multipath?)" if c.get("ripple_db", 0) > 10 else "no lock"
    print(f"{c['rf']:3d} {c['freq_mhz']:4d} {(f"{c['ripple_db']:.1f}dB" if 'ripple_db' in c else '-'):>7} {err:>7}  {names}")
EOF
}

best() {
    [[ -f $SCAN ]] || { echo "no scan yet; run ./atsc.sh scan" >&2; exit 1; }
    python3 - "$SCAN" <<'EOF'
import json, sys
chans = [c for c in json.load(open(sys.argv[1]))["channels"]
         if c["kind"] == "atsc1" and c.get("services") and c.get("errors_pct", 100) <= 1]
if not chans:
    sys.exit("no cleanly received ATSC 1.0 channel in the last scan")
print(max(chans, key=lambda c: c["level_db"])["rf"])
EOF
}

tune() {
    local rf=${1:-} gain=${2:-}
    [[ $rf =~ ^[0-9]+$ ]] && (( rf >= 2 && rf <= 36 )) || { echo "usage: ./atsc.sh tune <rf 2-36> [gain 0-21]" >&2; exit 2; }
    [[ -n $gain ]] || gain=$(conf_get GAIN)
    gain=${gain:-11}
    mkdir -p atsc/config
    printf '# Written by ./atsc.sh tune. RF_CHANNEL / GAIN environment variables override.\nRF_CHANNEL=%s\nGAIN=%s\n' \
        "$rf" "$gain" > "$CONF"
    echo "atsc-rx -> RF $rf (gain $gain), UDP port $((5500 + rf))"
    docker compose up -d atsc-rx >/dev/null 2>&1
    docker compose restart atsc-rx >/dev/null 2>&1
    python3 atsc/tvh_add_mux.py --channel "$rf" --port $((5500 + rf)) --exclusive
}

status() {
    echo "configured: RF $(conf_get RF_CHANNEL || true), gain $(conf_get GAIN || true)   ($CONF)"
    docker compose ps --format '{{.Name}}: {{.Status}}' atsc-rx tvheadend
    # atsc-rx logs its transport stream rate every 60 s: ~19.39 Mbps = locked, 0 = no signal
    docker logs atsc-rx 2>&1 | grep '^atsc_rx:' | tail -2
}

urls() {
    python3 atsc/tvh_viewer.py --host "$(hostname -I | awk '{print $1}')"
}

case ${1:-} in
    scan) shift; scan "$@" ;;
    list) list ;;
    tune) shift; tune "$@" ;;
    best) best ;;
    status) status ;;
    urls) urls ;;
    *) usage ;;
esac
