# ATSC TV decode/streamer!

Free over-the-air TV (US ATSC 1.0) received with an **Airspy R2** software
defined radio, decoded entirely in software, and served to every device on the
local network: a **web page** with channels, reception and a player, plus
[Tvheadend](https://tvheadend.org) for VLC, Kodi, Jellyfin, Stremio and other
players. Pick a channel anywhere and the radio retunes on demand.

```
                                                             ┌─► webtv :80  (browser page, H.264 for browsers)
antenna ─► Airspy R2 ─► atsc-rx (on-demand tuner) ◄─ /rf/<n> ─┤
            (USB)       GNU Radio gr-dtv            MPEG-TS  └─► Tvheadend :9981 HTTP (M3U, XMLTV, streams)
                        127.0.0.1:5600                                     :9982 HTSP (Kodi)
```

Everything runs in Docker, started by a systemd user unit, and the whole setup
is reproducible from this repo with two scripts.

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Install](#install)
- [The web page](#the-web-page)
- [Other players](#other-players): VLC, Kodi, Jellyfin, Stremio
- [Channels and reception](#channels-and-reception): scans, rescan, antenna
- [Configuration](#configuration)
- [Operations](#operations): status, logs, updates, uninstall
- [Troubleshooting](#troubleshooting)
- [Remote access (away from home)](#remote-access-away-from-home): VLC and Apple TV over the internet
- [Repository layout](#repository-layout)
- [Receiver internals](#receiver-internals)

## How it works

A broadcast **RF channel** (6 MHz wide, e.g. RF 26 at 545 MHz) carries one
MPEG transport stream of about 19.4 Mbps holding several **virtual channels**
(8.1 KGW, 8.2 Quest, ...). The Airspy can receive one RF channel at a time.

| Container | Role |
|---|---|
| `atsc-rx` | **On-demand tuner.** `GET http://127.0.0.1:5600/rf/<n>` tunes the Airspy to RF channel *n*, runs the GNU Radio ATSC decoder and streams that RF channel's transport stream; another RF channel retunes; with nobody watching for 10 s it stops decoding. It also **scans for channels**: every time the stack starts, and when asked (`POST /scan`, from the web page or `./atsc.sh scan`). |
| `tvheadend` | One IPTV mux per receivable RF channel. Reads the broadcasters' channel list and guide (PSIP), serves playlist, guide and streams to players. |
| `webtv` | The web page on port 80: channel list with reception, an optional in-page player (transcodes to H.264/AAC on the GPU), live tuner status, scan results and a **Rescan** button. After every finished scan it updates Tvheadend, so the channel list follows reception by itself. |

So **every channel that can currently be received is always listed**, in the
web page and in every player. Picking one retunes the radio (a second or two)
and the picture starts. With one Airspy:

- Any number of viewers can watch channels **on the same RF channel** at once
  (e.g. 8.1 and 8.2).
- While someone watches, a channel on a **different** RF channel is refused
  until they stop (the page says so).
- Background jobs (guide grabbing) have lower priority and wait for the tuner;
  a scan stops all viewing for its duration (about 2 minutes).

Limits:

- **ATSC 1.0 only.** ATSC 3.0 (NextGen TV) stations show up in a scan as
  "signal without 8VSB pilot" and can't be decoded.
- **Multipath-sensitive.** GNU Radio's equalizer is much simpler than a TV's
  tuner chip; channels with strong reflections won't lock. Antenna placement
  matters more than with a TV.
- **CPU-bound.** Real-time decoding takes about 3 of the 4 threads of the
  2-core laptop this was built on (see [Receiver internals](#receiver-internals)).
  Watching in an app (VLC, Kodi, ...) adds almost nothing; watching **in the web
  page** adds a transcode, which on that laptop occasionally starves the
  decoder (brief glitches, then automatic recovery). A 4-core machine has
  plenty of headroom for both.

## Requirements

- An **Airspy R2** (10 MSPS) and a UHF/VHF TV antenna.
- An x86-64 Linux host (Debian/Ubuntu tested) with **Docker Engine and the
  compose plugin**. Developed on an Intel i5-7300U (2 cores / 4 threads,
  AVX2). An Intel or AMD GPU (`/dev/dri`) makes the web page's transcode cheap.
- The first user (UID/GID 1000): the decoder and Tvheadend containers run as
  `1000:1000` so files in `config/` and `recordings/` stay owned by that user.

## Install

```sh
git clone <this repo> ~/tvheadend && cd ~/tvheadend
sudo ./host-setup.sh   # one-time host prep (needs root)
# log out and back in so the group changes apply
./bootstrap.sh         # everything else, as your normal user
```

`host-setup.sh` (idempotent): installs the Airspy udev rule, adds you to the
`docker`, `plugdev` and `render` groups, enables systemd linger so the stack
starts at boot without a login, and removes `nomodeset` from GRUB so the GPU
(`/dev/dri`) is available.

`bootstrap.sh` (idempotent):

1. Creates `config/`, `recordings/`, `atsc/config/` (with a default
   `atsc-rx.conf`).
2. If there's no Tvheadend config yet, seeds an `admin` account with a random
   password and **prints it once** (stored obfuscated in `config/superuser`).
3. Pulls Tvheadend, builds the `atsc-rx` and `webtv` images, and profiles the
   CPU's SIMD kernels (VOLK) into `atsc/config/volk/`.
4. Installs and starts `~/.config/systemd/user/tvheadend.service`.
5. Creates a streaming-only `viewer` account for players and prints the URLs.
6. Follows the channel scan that runs on every stack start (about 2 minutes)
   and shows the result.

Then open **`http://<host>/`**. Tvheadend's own admin UI is on
`http://<host>:9981` (its first-run wizard can be skipped).

## The web page

`http://<host>/` (port set in `docker-compose.yml`, default 80) works on
phones and desktops and updates live (Server-Sent Events, no reloading):

- **Channels**, grouped by RF channel, each with a **reception** badge:
  green 99–100% (clean), yellow 95–98% (some glitches), orange 80–94%
  (frequent glitches), red below 80% or no signal. 100% means a perfect
  signal: it's 100 minus the packet error rate, measured live for the channel
  being watched and taken from the last scan for the others.
- **Receiver status**: which RF channel is tuned, lock, live reception.
- **Play in this page** (a per-device switch, off by default on phones):
  - On: click a channel to watch it in the page. The broadcast's MPEG-2 is
    transcoded to H.264/AAC (up to 720p) on the GPU; the player buffers about
    2 seconds and reconnects by itself after a reception dropout. Switching to
    a channel on another RF channel retunes the receiver.
  - Off: tapping a channel opens the original broadcast stream in the
    device's player app (e.g. VLC on a phone). Use this on iPhones, whose
    browser can't play this kind of live stream, and whenever the in-page
    player struggles.
- **Rescan channels**: a big button that runs a fresh scan. It's locked for
  everyone while a scan (or the Tvheadend update after it) is running and
  shows progress; TV stops for the ~2 minutes it takes.
- **Scan results** for every RF channel: reception, stations, ripple
  (multipath indicator) and status (available / no lock / too weak / ATSC 3.0).

The page is open to anyone on the LAN (no login).

## Other players

```sh
./atsc.sh urls
```

```
Playlist (M3U, all enabled channels):  http://192.168.1.5:9981/playlist/auth/channels.m3u?auth=<code>
Guide (XMLTV):                         http://192.168.1.5:9981/xmltv/channels?auth=<code>
One channel, e.g. 8.1:                 http://192.168.1.5:9981/stream/channelnumber/8.1?auth=<code>
Web TV (browser):                      http://192.168.1.5/
```

These use a **`viewer`** account in Tvheadend that can only stream (no web UI,
recording or admin rights) and is only accepted from private LAN addresses
(10/8, 172.16/12, 192.168/16). Its password uses Tvheadend's *persistent
authentication*: the `auth` code in the URL authenticates, and the playlist
embeds it in every channel URL. Treat the code like a password for watching
TV; to revoke it, disable or delete the `viewer` entry under
*Configuration → Users → Passwords* and run `./atsc.sh urls` again.

These streams are the broadcast MPEG-2 video and AC-3 audio, untouched: about
2-4 Mbps for an SD subchannel and 7-12 Mbps for HD. **After a rescan, reload
the playlist in your player** so it picks up channels that appeared or
disappeared.

### VLC (desktop, Android, iOS)

- **Whole channel list:** *Media → Open Network Stream* (Ctrl+N), paste the
  playlist URL. Open the playlist view (Ctrl+L) to switch channels.
- **Single channel:** paste the `stream/channelnumber/<n>` URL instead.
- Mobile: *More → New stream* (Android) or *Network → Open Network Stream*
  (iOS), or just tap a channel in the web page with its player switched off.
- 1080i channels: enable *Video → Deinterlace → On* to remove combing.
- VLC moves on to the next playlist entry when a stream ends; stop playback
  when you're done so the tuner is free.

### Kodi

Install the **Tvheadend HTSP Client** (`pvr.hts`) add-on, host `<host>`, HTTP
port 9981, HTSP port 9982, with a Tvheadend user that has streaming rights
including *HTSP* (create one under *Configuration → Users*). Kodi gets
channels, guide and timeshift natively.

### Jellyfin / Plex / other IPTV apps

Anything that accepts an M3U tuner and an XMLTV guide works with the two URLs
above, e.g. Jellyfin: *Dashboard → Live TV → Tuner devices → M3U Tuner*
(playlist) and *TV guide data providers → XMLTV* (guide). Tell the app it has
**one tuner**.

### Stremio

Stremio needs a live-TV add-on; a self-hosted one that takes an M3U playlist
plus an XMLTV guide is
[M3U-XCAPI-EPG-IPTV-Stremio](https://github.com/Inside4ndroid/M3U-XCAPI-EPG-IPTV-Stremio):
run it (port 7000), choose **Direct M3U / EPG**, paste the Playlist and Guide
URLs, and install the generated `manifest.json` in Stremio. Stremio only
installs add-ons over HTTPS except from `http://localhost`, so on other devices
the add-on needs to sit behind a LAN reverse proxy with a trusted certificate.
(Not deployed or tested by this repo.)

## Channels and reception

Which channels exist is decided by a **scan**: every RF channel that locks
cleanly (at most 10% packet errors) becomes available, everything else is
hidden. A fresh scan runs:

- **every time the stack starts** (boot, `systemctl --user restart tvheadend`,
  `./bootstrap.sh`), but not when Docker merely restarts a crashed container;
- when you press **Rescan channels** in the web page;
- with `./atsc.sh scan` on the server.

After any scan, `webtv` updates Tvheadend: RF channels that lock get a mux and
their channels appear; ones that no longer lock are hidden (kept, so they come
back unchanged when reception improves). Rescan after moving the antenna.

```sh
./atsc.sh scan            # all RF channels 2-36 (about 2 minutes)
./atsc.sh scan 10 24      # just these; updates those entries
./atsc.sh follow          # watch a running scan (e.g. the start-up one) finish
./atsc.sh list            # show the last scan
./atsc.sh sync 20         # re-apply the last scan, accepting up to 20% errors
```

Reading a scan:

```
 RF  MHz  ripple  errors  stations
 10  195   6.4dB  100.0%  no lock
 22  521   7.5dB    2.0%  22.1 ION, 22.2 Bounce, 22.3 Laff, ...
 24  533   4.5dB   17.9%  2.1 KATU, 2.2 KUNP, 2.3 Comet, 32.1 KRCW
 26  545   4.2dB    1.1%  8.1 KGW, 8.2 Quest, 8.3 Crime, 49.2 Mystery, 49.4 CourtTV
 30  569       -       -  signal without 8VSB pilot (ATSC 3.0?)
```

- **RF / MHz:** the physical channel; virtual numbers can differ (RF 24
  carries 2.x and 32.1 here).
- **ripple:** how uneven the signal is across the 6 MHz channel. Under ~8 dB
  is clean; over ~10 dB means multipath (reflections). Low ripple with no lock
  usually means the signal is too weak.
- **errors:** packet error rate while decoding for a few seconds (the web page
  shows this as reception = 100 − errors).
- **stations:** the station's own channel list (PSIP).

Reception varies from scan to scan, especially for marginal stations (above,
RF 24 is just over the 10% cut-off). Move or re-aim the antenna and rescan
just the channels you care about to compare. VHF channels (RF 2-13) need an
antenna with VHF elements; many indoor antennas are UHF-only.

## Configuration

`atsc/config/` holds the receiver's state (bind-mounted into `atsc-rx` as
`/config`, not tracked by git): `atsc-rx.conf` (settings below),
`channels.json` (last scan), `viewer.json` (the players' auth code) and
`volk/volk_config` (SIMD profile for this CPU).

`atsc-rx.conf`; an environment variable of the same name overrides each:

| Setting | Default | Meaning |
|---|---|---|
| `GAIN` | 11 | Airspy linearity gain 0-21 (same scale as `airspy_rx -g`). 11 is clean for strong local stations; higher helps weak ones until the front end overloads (around 13+ here). It doesn't fix multipath. |
| `SCAN_ON_START` | 1 | Scan whenever the container is created (stack start). 0 keeps the last scan. |
| `LISTEN` | `127.0.0.1:5600` | Tuner address (the Tvheadend mux helper and `webtv` use the default). |
| `IDLE_STOP` | 10 | Seconds without viewers before decoding stops. |
| `LOCK_TIMEOUT` | 10 | Seconds without transport stream before a stream is dropped (no lock). |

Changes take effect after `docker compose restart atsc-rx` (a restart keeps
the container, so it doesn't rescan).

`webtv` settings are environment variables in `docker-compose.yml`: `PORT`
(80), `HEIGHT` (720, the in-page stream's maximum resolution) and `BITRATE`
(3M). Apply with `docker compose up -d webtv`.

For debugging, the receiver can decode one RF channel straight to a file (stop
the tuner first; it owns the Airspy):

```sh
docker compose stop atsc-rx
docker compose run --rm atsc-rx --channel 24 --out /config/rf24.ts   # Ctrl+C to stop
docker compose start atsc-rx
```

## Operations

```sh
./atsc.sh status                         # tuner state, containers, recent tuner log
systemctl --user status tvheadend        # the whole stack
systemctl --user restart tvheadend       # restart everything (runs a fresh scan)
docker logs -f atsc-rx                   # tunes, scans, viewers, decoder restarts
docker logs -f webtv                     # web viewers, Tvheadend updates after scans
curl -s http://127.0.0.1:5600/status     # {"rf": 26, "locked": true, "errors_pct": 0.4, "scan": {...}, ...}
```

**Update** (after `git pull`, or for a newer Tvheadend image): `./bootstrap.sh`.

**Uninstall:**

```sh
./bootstrap.sh uninstall           # stop; remove unit, containers, images, generated files
./bootstrap.sh uninstall --purge   # ...and Tvheadend config, recordings, atsc/config
sudo ./host-setup.sh --uninstall   # remove the udev rule
```

`uninstall` keeps `config/`, `recordings/` and `atsc/config/` unless `--purge`
is given. Group memberships, linger and the GRUB change are deliberately left
in place.

## Troubleshooting

| Symptom | Check |
|---|---|
| A channel is missing | Its RF channel didn't lock in the last scan (scan table in the page, or `./atsc.sh list`); for other players, also reload the playlist. |
| "Receiver is busy" / channel refused | Someone is watching a channel on another RF channel. One Airspy, one RF channel at a time. |
| Everything refused for ~2 minutes after a start | The start-up scan is running; the page shows its progress. |
| In-page player buffers or glitches | Live reception badge low: signal problem (antenna). Badge fine but `atsc-rx` logs runs of `O`: the host is short of CPU while transcoding; watch in an app instead (switch the page's player off) or lower `HEIGHT`. |
| `atsc-rx` logs "restarting the decoder" | Its watchdog recovering from a burst of dropped samples (the GNU Radio equalizer doesn't recover by itself). Occasional is fine. |
| Player gets HTTP 401 | Wrong or revoked `auth` code (`./atsc.sh urls`), or the player isn't on a private LAN address. |
| iPhone page won't play | Expected: turn "Play in this page" off and tap a channel to open it in VLC. |
| Picture combing on motion (apps) | 1080i content; enable deinterlacing in the player. The web page deinterlaces for you. |

## Remote access (away from home)

Optional, and off until you set it up: players outside the LAN (VLC on a
laptop or phone elsewhere, an Apple TV at a friend's) can watch through two
router port forwards and short links served by the `shortlink` container.
Everything about remote access is in this section.

### How it works

`shortlink` (a service in `docker-compose.yml`, code in `atsc/shortlink.py`)
listens on port 9980 and answers two kinds of short link:

- `/<code>` **redirects to Tvheadend's playlist**,
  `http://<public IP>:9981/playlist/auth/channels.m3u?auth=<auth code>`.
- `/<code>/8.1` **relays one channel** from Tvheadend and adds the channel's
  name in an `Icy-Name` header. VLC shows that as the title ("22.1 ION"); a
  redirect would leave it showing just "22.1" from the URL. Relaying is a
  plain socket copy, well under 1% CPU.

Anything else on 9980 gets a 404. `<code>` is derived from the `viewer`
account's auth code, so it isn't stored anywhere or in git, and it only
changes when that auth code is revoked. `./atsc.sh urls` prints it on its last
line, `Short link (away from home)`.

Only the two forwarded ports are reachable from the internet: 9981
(Tvheadend: playlist, streams, and its admin login, which is protected by the
random `admin` password from install) and 9980 (`shortlink`). The web page
(port 80, no login) and HTSP (9982) stay LAN-only; never forward port 80 to
the web page.

### Setup

1. **Give this machine a fixed LAN address** in the router (DHCP
   reservation), so the forwards keep pointing at it.
2. **Add two port forwards** (TCP) to that address:

   | Router (external) | To this machine | For |
   |---|---|---|
   | 9981 | 9981 | Tvheadend: the playlist link and its channels |
   | 9980 | 9980 | `shortlink`: all short links |

   One-channel short links only need 9980; the playlist link redirects to
   9981. Keep the external ports the same as the internal ones, and don't use
   external port 80 (many routers, UniFi included, answer on it themselves).
   On UniFi gateways the rules are under *Settings → Routing → Port
   Forwarding*, or *Settings → Policy Engine → Port Forwarding* in Network
   9.4 and later.
3. **Let the `viewer` account in from anywhere.** It only accepts private LAN
   addresses by default. In Tvheadend (`http://<host>:9981`), *Configuration
   → Users → Access Entries → viewer*, set **Allowed networks** to
   `0.0.0.0/0,::/0` and save. Without this, outside players get HTTP 401.
4. **Check from the outside:**

   ```sh
   curl -s https://ifconfig.co/port/9981   # "reachable": true
   curl -s https://ifconfig.co/port/9980
   ```

   If they stay unreachable with the forwards in place, compare the router's
   WAN address with your public IP (`curl -s https://ifconfig.co`). A WAN
   address in 100.64.0.0/10 means carrier-grade NAT: the ISP doesn't let
   incoming connections through, and port forwarding can't work.

### Links

| Link | Opens | For |
|---|---|---|
| `http://<public IP>:9980/<code>` | the playlist (every channel) | VLC on computers and phones |
| `http://<public IP>:9980/<code>/8.1` | one channel, by number, with its name | VLC on Apple TV; any player |

- The channel numbers are in the web page and in `./atsc.sh list`.
- The long URLs work too, from outside: the playlist URL and the
  `stream/channelnumber/<n>` URL from `./atsc.sh urls`, with this machine's
  LAN address replaced by your public IP. A long one-channel URL plays
  without the channel's name.
- A dynamic-DNS name works in place of the IP; the playlist redirect keeps
  whatever address the player used.
- The links work from home too, through the router's NAT loopback (UniFi does
  this), which is a quick way to test them.

### Watching

- **VLC on a computer or phone:** open the playlist link (*Media → Open
  Network Stream*, Ctrl+N; *More → New stream* on Android, *Network* on
  iOS). The playlist view (Ctrl+L) switches channels.
- **VLC on Apple TV:** use **one-channel links**. VLC there loads the
  playlist but doesn't play its entries. Enter a link in the *Network Stream*
  tab. Typing is easier on an iPhone (tap the "Apple TV Keyboard"
  notification) or in a browser: VLC's *Remote Playback* tab shows an address
  to open on a phone or computer on the same Wi-Fi, where links can be
  pasted. Every link opened stays in the *Network Stream* list, so opening
  one per channel once leaves a channel menu; the player shows the channel's
  number and name.

### Limits

- **Anyone with a link can watch.** The links are plain HTTP, so the code is
  readable on the network path too. Share them only with people you trust,
  and revoke them if they spread (below).
- **Upload bandwidth:** each remote viewer gets the untouched broadcast,
  2-4 Mbps for SD and 7-12 Mbps for HD, from your home upload. Transcoding to
  something smaller would starve the receiver on this CPU.
- **One tuner:** a remote viewer holds the Airspy on their RF channel. Until
  they stop, nobody (at home or away) can watch a channel on a different RF
  channel.
- **Channels come and go with scans** (every stack start rescans): a link
  for a channel that's missing from the last scan fails until it's back.
- **Public IP changes** break the links. Use dynamic DNS (built into most
  routers, UniFi included) and hand out the name instead of the IP.

### Who's watching

```sh
docker logs -f shortlink                       # client IP -> playlist / channel (and when it stopped), or "miss"
docker logs tvheadend 2>&1 | grep subscription # streams started and stopped: channel, client IP, player
```

Tvheadend sees relayed one-channel viewers as this host; the `shortlink` log
has their real address. Devices at home that use the public address show up
as the router's LAN address (NAT loopback). A client that keeps fetching the
playlist but never starts a stream is a player that can't use the playlist
(VLC on Apple TV): send it one-channel links.

### Revoking and closing

- **New links** (one was shared too widely): in Tvheadend, *Configuration →
  Users → Passwords*, delete the `viewer` entry, then run `./atsc.sh urls`.
  It creates a new auth code and prints the new short code. Every remote
  player needs the new links. Players at home that use the web page are
  unaffected.
- **Close it:** remove both port forwards and set the `viewer` entry's
  Allowed networks back to `10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.0/8`.
  `shortlink` can keep running; without the forwards it's only reachable on
  the LAN.

### Settings

Environment variables of the `shortlink` service in `docker-compose.yml`;
apply with `docker compose up -d shortlink`:

| Variable | Default | Meaning |
|---|---|---|
| `SHORT_PORT` | 9980 | Port the short links are served on. |
| `TVH_PUBLIC_PORT` | 9981 | External port the router forwards to Tvheadend; the playlist redirect points there. |

### Troubleshooting

| Symptom | Check |
|---|---|
| Outside player can't connect | The forwards: one-channel links need 9980, the playlist link also 9981. `curl -s https://ifconfig.co/port/9980` (and `9981`) from this machine. |
| HTTP 401 | The `viewer` entry's *Allowed networks* isn't open (Setup, step 3), or the auth code was revoked. |
| Short link gives 404 | Only `/<code>` and `/<code>/<channel number>` exist; check the code with `./atsc.sh urls`. `docker logs shortlink` shows each miss. |
| One-channel link gives 400 | That channel isn't in the last scan (`./atsc.sh list`): its RF channel didn't lock. |
| VLC on Apple TV loads, nothing plays | It can't play playlist entries; use one-channel links. |
| Player shows only the number ("22.1") | It's using a long `:9981` URL; the short one-channel link carries the name. |

## Repository layout

| Path | What |
|---|---|
| `docker-compose.yml` | `atsc-rx`, `tvheadend`, `webtv`; host networking |
| `atsc.sh` | scan / follow / sync / list / status / urls |
| `bootstrap.sh` | install / update / uninstall for the current user |
| `host-setup.sh` | one-time root setup (udev, groups, linger, GRUB) |
| `systemd/tvheadend.service` | user unit template (bootstrap fills in the path) |
| `udev/60-airspy.rules` | Airspy device permissions (group `plugdev`) |
| `atsc/Dockerfile` | receiver image (Debian slim + GNU Radio libraries, no GUI deps) |
| `atsc/atsc_rx.py` | the receiver: on-demand tuner and scans (`--serve`), one-shot decoding |
| `atsc/atsc_scan.py` | the scanner (used by the tuner; also runnable on its own) |
| `atsc/tvh_sync.py` | makes Tvheadend match the last scan |
| `atsc/tvh_pipe.py` | Tvheadend mux helper: copies a tuner stream, reconnects after tuner restarts |
| `atsc/tvh_viewer.py` | creates the `viewer` account, prints player URLs |
| `atsc/tvh_api.py` | small Tvheadend API client |
| `web/webtv.py`, `web/Dockerfile` | the web page and its transcoding |
| `atsc/config/` | receiver settings, scan results, auth code, VOLK profile (git-ignored) |
| `config/`, `recordings/` | Tvheadend state and DVR output (git-ignored) |

No passwords are stored in tracked files: the admin and viewer credentials are
generated at install time into the git-ignored `config/` and `atsc/config/`.

## Receiver internals

`atsc/atsc_rx.py` is GNU Radio's gr-dtv ATSC receiver with a rebuilt front
end, because the stock chain only reaches 0.79x real time on the i5-7300U:

| Stage | Stock gr-dtv | Here |
|---|---|---|
| Matched filter + resample (10 → 11.85 MSPS) | `pfb_arb_resampler` | 32/27 rational polyphase RRC (one dot product per output) |
| Carrier recovery | `atsc_fpll` (per-sample sincos/atan2 loop) | Feed-forward: VOLK rotator puts the pilot at DC, a centred moving average estimates its phase, de-rotate. Normalised by pilot power, so the AGC starts at the right level and stations lock in ~0.5 s |
| DC (pilot) removal | `dc_blocker_ff(4096)` | one-pole IIR |
| Sync, equalizer, Viterbi, RS, ... | gr-dtv | gr-dtv (unchanged) |

Result: about **1.15x real time**, using roughly 3 of 4 hardware threads.

Keeping it real-time on a busy machine:

- A **2 s sample buffer** after the Airspy source rides out CPU stalls
  (SoapyAirspy's own ring is only ~52 ms and is dropped whole on overflow).
- `cpu_shares` gives the decoder container priority; the web transcode runs
  at `nice 19`.
- A **watchdog** watches the live packet error rate: dropped samples can
  leave the equalizer diverged (synced, but every packet uncorrectable), so
  after 2 s of ≥80% errors it rebuilds the decoder on the same channel while
  viewers stay connected.

The tuner wraps this in a small threaded HTTP server: the flowgraph writes the
transport stream into a pipe (`file_descriptor_sink`), and a reader thread
counts errored packets (live reception) and fans the stream out to every
client of the current RF channel. A different RF channel, or a scan, stops the
flowgraph and builds a new one (a clean restart, so no packets from the old
channel leak into the new stream). Tvheadend reads each RF channel through
`atsc/tvh_pipe.py` with *respawn* on, because Tvheadend never reconnects a
plain HTTP stream that ends: if the tuner restarts, the helper reconnects.

Offline decoding of a recording: `atsc_rx.py --iq capture.iq --out out.ts`
(int16 IQ at 10 MSPS, as written by `airspy_rx -t 2`).
