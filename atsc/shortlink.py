#!/usr/bin/env python3
"""Short links for players outside the LAN, so nobody has to type the long auth code:

  http://<host>[:port]/<code>        Tvheadend's playlist (all channels): a redirect
                                     to http://<host>:9981/...?auth=...
  http://<host>[:port]/<code>/8.1    one channel (for players without a channel
                                     list, e.g. VLC on Apple TV), relayed from
                                     Tvheadend with the channel's name in an
                                     Icy-Name header: VLC shows that as the title
                                     ("8.1 KGW"), where a redirect would leave
                                     it showing just "8.1" from the URL

Anything else gets a 404. Whoever knows the short link can watch, same as with
the long one.

The code is derived from the viewer's auth code (atsc/config/viewer.json, re-read
on every request): it never changes unless that code is revoked, which also
kills the short link, and it's not in git. The redirect keeps the host the
player used and swaps in Tvheadend's port.

Relaying a channel costs next to no CPU (a socket copy at 2-12 Mbps); the
viewer shows up in Tvheadend as this host, and in this container's log with its
real address.

  shortlink.py            serve (SHORT_PORT, default 9980)
  shortlink.py --code     print the short code
"""
import base64, hashlib, hmac, json, os, re, sys, time, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("SHORT_PORT", "9980"))
TVH_PORT = os.environ.get("TVH_PUBLIC_PORT", "9981")   # Tvheadend's port as players outside see it
TVH = os.environ.get("TVH_URL", "http://127.0.0.1:9981")  # Tvheadend from this host
HOST = re.compile(r"^(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(:\d+)?$")
CHANNEL = re.compile(r"^\d+(\.\d+)?$")


def auth_code():
    with open(os.path.join(HERE, "config", "viewer.json")) as f:
        return json.load(f)["auth"]


def short_code(auth):
    """7 lowercase letters/digits, stable for a given auth code."""
    return base64.b32encode(hashlib.sha256(f"shortlink:{auth}".encode()).digest()).decode().lower()[:7]


def channel_name(number, auth):
    """'8.1 KGW' from Tvheadend's playlist, or just the number."""
    try:
        with urllib.request.urlopen(f"{TVH}/playlist/auth/channels.m3u?auth={auth}", timeout=5) as r:
            m3u = r.read().decode("utf-8", "replace")
    except OSError:
        return number
    for chno, name in re.findall(r'tvg-chno="([^"]*)"[^,\n]*,([^\n]*)', m3u):
        if chno == number and name.strip():
            return f"{number} {name.strip()}"
    return number


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        auth = auth_code()
        m = HOST.match(self.headers.get("Host", ""))
        code, _, channel = self.path.split("?")[0].strip("/").partition("/")
        if not (m and hmac.compare_digest(code, short_code(auth)) and (not channel or CHANNEL.match(channel))):
            print(f"shortlink: {self.client_address[0]} miss", file=sys.stderr, flush=True)
            self.send_error(404)
            return
        if channel:
            self.relay(channel, auth)
            return
        print(f"shortlink: {self.client_address[0]} -> playlist", file=sys.stderr, flush=True)
        self.send_response(302)
        self.send_header("Location", f"http://{m.group(1)}:{TVH_PORT}/playlist/auth/channels.m3u?auth={auth}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def relay(self, channel, auth):
        name = channel_name(channel, auth)
        if self.command == "HEAD":   # don't tune for a HEAD
            up = None
        else:
            try:
                up = urllib.request.urlopen(f"{TVH}/stream/channelnumber/{channel}?auth={auth}", timeout=30)
            except urllib.error.HTTPError as e:   # e.g. 400: no such channel (not in the last scan)
                print(f"shortlink: {self.client_address[0]} -> {name}: Tvheadend said {e.code}",
                      file=sys.stderr, flush=True)
                self.send_error(e.code)
                return
            except OSError as e:
                self.send_error(502, str(e))
                return
        self.send_response(200)
        self.send_header("Content-Type", up.headers.get("Content-Type", "video/mp2t") if up else "video/mp2t")
        self.send_header("Icy-Name", name.encode("latin-1", "replace").decode("latin-1"))
        self.end_headers()
        if not up:
            return
        print(f"shortlink: {self.client_address[0]} -> {name}", file=sys.stderr, flush=True)
        start = time.monotonic()
        with up:
            try:
                while chunk := up.read(65536):
                    self.wfile.write(chunk)
            except OSError:   # viewer gone, or the stream stalled
                pass
        print(f"shortlink: {self.client_address[0]} stopped {name} after {time.monotonic() - start:.0f} s",
              file=sys.stderr, flush=True)

    do_HEAD = do_GET

    def log_message(self, fmt, *args):
        pass   # the default log line would contain the code


if __name__ == "__main__":
    if sys.argv[1:] == ["--code"]:
        print(short_code(auth_code()))
    else:
        print(f"shortlink: serving on port {PORT}", file=sys.stderr, flush=True)
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
