# GUI 原生登录和刷新协议

依据 `0dcloud-2.0.30.deb` 中的 Dart AOT 库恢复，并通过真实账号登录及配置下载验证。以下常量来自公开客户端，未记录用户账号、密码、Cookie、订阅令牌或节点配置。

## 取证依据

- `libapp.so` SHA-256：`e9531d82ffc4832e325979858a9c4d51ad6fb526d0c8b68e5b2cea7065fa0e1c`。
- Dart 3.9.2，snapshot hash `97ff04a728735e6b6b098bdf983faaba`，x64 Linux，无压缩指针。
- REA 原始 partial 字符串证据：`ev_b7568c80ea38392790a7f16c1af21a064acf91ded94cd0090e080a07e2e62f1c`。后续函数、常量恢复来自匹配版本 Dart 运行库的离线反序列化和 Capstone 注释反汇编，不冒充 REA 完整反编译。
- 分析脚本位于工作区 `analysis/tools/snapshot_reader.cc`、`analysis/tools/disassemble_snapshot.py`；没有调用 GUI 入口或执行业务函数。

| 样本函数 | 入口 | 确认用途 |
|---|---|---|
| `_loginOnce` | `0x805a94` | GET `/authorization`，查询参数 email/passwd |
| `_fetchConfigFromClientApi` | `0x81a688` | 构造签名请求头及 form-urlencoded body |
| `clientSignature` | `0x81b72c` | HMAC-SHA256 签名串 |
| `Hmac.` | `0x81b854` | 明确使用 `_Sha256` |
| `clientNonce` | `0x81ba6c` | 16 随机字节转 32 位小写 hex |
| `getOrCreateClientDeviceId` | `0x81bc8c` | 稳定随机设备 ID |
| `_sharedAesKey` | `0x81b48c` | SHA256(UTF8 内置 AES 文本) |
| `decryptClientPayloadB64` | `0x81b05c` | 12 字节 IV + GCM 密文及 tag |
| `_x` | `0x81804c` | 恢复六个内置发现地址和密钥文本 |

## 公开发现

`proxyctl discover --direct` 从 GUI 内置 OSS 读取 `api.json`。失败时尝试 DNS TXT 和腾讯 COS；TXT 支持跨记录拼接、Base64 和样本 `profileXor` 解码。地址字段按 `api_url`、`api_url_fallback`、`api_urls` 顺序读取，兼容嵌套列表并去重。

发现只展示候选地址，不发送账号、不保存新的凭据目标。2026-10-28 OSS 成功返回 `http://8.134.141.98:7985`；COS 当时返回 403。发现服务将来可能变化，应重新核实地址。HTTPS 不可用时，必须对每次客户端命令显式开启 `--allow-http`。

## 登录

GET `{base}/authorization`，参数 `email`、`passwd`，成功要求 JSON 中整数 `ret == 1`。保存客户端 Cookie，不与网站 Cookie 混用。CLI 无需为了刷新调用 GUI 的套餐、订单或用户资料页面。

客户端密码不会保存。`update --from-client-api` 每次都从 `_0dcloud_mail`、`_0dcloud_pass` 取得账号；缺失时提示重新加载并导出环境变量。设备 ID 保存为 32 位随机小写 hex，不读取系统 machine-id。GUI 对偏好中的设备 ID 进行本地加密；CLI 使用权限为 600 的独立状态文件保存该非凭据标识。

## 配置刷新

POST `{base}/clientConfig`，form-urlencoded：

```text
email=<环境变量>
passwd=<环境变量>
platform=linux
```

请求头包括 `User-Agent: 0dcloud/2026`、`X-App-Version: 2.0.30`、`X-Client-Id/Ts/Nonce/Sig`。时间戳为 Unix 秒，nonce 为 16 安全随机字节的小写 hex。

签名消息为以下 UTF-8 字符串，末尾没有换行：

```text
POST\n/clientConfig\n{timestamp}\n{nonce}\n{device_id}\n{email}
```

用样本内置签名文本的原始 UTF-8 字节做 HMAC-SHA256，输出小写 hex。不是把该文本做 Base64 解码。`enc == aes-256-gcm` 时，`data` 为 Base64：

```text
12 字节 IV | 密文 | 16 字节认证 tag
```

AES key 为 SHA256(UTF8 内置 AES 文本)，空 AAD。必须先验证 GCM tag，再解析 UTF-8 YAML。tag 错误、JSON/节点配置无效时保留旧配置。非加密响应兼容明文和 Base64；未知加密标记会明确拒绝。

HMAC 和响应加密不会隐藏 HTTP 请求中的账号密码。当前 HTTP API 的使用需要用户明确接受这一限制；代码不关闭 HTTPS 证书校验，也不自动跟随账号重定向。

## 使用

```zsh
source ~/.zshrc
export _0dcloud_mail _0dcloud_pass
./proxyctl login --client-api --api-url http://8.134.141.98:7985 --allow-http --direct
./proxyctl update --from-client-api --allow-http --direct
./proxyctl core geodata --direct
./proxyctl start
```

运行中刷新仅更新缓存；应用到运行核心时，再执行 `stop`、`start`。需要手动选择节点时使用实际组名：`proxyctl use '节点名称' --group Proxy`。

原生刷新成功取得 22 个 AnyTLS 节点，与网站标准订阅下载路径独立。当前网站订阅的 504 不再阻塞原生刷新。完整运行验证见 [验证记录](validation.md)。
