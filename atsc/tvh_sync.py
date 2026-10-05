#!/usr/bin/env python3
"""Make Tvheadend match the last channel scan (atsc/config/channels.json).

  * One IPTV mux per RF channel, reading the on-demand tuner
    (http://127.0.0.1:5600/rf/<n>) through atsc/tvh_pipe.py with respawn.
    Muxes of RF channels that locked in the scan
    (packet errors <= --max-errors, station names decoded) are enabled; the
    rest are disabled, not deleted, so they come back as they were.
  * The network allows one input stream (there is one Airspy): picking a
    channel on another RF channel retunes, and background jobs (guide
    grabbing, scans) wait rather than interrupt someone watching.
  * Channels are owned by the network's bouquet with auto-map on, so services
    that Tvheadend discovers later (e.g. a scan that had to wait for a viewer)
    become channels by themselves, numbered as broadcast (8.1, 8.2, ...).
  * A service is enabled only if its RF channel locks and the station
    announces it in PSIP (hidden placeholders are dropped); a channel is
    enabled only if its RF channel locks.

  tvh_sync.py                    sync from atsc/config/channels.json
  tvh_sync.py --max-errors 20    also accept noisier channels
"""
import argparse, json, os, re, sys, time
from tvh_api import NETWORK_NAME, HERE, api_client

# Tvheadend runs atsc/tvh_pipe.py (the repo's atsc/ is mounted at /atsc) per mux;
# it reconnects across tuner restarts (plain http:// never reconnects).
TUNER = "pipe:///usr/bin/python3 /atsc/tvh_pipe.py {rf}"


def mux_rf(m):
    """RF channel of one of our muxes, from its name or URL."""
    for text in (m.get("name") or "", m.get("iptv_url") or ""):
        g = (re.search(r"RF (\d+)$", text) or re.search(r"/rf/(\d+)$", text)
             or re.search(r"(?:atsc-pipe|tvh_pipe\.py) (\d+)$", text) or re.search(r":55(\d\d)$", text))
        if g:
            return int(g.group(1))
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scan", default=os.path.join(HERE, "config", "channels.json"))
    ap.add_argument("--max-errors", type=float, default=10.0,
                    help="highest packet error %% in the scan to still enable a channel (default 10)")
    a = ap.parse_args()
    api = api_client()

    with open(a.scan) as f:
        scan = json.load(f)
    good = {c["rf"]: c for c in scan["channels"]
            if c["kind"] == "atsc1" and c.get("services") and c.get("errors_pct", 100) <= a.max_errors}
    announced = {c["rf"]: {s["number"] for s in c.get("services", [])} for c in scan["channels"]}
    if not good:
        sys.exit(f"no channel in {a.scan} locked with <= {a.max_errors}% errors; re-aim the antenna and rescan")

    # Network: one input stream, a bouquet for auto-mapping, fail fast without lock
    net_conf = {"enabled": True, "networkname": NETWORK_NAME, "pnetworkname": NETWORK_NAME,
                "max_streams": 1, "max_timeout": 15, "skipinitscan": True, "scan_create": True,
                "bouquet": True}
    net = next((n for n in api("mpegts/network/grid", limit=500)["entries"]
                if n.get("networkname") == NETWORK_NAME), None)
    if net:
        net_uuid = net["uuid"]
        api("idnode/save", node={"uuid": net_uuid, **net_conf})
    else:
        net_uuid = api("mpegts/network/create", **{"class": "iptv_network", "conf": net_conf})["uuid"]
        print(f"created network {NETWORK_NAME}")

    def our_muxes():
        return [m for m in api("mpegts/mux/grid", limit=500, hidemode="none")["entries"]
                if m.get("network_uuid") == net_uuid]

    def our_services():
        ids = {m["uuid"] for m in our_muxes()}
        return [s for s in api("mpegts/service/grid", limit=2000, hidemode="none")["entries"]
                if s.get("multiplex_uuid") in ids]

    # Muxes: one per good RF channel on the tuner URL; everything else disabled
    have = {}
    for m in our_muxes():
        rf = mux_rf(m)
        if rf in good and rf not in have:
            have[rf] = m
            want = {"iptv_url": TUNER.format(rf=rf), "iptv_muxname": f"RF {rf}",
                    "iptv_atsc": True, "iptv_respawn": True, "enabled": 1}
            if any(m.get(k) != v for k, v in want.items()):
                api("idnode/save", node={"uuid": m["uuid"], **want})
        elif m.get("enabled") != 0:
            api("idnode/save", node={"uuid": m["uuid"], "enabled": 0})
    for rf in sorted(set(good) - set(have)):
        api("mpegts/network/mux_create", uuid=net_uuid, conf={
            "enabled": 1, "iptv_muxname": f"RF {rf}", "iptv_url": TUNER.format(rf=rf),
            "iptv_atsc": True, "iptv_respawn": True, "epg": 1})
        print(f"created mux RF {rf}")
    mux_of = {m["uuid"]: mux_rf(m) for m in our_muxes()}

    # Services: enabled iff the RF channel locks and PSIP announces the number
    for s in our_services():
        rf = mux_of.get(s["multiplex_uuid"])
        want = rf in good and f"{s.get('lcn')}.{s.get('lcn_minor')}" in announced.get(rf, set())
        if s.get("enabled") != want:
            api("idnode/save", node={"uuid": s["uuid"], "enabled": want})

    # Bouquet: created by the network; auto-map its services to channels
    bq = None
    for _ in range(10):
        bq = next((b for b in api("bouquet/grid", limit=500)["entries"] if b.get("name") == NETWORK_NAME), None)
        if bq:
            break
        time.sleep(1)
    if not bq:
        sys.exit("Tvheadend did not create the network bouquet")
    api("idnode/save", node={"uuid": bq["uuid"], "enabled": True, "maptoch": True,
                             "mapopt": ["mapradio"], "chtag": []})

    # One-time migration: channels mapped by hand earlier aren't the bouquet's
    # and would be duplicated by auto-map; remove them so the bouquet owns all.
    svc_mux = {s["uuid"]: s["multiplex_uuid"] for s in our_services()}
    stale = [ch["uuid"] for ch in api("channel/grid", limit=2000, all=1)["entries"]
             if ch.get("bouquet") != bq["uuid"] and ch.get("services")
             and all(u in svc_mux for u in ch["services"])]
    if stale:
        api("idnode/delete", uuid=json.dumps(stale))
        print(f"handed {len(stale)} hand-mapped channels over to the bouquet")

    # Let auto-map settle (channel count stable), then enable by RF channel
    prev, stable = -1, 0
    for _ in range(20):
        n = api("channel/grid", limit=1, all=1)["total"]
        stable = stable + 1 if n == prev else 0
        if stable >= 2:
            break
        prev = n
        time.sleep(1)
    good_mux = {u for u, rf in mux_of.items() if rf in good}
    svc_mux = {s["uuid"]: s["multiplex_uuid"] for s in our_services()}
    for ch in api("channel/grid", limit=2000, all=1)["entries"]:
        mux_ids = {svc_mux.get(u) for u in ch.get("services", [])}
        if not mux_ids or not mux_ids <= set(svc_mux.values()):
            continue  # not purely an Airspy channel; leave it alone
        want = bool(mux_ids & good_mux)
        if ch.get("enabled") != want:
            api("idnode/save", node={"uuid": ch["uuid"], "enabled": want})

    # Muxes with no services yet get scanned when the tuner is free; the
    # bouquet then maps their channels automatically.
    pending = []
    for m in our_muxes():
        if mux_of.get(m["uuid"]) in good and not m.get("num_svc"):
            pending.append(mux_of[m["uuid"]])
            if m.get("scan_state") == 0:
                api("idnode/save", node={"uuid": m["uuid"], "scan_state": 1})

    chans = {}
    for ch in api("channel/grid", limit=2000, all=1)["entries"]:
        if ch.get("enabled") and ch.get("bouquet") == bq["uuid"]:
            rf = next((mux_of.get(svc_mux.get(u)) for u in ch.get("services", []) if u in svc_mux), None)
            chans.setdefault(rf, []).append((float(ch.get("number") or 0), f"{ch.get('number')} {ch.get('name')}"))
    print("\nTvheadend channels (pick any in a player; the Airspy retunes on demand):")
    for rf in sorted(good):
        names = ", ".join(n for _, n in sorted(chans.get(rf, [])))
        if rf in pending:
            names = "scan pending: runs when nobody is watching, channels then appear by themselves"
        print(f"  RF {rf:2d} ({good[rf].get('errors_pct', '?')}% errors in scan): {names}")
    dropped = sorted(c["rf"] for c in scan["channels"] if c["kind"] == "atsc1" and c["rf"] not in good)
    if dropped:
        print(f"  not enabled (no clean lock in the scan): RF {', '.join(map(str, dropped))}")


if __name__ == "__main__":
    main()
