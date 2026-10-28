# 0dcloud-sh

通过命令行管理订阅配置和官方 Mihomo 核心。支持配置下载、HTTP/SOCKS 代理、节点选择及进程管理，不需要 Flutter 界面或 root。

已补齐 0dcloud 2.0.30 GUI 的原生登录、HMAC-SHA256 签名刷新和 AES-256-GCM 配置解密。真实接口成功返回 22 个 AnyTLS 节点；不需要手动输入订阅地址。网站登录和标准订阅方式也保留。详见 [原生协议](docs/native-client-protocol.md) 和 [验证记录](docs/validation.md)。

## 安装

服务器部署、SCP 上传和初始化脚本见 [服务器使用流程](docs/server-usage.md)。

需要 Linux amd64/arm64、Python 3.10+。

```sh
cd 0dcloud-sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
proxyctl --help
```

如果环境已有 PyYAML、requests 和 cryptography，也可以直接执行 `./proxyctl`。

## GUI 同款登录和刷新（无需订阅地址）

```zsh
source ~/.zshrc
export _0dcloud_mail _0dcloud_pass

# 只读取 GUI 的公开发现配置，不发送账号，也不自动信任发现的地址。
./proxyctl discover --direct

# 本次已确认的 GUI API；HTTP 明文传输账号须显式开启 --allow-http。
./proxyctl login --client-api --api-url http://8.134.141.98:7985 --allow-http --direct
./proxyctl update --from-client-api --allow-http --direct
./proxyctl core geodata --direct
./proxyctl start
./proxyctl nodes
```

也可以用 `./proxyctl.zsh`，它会先 `source ~/.zshrc` 并导出账号变量。刷新接口每次需要邮箱、密码，密码只从环境读取，不写入磁盘；本地保存独立客户端会话和随机设备 ID。网站登录 Cookie 不能替代这个客户端会话。

`--allow-http` 只对本次原生客户端操作生效。当前 GUI 发现服务给出 HTTP API；它的登录请求把邮箱、密码放入查询参数，刷新请求也携带密码，HMAC 与响应加密不能保护明文 HTTP 请求。HTTPS API 不需要这个选项。发现地址变化时必须重新指定并确认，不自动把账号发送到新地址。

初始化脚本也支持同一流程：

```sh
./init.sh --client-api --api-url http://8.134.141.98:7985 --allow-http --direct --start
# 已安装核心时附加 --skip-core；已有客户端会话时可用 --skip-login。
```

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

## Dashboard、代理模式与 TUN

启动 HTTP 面板，默认监听 **`0.0.0.0:8787`**：

```sh
proxyctl dashboard
# 另一个终端查看带访问令牌的链接
proxyctl dashboard --show-url
```

启动后输出使用实际网卡 IP、带 `#token=` 的访问链接。本机监听时使用 `127.0.0.1`；云服务器公网 NAT 场景可用 `--public-host 152.136.54.246` 指定公网 IP，启动后保留，后续 `--show-url` 直接输出该地址。也可直接打开 `http://服务器IP:8787/` 后输入面板令牌。每次重启面板生成新令牌；令牌保存于私有状态目录，浏览器仅在当前会话保存。HTTP 明文传输面板操作与令牌，跨公网使用时可通过 HTTPS 反向代理或 SSH 转发访问。

面板支持节点选择、延迟测试、连接与断开、Proxy Mode 和 TUN 开关，无需前端依赖或第三方 CDN。服务在前台运行；`Ctrl-C` 关闭面板，核心保持当前运行状态。可用 `--port 18787` 换端口、`--host 127.0.0.1` 限制本机访问、`--open` 打开浏览器。

命令行也支持相同控制：

```sh
proxyctl mode           # 查看模式与 TUN 状态
proxyctl mode rule      # 按配置规则分流
proxyctl mode global    # 使用 GLOBAL 组的选择
proxyctl nodes          # 查看实际代理组名称和候选节点
proxyctl use '节点名称' --group GLOBAL
proxyctl mode direct    # 直接连接
proxyctl tun on
proxyctl tun off
```

模式与 TUN 设置在线生效，并保留到后续启动；核心未运行时会保存下次启动的设置。TUN 仅支持 Linux，需要 `/dev/net/tun` 和核心进程的 `CAP_NET_ADMIN`（通常以 root 启动核心）。默认关闭；显式开启后创建 `odcloud0` 虚拟网卡并自动设置路由，DNS 沿用系统解析器，不启用 DNS 劫持或 fake-IP。面板控制的是**运行服务的机器**；连接远端面板不会改变浏览器所在电脑的路由。SSH 连接服务器时请确认网络条件后再开启。

这里的 Proxy Mode 是 Mihomo 的规则、全局、直连模式。终端应用通过 `eval "$(proxyctl env)"` 使用代理；桌面系统的全局代理开关未接入。详见 [面板使用说明](docs/dashboard.md)。

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

GUI 原生协议入口为 `login --client-api` 和 `update --from-client-api`，支持真实签名和加密响应。`clientconfig://app2` 是 GUI 内部标记，不是下载 URL。标准订阅链接可独立于登录使用。

`proxyctl logout` 删除本地会话，不删除已下载配置，也不请求服务器注销。

## 运行约定

- 默认代理仅监听 `127.0.0.1:7890`，控制器仅监听随机本机端口并使用随机 secret。可用 `start --port 17890 --controller-port 19090` 修改。
- 默认不启用 TUN、透明代理或独立 DNS 监听；显式 `tun on` 会修改运行核心所在机器的路由。不修改桌面系统代理或开机自启动。`env` 只输出命令，须用户 `eval` 才影响当前 shell。
- `env` 同时输出大小写代理变量，并合并既有 `NO_PROXY`/`no_proxy`。`env --unset` 清除代理变量，不恢复此前值，保留绕过列表。
- 配置、URL、Cookie 和节点密码保存在 `$XDG_STATE_HOME/0dcloud-sh`（默认 `~/.local/state/0dcloud-sh`）。目录权限 700，文件 600；这些文件仍包含敏感信息，不应提交或分享。
- 使用 `--state-dir /path` 创建独立实例。停止进程前会核对 PID、启动时间、boot ID 和 executable inode。
- 远程请求默认要求 HTTPS，校验证书；原生 API 仅在显式 `--allow-http` 时允许明文账号请求。订阅跨站跳转会被拒绝，需用户核实最终 URL。账号 Cookie 不会附加到订阅/CDN 请求。
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

真实网站和 GUI 原生登录均已验证，原生接口返回 AES-GCM 加密配置并成功解出 22 个节点。标准订阅目前仍返回 HTTP 504，但原生配置刷新不依赖它。账号密码、Cookie、完整订阅令牌和节点配置不写入仓库。
