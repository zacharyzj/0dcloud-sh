# HTTP 控制面板

安装 Python 项目依赖后即可启动，无需 Node、前端构建或额外 UI 下载：

```sh
cd /path/to/0dcloud-sh
source .venv/bin/activate
source ~/.zshrc
export _0dcloud_pass
proxyctl dashboard --host 0.0.0.0 --port 8787
```

默认值就是 `0.0.0.0:8787`。启动前先用现有命令完成登录、刷新配置和安装核心。面板中的“启动核心”使用已缓存的 `subscription.yaml` 和默认代理端口 7890；使用自定义核心路径或私有实例时，CLI 和面板需传入相同的 `--core` / `--state-dir` 全局选项。

## 访问

终端输出直接访问链接，不含令牌。在服务器上另开终端也可以查看：

```sh
proxyctl dashboard --show-url
```

直接打开输出链接即可。监听全部网卡时自动使用出口网卡 IP；监听本机时使用 `127.0.0.1`。云服务器在 NAT 后时可指定 `proxyctl dashboard --public-host 152.136.54.246`，该地址会保存供以后启动和 `--show-url` 使用，不需要外部 IP 查询服务。打开页面后输入与服务启动时环境变量 `_0dcloud_pass` 相同的密码。密码仅在本地服务校验，不触发账号站点登录；服务端只在内存保存加盐 scrypt 哈希，不写入状态文件、日志或访问链接，浏览器不把密码保存到 localStorage/sessionStorage。未设置该环境变量时面板拒绝启动。

登录成功设置 HttpOnly、SameSite=Strict Cookie，服务端会话最多有效 12 小时。退出面板使当前会话失效，重启面板使全部会话失效；重新加载页面可以恢复尚未过期的会话。JSON 接口校验浏览器来源，登录接口每分钟最多接受 10 次尝试。HTTPS 来源下 Cookie 带 Secure 属性。

`dashboard-access.json` 只保存访问地址、端口等信息，权限 600，不再包含认证令牌或密码。`--show-url` 显示上次启动的地址，不表示进程当前一定存活。服务不返回订阅 URL、节点密码或核心 secret，不使用外部脚本和字体。HTTP 不加密登录密码与会话，跨公网建议 HTTPS 或 SSH 转发。

HTTPS 反向代理需保留浏览器的 Host 与 Origin；服务接受 Host 一致的 HTTP/HTTPS 来源，拒绝异源浏览器请求。也可用 SSH 加密转发：

```sh
ssh -N -L 8787:127.0.0.1:8787 root@152.136.54.246
```

随后将访问链接的主机改为 `127.0.0.1`，保留端口。监听全部网卡不等于云安全组或系统防火墙已放通端口，本工具不修改这些规则。

## 操作

- **代理组与节点**：点击 Selector 组中的节点切换选择；自动选择与故障转移组显示核心的当前结果。
- **延迟测试**：通过选定节点请求 Google `generate_204`，显示毫秒数；这是连通延迟测试，不测下载带宽。每个节点都可单独测试。
- **Proxy Mode**：Rule 按订阅规则分流，Global 使用 `GLOBAL` 组，Direct 全部直连。切换立即生效，已有长连接可能继续使用原路径。
- **TUN**：默认关闭。Linux 核心需要可访问的 `/dev/net/tun` 与 `CAP_NET_ADMIN`；权限不满足时禁止开启并显示原因。创建 `odcloud0`，使用 mixed 栈、自动路由，保留系统 DNS，不启用 auto-redirect、DNS 劫持和 fake-IP。它改变的是服务端的路由；先确认服务器连接和网络条件。
- **连接 / 断开**：连接使用缓存配置启动核心，等待控制器就绪后显示已连接；断开停止核心及其代理连接，面板继续运行。重复操作安全；等待过程中按钮禁用，失败时显示错误并重新读取真实状态。退出面板只退出浏览器会话，连接保持。连接不触发登录和订阅刷新，也不更改浏览器所在机器的系统代理。新下载的订阅需断开后重新连接才应用。
- **配置保留**：模式、TUN 设置与核心保存的节点选择在重新连接后恢复；面板运行期间也会保留断开前的代理端口。已连接表示核心服务就绪，不代表每个远端节点均可用，可通过延迟测试检查节点连通性。

TUN 开启时读取 `/etc/resolv.conf`，将非回环 IPv4 DNS 地址排除出自动路由；使用 systemd-resolved 本地 stub 时也读取 `/run/systemd/resolve/resolv.conf` 的上游地址。这样系统 DNS 请求经原网卡发送，不依赖远端节点的 UDP 能力。此方式不加密或替换系统 DNS，系统 DNS 不可达或返回错误记录仍会影响解析；DNS 地址变更后重新切换 TUN 以更新排除项。验证 TUN 可使用 `curl --noproxy '*' -I https://www.google.com`，ICMP ping 不代表远端节点的代理能力。

模式和 TUN 设置保存在 `core-options.json`，面板与 CLI 共用。设置在线更新后会读取控制器确认；保存失败时尝试恢复原值。下次启动应用这些设置。若上次启用 TUN 而当前权限不足，核心拒绝启动，可先运行 `proxyctl tun off`。

服务在前台运行，`Ctrl-C` 只关闭面板。核心独立运行，关闭浏览器也不会停止核心。需要后台运行时可用进程管理器，或：

```sh
umask 077
nohup .venv/bin/proxyctl dashboard --host 0.0.0.0 --port 8787 >dashboard.log 2>&1 </dev/null &
```

启动前必须导出 `_0dcloud_pass`，后台进程继承该变量。密码变更后需要带新变量重启面板。上述方式不配置开机自启动。
