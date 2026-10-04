#!/usr/bin/env python3
"""Live ATSC 1.0 (8VSB) receiver: Airspy R2 -> GNU Radio gr-dtv -> MPEG-TS.

Tuned for a 2-core laptop CPU. Differences from stock gr-dtv atsc_rx:
  * RRC matched filter + resample via a 32/27 rational polyphase resampler
    (10 MSPS -> 11.85 MSPS, ~1.1 samples/symbol) instead of pfb_arb_resampler.
  * Feed-forward pilot carrier recovery (VOLK rotator + centred moving average
    of the pilot) instead of atsc_fpll's per-sample sincos/atan2 loop.
  * One-pole IIR DC removal instead of dc_blocker_ff(4096).

Output is the full RF channel's transport stream (all subchannels), ~19.4 Mbps.

Settings come from CLI flags, then environment variables, then the config file
(/config/atsc-rx.conf: RF_CHANNEL=26, GAIN=11, optional UDP_PORT).

  atsc_rx.py                                  # use /config/atsc-rx.conf
  RF_CHANNEL=22 atsc_rx.py                    # override the channel
  atsc_rx.py --channel 26 --out /dev/shm/rf26.ts
  atsc_rx.py --iq rf26.iq --out rf26.ts       # offline, from an airspy_rx -t 2 capture
"""
import argparse, math, os, signal, sys, threading, time
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
    def __init__(self, freq, gain, out, pilot_avg=2048, buffer_ms=500, iq_file=None,
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
            # overflow, which forces the ATSC decoder to re-lock. A large output
            # buffer here keeps that ring drained through short CPU stalls.
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

        if out.startswith("udp://"):
            host, port = out[len("udp://"):].rsplit(":", 1)
            # 7 TS packets per datagram, no header: what IPTV receivers expect
            sink = network.udp_sink(gr.sizeof_char, 1, host, int(port), 0, 1316, False)
        else:
            sink = blocks.file_sink(gr.sizeof_char, "/dev/stdout" if out == "-" else out, False)
            sink.set_unbuffered(out == "-")
        self.connect(dep, sink)

        self.rate_probe = blocks.probe_rate(gr.sizeof_char, 2000, 0.5)
        self.connect(dep, self.rate_probe)


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


def setting(name, cli, conf):
    """CLI flag, then environment variable, then config file."""
    if cli is not None:
        return cli
    return os.environ.get(name) or conf.get(name) or None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.environ.get("ATSC_CONFIG", "/config/atsc-rx.conf"),
                    help="KEY=VALUE file with RF_CHANNEL / GAIN / UDP_PORT (default /config/atsc-rx.conf)")
    ap.add_argument("--channel", type=int, help="RF channel (2-36), not the virtual channel [RF_CHANNEL]")
    ap.add_argument("--freq", type=float, help="channel centre frequency in Hz (overrides --channel)")
    ap.add_argument("--gain", type=int, help="Airspy linearity gain 0-21 [GAIN, default 11]")
    ap.add_argument("--out", help="udp://host:port, a file path, or - for stdout "
                                  "[default udp://127.0.0.1:UDP_PORT, UDP_PORT default 5500 + channel]")
    ap.add_argument("--iq", help="decode an int16 IQ recording (airspy_rx -t 2, 10 MSPS) instead of the Airspy")
    ap.add_argument("--buffer-ms", type=int, default=500,
                    help="sample buffer after the Airspy source, absorbs CPU stalls (default 500)")
    ap.add_argument("--stats", type=float, default=float(os.environ.get("STATS", 60)),
                    help="seconds between rate log lines (0=off) [STATS, default 60]")
    a = ap.parse_args()
    log = sys.stderr

    # Without a channel there is nothing to do; wait for one rather than crash-looping.
    waited = False
    while True:
        conf = load_config(a.config)
        channel = setting("RF_CHANNEL", a.channel, conf)
        if channel is not None or a.freq or a.iq:
            break
        if not waited:
            print(f"atsc_rx: no RF_CHANNEL in {a.config} or the environment; waiting "
                  "(run ./atsc.sh scan, then ./atsc.sh tune <rf>)", file=log, flush=True)
            waited = True
        time.sleep(10)

    channel = int(channel) if channel is not None else None
    freq = a.freq or (channel_center_hz(channel) if channel else 0)
    gain = setting("GAIN", a.gain, conf)
    gain = int(gain) if gain is not None else 11
    out = a.out
    if out is None:
        port = setting("UDP_PORT", None, conf) or (5500 + channel if channel else 5500)
        out = f"udp://127.0.0.1:{port}"
    tb = atsc_receiver(freq, gain, out, buffer_ms=a.buffer_ms, iq_file=a.iq)

    def stop(*_):
        tb.stop()
        tb.wait()
        sys.exit(0)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    label = f"RF {channel}, " if channel else ""
    print(f"atsc_rx: {label}{freq/1e6:.3f} MHz, gain {gain}, out {out}", file=log, flush=True)
    tb.start()
    if a.stats > 0:
        def stats():
            while True:
                time.sleep(a.stats)
                # 19.39 Mbps is a full, healthy channel; 0 means no lock
                print(f"atsc_rx: TS {tb.rate_probe.rate() * 8 / 1e6:.2f} Mbps", file=log, flush=True)
        threading.Thread(target=stats, daemon=True).start()
    tb.wait()


if __name__ == "__main__":
    main()
