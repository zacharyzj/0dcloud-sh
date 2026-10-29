#!/usr/bin/env bash
# Source from bash/zsh to also enable proxy variables in the current terminal.
_odcloud_start_proxy() {
    local script_path project_dir python proxy_env sourced=false autostart_action="" action
    local -a global_args=() connect_args=()
    if [ -n "${BASH_VERSION:-}" ]; then
        script_path="${BASH_SOURCE[0]}"
        [ "$script_path" = "$0" ] || sourced=true
    elif [ -n "${ZSH_VERSION:-}" ]; then
        script_path="${(%):-%x}"
        case "$ZSH_EVAL_CONTEXT" in *:file|*:file:*) sourced=true ;; esac
    else
        printf '%s\n' '请使用 bash 执行，或在 bash/zsh 中 source 此脚本。' >&2
        return 2
    fi
    project_dir="$(cd -- "$(dirname -- "$script_path")" && pwd)" || return
    python="$project_dir/.venv/bin/python"
    if [ ! -x "$python" ]; then
        python=python3
    fi
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --state-dir|--core)
                if [ "$#" -lt 2 ] || [ -z "$2" ]; then
                    printf '%s 需要一个值。\n' "$1" >&2
                    return 2
                fi
                global_args+=("$1" "$2")
                shift 2
                ;;
            --enable-autostart|--disable-autostart|--autostart-status)
                case "$1" in
                    --enable-autostart) action=enable ;;
                    --disable-autostart) action=disable ;;
                    --autostart-status) action=status ;;
                esac
                if [ -n "$autostart_action" ] && [ "$autostart_action" != "$action" ]; then
                    printf '%s\n' '开机自启动选项不能同时使用。' >&2
                    return 2
                fi
                autostart_action="$action"
                shift
                ;;
            -h|--help)
                printf '%s\n' '用法: source ./start-proxy.sh [--state-dir PATH] [--core PATH] [--group NAME] [--port PORT] [--controller-port PORT]' >&2
                printf '%s\n' '  --enable-autostart   启动代理并登记机器开机自启动（systemd，需 root）' >&2
                printf '%s\n' '  --disable-autostart  取消开机自启动，保留当前运行的代理（需 root）' >&2
                printf '%s\n' '  --autostart-status   仅查看开机自启动状态' >&2
                printf '%s\n' '直接执行会启动核心并输出 export 命令；也可用 eval "$(./start-proxy.sh)" 设置当前终端。' >&2
                return 0
                ;;
            *) connect_args+=("$1"); shift ;;
        esac
    done
    # Run from the source tree even when called from a different directory.
    if [ "$autostart_action" = disable ] || [ "$autostart_action" = status ]; then
        (cd -- "$project_dir" && "$python" "$project_dir/proxyctl" "${global_args[@]}" autostart "$autostart_action" "${connect_args[@]}") >&2 || return
        return 0
    fi
    (cd -- "$project_dir" && "$python" "$project_dir/proxyctl" "${global_args[@]}" connect "${connect_args[@]}") >&2 || return
    if [ "$autostart_action" = enable ]; then
        (cd -- "$project_dir" && "$python" "$project_dir/proxyctl" "${global_args[@]}" autostart enable "${connect_args[@]}") >&2 || return
    fi
    proxy_env="$(cd -- "$project_dir" && "$python" "$project_dir/proxyctl" "${global_args[@]}" env)" || return
    if [ "$sourced" = true ]; then
        eval "$proxy_env"
    else
        printf '%s\n' "$proxy_env"
        printf '%s\n' '代理已启动。设置当前终端请运行 source ./start-proxy.sh，或 eval "$(./start-proxy.sh)"。' >&2
    fi
}

if _odcloud_start_proxy "$@"; then
    unset -f _odcloud_start_proxy
else
    unset -f _odcloud_start_proxy
    return 1 2>/dev/null || exit 1
fi
