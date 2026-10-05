#!/usr/bin/env python3
"""ATSC 1.0 (8VSB) receiver: Airspy R2 -> GNU Radio gr-dtv -> MPEG-TS.

Normally runs as an on-demand tuner (--serve): an HTTP server where
GET /rf/<n> tunes the Airspy to RF channel n and streams that channel's whole
transport stream (~19.4 Mbps, all subchannels). One RF channel at a time: a
request for another channel retunes (newest wins) and the decoder stops when
nobody has been watching for IDLE_STOP seconds. Tvheadend has one IPTV mux per
RF channel pointing here, so picking a channel in any player tunes the radio.
GET /status returns JSON (current channel, clients, rate, live errors, scan
progress); POST /scan[?rf=22,24&quick=1] runs a channel scan in the background
(streams are refused meanwhile). A scan also runs whenever the container is
created (stack start), but not when Docker restarts it after a crash.

Tuned for a 2-core laptop CPU. Differences from stock gr-dtv atsc_rx:
  * RRC matched filter + resample via a 32/27 rational polyphase resampler
    (10 MSPS -> 11.85 MSPS, ~1.1 samples/symbol) instead of pfb_arb_resampler.
  * Feed-forward pilot carrier recovery (VOLK rotator + centred moving average
    of the pilot) instead of atsc_fpll's per-sample sincos/atan2 loop.
  * One-pole IIR DC removal instead of dc_blocker_ff(4096).

Settings: CLI flags, then environment variables, then /config/atsc-rx.conf
(GAIN=11, LISTEN=127.0.0.1:5600, IDLE_STOP=10, LOCK_TIMEOUT=10, SCAN_ON_START=1).

  atsc_rx.py --serve                           # on-demand tuner (the container default)
  atsc_rx.py --channel 26 --out rf26.ts        # decode one channel to a file / udp://host:port / -
  atsc_rx.py --iq rf26.iq --out rf26.ts        # offline, from an airspy_rx -t 2 capture
"""
import argparse, fcntl, json, math, os, queue, re, select, signal, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from gnuradio import gr, blocks, filter, analog, dtv, soapy, network
from gnuradio.dtv.atsc_rx_filter import ATSC_SYMBOL_RATE, ATSC_RRC_SYMS

SAMP_RATE = 10e6
INTERP, DECIM = 32, 27          # 10 MSPS * 32/27 = 11.852 MSPS
PILOT_OFFSET = -3e6 + 309.44e3  # 8VSB pilot relative to channel centre
PILOT_SCALE = 1.0               # pilot-normalised level -> AGC reference (measured)

# libairspy "linearity" gain tables, indexed by 21 - gain (same as airspy_rx -g)
LIN_VGA = [13, 12, 11, 11, 11, 11, 11, 10, 10, 10, 10, 10, 10, 10, 10, 10, 9, 8, 7, 6, 5, 4]
LIN_MIX = [12, 12, 11, 9, 8, 7, 6, 6, 5, 0, 0, 1, 0, 0, 2, 2, 1, 1, 1, 1, 0, 0]
LIN_LNA = [14, 14, 14, 13, 12, 10, 9, 9, 8, 9, 8, 6, 5, 3, 1, 0, 0, 0, 0, 0, 0, 0]


def log(msg):
    print(f"atsc_rx: {msg}", file=sys.stderr, flush=True)


def channel_center_hz(ch):
    if 2 <= ch <= 4:
        return (57 + 6 * (ch - 2)) * 1e6
    if 5 <= ch <= 6:
        return (79 + 6 * (ch - 5)) * 1e6
    if 7 <= ch <= 13:
        return (177 + 6 * (ch - 7)) * 1e6
    if 14 <= ch <= 36:
        return (473 + 6 * (ch - 14)) * 1e6
    raise ValueError(f"RF channel {ch} is outside the US TV bands (2-36)")


class atsc_receiver(gr.top_block):
    """out: udp://host:port, a file path, '-' (stdout), or a GNU Radio sink block."""

    def __init__(self, freq, gain, out, pilot_avg=2048, buffer_ms=2000, iq_file=None,
                 pilot_scale=PILOT_SCALE):
        gr.top_block.__init__(self, "atsc_rx")
        out_rate = SAMP_RATE * INTERP / DECIM

        if iq_file:
            # Offline: interleaved int16 IQ at 10 MSPS (airspy_rx -t 2)
            raw = blocks.file_source(gr.sizeof_short, iq_file, False)
            src = blocks.interleaved_short_to_complex(False, False, 32768.0)
            self.connect(raw, src)
        else:
            src = soapy.source("driver=airspy", "fc32", 1, "", "", [""], [""])
            src.set_sample_rate(0, SAMP_RATE)
            src.set_frequency(0, freq)
            src.set_gain_mode(0, False)
            idx = 21 - max(0, min(21, gain))
            src.set_gain(0, "LNA", LIN_LNA[idx])
            src.set_gain(0, "MIX", LIN_MIX[idx])
            src.set_gain(0, "VGA", LIN_VGA[idx])
            # SoapyAirspy's own ring is fixed at ~52 ms and is dropped whole on
            # overflow, which can derail the ATSC decoder. A large output buffer
            # here (2 s by default) keeps that ring drained through CPU stalls.
            src.set_min_output_buffer(int(SAMP_RATE * buffer_ms / 1000))

        # RRC matched filter + resample. Prototype gain scaled so each polyphase
        # arm matches stock atsc_rx_filter (16 arms) and the AGC settles quickly.
        half_sym = ATSC_SYMBOL_RATE / 2.0
        ntaps = int((2 * ATSC_RRC_SYMS + 1) * SAMP_RATE * INTERP / ATSC_SYMBOL_RATE) | 1
        taps = filter.firdes.root_raised_cosine(INTERP / 16 * half_sym / SAMP_RATE,
                                                SAMP_RATE * INTERP, half_sym, 0.1152, ntaps)
        rrc = filter.rational_resampler_ccf(INTERP, DECIM, taps)

        # Feed-forward carrier recovery: pilot to DC, estimate it with a centred
        # moving average p, de-rotate, keep I. Dividing by |p|^2 (not |p|) puts
        # the output in units of the pilot, which has a fixed size relative to
        # the 8VSB symbols, so the level is right for the AGC whatever the signal
        # strength and weak stations lock as fast as strong ones. pilot_scale is
        # folded into the average: y = Re(x p*) / |p|^2 * pilot_scale.
        rot = blocks.rotator_cc(-2 * math.pi * PILOT_OFFSET / out_rate)
        avg = blocks.moving_average_cc(pilot_avg, 1.0 / (pilot_avg * pilot_scale), 4000)
        dly = blocks.delay(gr.sizeof_gr_complex, (pilot_avg - 1) // 2)
        mcj = blocks.multiply_conjugate_cc()
        c2r = blocks.complex_to_real()
        mag2 = blocks.complex_to_mag_squared()
        norm = blocks.divide_ff()
        self.connect(src, rrc, rot)
        self.connect(rot, dly, (mcj, 0))
        self.connect(rot, avg, (mcj, 1))
        self.connect(mcj, c2r, (norm, 0))
        self.connect(avg, mag2, (norm, 1))

        # DC (the demodulated pilot) removal: x - lowpass(x)
        dc_lp = filter.single_pole_iir_filter_ff(1.0 / 4096)
        dc_sub = blocks.sub_ff()
        self.connect(norm, (dc_sub, 0))
        self.connect(norm, dc_lp, (dc_sub, 1))

        agc = self.agc = analog.agc_ff(1e-5, 4.0)
        sync = dtv.atsc_sync(out_rate)
        fsc = dtv.atsc_fs_checker()
        equ = dtv.atsc_equalizer()
        vit = dtv.atsc_viterbi_decoder()
        dei = dtv.atsc_deinterleaver()
        rsd = dtv.atsc_rs_decoder()
        der = dtv.atsc_derandomizer()
        dep = dtv.atsc_depad()
        self.connect(dc_sub, agc, sync, fsc)
        for a, b in [(fsc, equ), (equ, vit), (vit, dei), (dei, rsd), (rsd, der)]:
            self.connect((a, 0), (b, 0))
            self.connect((a, 1), (b, 1))
        self.connect((der, 0), (dep, 0))

        if not isinstance(out, str):
            sink = out
        elif out.startswith("udp://"):
            host, port = out[len("udp://"):].rsplit(":", 1)
            # 7 TS packets per datagram, no header: what IPTV receivers expect
            sink = network.udp_sink(gr.sizeof_char, 1, host, int(port), 0, 1316, False)
        else:
            sink = blocks.file_sink(gr.sizeof_char, "/dev/stdout" if out == "-" else out, False)
            sink.set_unbuffered(out == "-")
        self.connect(dep, sink)

        self.rate_probe = blocks.probe_rate(gr.sizeof_char, 2000, 0.5)
        self.connect(dep, self.rate_probe)


# ---- on-demand tuner -------------------------------------------------------

HIGH_BIT = bytes(range(128, 256))


class Client:
    def __init__(self):
        self.q = queue.Queue(maxsize=256)  # up to 16 MB at 64 KB/item; a stalled client gets dropped
        self.dead = False


class Tuner:
    """One Airspy, one RF channel at a time, decoding only while someone watches.
    Also runs channel scans, since it owns the Airspy; streams are refused meanwhile."""

    def __init__(self, gain, idle_stop, scan_out):
        self.gain, self.idle_stop, self.scan_out = gain, idle_stop, scan_out
        self.scan = {"running": False, "phase": None, "rf": None, "done": 0, "total": 0,
                     "started": None, "finished": None, "error": None, "count": 0}
        self.mutex = threading.Lock()
        self.tb = self.rf = self.idle_timer = None
        self.clients = ()            # replaced, never mutated: feed() reads it without the lock
        self.tuned_at = self.last_data = 0.0
        self.ts_offset, self.err_window = 0, []   # live packet error stats: [second, packets, errors]
        self.bad_since = self.last_restart = 0.0
        threading.Thread(target=self._watchdog, daemon=True).start()

    def feed(self, data):
        now = self.last_data = time.monotonic()
        # Live reception: count TS packets flagged with transport_error_indicator.
        # The stream is packet-aligned from its first byte, so track the offset.
        first = (-self.ts_offset) % 188
        flags = data[first + 1::188]
        errs = len(flags) - len(flags.translate(None, HIGH_BIT))   # bytes with the TEI bit set
        self.ts_offset += len(data)
        sec = int(now)
        if self.err_window and self.err_window[-1][0] == sec:
            self.err_window[-1][1] += len(flags)
            self.err_window[-1][2] += errs
        else:
            self.err_window.append([sec, len(flags), errs])
            while self.err_window and self.err_window[0][0] < sec - 5:
                self.err_window.pop(0)
        for c in self.clients:
            if c.dead:
                continue
            try:
                c.q.put_nowait(data)
            except queue.Full:
                c.dead = True        # too slow; its handler sees this and disconnects

    def _pump(self, r, stop):
        try:
            while not stop.is_set():
                if not select.select([r], [], [], 0.5)[0]:
                    continue
                data = os.read(r, 65536)
                if not data:
                    break
                self.feed(data)
        finally:
            os.close(r)

    def subscribe(self, rf):
        with self.mutex:
            if self.scan["running"]:
                raise RuntimeError("scanning")
            if self.idle_timer:
                self.idle_timer.cancel()
                self.idle_timer = None
            if rf != self.rf:
                self._stop(f"retune to RF {rf}" if self.tb else None)
                self._start(rf)
            c = Client()
            self.clients = self.clients + (c,)
            log(f"RF {rf}: client connected ({len(self.clients)} watching)")
            return c

    def unsubscribe(self, c):
        with self.mutex:
            if c not in self.clients:
                return
            self.clients = tuple(x for x in self.clients if x is not c)
            log(f"RF {self.rf}: client left ({len(self.clients)} watching)")
            if not self.clients and self.tb and not self.idle_timer:
                self.idle_timer = threading.Timer(self.idle_stop, self._idle)
                self.idle_timer.daemon = True
                self.idle_timer.start()

    def _idle(self):
        with self.mutex:
            self.idle_timer = None
            if not self.clients:
                self._stop("idle")

    def _start(self, rf):
        # The decoder writes into a pipe (C++ file_descriptor_sink, which owns and
        # closes the write end); a thread drains it into the client queues.
        r, w = os.pipe()
        try:
            fcntl.fcntl(w, 1031, 1 << 20)  # F_SETPIPE_SZ: 1 MiB of slack
        except OSError:
            pass
        self.tb = atsc_receiver(channel_center_hz(rf), self.gain,
                                blocks.file_descriptor_sink(gr.sizeof_char, w))
        self.rf, self.tuned_at, self.last_data = rf, time.monotonic(), 0.0
        self.ts_offset, self.err_window = 0, []
        self.pump_stop = threading.Event()
        threading.Thread(target=self._pump, args=(r, self.pump_stop), daemon=True).start()
        self.tb.start()
        log(f"tuned RF {rf} ({channel_center_hz(rf)/1e6:.0f} MHz, gain {self.gain})")

    def _stop_pipeline(self):
        tb, self.tb = self.tb, None
        tb.stop()
        tb.wait()
        self.pump_stop.set()
        del tb                       # destroys the sink, closing the pipe's write end

    def _stop(self, why):
        if not self.tb:
            return
        for c in self.clients:
            c.dead = True
            try:
                c.q.put_nowait(None)
            except queue.Full:
                pass
        self.clients = ()
        self._stop_pipeline()
        if why:
            log(f"stopped RF {self.rf} ({why})")
        self.rf = None

    def _errors(self, seconds=5):
        """Packet error % over the last complete `seconds`, or None without data."""
        window = [w for w in list(self.err_window)[:-1] if w[0] >= int(time.monotonic()) - seconds]
        pkts, errs = sum(w[1] for w in window), sum(w[2] for w in window)
        return round(100 * errs / pkts, 1) if pkts else None

    def _watchdog(self):
        """Dropped samples (CPU starvation) can leave gr-dtv's equalizer diverged:
        still synced, every packet uncorrectable, and it never recovers. If errors
        stay >= 80% for 2 s, rebuild the decoder on the same channel; viewers stay
        connected and see a few seconds' glitch."""
        while True:
            time.sleep(1)
            with self.mutex:
                now = time.monotonic()
                if not self.tb or not self.clients or now - self.tuned_at < 5:
                    self.bad_since = 0.0
                    continue
                err = self._errors(2)
                if err is None or err < 80:
                    self.bad_since = 0.0
                    continue
                self.bad_since = self.bad_since or now
                if now - self.bad_since < 2 or now - self.last_restart < 10:
                    continue
                rf = self.rf
                log(f"RF {rf}: {err}% packet errors for {now - self.bad_since:.0f}s; restarting the decoder")
                self._stop_pipeline()
                self._start(rf)
                self.bad_since, self.last_restart = 0.0, time.monotonic()

    def status(self):
        tb = self.tb
        locked = bool(tb and self.last_data and time.monotonic() - self.last_data < 2)
        err = self._errors()
        return {"rf": self.rf, "clients": len(self.clients),
                "mbps": round(tb.rate_probe.rate() * 8 / 1e6, 2) if tb else 0.0,
                "locked": locked,
                # packet errors over the last ~5 s of decoding (None until there's data)
                "errors_pct": err if locked else None,
                "tuned_for_s": round(time.monotonic() - self.tuned_at) if tb else 0,
                "scan": dict(self.scan)}

    def start_scan(self, chans=None, quick=False, why="requested"):
        """Scan in the background (stops any stream first). False if one is running."""
        with self.mutex:
            if self.scan["running"]:
                return False
            if self.idle_timer:
                self.idle_timer.cancel()
                self.idle_timer = None
            self._stop("scan")
            self.scan.update(running=True, phase="starting", rf=None, done=0, total=0,
                             started=time.time(), error=None)
        log(f"scan started ({why}){' for RF ' + ','.join(map(str, chans)) if chans else ''}")
        threading.Thread(target=self._run_scan, args=(chans, quick), daemon=True).start()
        return True

    def _run_scan(self, chans, quick):
        import atsc_scan
        try:
            res = atsc_scan.run_scan(chans, self.gain, self.scan_out, quick,
                                     progress=lambda **kw: self.scan.update(**kw),
                                     log=lambda m: log(f"scan: {m.strip()}"))
            ok = [c["rf"] for c in res["channels"] if c["kind"] == "atsc1" and c.get("services")
                  and c.get("errors_pct", 100) <= 10]
            log(f"scan finished: clean reception on RF {', '.join(map(str, ok)) or 'none'}")
        except Exception as e:
            self.scan["error"] = str(e)
            log(f"scan failed: {e}")
        finally:
            with self.mutex:
                self.scan.update(running=False, phase=None, rf=None, finished=time.time(),
                                 count=self.scan["count"] + 1)

    def close(self):
        with self.mutex:
            self._stop("shutdown")


def serve(listen, gain, idle_stop, lock_timeout, scan_out, scan_on_start):
    tuner = Tuner(gain, idle_stop, scan_out)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"   # stream until either side closes

        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            path = self.path.split("?", 1)[0].rstrip("/")
            if path == "/status":
                body = json.dumps(tuner.status()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            m = re.fullmatch(r"/rf/(\d+)", path)
            if not m or not 2 <= int(m.group(1)) <= 36:
                self.send_error(404, "use /rf/<2-36> or /status")
                return
            rf = int(m.group(1))
            try:
                c = tuner.subscribe(rf)
            except RuntimeError:
                self.send_error(503, "scanning for channels; try again in a couple of minutes")
                return
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while not c.dead:
                    try:
                        data = c.q.get(timeout=lock_timeout)
                    except queue.Empty:
                        log(f"RF {rf}: no transport stream for {lock_timeout}s (no lock?); closing")
                        break
                    if data is None:
                        break
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                tuner.unsubscribe(c)

        def do_POST(self):
            path, _, query = self.path.partition("?")
            if path.rstrip("/") != "/scan":
                self.send_error(404)
                return
            q = dict(kv.split("=", 1) for kv in query.split("&") if "=" in kv)
            chans = [int(x) for x in q.get("rf", "").split(",") if x.strip().isdigit()] or None
            started = tuner.start_scan(chans, q.get("quick") in ("1", "true"), "requested over HTTP")
            body = json.dumps({"started": started, "scan": tuner.scan}).encode()
            self.send_response(202 if started else 409)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    host, port = listen.rsplit(":", 1)
    httpd = ThreadingHTTPServer((host, int(port)), Handler)
    httpd.daemon_threads = True

    def shutdown(*_):
        threading.Thread(target=httpd.shutdown, daemon=True).start()
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log(f"on-demand tuner on http://{listen}/rf/<n> (gain {gain}, idle stop {idle_stop}s)")
    # Fresh scan whenever the container is (re)created, i.e. the stack was
    # started; not when Docker merely restarts it after a crash, since /tmp
    # (and this marker) survive a restart of the same container.
    marker = "/tmp/atsc-rx.scanned"
    if scan_on_start and not os.path.exists(marker):
        open(marker, "w").close()
        tuner.start_scan(why="container start")
    httpd.serve_forever()
    tuner.close()


# ---- configuration ---------------------------------------------------------

def load_config(path):
    """KEY=VALUE file (comments allowed); missing file -> {}."""
    conf = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    conf[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return conf


def setting(name, cli, conf, default=None):
    """CLI flag, then environment variable, then config file, then default."""
    if cli is not None:
        return cli
    return os.environ.get(name) or conf.get(name) or default


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.environ.get("ATSC_CONFIG", "/config/atsc-rx.conf"),
                    help="KEY=VALUE settings file (default /config/atsc-rx.conf)")
    ap.add_argument("--serve", nargs="?", const="", metavar="HOST:PORT",
                    help="run the on-demand tuner [LISTEN, default 127.0.0.1:5600]")
    ap.add_argument("--channel", type=int, help="decode this RF channel (2-36) [RF_CHANNEL]")
    ap.add_argument("--freq", type=float, help="channel centre frequency in Hz (overrides --channel)")
    ap.add_argument("--iq", help="decode an int16 IQ recording (airspy_rx -t 2, 10 MSPS) instead of the Airspy")
    ap.add_argument("--out", default="-", help="udp://host:port, a file path, or - for stdout (default)")
    ap.add_argument("--gain", type=int, help="Airspy linearity gain 0-21 [GAIN, default 11]")
    ap.add_argument("--buffer-ms", type=int, default=2000,
                    help="sample buffer after the Airspy source, absorbs CPU stalls (default 2000)")
    a = ap.parse_args()
    conf = load_config(a.config)
    gain = int(setting("GAIN", a.gain, conf, 11))

    if a.serve is not None:
        serve(a.serve or setting("LISTEN", None, conf, "127.0.0.1:5600"), gain,
              float(setting("IDLE_STOP", None, conf, 10)), float(setting("LOCK_TIMEOUT", None, conf, 10)),
              os.path.join(os.path.dirname(a.config), "channels.json"),
              str(setting("SCAN_ON_START", None, conf, "1")).lower() not in ("0", "no", "false"))
        return

    channel = setting("RF_CHANNEL", a.channel, conf)
    if channel is None and not (a.freq or a.iq):
        ap.error("give --serve, --channel, --freq or --iq")
    channel = int(channel) if channel is not None else None
    freq = a.freq or (channel_center_hz(channel) if channel else 0)
    tb = atsc_receiver(freq, gain, a.out, buffer_ms=a.buffer_ms, iq_file=a.iq)

    def stop(*_):
        tb.stop()
        tb.wait()
        sys.exit(0)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    log(f"{f'RF {channel}, ' if channel else ''}{freq/1e6:.3f} MHz, gain {gain}, out {a.out}")
    tb.run()


if __name__ == "__main__":
    main()
