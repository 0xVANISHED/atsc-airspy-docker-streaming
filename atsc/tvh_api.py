"""Minimal Tvheadend JSON API client shared by the atsc/ helper scripts.

Credentials: TVH_USER/TVH_PASS, else the superuser seeded in ../config/superuser.
"""
import base64, json, os, urllib.parse, urllib.request

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


def api_client(base="http://127.0.0.1:9981", user=None, pw=None):
    if user is None:
        user, pw = credentials()
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
