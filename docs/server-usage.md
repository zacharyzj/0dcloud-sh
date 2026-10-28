# 服务器部署和使用

部署目标：`root@152.136.54.246`，项目目录 `/root/0dcloud-sh`。系统 Debian 12，Python 3.11。

## 上传

在本地项目目录运行：

```sh
./deploy.sh root@152.136.54.246 /root/0dcloud-sh
```

脚本用 SCP 上传源码，覆盖前备份远端源码，不上传 `.git`、`.venv`、账号会话或密码。GitHub 下载受限时，可传入本地已经校验过的官方核心作为第三个参数：

```sh
./deploy.sh root@152.136.54.246 /root/0dcloud-sh "$HOME/.local/state/0dcloud-sh/bin/mihomo"
```

脚本输出远端初始化命令和该核心文件的 SHA-256；远端 `init.sh` 会再次校验后安装。

## 首次初始化

```sh
ssh root@152.136.54.246
cd /root/0dcloud-sh
./init.sh --client-api --api-url http://8.134.141.98:7985 --allow-http --direct --start
```

邮箱和密码可通过 `_0dcloud_mail` / `_0dcloud_pass` 环境变量传入，未设置时隐藏输入。脚本不保存密码。Debian 若缺少 venv，使用 `./init.sh --install-system --direct --start`，只额外安装 Python/venv/CA 系统包。

初始化会依次安装 Python 依赖、官方 Mihomo、GUI 原生登录、签名获取并解密配置、安装必需的地理规则数据库，`--start` 表示最后启动本机代理。当前原生 API 是 HTTP；`--allow-http` 表示明确允许明文传输账号密码，仅对本次客户端操作生效。可先运行 `proxyctl discover --direct` 查看 GUI 的公开配置。

GitHub 资产下载超时可使用上传的本地核心：

```sh
./init.sh --client-api --api-url http://8.134.141.98:7985 --allow-http --core-file .mihomo-upload --core-sha256 '<deploy.sh输出的SHA256>' --direct --start
```

已经登录和安装核心后，重复初始化可以复用现有状态：

```sh
./init.sh --client-api --skip-login --skip-core --allow-http --direct --start
```

如果上游订阅不可用，脚本返回失败，不会用 HTML 错误页面启动代理。可先完成环境部署：

```sh
./init.sh --skip-login --skip-core --skip-update
```

## 日常操作

```sh
cd /root/0dcloud-sh
source .venv/bin/activate

# 当前服务器已完成客户端登录，已有配置且核心正在运行。
# 再次刷新需环境变量；远端可交互隐藏输入，不要把密码写进命令历史。
read -r -s -p 'Email: ' _0dcloud_mail; echo
read -r -s -p 'Password: ' _0dcloud_pass; echo
export _0dcloud_mail _0dcloud_pass
proxyctl update --from-client-api --allow-http --direct
unset _0dcloud_mail _0dcloud_pass
proxyctl core geodata --direct
proxyctl stop
proxyctl start
proxyctl status
proxyctl nodes
proxyctl use '节点名称' --group Proxy

eval "$(proxyctl env)"
curl --connect-timeout 10 --max-time 30 https://www.google.com/generate_204 -o /dev/null -w '%{http_code}\n'
```

结束：

```sh
proxyctl stop
eval "$(proxyctl env --unset)"
```

Mihomo 作为后台子进程运行，退出 SSH 不会停止；没有配置开机自启动。代理仅绑定 `127.0.0.1:7890`。TUN 默认关闭；显式 `tun on` 会修改核心所在服务器的路由，DNS 沿用系统解析器。

## Dashboard

```sh
cd /root/0dcloud-sh
source .venv/bin/activate
# 先设置环境变量 _0dcloud_pass（不要在命令历史中填写密码）
proxyctl dashboard                 # 默认监听 0.0.0.0:8787，前台运行
# 另一个 SSH 终端
proxyctl dashboard --show-url
```

服务器已保存公网访问地址 `152.136.54.246`，直接打开 `--show-url` 输出的完整链接即可。面板提供节点选择、延迟测试、连接与断开、Rule/Global/Direct 切换和 TUN 开关。打开页面输入与 `_0dcloud_pass` 相同的密码；不需要邮箱。退出面板使当前登录会话失效，重启面板后需要重新登录。远程面板的 TUN 开关作用于服务器，不是访问面板的电脑。HTTP 无传输加密，HTTPS/SSH 转发和后台运行方式见 [面板说明](dashboard.md)。

2026-10-28 已后台启动 `http://152.136.54.246:8787/`，无需再启动第二个同端口进程。用上面的 `--show-url` 获取无令牌访问地址，登录密码与本地 `_0dcloud_pass` 一致。进程 PID 记录在私有状态目录 `dashboard.pid`，日志在 `dashboard.log`，未配置开机自启动。

CLI 同步提供 `proxyctl mode rule|global|direct`、`proxyctl tun on|off`。设置持久化，模式可在线切换。服务器日常使用可保持 TUN 关闭并通过代理端口访问。

状态与配置在 `/root/.local/state/0dcloud-sh`，目录权限 700、敏感文件 600；不要分享 Cookie、完整订阅 URL 或 YAML 中的密码。备份和 `.mihomo-upload` 在服务器项目目录之外或被 Git 忽略。

## 本次部署验证与限制

- 通过 SSH 连接及 SCP 上传完成，原项目备份在 `/root/0dcloud-sh-backup.uIeWRd/source-before.tar.gz`。
- 远端已安装新增 cryptography 依赖，真实 GUI `/authorization` 登录和 `/clientConfig` 签名刷新通过，响应 `enc=aes-256-gcm`，成功得到 22 个 AnyTLS 节点。
- GitHub 资产在远端直连超时，使用本地官方已校验的 v1.19.33 compatible 核心，经 SCP 后再次核对 SHA-256 安装。
- 服务器官方核心已启动，监听 `127.0.0.1:7890`。通过真实节点访问 Google 返回 HTTP 204、Cloudflare 返回 HTTP 200；不再依赖返回 504 的标准订阅地址。
- 配置依赖的 GeoSite/Country 数据库通过 `core geodata --direct` 从官方 MetaCubeX CDN 安装；初始化 `--start` 自动补齐。
- 53 项自动测试覆盖网站/原生协议、加密响应、发现回退、规则库、代理管理和错误脱敏。GitHub API 失败自动回退官方发布页。测试结果见 `docs/validation.md`。
