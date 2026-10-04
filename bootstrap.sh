#!/usr/bin/env bash
# Install or remove the stack for the current user (run after sudo ./host-setup.sh).
#
#   ./bootstrap.sh                     install / update (idempotent)
#   ./bootstrap.sh uninstall           stop, remove systemd unit, containers, images, generated files
#   ./bootstrap.sh uninstall --purge   ...and delete config/, recordings/ and atsc/config/
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
REPO=$PWD
UNIT=tvheadend.service
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
TVH_IMAGE=ghcr.io/tvheadend/tvheadend:master-debian
RX_IMAGE=atsc-rx:local
VOLK_KERNELS="32fc_32f_dot_prod_32fc|32fc_s32fc_x2_rotator|32fc_x2_multiply_conjugate_32fc|32fc_deinterleave_real_32f|32f_x2_add_32f|32f_x2_subtract_32f|32fc_x2_add_32fc|32f_s32f_multiply_32f|32fc_x2_multiply_32fc|32f_x2_dot_prod_32f|16i_s32f_convert_32f|32fc_magnitude_32f"

need_docker() {
    docker info >/dev/null 2>&1 || { echo "can't reach docker; are you in the docker group? (sudo ./host-setup.sh, then re-login)" >&2; exit 1; }
}

install() {
    need_docker
    [[ $(id -u):$(id -g) == 1000:1000 ]] || echo "note: containers run as 1000:1000 (docker-compose.yml); you are $(id -u):$(id -g)"

    echo "== directories"
    mkdir -p config recordings atsc/config/volk

    if [[ ! -f config/superuser ]]; then
        echo "== seeding Tvheadend superuser"
        local pw
        pw=$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')
        (umask 077; printf '{\n  "username": "admin",\n  "password2": "%s"\n}\n' \
            "$(printf 'TVHeadend-Hide-%s' "$pw" | base64 -w0)" > config/superuser)
        echo "   Tvheadend login: admin / $pw   (stored obfuscated in config/superuser)"
    fi

    echo "== images"
    docker compose pull tvheadend
    docker compose build atsc-rx

    if [[ ! -s atsc/config/volk/volk_config ]]; then
        echo "== profiling VOLK SIMD kernels for this CPU (about a minute)"
        docker run --rm --user "$(id -u):$(id -g)" -v "$REPO/atsc/config:/config" --entrypoint volk_profile \
            "$RX_IMAGE" -R "$VOLK_KERNELS" -p /config/volk >/dev/null
    fi

    echo "== systemd user unit ($UNIT_DIR/$UNIT)"
    mkdir -p "$UNIT_DIR"
    sed "s|%h/tvheadend|$REPO|" "systemd/$UNIT" > "$UNIT_DIR/$UNIT"
    systemctl --user daemon-reload
    systemctl --user enable "$UNIT"
    local since
    since=$(date +%s)
    systemctl --user restart "$UNIT"

    echo "== waiting for Tvheadend"
    # restart returns before compose has recreated the containers; wait for the new one
    local started ok=
    for _ in $(seq 90); do
        started=$(docker inspect -f '{{.State.StartedAt}}' tvheadend 2>/dev/null || echo 1970-01-01T00:00:00Z)
        if (( $(date -d "$started" +%s) >= since )) && curl -sf -m 2 http://127.0.0.1:9981/ping >/dev/null; then
            ok=1; break
        fi
        sleep 2
    done
    [[ -n $ok ]] || { echo "Tvheadend did not come up; see: journalctl --user -u $UNIT" >&2; exit 1; }

    local rf
    rf=$(sed -n 's/^RF_CHANNEL=//p' atsc/config/atsc-rx.conf 2>/dev/null | tail -1)
    if [[ -n $rf ]]; then
        echo "== registering RF $rf (atsc/config/atsc-rx.conf) in Tvheadend"
        python3 atsc/tvh_add_mux.py --channel "$rf" --port $((5500 + rf)) --exclusive
    else
        echo "== no channel configured: scanning, then tuning the strongest station"
        ./atsc.sh scan
        if rf=$(./atsc.sh best); then
            ./atsc.sh tune "$rf"
        else
            echo "   nothing receivable found; check the antenna, then ./atsc.sh scan / ./atsc.sh tune <rf>"
        fi
    fi

    echo "== player URLs (creates the streaming-only 'viewer' account once)"
    ./atsc.sh urls
}

uninstall() {
    local purge=${1:-}
    echo "== systemd user unit"
    if [[ -f $UNIT_DIR/$UNIT ]]; then
        systemctl --user disable --now "$UNIT" || true
        rm -f "$UNIT_DIR/$UNIT"
        systemctl --user daemon-reload
    fi

    echo "== containers and images"
    if docker info >/dev/null 2>&1; then
        docker compose down --remove-orphans || true
        docker image rm "$RX_IMAGE" "$TVH_IMAGE" 2>/dev/null || true
    else
        echo "   docker not reachable; skipped"
    fi

    echo "== generated files"
    rm -rf atsc/config/volk atsc/__pycache__

    if [[ $purge == --purge ]]; then
        echo "== purging config/, recordings/ and atsc/config/"
        rm -rf config recordings atsc/config
    else
        echo "   kept config/, recordings/ and atsc/config/ (use: ./bootstrap.sh uninstall --purge)"
    fi
    echo "done. Host-level changes are undone with: sudo ./host-setup.sh --uninstall"
}

case ${1:-install} in
    install) install ;;
    uninstall) uninstall "${2:-}" ;;
    *) echo "usage: $0 [install | uninstall [--purge]]" >&2; exit 2 ;;
esac
