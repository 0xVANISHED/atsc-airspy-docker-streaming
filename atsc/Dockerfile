# ATSC 1.0 decoder: Airspy R2 -> GNU Radio gr-dtv -> MPEG-TS over UDP.
# Only the GNU Radio libraries the receiver uses are installed; the Python
# bindings are lifted from the gnuradio .deb to avoid its Qt/GUI dependencies.
# airspy (airspy_rx) is for atsc_scan.py, libvolk-bin (volk_profile) for bootstrap.
FROM debian:trixie-slim
ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-numpy \
      libgnuradio-runtime3.10.12 libgnuradio-pmt3.10.12 libgnuradio-blocks3.10.12 \
      libgnuradio-fft3.10.12 libgnuradio-filter3.10.12 libgnuradio-analog3.10.12 \
      libgnuradio-dtv3.10.12 libgnuradio-soapy3.10.12 libgnuradio-network3.10.12 \
      soapysdr-module-airspy airspy libvolk-bin \
 && cd /tmp && apt-get download gnuradio \
 && dpkg-deb -x gnuradio_*.deb /tmp/gr \
 && cp -a /tmp/gr/usr/lib/python3/dist-packages/gnuradio /tmp/gr/usr/lib/python3/dist-packages/pmt \
          /usr/lib/python3/dist-packages/ \
 && rm -rf /tmp/gr /tmp/gnuradio_*.deb /var/lib/apt/lists/* \
 && python3 -c "from gnuradio import gr, blocks, filter, analog, dtv, soapy, network; print('gnuradio', gr.version())"

# /config (bind mount): atsc-rx.conf, channels.json, volk/volk_config.
# Writable HOME for any UID (GNU Radio prefs).
RUN mkdir -p /home/atsc /config && chmod 1777 /home/atsc
ENV HOME=/home/atsc VOLK_CONFIGPATH=/config PYTHONUNBUFFERED=1
WORKDIR /app

COPY atsc_rx.py atsc_scan.py /app/
ENTRYPOINT ["python3", "/app/atsc_rx.py"]
