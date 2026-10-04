# tvheadend

Free over-the-air TV (US ATSC 1.0) received with an **Airspy R2** software
defined radio, decoded entirely in software, and served to every device on the
local network by [Tvheadend](https://tvheadend.org): as an M3U playlist with a
programme guide for VLC, Stremio, Jellyfin and other players, or over HTSP
for Kodi.

```
antenna ─► Airspy R2 ─► atsc-rx ──── MPEG-TS / UDP ───► Tvheadend ─► players on the LAN
            (USB)       GNU Radio    127.0.0.1:5500+RF    :9981 HTTP (M3U, XMLTV, streams)
                        gr-dtv                            :9982 HTSP (Kodi)
```

Everything runs in Docker, started by a systemd user unit, and the whole setup
is reproducible from this repo with two scripts.

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Install](#install)
- [Watching on the local network](#watching-on-the-local-network): VLC, Stremio, Kodi, Jellyfin
- [Choosing the TV channel](#choosing-the-tv-channel): scan, tune, configuration
- [Operations](#operations): status, logs, updates, uninstall
- [Troubleshooting](#troubleshooting)
- [Repository layout](#repository-layout)
- [Receiver internals](#receiver-internals)

## How it works

A broadcast **RF channel** (6 MHz wide, e.g. RF 26 at 545 MHz) carries one
MPEG transport stream of about 19.4 Mbps holding several **virtual channels**
(8.1 KGW, 8.2 Quest, ...). The receiver demodulates one RF channel at a time
and hands the whole transport stream to Tvheadend, which splits it into
channels, reads the broadcasters' guide data (PSIP), and serves them.

| Container | Role |
|---|---|
| `atsc-rx` | GNU Radio ATSC 1.0 (8VSB) receiver. Reads the Airspy over USB, writes the transport stream to `udp://127.0.0.1:<5500 + RF channel>`. |
| `tvheadend` | Ingests that stream as an IPTV mux with ATSC/PSIP parsing; serves the web UI, playlists, guide and streams. |
| `atsc-scan` | One-off channel scanner (same image, compose `tools` profile), run by `./atsc.sh scan`. |

Limits:

- **One RF channel at a time** (one Airspy, one decoder). All virtual channels
  on that RF channel are available simultaneously. `./atsc.sh tune` switches.
- **ATSC 1.0 only.** ATSC 3.0 (NextGen TV) stations show up in a scan as
  "signal without 8VSB pilot" and can't be decoded.
- **Multipath-sensitive.** GNU Radio's equalizer is much simpler than a TV's
  tuner chip; channels with strong reflections (high "ripple" in a scan)
  won't lock. Antenna placement matters more than with a TV.
- **CPU-bound.** Real-time decoding needs roughly 2 cores of a 2017-era laptop
  CPU (see [Receiver internals](#receiver-internals)). Don't add software
  transcoding on the same machine.

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

1. Creates `config/`, `recordings/`, `atsc/config/`.
2. If there's no Tvheadend config yet, seeds an `admin` account with a random
   password and **prints it once** (stored obfuscated in `config/superuser`).
3. Pulls Tvheadend, builds the `atsc-rx` image, and profiles the CPU's SIMD
   kernels (VOLK) into `atsc/config/volk/`.
4. Installs and starts `~/.config/systemd/user/tvheadend.service`.
5. Registers the configured RF channel in Tvheadend, or on a fresh machine
   scans and tunes the strongest clean station.
6. Prints the player URLs (see below).

Web UI: `http://<host>:9981`. The first time, Tvheadend's setup wizard may
open; you can skip the tuner/network steps (the receiver is already
configured) and use it to set your own admin password.

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

Streams are the broadcast MPEG-2 video and AC-3 audio, untouched (`profile=pass`):
about 2-4 Mbps for an SD subchannel and 7-12 Mbps for HD, so wired Ethernet or
decent Wi-Fi is enough. The playlist only lists channels on the RF channel
currently tuned.

### VLC (desktop, Android, iOS)

- **Whole channel list:** *Media → Open Network Stream* (Ctrl+N), paste the
  playlist URL, Play. Open the playlist view (Ctrl+L) to switch channels.
- **Single channel:** paste the `stream/channelnumber/<n>` URL instead.
- Command line: `vlc "http://192.168.1.5:9981/playlist/auth/channels.m3u?auth=<code>"`
- Mobile: *More → New stream* (Android) or *Network → Open Network Stream* (iOS).
- 1080i channels: enable *Video → Deinterlace → On* (or *Yadif (2x)*) to
  remove combing on motion.

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
(playlist URL) and *TV guide data providers → XMLTV* (guide URL).

## Choosing the TV channel

The receiver's settings live in `atsc/config/` (bind-mounted into the
containers as `/config`, not tracked by git):

| File | What |
|---|---|
| `atsc-rx.conf` | `RF_CHANNEL=26` and `GAIN=11`, written by `./atsc.sh tune` |
| `channels.json` | results of the last scan |
| `volk/volk_config` | SIMD profile for this CPU (generated by `bootstrap.sh`) |

### Scan

```sh
./atsc.sh scan            # all RF channels 2-36, about 2 minutes
./atsc.sh scan 22 24 26   # just these; updates those entries in channels.json
./atsc.sh scan --quick    # spectrum only, no station names
./atsc.sh list            # show the last scan again
```

The scan needs the Airspy to itself, so it pauses the receiver (TV stops for
the duration) and resumes it afterwards. Example:

```
 RF  MHz  ripple  errors  stations
 22  521   4.4dB    1.0%  22.1 ION, 22.2 Bounce, 22.3 Laff, ...
 24  533  22.4dB  100.0%  no lock (multipath?)
 26  545   4.1dB    0.2%  8.1 KGW, 8.2 Quest, 8.3 Crime, 49.2 Mystery, 49.4 CourtTV
 33  587       -       -  signal without 8VSB pilot (ATSC 3.0?)
```

- **RF / MHz:** the physical channel. This is what you tune, not the virtual
  number (RF 32 carries virtual 24.x, for example).
- **ripple:** how uneven the signal is across the 6 MHz channel. Under ~8 dB
  is clean; over ~10 dB means multipath and the station probably won't lock.
  Re-aim or move the antenna and re-scan that channel to compare.
- **errors:** packet error rate while decoding for a few seconds. 0-1% is
  watchable; 100% means no lock.
- **stations:** virtual channels read from the broadcast itself.

### Tune

```sh
./atsc.sh tune 22        # switch to RF 22 (keeps the current gain)
./atsc.sh tune 22 9      # ...with Airspy gain 9
./atsc.sh best           # strongest clean channel from the last scan
./atsc.sh status         # configured channel, containers, stream rate
```

`tune` writes `atsc/config/atsc-rx.conf`, restarts `atsc-rx`, adds the RF
channel to Tvheadend the first time (as mux `RF <n>` on UDP port 5500 + n,
mapping its services to channels), and **enables only that RF channel's
channels**, disabling the rest so players never list channels that can't play.
Switching back later just re-enables them.

### Configuration reference

`atsc-rx` resolves each setting from, in order: command-line flag, environment
variable, `atsc/config/atsc-rx.conf`.

| Setting | Default | Meaning |
|---|---|---|
| `RF_CHANNEL` | (none) | RF channel 2-36. With none set, the receiver waits and logs a hint. |
| `GAIN` | 11 | Airspy linearity gain 0-21 (same scale as `airspy_rx -g`). |
| `UDP_PORT` | 5500 + RF | Where the stream goes; must match the Tvheadend mux URL. |
| `STATS` | 60 | Seconds between `TS xx.xx Mbps` log lines. |

Environment variables are handy for one-offs without touching the config:

```sh
docker compose stop atsc-rx
docker compose run --rm -e RF_CHANNEL=22 atsc-rx --out /config/rf22.ts   # record raw TS
docker compose start atsc-rx
```

**Gain:** 11 is clean for strong local stations. Higher gain helps weak
stations until the Airspy's front end overloads (around 13+ here, with many
strong stations nearby); lowering it doesn't fix multipath.

## Operations

```sh
systemctl --user status tvheadend        # the whole stack (both containers)
systemctl --user restart tvheadend
docker logs -f atsc-rx                   # "TS 19.39 Mbps" = locked, 0 = no signal
docker logs -f tvheadend
./atsc.sh status
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
accounts, guide, recordings, your tuned channel) unless `--purge` is given.
Group memberships, linger and the GRUB change are deliberately left in place.

## Troubleshooting

| Symptom | Check |
|---|---|
| `atsc-rx` logs `TS 0.00 Mbps` | No lock: `./atsc.sh scan <rf>` and look at ripple/errors; antenna; try another RF channel. |
| `atsc-rx` logs a run of `O` characters | Sample overflows: the CPU couldn't keep up (other load on the host). Short bursts are absorbed by a 0.5 s buffer. |
| `atsc-rx` exits with a USB/device error | Airspy unplugged or in use: `docker compose ps`, nothing else running `airspy_rx`; it restarts automatically. |
| Player gets HTTP 401 | Wrong or revoked `auth` code (`./atsc.sh urls`), or the player isn't on a private LAN address. |
| Playlist is empty | No channels enabled: `./atsc.sh tune <rf>` re-registers and enables them. |
| Channels listed but won't play | Receiver is on a different RF channel than the one Tvheadend expects; `./atsc.sh status`, then `./atsc.sh tune`. |
| Picture combing on motion | 1080i content; enable deinterlacing in the player. |

## Repository layout

| Path | What |
|---|---|
| `docker-compose.yml` | `atsc-rx`, `atsc-scan` (tools profile), `tvheadend`; host networking |
| `atsc.sh` | scan / list / tune / best / status / urls |
| `bootstrap.sh` | install / update / uninstall for the current user |
| `host-setup.sh` | one-time root setup (udev, groups, linger, GRUB) |
| `systemd/tvheadend.service` | user unit template (bootstrap fills in the path) |
| `udev/60-airspy.rules` | Airspy device permissions (group `plugdev`) |
| `atsc/Dockerfile` | receiver image (Debian slim + GNU Radio libraries, no GUI deps) |
| `atsc/atsc_rx.py` | the receiver |
| `atsc/atsc_scan.py` | the scanner |
| `atsc/tvh_add_mux.py` | registers an RF channel in Tvheadend; `--exclusive` enables only it |
| `atsc/tvh_viewer.py` | creates the `viewer` account, prints player URLs |
| `atsc/config/` | receiver config, scan results, VOLK profile (git-ignored) |
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
Offline testing: `atsc_rx.py --iq capture.iq --out out.ts` decodes an
`airspy_rx -t 2` (int16 IQ, 10 MSPS) recording.
