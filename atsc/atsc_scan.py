#!/usr/bin/env python3
"""Scan the US TV bands for ATSC 1.0 stations with the Airspy.

Pass 1 (spectrum): per RF channel, capture 0.2 s with airspy_rx, look for the
8VSB pilot (lower band edge + 309.44 kHz) and measure in-band ripple: deep
notches across the 6 MHz channel mean multipath, which GNU Radio's equalizer
can't handle beyond ~10 dB (a TV tuner copes better). Re-aim or move the
antenna to bring it down.
Pass 2 (identify): decode a few seconds of each channel with a pilot to read
the PSIP virtual channel table (8.1 KGW, ...) and measure packet errors.

Writes JSON (default /config/channels.json; a scan of selected channels updates
just those entries) and prints a table. The tuner (atsc_rx.py --serve) runs
scans itself via run_scan(); standalone use needs the tuner stopped, since the
Airspy can only be opened once.

  atsc_scan.py              all channels 2-36
  atsc_scan.py 22 26        just these
  atsc_scan.py --quick      spectrum pass only
"""
import argparse, datetime, json, os, subprocess, time
import numpy as np

FS = 10_000_000
NFFT = 8192
TMPDIR = os.environ.get("TMPDIR", "/tmp")
SERVICE_TYPES = {2: "tv", 3: "audio", 4: "data"}


def center_mhz(ch):
    if 2 <= ch <= 4:   return 57 + 6 * (ch - 2)
    if 5 <= ch <= 6:   return 79 + 6 * (ch - 5)
    if 7 <= ch <= 13:  return 177 + 6 * (ch - 7)
    if 14 <= ch <= 36: return 473 + 6 * (ch - 14)
    raise ValueError(ch)


# ---- pass 1: spectrum ------------------------------------------------------

freqs = (np.arange(NFFT) - NFFT // 2) * FS / NFFT


def band(lo, hi):
    return (freqs >= lo) & (freqs <= hi)


def psd_db(iq):
    win = np.hanning(NFFT)
    n = len(iq) // NFFT
    segs = iq[: n * NFFT].reshape(n, NFFT) * win
    p = np.mean(np.abs(np.fft.fftshift(np.fft.fft(segs, axis=1), axes=1)) ** 2, axis=0)
    return 10 * np.log10(p + 1e-20)


def pilot_snr(p, off):
    peak = p[band(off - 15e3, off + 15e3)].max()
    return peak - np.median(p[band(off - 300e3, off + 300e3)])


def spectrum(ch, gain):
    path = os.path.join(TMPDIR, "atsc_scan.iq")
    subprocess.run(["airspy_rx", "-r", path, "-f", str(center_mhz(ch)), "-a", str(FS), "-t", "2",
                    "-g", str(gain), "-n", "3000000"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    try:
        raw = np.fromfile(path, dtype=np.int16)
    finally:
        if os.path.exists(path):
            os.remove(path)
    if raw.size < 4 * NFFT:
        return None
    iq = (raw[0::2] + 1j * raw[1::2]).astype(np.complex64)[1_000_000:]  # skip settling
    p = psd_db(iq)
    pilot = pilot_snr(p, -3e6 + 309.44e3)
    inband = float(np.median(p[band(-2.4e6, 2.4e6)]))
    # ~100 kHz smoothed spectrum across the data band, pilot excluded
    smooth = np.convolve(p, np.ones(81) / 81, mode="same")
    data = band(-2.5e6, 2.5e6) & ~band(-3e6 + 309.44e3 - 100e3, -3e6 + 309.44e3 + 100e3)
    ripple = float(smooth[data].max() - smooth[data].min())
    edge = np.median(np.concatenate([p[band(-4.6e6, -3.4e6)], p[band(3.4e6, 4.6e6)]]))
    if pilot > 10:
        kind = "atsc1"
    elif inband - edge > 6:
        kind = "no-pilot"  # usually ATSC 3.0
    else:
        kind = None
    return {"pilot_db": round(float(pilot), 1), "level_db": round(inband, 1),
            "ripple_db": round(ripple, 1), "kind": kind}


# ---- pass 2: identify via PSIP ---------------------------------------------

def parse_tvct(sec):
    """Terrestrial Virtual Channel Table (A/65) -> (tsid, [channels])."""
    tsid = (sec[3] << 8) | sec[4]
    chans, i = [], 10
    for _ in range(sec[9]):
        if i + 32 > len(sec) - 4:
            break
        name = sec[i:i + 14].decode("utf-16-be", "replace").rstrip("\x00 ")
        major = ((sec[i + 14] & 0x0F) << 6) | (sec[i + 15] >> 2)
        minor = ((sec[i + 15] & 0x03) << 8) | sec[i + 16]
        hidden = bool(sec[i + 26] & 0x10)
        stype = sec[i + 27] & 0x3F
        dlen = ((sec[i + 30] & 0x03) << 8) | sec[i + 31]
        i += 32 + dlen
        if not hidden:
            chans.append({"number": f"{major}.{minor}", "name": name,
                          "type": SERVICE_TYPES.get(stype, f"type {stype}")})
    return tsid, chans


def analyse_ts(data):
    """Packet error rate (second half, after lock) and the first TVCT on PID 0x1FFB."""
    pkts = [data[i:i + 188] for i in range(0, len(data) - 187, 188)]
    tail = pkts[len(pkts) // 2:]
    errors = (sum(1 for p in tail if p[0] != 0x47 or p[1] & 0x80) / len(tail)) if tail else 1.0
    buf, tvct = b"", None
    for p in pkts:
        if p[0] != 0x47 or p[1] & 0x80:
            buf = b""
            continue
        if (((p[1] & 0x1F) << 8) | p[2]) != 0x1FFB or not (p[3] & 0x10):
            continue
        payload = p[4 + (1 + p[4] if p[3] & 0x20 else 0):]
        sections = []
        if p[1] & 0x40:  # payload unit start: pointer field, then a new section
            ptr = payload[0]
            if buf:
                sections.append(buf + payload[1:1 + ptr])
            buf = payload[1 + ptr:]
        elif buf:
            buf += payload
        else:
            continue
        # complete sections at the head of buf
        while len(buf) >= 3 and buf[0] != 0xFF:
            slen = ((buf[1] & 0x0F) << 8) | buf[2]
            if len(buf) < 3 + slen:
                break
            sections.append(buf[:3 + slen])
            buf = buf[3 + slen:]
        for sec in sections:
            if len(sec) > 14 and sec[0] == 0xC8:
                tvct = parse_tvct(sec)
                break
        if tvct:
            break
    return errors, tvct


def identify(ch, gain, dwell):
    from atsc_rx import atsc_receiver, channel_center_hz
    path = os.path.join(TMPDIR, "atsc_scan.ts")
    tb = atsc_receiver(channel_center_hz(ch), gain, path)
    tb.start()
    time.sleep(dwell)
    tb.stop()
    tb.wait()
    del tb
    with open(path, "rb") as f:
        data = f.read()
    os.remove(path)
    return analyse_ts(data)


def print_table(chans):
    print(f"{'RF':>3} {'MHz':>4} {'ripple':>7} {'errors':>7}  stations")
    for c in chans:
        if c["kind"] != "atsc1":
            print(f"{c['rf']:3d} {c['freq_mhz']:4d} {'-':>7} {'-':>7}  signal without 8VSB pilot (ATSC 3.0?)")
            continue
        err = f"{c['errors_pct']}%" if "errors_pct" in c else "-"
        names = ", ".join(f"{s['number']} {s['name']}" for s in c.get("services", []))
        if not names and "errors_pct" in c:
            names = "no lock (multipath?)" if c.get("ripple_db", 0) > 10 else "no lock"
        print(f"{c['rf']:3d} {c['freq_mhz']:4d} {(f"{c['ripple_db']:.1f}dB" if 'ripple_db' in c else '-'):>7} {err:>7}  {names}")


def run_scan(chans=None, gain=11, out="/config/channels.json", quick=False, dwell=4.0,
             progress=lambda **kw: None, log=print):
    """Scan, write results to `out` (a partial scan replaces just its channels),
    return the result dict. progress(phase=, rf=, done=, total=) reports where
    we are. The caller must make sure nothing else is using the Airspy."""
    partial = bool(chans)
    chans = list(chans or range(2, 37))

    log(f"spectrum pass: {len(chans)} channels, gain {gain}")
    found = []
    for i, ch in enumerate(chans):
        progress(phase="spectrum", rf=ch, done=i, total=len(chans))
        r = spectrum(ch, gain)
        if r and r["kind"]:
            found.append({"rf": ch, "freq_mhz": center_mhz(ch), **r})
            log(f"  RF {ch:2d}: {'8VSB pilot' if r['kind'] == 'atsc1' else 'signal, no pilot'}"
                f", ripple {r['ripple_db']} dB")

    atsc1 = [c for c in found if c["kind"] == "atsc1"]
    if not quick and atsc1:
        log(f"identify pass: decoding {len(atsc1)} channels for {dwell:g}s each")
        for i, c in enumerate(atsc1):
            progress(phase="identify", rf=c["rf"], done=i, total=len(atsc1))
            errors, tvct = identify(c["rf"], gain, dwell)
            c["errors_pct"] = round(100 * errors, 1)
            if tvct:
                c["tsid"], c["services"] = tvct

    if out:
        if partial and os.path.exists(out):
            with open(out) as f:
                prev = json.load(f).get("channels", [])
            found = sorted([c for c in prev if c["rf"] not in chans] + found, key=lambda c: c["rf"])
    result = {"scanned": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
              "gain": gain, "channels": found}
    if out:
        tmp = out + ".tmp"
        with open(tmp, "w") as f:
            json.dump(result, f, indent=2)
        os.replace(tmp, out)
        log(f"wrote {out}")
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("channels", nargs="*", type=int, help="RF channels (default 2-36)")
    ap.add_argument("--gain", type=int, default=int(os.environ.get("GAIN", 11)),
                    help="Airspy linearity gain 0-21 (default 11)")
    ap.add_argument("--out", default="/config/channels.json", help="JSON results ('' = don't write)")
    ap.add_argument("--quick", action="store_true", help="spectrum pass only, no station names")
    ap.add_argument("--dwell", type=float, default=4.0, help="seconds decoded per channel (default 4)")
    a = ap.parse_args()
    result = run_scan(a.channels, a.gain, a.out, a.quick, a.dwell, log=lambda m: print(m, flush=True))
    print()
    print_table(result["channels"])


if __name__ == "__main__":
    main()
