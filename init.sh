#!/usr/bin/env bash
# Run, do not source. Passwords stay in this process and are never persisted.
set +x
set -euo pipefail
umask 077
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

api_url="https://www.0dy12.com"
state_dir="${XDG_STATE_HOME:-$HOME/.local/state}/0dcloud-sh"
direct=()
skip_login=false
skip_core=false
start=false
core_file=""
wheel_dir=""
install_system=false

usage() {
    cat <<'EOF'
Usage: ./init.sh [options]
  --direct             Ignore proxy environment for account/core/subscription HTTP
  --api-url URL        Account site (default https://www.0dy12.com)
  --state-dir PATH     Private state directory
  --skip-login         Reuse the saved login session
  --skip-core          Reuse an installed Mihomo core
  --core-file PATH     Install an existing official Linux Mihomo ELF locally
  --wheel-dir PATH     Install Python dependencies from an offline wheels directory
  --install-system     On Debian/Ubuntu as root, install python3-venv and CA packages
  --start              Download subscription and start proxy after setup
  -h, --help           Show this help

Credentials: exported _0dcloud_mail and _0dcloud_pass, or hidden interactive input.
If a cached subscription is unavailable, setup reports the download error without
starting a proxy. Re-run update --from-account after connectivity is restored.
EOF
}

while (($#)); do
    case "$1" in
        --direct) direct=(--direct); shift ;;
        --api-url) api_url="${2:?--api-url needs a value}"; shift 2 ;;
        --state-dir) state_dir="${2:?--state-dir needs a value}"; shift 2 ;;
        --core-file) core_file="${2:?--core-file needs a value}"; shift 2 ;;
        --wheel-dir) wheel_dir="${2:?--wheel-dir needs a value}"; shift 2 ;;
        --skip-login) skip_login=true; shift ;;
        --skip-core) skip_core=true; shift ;;
        --install-system) install_system=true; shift ;;
        --start) start=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done

if $install_system; then
    if [[ "$(id -u)" != 0 ]] || ! command -v apt-get >/dev/null; then
        printf '%s\n' '--install-system requires root on Debian/Ubuntu.' >&2; exit 1
    fi
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv ca-certificates
fi
python3 -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ required"'
if [[ ! -x .venv/bin/python ]]; then
    if ! python3 -m venv .venv; then
        printf '%s\n' 'Cannot create venv. Install python3-venv or rerun with --install-system.' >&2
        exit 1
    fi
fi
pip_args=()
if [[ -n "$wheel_dir" ]]; then
    pip_args=(--no-index --find-links "$wheel_dir")
fi
printf '%s\n' 'Installing Python dependencies...'
.venv/bin/python -m pip install "${pip_args[@]}" -e .
ctl=("$PROJECT_DIR/.venv/bin/proxyctl" --state-dir "$state_dir")

if [[ -n "$core_file" ]]; then
    .venv/bin/python - "$core_file" "$state_dir" <<'PY'
import hashlib, json, os, platform, sys
from pathlib import Path
from odcloud_sh.storage import State, atomic_write
from odcloud_sh.core import _atomic
source = Path(sys.argv[1]).resolve()
payload = source.read_bytes()
arch = platform.machine()
machine = 62 if arch in ('x86_64','amd64') else 183 if arch in ('aarch64','arm64') else None
if not machine or len(payload) < 64 or payload[:6] != b'\x7fELF\x02\x01' or int.from_bytes(payload[18:20], 'little') != machine:
    raise SystemExit('The provided core file is not a compatible Linux ELF.')
state = State(sys.argv[2])
from odcloud_sh.core import CoreManager
manager = CoreManager(state.directory)
with manager._lock():
    if manager.status()['running']:
        raise SystemExit('Stop the existing core before replacing it.')
    dest = state.path('bin/mihomo')
    _atomic(dest, payload, 0o700)
    state.write({'path':str(dest), 'source':'local-file', 'sha256':hashlib.sha256(payload).hexdigest()}, 'core-install.json')
print('Installed provided core; SHA256:', hashlib.sha256(payload).hexdigest())
PY
elif ! $skip_core; then
    printf '%s\n' 'Installing official Mihomo core...'
    "${ctl[@]}" core install "${direct[@]}"
fi

if ! $skip_login; then
    if [[ -z "${_0dcloud_mail:-}" ]]; then
        read -rsp 'Account email (hidden): ' _0dcloud_mail; printf '\n'
    fi
    if [[ -z "${_0dcloud_pass:-}" ]]; then
        read -rsp 'Password (hidden): ' _0dcloud_pass; printf '\n'
    fi
    export _0dcloud_mail _0dcloud_pass
    printf '%s\n' 'Signing in...'
    "${ctl[@]}" login --api-url "$api_url" "${direct[@]}"
fi
unset _0dcloud_mail _0dcloud_pass _0dcloud_otp
printf '%s\n' 'Downloading subscription from the account dashboard...'
"${ctl[@]}" update --from-account "${direct[@]}"
if $start; then
    if ! "${ctl[@]}" status | .venv/bin/python -c 'import json,sys;sys.exit(not json.load(sys.stdin)["running"])'; then
        "${ctl[@]}" start
    fi
fi
"${ctl[@]}" status
printf '%s\n' 'Setup complete. Activate with: source .venv/bin/activate'
printf 'For this terminal only: eval "$(proxyctl --state-dir %q env)"\n' "$state_dir"
