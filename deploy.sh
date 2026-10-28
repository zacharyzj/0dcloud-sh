#!/usr/bin/env bash
# Upload source plus an optional verified core through SCP. No credentials.
set -euo pipefail
umask 077
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
target="${1:-root@152.136.54.246}"
remote_dir="${2:-/root/0dcloud-sh}"
core_file="${3:-}"
if [[ ! "$target" =~ ^[a-zA-Z0-9_.@:-]+$ || ! "$remote_dir" =~ ^/[a-zA-Z0-9_./-]+$ ]]; then
    printf '%s\n' 'Invalid SSH target or absolute remote path.' >&2; exit 2
fi
tmp_dir="$(mktemp -d)"
trap 'rm -rf -- "$tmp_dir"' EXIT
tar --exclude=__pycache__ --exclude='*.pyc' -czf "$tmp_dir/source.tar.gz" -C "$project_dir" \
    README.md pyproject.toml .gitignore proxyctl proxyctl.zsh init.sh deploy.sh odcloud_sh tests docs
ssh "$target" "umask 077; mkdir -p '$remote_dir'; backup_dir=\$(mktemp -d /root/0dcloud-sh-backup.XXXXXX); tar --exclude=.git --exclude=.venv --exclude=__pycache__ -czf \"\$backup_dir/source-before.tar.gz\" -C '$remote_dir' .; printf 'Backup: %s\\n' \"\$backup_dir\""
scp "$tmp_dir/source.tar.gz" "$target:$remote_dir/.source-deploy.tar.gz"
ssh "$target" "tar -xzf '$remote_dir/.source-deploy.tar.gz' -C '$remote_dir'; rm '$remote_dir/.source-deploy.tar.gz'"
if [[ -n "$core_file" ]]; then
    core_digest="$(sha256sum -- "$core_file")"
    core_digest="${core_digest%% *}"
    scp "$core_file" "$target:$remote_dir/.mihomo-upload"
    printf 'Source and core uploaded. Initialize on the server:\ncd %q\n./init.sh --core-file .mihomo-upload --core-sha256 %s --direct --start\n' "$remote_dir" "$core_digest"
else
    printf 'Source uploaded. Initialize on the server:\ncd %q\n./init.sh --direct --start\n' "$remote_dir"
fi
