#!/usr/bin/env python3
"""Idempotently register an RF channel from atsc-rx in Tvheadend.

Creates the "Airspy ATSC" IPTV network if missing, adds an ATSC IPTV mux for
udp://@127.0.0.1:<port>, waits for the scan, and maps the services to channels.
With --exclusive, the other Airspy muxes and their channels are disabled (only
one RF channel can be received at a time), so clients only list what plays.

Credentials: TVH_USER/TVH_PASS, else the superuser seeded in ../config/superuser.

  tvh_add_mux.py --channel 26 --port 5526 --exclusive
"""
import argparse, base64, json, os, sys, time, urllib.parse, urllib.request

NETWORK_NAME = "Airspy ATSC"
HERE = os.path.dirname(os.path.abspath(__file__))


def credentials():
    if os.environ.get("TVH_USER") and os.environ.get("TVH_PASS"):
        return os.environ["TVH_USER"], os.environ["TVH_PASS"]
    with open(os.path.join(HERE, "..", "config", "superuser")) as f:
        su = json.load(f)
    pw = su.get("password")
    if su.get("password2"):
        pw = base64.b64decode(su["password2"]).decode()[len("TVHeadend-Hide-"):]
    return su["username"], pw


def api_client(base, user, pw):
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, base, user, pw)
    opener = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr))

    def call(path, **params):
        data = urllib.parse.urlencode(
            {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in params.items()}).encode()
        with opener.open(f"{base}/api/{path}", data, timeout=30) as r:
            body = r.read()
        return json.loads(body) if body.strip() else {}
    return call


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channel", type=int, required=True, help="RF channel")
    ap.add_argument("--port", type=int, required=True, help="UDP port atsc-rx sends to")
    ap.add_argument("--url", default="http://127.0.0.1:9981")
    ap.add_argument("--scan-timeout", type=int, default=90)
    ap.add_argument("--exclusive", action="store_true",
                    help="disable the other Airspy muxes and their channels")
    a = ap.parse_args()
    api = api_client(a.url, *credentials())

    nets = api("mpegts/network/grid", limit=500)["entries"]
    net = next((n["uuid"] for n in nets if n.get("networkname") == NETWORK_NAME), None)
    if not net:
        net = api("mpegts/network/create", **{"class": "iptv_network", "conf": {
            "enabled": True, "networkname": NETWORK_NAME, "pnetworkname": NETWORK_NAME,
            "skipinitscan": True, "scan_create": True, "max_timeout": 20}})["uuid"]
        print(f"created network {NETWORK_NAME}")

    url = f"udp://@127.0.0.1:{a.port}"
    same = lambda u: (u or "").replace("udp://@", "udp://") == url.replace("udp://@", "udp://")
    muxes = api("mpegts/mux/grid", limit=500, hidemode="none")["entries"]
    mux = next((m for m in muxes if same(m.get("iptv_url"))), None)
    if mux:
        print(f"mux for {url} already exists ({mux.get('name')})")
        if mux.get("enabled") != 1:
            api("idnode/save", node={"uuid": mux["uuid"], "enabled": 1})
    else:
        api("mpegts/network/mux_create", uuid=net, conf={
            "enabled": 1, "iptv_muxname": f"RF {a.channel}", "iptv_url": url,
            "iptv_atsc": True, "epg": 1})
        print(f"created mux RF {a.channel} -> {url}; waiting for scan (atsc-rx must be on RF {a.channel})")

    deadline = time.time() + a.scan_timeout
    svcs = []
    while time.time() < deadline:
        mux = next((m for m in api("mpegts/mux/grid", limit=500, hidemode="none")["entries"]
                    if same(m.get("iptv_url"))), {})
        svcs = [s for s in api("mpegts/service/grid", limit=500, hidemode="none")["entries"]
                if mux and s.get("multiplex_uuid") == mux["uuid"]]
        if svcs and mux.get("scan_state") == 0:  # idle after the scan
            break
        time.sleep(3)
    if not svcs:
        sys.exit(f"no services found on RF {a.channel}; is atsc-rx running on that channel?")

    api("service/mapper/save", node={
        "services": [s["uuid"] for s in svcs], "check_availability": False,
        "encrypted": False, "merge_same_name": False, "type_tags": True,
        "provider_tags": False, "network_tags": True})
    for s in sorted(svcs, key=lambda s: s.get("lcn", 0)):
        print(f"  mapped {s.get('svcname')}")

    if a.exclusive:
        make_exclusive(api, net, mux["uuid"])


def make_exclusive(api, net, current_mux):
    """Enable only current_mux (and its channels) within our network."""
    muxes = [m for m in api("mpegts/mux/grid", limit=500, hidemode="none")["entries"]
             if m.get("network_uuid") == net]
    for m in muxes:
        want = 1 if m["uuid"] == current_mux else 0
        if m.get("enabled") != want:
            api("idnode/save", node={"uuid": m["uuid"], "enabled": want})
    ours = {m["uuid"] for m in muxes}
    svc_mux = {s["uuid"]: s.get("multiplex_uuid")
               for s in api("mpegts/service/grid", limit=1000, hidemode="none")["entries"]}
    off = []
    for ch in api("channel/grid", limit=1000, all=1)["entries"]:
        mux_ids = {svc_mux.get(u) for u in ch.get("services", [])}
        if not mux_ids or not mux_ids <= ours:
            continue  # not purely an Airspy channel; leave it alone
        want = current_mux in mux_ids
        if ch.get("enabled") != want:
            api("idnode/save", node={"uuid": ch["uuid"], "enabled": want})
        if not want:
            off.append(ch.get("name"))
    if off:
        print(f"  disabled channels from other RF channels: {', '.join(off)}")


if __name__ == "__main__":
    main()
