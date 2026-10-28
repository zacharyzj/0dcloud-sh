# 0dcloud-sh

通过命令行管理订阅配置和官方 Mihomo 核心。支持配置下载、HTTP/SOCKS 代理、节点选择及进程管理，不需要 Flutter 界面或 root。

已实现网站登录、从账号页发现标准订阅、下载配置和核心管理。真实网站登录与官方核心转发已经验证；用户提供的订阅目前返回 HTTP 504，因此真实节点转发尚未验证。原 Flutter 客户端的私有签名和加密 `/clientConfig` 协议仍未复刻；当前网站与标准订阅流程不依赖该协议。详见 [验证记录](docs/validation.md)。

## 安装

需要 Linux amd64/arm64、Python 3.10+。

```sh
cd 0dcloud-sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
proxyctl --help
```

如果环境已有 PyYAML 和 requests，也可以直接执行 `./proxyctl`。

## 订阅和启动

```sh
# 隐藏输入完整订阅 URL，避免进入 shell 历史和命令行参数。
proxyctl subscription set
proxyctl update

# 下载官方 MetaCubeX/mihomo release，安装在用户状态目录。
proxyctl core install
proxyctl start
proxyctl status
proxyctl nodes
proxyctl use '香港-01' --group 'PROXY'

# 仅改变当前终端及后续子进程的代理设置。
eval "$(proxyctl env)"

proxyctl stop
eval "$(proxyctl env --unset)"
```

支持 Clash/Mihomo YAML 或 JSON 配置，以及普通或 Base64 编码的 SS、基础 TCP Trojan URI 列表。其他 URI 协议和带插件/特殊传输的节点请使用 Clash/Mihomo YAML 导出；不会静默丢弃不支持的节点。只有节点而无代理组的配置会生成 `PROXY` 选择组与兜底规则。

`update` 成功后保存配置，下载或解析失败保留旧配置；它不会自动影响正在运行的核心。需要应用新配置时执行 `stop`、`start`。

安装器优先查询 GitHub API；API 被限流或无法访问时，自动从官方发布页读取版本、资产和 SHA-256。下载仍失败时，可以仅对本次安装绕过环境代理：

```sh
./proxyctl core install --direct
```

也可以指定官方发布页的资产地址；可额外提供压缩下载文件的 SHA-256：

```sh
proxyctl core install --url 'https://github.com/MetaCubeX/mihomo/releases/download/<tag>/<asset>.gz' --sha256 '<64位SHA256>'
```

也可以使用已有官方核心：`proxyctl --core /absolute/path/mihomo start`。不要传入原 deb 中的 `0dcloudCore`，它依赖原客户端 IPC，并不是标准 Mihomo CLI。

## 账号登录

验证前先加载变量，工具不会把邮箱和密码作为命令行参数或写入配置：

```zsh
source ~/.zshrc
export _0dcloud_mail _0dcloud_pass
proxyctl login --api-url 'https://已确认的账号API地址'
```

如果变量在 `.zshrc` 中定义但未 export，可以用包装脚本，它会 source `.zshrc` 并导出这两个变量。激活虚拟环境后调用：

```zsh
./proxyctl.zsh login --api-url 'https://已确认的账号API地址'
```

已从用户提供站点的公开登录页和 `auth.js` 确认网站表单：向 `/auth/login` POST `email`、`passwd`、`remember_me=on` 和 `code`，JSON `ret=1` 表示成功，`ret=2` 要求二步验证。工具保存响应 Cookie；启用二步验证时可通过环境变量 `_0dcloud_otp` 提供动态码。可用 `--login-path` 和 `--password-field password` 调整其他已确认的服务参数。网站登录与原客户端 `/authorization` 的协议不同；网站登录成功不代表已兼容私有客户端接口。工具不会把登录失败当作成功，也不会将密码发送到未经确认的其他站点或跟随登录重定向。

登录后执行 `proxyctl update --from-account`：工具携带网站会话读取同站 `/user`，从页面明确给出的 `/link/` 地址中选择 Clash 2 订阅（`clash=2`，没有时选择 `clash=1`），再使用独立会话下载配置。它不会构造域名或令牌，也不会把账号 Cookie 发送给订阅站点。下载和解析都成功后才保存新订阅地址；失败保留原配置和地址。账号过期、页面没有订阅或存在多个不同链接时会提示处理。

`proxyctl update --from-client-api` 是单独的实验性明文 `/clientConfig` 适配器，只有在已确认该 API 地址和协议时使用。原样本还存在 `X-Client-*` 签名、AES-GCM 及 `clientconfig://app2` 内部标记；这些私有协议仍未兼容。内部标记不能代替 HTTPS 订阅 URL。标准订阅链接可独立于登录使用。

`proxyctl logout` 删除本地会话，不删除已下载配置，也不请求服务器注销。

## 运行约定

- 默认代理仅监听 `127.0.0.1:7890`，控制器仅监听随机本机端口并使用随机 secret。可用 `start --port 17890 --controller-port 19090` 修改。
- 不启用 TUN、透明代理或独立 DNS 监听；不修改系统代理、路由、防火墙或开机自启动。`env` 只输出命令，须用户 `eval` 才影响当前 shell。
- `env` 同时输出大小写代理变量，并合并既有 `NO_PROXY`/`no_proxy`。`env --unset` 清除代理变量，不恢复此前值，保留绕过列表。
- 配置、URL、Cookie 和节点密码保存在 `$XDG_STATE_HOME/0dcloud-sh`（默认 `~/.local/state/0dcloud-sh`）。目录权限 700，文件 600；这些文件仍包含敏感信息，不应提交或分享。
- 使用 `--state-dir /path` 创建独立实例。停止进程前会核对 PID、启动时间、boot ID 和 executable inode。
- 远程请求要求 HTTPS，校验证书；仅本机测试允许 HTTP。订阅跨站跳转会被拒绝，需用户核实最终 URL。账号 Cookie 不会附加到订阅/CDN 请求。
- 日志保存在状态目录 `core.log`，核心日志可能包含访问域名和节点名。

## 验证

登录出现网络错误时，先只读检查登录页（不发送邮箱、密码或已有会话）：

```sh
./proxyctl doctor --api-url https://www.0dy12.com
./proxyctl doctor --api-url https://www.0dy12.com --direct
```

默认遵循当前终端的 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 及绕过列表。若默认模式失败而直连成功，可仅对本次请求使用直连：

```sh
./proxyctl login --api-url https://www.0dy12.com --direct
./proxyctl update --from-account --direct
```

`--direct` 不修改终端环境变量，仍校验 HTTPS 证书并保留 `REQUESTS_CA_BUNDLE`/`CURL_CA_BUNDLE` 信任设置。错误提示区分代理失败、DNS、连接/读取超时和 TLS 错误，不打印原始异常中的订阅令牌或代理密码。

```sh
python3 -m unittest discover -s tests -v
```

测试使用本机模拟账号/订阅服务与假核心，验证会话、重定向边界、敏感信息不回显、失败保留旧配置、节点解析、配置校验、进程身份和控制 API。测试需要系统允许创建本机 socket。

真实账号登录已经按用户提供站点验证成功，密码从环境变量读取。真实订阅目前返回 HTTP 504，尚未验证远端节点转发；站点和订阅令牌没有写入仓库。
