#!/usr/bin/env zsh
# A wrapper for credentials defined as either exported or shell-only variables.
# Never use `zsh -x` with credentials in this environment.
set +x
source ~/.zshrc
set +x
export _0dcloud_mail _0dcloud_pass
exec python3 "${0:A:h}/proxyctl" "$@"

