#!/usr/bin/env python3
"""Create (once) a streaming-only Tvheadend account for players and print the
URLs they need.

The "viewer" account can stream and read the guide but has no web UI, DVR or
admin rights, and is only accepted from private (LAN) addresses. Its password
has persistent authentication enabled, so players use a URL containing
?auth=<code> instead of logging in: the playlist embeds that code in every
stream URL.

The code is also saved to atsc/config/viewer.json for the web TV container.

  tvh_viewer.py --host 192.168.1.5
"""
import argparse, json, os, secrets
from tvh_api import HERE, api_client

USER = "viewer"
LAN = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.0/8"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True, help="LAN address clients use to reach this machine")
    ap.add_argument("--url", default="http://127.0.0.1:9981")
    a = ap.parse_args()
    api = api_client(a.url)

    if not any(e.get("username") == USER for e in api("access/entry/grid", limit=500)["entries"]):
        api("access/entry/create", conf={
            "enabled": True, "username": USER, "prefix": LAN,
            "streaming": ["basic", "advanced", "htsp"], "dvr": [], "webui": False, "admin": False,
            "comment": "players on the LAN (created by atsc.sh urls)"})
        print(f"created access entry '{USER}' (streaming only, LAN addresses)")

    pw = [e for e in api("passwd/entry/grid", limit=500)["entries"] if e.get("username") == USER]
    if not pw:
        api("passwd/entry/create", conf={
            "enabled": True, "username": USER, "password": secrets.token_urlsafe(16),
            "auth": ["enable"], "comment": "persistent auth code for player URLs"})
        pw = [e for e in api("passwd/entry/grid", limit=500)["entries"] if e.get("username") == USER]
        print(f"created password entry for '{USER}' with persistent authentication")
    code = pw[0].get("authcode")
    if not code:
        api("idnode/save", node={"uuid": pw[0]["uuid"], "auth": ["enable"]})
        code = next(e for e in api("passwd/entry/grid", limit=500)["entries"]
                    if e.get("username") == USER).get("authcode")

    path = os.path.join(HERE, "config", "viewer.json")
    old = os.umask(0o077)
    try:
        with open(path, "w") as f:
            json.dump({"user": USER, "auth": code}, f)
    finally:
        os.umask(old)

    base = f"http://{a.host}:9981"
    print(f"""
Playlist (M3U, all enabled channels):  {base}/playlist/auth/channels.m3u?auth={code}
Guide (XMLTV):                         {base}/xmltv/channels?auth={code}
One channel, e.g. 8.1:                 {base}/stream/channelnumber/8.1?auth={code}
Web UI (admin):                        {base}/
Kodi / HTSP clients:                   {a.host}, port 9982 (use a Tvheadend user with HTSP rights)
Web TV (browser):                      http://{a.host}/""")


if __name__ == "__main__":
    main()
