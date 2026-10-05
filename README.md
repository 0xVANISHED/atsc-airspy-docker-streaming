# ATSC TV decode/streamer!

Free over-the-air TV (US ATSC 1.0) received with an **Airspy R2** software
defined radio, decoded entirely in software, and served to every device on the
local network by [Tvheadend](https://tvheadend.org): as an M3U playlist with a
programme guide for VLC, Stremio, Jellyfin and other players, or over HTSP
for Kodi. Switch channels in your player; the radio retunes on demand.

```
antenna ─► Airspy R2 ─► atsc-rx (on-demand tuner) ◄── HTTP /rf/<n> ── Tvheadend ─► players on the LAN
            (USB)       GNU Radio gr-dtv           ── MPEG-TS ──────►  :9981 HTTP (M3U, XMLTV, streams)
                        127.0.0.1:5600                                 :9982 HTSP (Kodi)
```

Everything runs in Docker, started by a systemd user unit, and the whole setup
is reproducible from this repo with two scripts.

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Install](#install)
- [Watching on the local network](#watching-on-the-local-network): VLC, Stremio, Kodi, Jellyfin
- [Channels and reception](#channels-and-reception): scan, sync, antenna
- [Configuration](#configuration)
- [Operations](#operations): status, logs, updates, uninstall
- [Troubleshooting](#troubleshooting)
- [Repository layout](#repository-layout)
- [Receiver internals](#receiver-internals)

## How it works

A broadcast **RF channel** (6 MHz wide, e.g. RF 26 at 545 MHz) carries one
MPEG transport stream of about 19.4 Mbps holding several **virtual channels**
(8.1 KGW, 8.2 Quest, ...). The Airspy can receive one RF channel at a time.

| Container | Role |
|---|---|
| `atsc-rx` | **On-demand tuner.** `GET http://127.0.0.1:5600/rf/<n>` tunes the Airspy to RF channel *n*, runs the GNU Radio ATSC decoder and streams that RF channel's transport stream. A request for another RF channel retunes; with nobody watching for 10 s it stops decoding (the CPU idles). `GET /status` reports what it's doing. |
| `tvheadend` | One IPTV mux per receivable RF channel, each pointing at the tuner. Reads the broadcasters' channel list and guide (PSIP), and serves the web UI, playlist, guide and streams. |
| `atsc-scan` | Channel scanner (same image, compose `tools` profile), run by `./atsc.sh scan`. |

So **every channel that can be received is always in the playlist**. Picking
one makes Tvheadend open that RF channel's mux, the tuner retunes (1-2 s), and
the picture starts. Tvheadend allows only one input stream on the network (one
Airspy), so:

- Any number of viewers can watch channels **on the same RF channel** at the
  same time (e.g. 8.1 and 8.2).
- While someone watches, a request for a channel on a **different** RF channel
  is refused (Tvheadend reports no free tuner) until they stop.
- Background jobs (guide grabbing, scans) have lower priority and wait for the
  tuner rather than interrupting anyone.

Limits:

- **ATSC 1.0 only.** ATSC 3.0 (NextGen TV) stations show up in a scan as
  "signal without 8VSB pilot" and can't be decoded.
- **Multipath-sensitive.** GNU Radio's equalizer is much simpler than a TV's
  tuner chip; channels with strong reflections (high "ripple" in a scan)
  won't lock. Antenna placement matters more than with a TV.
- **CPU-bound while watching.** Real-time decoding needs roughly 2 cores of a
  2017-era laptop CPU (see [Receiver internals](#receiver-internals)). Don't
  add software transcoding on the same machine.

## Requirements

- An **Airspy R2** (10 MSPS) and a UHF/VHF TV antenna.
- An x86-64 Linux host (Debian/Ubuntu tested) with **Docker Engine and the
  compose plugin**. Developed on an Intel i5-7300U (2 cores / 4 threads, AVX2);
  slower CPUs may not keep up.
- The first user (UID/GID 1000): the containers run as `1000:1000` so files
  in `config/` and `recordings/` stay owned by that user.

## Install

```sh
git clone <this repo> ~/tvheadend && cd ~/tvheadend
sudo ./host-setup.sh   # one-time host prep (needs root)
# log out and back in so the group changes apply
./bootstrap.sh         # everything else, as your normal user
```

`host-setup.sh` (idempotent): installs the Airspy udev rule, adds you to the
`docker`, `plugdev` and `render` groups, enables systemd linger so the stack
starts at boot without a login, and removes `nomodeset` from GRUB so the Intel
GPU (`/dev/dri`) is available for future hardware transcoding.

`bootstrap.sh` (idempotent):

1. Creates `config/`, `recordings/`, `atsc/config/` (with a default
   `atsc-rx.conf`).
2. If there's no Tvheadend config yet, seeds an `admin` account with a random
   password and **prints it once** (stored obfuscated in `config/superuser`).
3. Pulls Tvheadend, builds the `atsc-rx` image, and profiles the CPU's SIMD
   kernels (VOLK) into `atsc/config/volk/`.
4. Installs and starts `~/.config/systemd/user/tvheadend.service`.
5. Scans for channels on a fresh machine (about 2 minutes), otherwise syncs
   Tvheadend with the last scan.
6. Prints the player URLs (see below).

Web UI: `http://<host>:9981`. The first time, Tvheadend's setup wizard may
open; you can skip the tuner/network steps (they're already configured) and use
it to set your own admin password.

## Watching on the local network

Get the URLs:

```sh
./atsc.sh urls
```

```
Playlist (M3U, all enabled channels):  http://192.168.1.5:9981/playlist/auth/channels.m3u?auth=<code>
Guide (XMLTV):                         http://192.168.1.5:9981/xmltv/channels?auth=<code>
One channel, e.g. 8.1:                 http://192.168.1.5:9981/stream/channelnumber/8.1?auth=<code>
```

The first run creates a **`viewer`** account in Tvheadend that can only stream
(no web UI, recording or admin rights) and is only accepted from private LAN
addresses (10/8, 172.16/12, 192.168/16). Its password uses Tvheadend's
*persistent authentication*, so players don't log in: the `auth` code in the
URL authenticates, and the playlist embeds it in every channel URL. Treat the
code like a password for watching TV; to revoke it, disable or delete the
`viewer` entry under *Configuration → Users → Passwords* and run
`./atsc.sh urls` again for a new one.

Streams are the broadcast MPEG-2 video and AC-3 audio, untouched
(`profile=pass`): about 2-4 Mbps for an SD subchannel and 7-12 Mbps for HD,
so wired Ethernet or decent Wi-Fi is enough.

**After a rescan, reload the playlist in your player:** channels that appeared
or disappeared only show up in a freshly loaded playlist. The
`stream/channelnumber/<n>` URLs stay valid as long as that channel exists.

### VLC (desktop, Android, iOS)

- **Whole channel list:** *Media → Open Network Stream* (Ctrl+N), paste the
  playlist URL, Play. Open the playlist view (Ctrl+L) and double-click a
  channel to switch; a channel on another RF channel takes a second or two.
- **Single channel:** paste the `stream/channelnumber/<n>` URL instead.
- Command line: `vlc "http://192.168.1.5:9981/playlist/auth/channels.m3u?auth=<code>"`
- Mobile: *More → New stream* (Android) or *Network → Open Network Stream* (iOS).
- 1080i channels: enable *Video → Deinterlace → On* (or *Yadif (2x)*) to
  remove combing on motion.
- VLC moves on to the next playlist entry when a stream ends; stop playback
  when you're done so the tuner is free for scans and other viewers.

### Stremio

Stremio has no built-in M3U support; live TV comes in through an add-on. A
self-hosted one that takes an M3U playlist plus an XMLTV guide is
[M3U-XCAPI-EPG-IPTV-Stremio](https://github.com/Inside4ndroid/M3U-XCAPI-EPG-IPTV-Stremio):

1. Run the add-on (its README has a Dockerfile; it listens on port 7000).
2. Open its config page (`http://<addon-host>:7000/`), choose **Direct M3U /
   EPG**, and paste the **Playlist** and **Guide** URLs from `./atsc.sh urls`.
3. Install the generated `…/manifest.json` link in Stremio. Channels appear
   under the add-on's *IPTV Channels* catalog, with now/next guide info.

Stremio only installs add-ons over **HTTPS**, except from `http://localhost`
on the same machine. So:

- **Stremio on a PC:** run the add-on on that same PC and install it from
  `http://localhost:7000/...`. Nothing else is needed.
- **Stremio on a TV, phone or another computer:** the add-on must be reachable
  over HTTPS with a certificate the device trusts, e.g. behind a LAN reverse
  proxy with a real certificate for a hostname you own.

Only the add-on needs HTTPS; the TV streams themselves stay plain HTTP from
Tvheadend. (This path isn't deployed or tested by this repo yet.)

### Kodi

Install the **Tvheadend HTSP Client** (`pvr.hts`) add-on, host `<host>`, HTTP
port 9981, HTSP port 9982. It needs a username and password: create a
Tvheadend user with streaming rights (including *HTSP*) under
*Configuration → Users*. Kodi then gets channels, guide and timeshift natively.

### Jellyfin / Plex / other IPTV apps

Anything that accepts an M3U tuner and an XMLTV guide works with the two URLs
above, e.g. Jellyfin: *Dashboard → Live TV → Tuner devices → M3U Tuner*
(playlist URL) and *TV guide data providers → XMLTV* (guide URL). Tell the
app it has **one tuner**.

## Channels and reception

Which channels exist is decided by a **scan**: every RF channel that locks
cleanly becomes available in Tvheadend, everything else is hidden. Rescan
whenever reception changes: a new or moved antenna, a station changing
frequency, or after the FCC repacks channels.

```sh
./atsc.sh scan            # all RF channels 2-36 (about 2 minutes), then sync
./atsc.sh scan 10 24      # just these; updates those entries, then sync
./atsc.sh list            # show the last scan
./atsc.sh sync            # re-apply the last scan to Tvheadend
./atsc.sh sync 20         # ...also accepting channels with up to 20% errors
```

The scan needs the Airspy to itself, so it pauses the tuner (anyone watching
is interrupted) and resumes it afterwards. Example:

```
 RF  MHz  ripple  errors  stations
 10  195   6.8dB  100.0%  no lock
 24  533   3.2dB    5.2%  2.1 KATU, 2.2 KUNP, 2.3 Comet, 32.1 KRCW
 25  539   4.7dB   88.3%  6.1 KOIN-HD, 6.2 GREAT, 6.3 Rewind, 32.2 Antenna, ...
 26  545   4.7dB    0.1%  8.1 KGW, 8.2 Quest, 8.3 Crime, 49.2 Mystery, 49.4 CourtTV
 30  569       -       -  signal without 8VSB pilot (ATSC 3.0?)
```

- **RF / MHz:** the physical channel. Virtual numbers can differ (RF 24
  carries 2.x and 32.1 above).
- **ripple:** how uneven the signal is across the 6 MHz channel. Under ~8 dB
  is clean; over ~10 dB means multipath (reflections). Low ripple with no lock
  usually means the signal is simply too weak.
- **errors:** packet error rate while decoding for a few seconds. Under ~1%
  is clean, a few percent shows occasional glitches, above ~10% is unwatchable
  and isn't enabled by `sync`.
- **stations:** the station's own channel list (PSIP).

What **sync** does in Tvheadend (also run automatically after a scan):

- One mux per RF channel that locked, pointing at the tuner; muxes of RF
  channels that no longer lock are disabled (not deleted) and their channels
  hidden, so they come back unchanged when reception improves.
- Channels are created by the network's *bouquet* with auto-map, numbered as
  broadcast (8.1, 8.2, ...). Only services the station announces are used, so
  hidden placeholder services don't become channels.
- A newly enabled RF channel needs Tvheadend to look at it once to discover
  its services. If the tuner is busy (someone watching), that happens as soon
  as it's free, and the channels then appear by themselves.

**Improving reception:** move or re-aim the antenna, then rescan just the
channels you care about (`./atsc.sh scan 10 25`) and compare ripple and
errors. VHF channels (RF 2-13) need an antenna with VHF elements; many indoor
antennas are UHF-only.

## Configuration

`atsc/config/` holds the receiver's state (bind-mounted into the containers
as `/config`, not tracked by git):

| File | What |
|---|---|
| `atsc-rx.conf` | receiver settings (below) |
| `channels.json` | results of the last scan |
| `volk/volk_config` | SIMD profile for this CPU (generated by `bootstrap.sh`) |

`atsc-rx.conf` settings; an environment variable of the same name overrides
each one:

| Setting | Default | Meaning |
|---|---|---|
| `GAIN` | 11 | Airspy linearity gain 0-21 (same scale as `airspy_rx -g`). 11 is clean for strong local stations; higher helps weak ones until the front end overloads (around 13+ here). It doesn't fix multipath. |
| `LISTEN` | `127.0.0.1:5600` | Tuner address. The Tvheadend muxes point here (`tvh_sync.py`). |
| `IDLE_STOP` | 10 | Seconds without viewers before decoding stops. |
| `LOCK_TIMEOUT` | 10 | Seconds without transport stream before a viewer is disconnected (no lock). |

Changes take effect after `docker compose restart atsc-rx`.

For debugging, the receiver can also decode one RF channel straight to a file
(the tuner must be stopped, since it owns the Airspy):

```sh
docker compose stop atsc-rx
docker compose run --rm atsc-rx --channel 24 --out /config/rf24.ts   # Ctrl+C to stop
docker compose start atsc-rx
```

## Operations

```sh
./atsc.sh status                         # tuner state, containers, recent tuner log
systemctl --user status tvheadend        # the whole stack (both containers)
systemctl --user restart tvheadend
docker logs -f atsc-rx                   # tunes, retunes, viewers, lock problems
docker logs -f tvheadend
curl -s http://127.0.0.1:5600/status     # {"rf": 26, "clients": 1, "mbps": 19.39, "locked": true, ...}
```

**Update** (after `git pull`, or to pick up a newer Tvheadend image):

```sh
./bootstrap.sh
```

**Uninstall:**

```sh
./bootstrap.sh uninstall           # stop; remove unit, containers, images, generated files
./bootstrap.sh uninstall --purge   # ...and Tvheadend config, recordings, atsc/config
sudo ./host-setup.sh --uninstall   # remove the udev rule
```

`uninstall` keeps `config/`, `recordings/` and `atsc/config/` (channels,
accounts, guide, recordings, scan results, settings) unless `--purge` is
given. Group memberships, linger and the GRUB change are deliberately left in
place.

## Troubleshooting

| Symptom | Check |
|---|---|
| A channel doesn't start, or stops after a few seconds | Its RF channel no longer locks: `./atsc.sh status`, `docker logs atsc-rx` ("no transport stream ... no lock"), then `./atsc.sh scan <rf>`. |
| A channel is missing from the playlist | Its RF channel didn't lock in the last scan (`./atsc.sh list`), or the playlist in the player is stale (reload it). |
| "No free tuner" / channel refused | Someone is watching a channel on a different RF channel. One Airspy, one RF channel at a time. |
| Channels of a newly enabled RF channel never appear | Tvheadend is waiting for a free tuner to scan it; stop all players for ~20 s. |
| Player gets HTTP 401 | Wrong or revoked `auth` code (`./atsc.sh urls`), or the player isn't on a private LAN address. |
| `atsc-rx` logs a run of `O` characters | Sample overflows: the CPU couldn't keep up (other load on the host). Short bursts are absorbed by a 0.5 s buffer. |
| `atsc-rx` exits with a USB/device error | Airspy unplugged or in use by something else (`airspy_rx`, a scan); it restarts automatically. |
| Picture combing on motion | 1080i content; enable deinterlacing in the player. |

## Repository layout

| Path | What |
|---|---|
| `docker-compose.yml` | `atsc-rx`, `atsc-scan` (tools profile), `tvheadend`; host networking |
| `atsc.sh` | scan / sync / list / status / urls |
| `bootstrap.sh` | install / update / uninstall for the current user |
| `host-setup.sh` | one-time root setup (udev, groups, linger, GRUB) |
| `systemd/tvheadend.service` | user unit template (bootstrap fills in the path) |
| `udev/60-airspy.rules` | Airspy device permissions (group `plugdev`) |
| `atsc/Dockerfile` | receiver image (Debian slim + GNU Radio libraries, no GUI deps) |
| `atsc/atsc_rx.py` | the receiver: on-demand tuner (`--serve`) and one-shot decoding |
| `atsc/atsc_scan.py` | the scanner |
| `atsc/tvh_sync.py` | makes Tvheadend match the last scan |
| `atsc/tvh_viewer.py` | creates the `viewer` account, prints player URLs |
| `atsc/tvh_api.py` | small Tvheadend API client used by the two above |
| `atsc/config/` | receiver settings, scan results, VOLK profile (git-ignored) |
| `config/`, `recordings/` | Tvheadend state and DVR output (git-ignored) |

No passwords are stored in tracked files: the admin and viewer credentials are
generated at install time into the git-ignored `config/`.

## Receiver internals

`atsc/atsc_rx.py` is GNU Radio's gr-dtv ATSC receiver with a rebuilt front end,
because the stock chain only reaches 0.79x real time on the i5-7300U:

| Stage | Stock gr-dtv | Here |
|---|---|---|
| Matched filter + resample (10 → 11.85 MSPS) | `pfb_arb_resampler` | 32/27 rational polyphase RRC (one dot product per output) |
| Carrier recovery | `atsc_fpll` (per-sample sincos/atan2 loop) | Feed-forward: VOLK rotator puts the pilot at DC, a centred moving average estimates its phase, de-rotate. Normalised by pilot power, so the AGC starts at the right level and stations lock in ~0.5 s |
| DC (pilot) removal | `dc_blocker_ff(4096)` | one-pole IIR |
| Sync, equalizer, Viterbi, RS, ... | gr-dtv | gr-dtv (unchanged) |

Result: about **1.15x real time** using roughly 3 of 4 hardware threads, with
a 0.5 s buffer after the Airspy source so short CPU stalls don't drop samples.

The tuner wraps this in a small threaded HTTP server: the flowgraph writes the
transport stream into a pipe (`file_descriptor_sink`), and a reader thread fans
it out to every client of the current RF channel. A request for another RF
channel stops the flowgraph and builds a new one at the new frequency (a clean
restart, so no packets from the old channel leak into the new stream).

Offline decoding of a recording: `atsc_rx.py --iq capture.iq --out out.ts`
(int16 IQ at 10 MSPS, as written by `airspy_rx -t 2`).
