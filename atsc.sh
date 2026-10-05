#!/usr/bin/env bash
# Receiver helper. Channel switching happens in your player: every channel that
# locks is in Tvheadend, and picking one retunes the Airspy on demand.
#
#   ./atsc.sh scan [rf ...] [--quick]   rescan (pauses TV); Tvheadend follows automatically
#   ./atsc.sh follow                    follow a running scan (e.g. the start-up one) to the end
#   ./atsc.sh sync [max-errors%]        make Tvheadend match the last scan (default 10)
#   ./atsc.sh list                      show the last scan (atsc/config/channels.json)
#   ./atsc.sh status                    what the tuner is doing, container health
#   ./atsc.sh urls                      playlist / guide / stream URLs for players on the LAN
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
CONF=atsc/config/atsc-rx.conf
SCAN=atsc/config/channels.json

conf_get() { sed -n "s/^$1=//p" "$CONF" 2>/dev/null | tail -1; }

usage() { sed -n '2,11s/^# \{0,1\}//p' "$0"; exit 2; }

scan() {
    # The tuner owns the Airspy, so it runs the scan; webtv then updates
    # Tvheadend (as it does after every scan, including the start-up one).
    local rfs=() quick=0 q code st
    for x in "$@"; do
        case $x in --quick) quick=1 ;; *) rfs+=("$x") ;; esac
    done
    q="quick=$quick"
    (( ${#rfs[@]} )) && q+="&rf=$(IFS=,; echo "${rfs[*]}")"
    code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:5600/scan?$q") || code=000
    case $code in
        202) echo "scan started (TV pauses while it runs)" ;;
        409) echo "a scan is already running; following it" ;;
        *) echo "can't reach the tuner on 127.0.0.1:5600 (HTTP $code)" >&2; return 1 ;;
    esac
    follow
}

follow() {
    # Show a running scan's progress, then wait for Tvheadend to be updated
    local st
    while st=$(curl -sf http://127.0.0.1:5600/status) &&
          python3 -c 'import json,sys; sys.exit(0 if json.loads(sys.argv[1])["scan"]["running"] else 1)' "$st"; do
        python3 - "$st" <<'PY'
import json, sys
s = json.loads(sys.argv[1])["scan"]
what = "identifying stations" if s["phase"] == "identify" else "checking frequencies"
print(f"\r  {what}: RF {s['rf'] or '-'} ({s['done'] + 1}/{s['total'] or '?'})   ", end="", flush=True)
PY
        sleep 2
    done
    echo
    [[ -f $SCAN ]] || { echo "no scan results" >&2; return 1; }
    wait_for_sync
    list
}

wait_for_sync() {
    # webtv syncs Tvheadend after each scan; fall back to doing it here
    local finished st
    finished=$(curl -sf http://127.0.0.1:5600/status | python3 -c 'import json,sys; print(json.load(sys.stdin)["scan"]["finished"])')
    for _ in $(seq 150); do
        st=$(curl -sf -m 3 "http://127.0.0.1:${WEBTV_PORT:-80}/api/state") || break
        if python3 -c 'import json,sys; d=json.loads(sys.argv[1])["sync"]; sys.exit(0 if str(d["for_scan"]) == sys.argv[2] and not d["running"] else 1)' "$st" "$finished"; then
            echo "Tvheadend updated by webtv"
            return
        fi
        sleep 2
    done
    echo "webtv not reachable; updating Tvheadend here"
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
    follow) follow ;;
    sync) shift; sync_tvh "$@" ;;
    list) list ;;
    status) status ;;
    urls) urls ;;
    *) usage ;;
esac
