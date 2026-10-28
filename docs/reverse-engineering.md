# 0dcloud 2.0.30 逆向分析过程

分析日期：2026-10-28。对象为 Linux amd64 安装包 `0dcloud-2.0.30.deb`，包内版本 `2.0.30+74`。

本文记录从包结构检查、REA 初步识别，到 Go 核心反汇编、Dart AOT 快照解析、原生登录与刷新协议恢复，最后落地命令行实现的过程。“反编译”在这里包含多种方法：没有恢复完整 Dart 源码，也没有得到原项目源码。

静态分析阶段没有安装 deb、启动原 GUI 或调用它的业务函数。后续经过用户授权，独立实现的 CLI 才进行了真实账号登录、配置刷新和代理验证。这两个阶段的证据不能混为一谈。

## 1. 样本固定与解包

先记录大小及 SHA-256，再只解包数据和控制文件：

```sh
sha256sum 0dcloud-2.0.30.deb
dpkg-deb --info 0dcloud-2.0.30.deb
dpkg-deb --contents 0dcloud-2.0.30.deb
mkdir -p analysis/0dcloud-2.0.30
dpkg-deb --extract 0dcloud-2.0.30.deb analysis/0dcloud-2.0.30/root
dpkg-deb --control 0dcloud-2.0.30.deb analysis/0dcloud-2.0.30/control
```

安装包大小为 41,573,352 字节。关键对象的 SHA-256：

| 对象 | SHA-256 |
|---|---|
| `0dcloud-2.0.30.deb` | `fcb586f70c624e5ce693816353c72a56063e6a20c834cd340f96bcc8e7bf781a` |
| `usr/share/0dcloud/0dcloud` | `fb1040e383722a69ae325b4f30ba928d91721842df509a4ea25dfc2adb66ba7c` |
| `usr/share/0dcloud/lib/libapp.so` | `e9531d82ffc4832e325979858a9c4d51ad6fb526d0c8b68e5b2cea7065fa0e1c` |
| `usr/share/0dcloud/0dcloudCore` | `845793e2778a79c839caaf9c496d5d351ddc22ebbbaaec95cff5e224a943866f` |

阅读 `control/postinst`、`control/postrm` 和桌面入口后确认：安装脚本创建 `/usr/bin/0dcloud` 到 `/usr/share/0dcloud/0dcloud` 的符号链接并赋予执行权限，卸载脚本删除该链接。包文件清单中未发现 systemd unit、cron 或安装时下载执行脚本。这个结论只覆盖包本身，不覆盖依赖包和运行时动作；卸载脚本也没有清理用户配置。

用 `file`、`readelf`、`strings` 和启动壳反汇编区分三个层次：

| 层次 | 对象 | 后续分析方法 |
|---|---|---|
| 原生启动壳 | `0dcloud`，25,168 字节 | REA 调用 GNU objdump，确认 GTK/Flutter 初始化与插件 |
| 界面与账号业务 | `libapp.so`，12,682,160 字节 | REA AOT 识别、匹配版本 Dart 快照读取、Capstone 注释反汇编 |
| 代理核心 | `0dcloudCore`，31,760,568 字节 | REA Go 构建信息、`.gopclntab` 函数恢复、GNU objdump |

## 2. REA 环境与初步分析

用户提供的安装入口是 `npx rea-agents@latest setup`，环境由 nvm 管理。实际保存的分析来自 REA 6.8.0。doctor 显示当时 Node 24.18.0 低于该版声明的最低 24.22.1，同时没有配置 Ghidra/Hopper/IDA。正常返回的结果作为局部证据保存；不能把环境部分可用解释成已经完成完整反编译。

直接分析 deb 返回 `unsupported_target / unsupported binary format`，因此先由 `dpkg-deb` 解包，再分别提交 ELF 对象。以下记录的是 REA 输出中的 operation 和参数，不是推测的 CLI 子命令：

| 保存文件 | REA operation | 关键输入 | 证据标识 |
|---|---|---|---|
| `rea-go.json` | `inspect_go_binary` | `path=…/0dcloudCore` | `ev_8d6a228ced69f369639491fe5802f64d7a8b4de864d48facbcedf8eb99e33110` |
| `rea-launcher.json` | `inspect_with_objdump` | `operation=disassemble`，`provider_executable=objdump`，参数 `-d <启动壳>`，不跟随 debug links | `ev_d769f4362ba6cfe766f11d1d73d6bc3e4c7ff51d6dd30a204c1ce37a6cefeab9` |
| `rea-dart.json` | `inspect_dart_aot` | 合成 ZIP，`abi=x86_64` | `ev_b7568c80ea38392790a7f16c1af21a064acf91ded94cd0090e080a07e2e62f1c` |

REA 的 Dart 输入适配器要求 APK/ZIP 布局。本次把 deb 中原始 `libapp.so` 逐字节复制进合成 ZIP，并保存 `dart-wrapper-provenance.json` 说明来源。它不是原 APK，不能用它推断 Android 行为。`rea-dart.json` 关联的容器摘要与原 `libapp.so` 摘要是不同对象，不能混用。

REA 从导出符号和快照头识别 AOT 数据，字符串池覆盖为 partial，记录数还受到截断限制。其字符串、包 URI 和标识符只是定位入口的线索，不是完整类定义或调用关系。Go operation 也只恢复构建信息；后面的 35,688 个函数名来自额外解析，不是 REA 的 Go 反编译输出。

## 3. 确认核心来源，并追踪入口与 IPC

REA Go 构建信息显示：Go 1.24.1，Linux amd64，CGO 关闭，构建标签 `with_gvisor,cmfa`，本地替换关系为：

```text
github.com/metacubex/mihomo => ./Clash.Meta
```

因此可以确认包内代理核心基于 Mihomo，也就是 Clash.Meta 系列。`vcs.modified=true` 只证明构建工作区有改动，不能据此推断恶意，也不能证明整个 Flutter GUI 来自哪个开源仓库。

随后解析 ELF 的 `.gopclntab`，恢复 35,688 个函数名称与位置，保存为 `core.functions.json` / `core.functions.txt`。结合 GNU objdump 的 x86-64 反汇编，把直接调用目标映射回函数名，再围绕 `main.*`、配置应用、监听器、节点切换和文件操作追踪。

| 函数入口 | 观察到的调用 | 得到的结论 |
|---|---|---|
| `main.main` `0x1143300` | 把 `argv[1]` 传给 `main.startServer` | 原核心依赖 GUI 提供启动参数 |
| `main.startServer` `0x1143540` | 参数可转数字时 `net.Dial(tcp, 127.0.0.1:<参数>)`，否则 `net.Dial(unix, <参数>)` | 核心主动连接本地 IPC；函数名不能当作监听证据 |
| 同一 IPC 路径 | `ReadString('\n')`、`json.Unmarshal`；回包 `json.Marshal` 后追加换行 | UI 与核心交换换行分隔的 JSON |
| `main.applyConfig` `0x1140000` | `config.Parse` / `executor.ParseWithPath`、`hub.ApplyConfig` | 下载配置交给 Mihomo 应用 |
| `main.updateListeners` `0x113f500` | HTTP、SOCKS、mixed、redir、TProxy、TUN 等 ReCreate 调用 | 入站与 TUN 由配置驱动 |
| `main.updateConfig` `0x113fbe0` | `route.ReCreateServer` | 外部控制器与上述 IPC 是两条路径 |
| `handleChangeProxy.func1` `0x1140f40` | `CacheFile.SetSelected` | 节点选择有持久化机制 |
| `handleDelFile.func1` `0x1142da0` | 对给定路径调用 `os.Remove` / `os.removeAll` | 有权限敏感的本地文件删除指令；未证明公网可调用 |

另对 x86 `E8 rel32` 直接调用扫描 `os/exec.Command`、`shell.Exec`、`cmd.ExecCmd` 的交叉引用，定位到依赖中的 iptables、sing-tun 和 systemd-resolved 相关路径。它们证明相关模式有修改网络的能力，不证明每次启动都会执行，也不排除未覆盖的间接调用。

原核心不是标准 Mihomo 命令行入口。因此 `0dcloud-sh` 使用官方 Mihomo 可执行文件和本机控制器，不直接把 `0dcloudCore` 当成 `mihomo -f config.yaml` 启动。

## 4. 从 Dart 字符串转到精确版本快照解析

初步从字符串定位到 `/authorization`、`/clientConfig`、`clientSignature`、`decryptClientPayloadB64`、OSS/COS/TXT 发现、偏好存储等名称。仅靠字符串无法回答 HTTP 方法、签名字段顺序、时间单位和 AES 封装，因而继续读取实际对象与机器码。

快照识别结果为 Dart **3.9.2**，snapshot hash **`97ff04a728735e6b6b098bdf983faaba`**，Linux x64，**未使用压缩指针**。用 Blutter 相关构建工具准备精确版本 Dart 源码与预编译运行库，构建为 Linux/x64、`COMPRESSED_PTRS=0`。这里借用了构建基础设施，没有声称用 Blutter 一键恢复出完整业务源码。

现存构建目录的关键配置如下，路径相对原分析工作区：

```sh
cmake -S analysis/tools/blutter/dartsdk/v3.9.2 \
  -B analysis/tools/blutter/build/dartvm3.9.2-native-x64 -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DTARGET_OS=linux -DTARGET_ARCH=x64 \
  -DCOMPRESSED_PTRS=0
cmake --build analysis/tools/blutter/build/dartvm3.9.2-native-x64
```

上述命令要求源码和生成的 CMake 文件已准备好，不是仅下载原版 SDK 就能直接执行的完整安装脚本。静态库为 `libdartvm3.9.2_linux_x64.a`；构建定义包括 `DART_PRECOMPILED_RUNTIME`、`DART_TARGET_OS_LINUX`、`TARGET_ARCH_X64` 和 `PRODUCT`，不定义 `DART_COMPRESSED_POINTERS`。

`readelf -Ws libapp.so` 给出快照符号。结合 ELF 段到文件偏移的映射，本样本读取器使用以下位置：

| 快照对象 | 本样本中的位置 |
|---|---:|
| `_kDartVmSnapshotData` | `0x340` |
| `_kDartIsolateSnapshotData` | `0x9a80` |
| `_kDartVmSnapshotInstructions` | `0x584000` |
| `_kDartIsolateSnapshotInstructions` | `0x58ac80` |

这些位置绑定本文中的 `libapp.so` 摘要，不是通用常量；其他版本必须重新解析 ELF，不能盲目复用。

专用 `snapshot_reader.cc` 以私有映射读取目标，使用匹配运行库初始化 VM、创建 isolate group，再遍历全局 ObjectPool 和堆中的 Code 对象，输出三类 TSV 行：

```text
POOL <pool offset> <对象值>
IMM  <pool offset> <原始立即数/相对位置>
CODE <entry> <payload start> <size> <owner name>
```

它没有调用 `Dart_Invoke`、GUI 入口或应用业务函数。初始化和反序列化仍是解析原生快照，不应描述成完全没有执行解析器代码；对未知样本宜隔离处理。原分析输出保存为 `snapshot-metadata.tsv`。

## 5. 对机器码添加对象池与调用注释

`disassemble_snapshot.py` 使用 Capstone x86-64 模式，对 Code 对象提供的边界反汇编：

1. 读取 `CODE` 行，获得函数入口、payload 位置、大小和 owner 名称。
2. 对 `[r15 + offset]` 的池访问，使用 `POOL` / `IMM` 结果标注字符串、类型及常量。
3. 对直接 `call` / `jmp`，用入口地址映射回函数名。
4. 结合寄存器传参、Map/List 构造、Dio 的命名参数和异常分支人工核对数据流。

在原分析工作区，可按函数名过滤生成证据：

```sh
analysis/tools/snapshot_reader \
  analysis/0dcloud-2.0.30/root/usr/share/0dcloud/lib/libapp.so \
  > analysis/0dcloud-2.0.30/snapshot-metadata.tsv

python3 analysis/tools/disassemble_snapshot.py \
  analysis/0dcloud-2.0.30/snapshot-metadata.tsv \
  analysis/0dcloud-2.0.30/root/usr/share/0dcloud/lib/libapp.so \
  clientSignature
```

需要安装 Capstone；`snapshot_reader` 需用前述匹配运行库编译。这两个工具及大型原始证据保留于原分析工作区，不在本次单独 SCP 的 Markdown 文档中，远端项目没有这些文件时不能直接执行以上命令。

## 6. 恢复登录与自动刷新协议

关键函数与入口为：

| 函数 | 入口 | 核对点 |
|---|---|---|
| `_loginOnce` | `0x805a94` | GET 方法、查询参数及返回值分支 |
| `_fetchConfigFromClientApi` | `0x81a688` | POST body、签名头和响应分流 |
| `clientSignature` | `0x81b72c` | 拼接顺序、换行及 HMAC 输入 |
| `Hmac.` | `0x81b854` | SHA-256 算法 |
| `clientNonce` | `0x81ba6c` | 16 个随机字节转小写 hex |
| `getOrCreateClientDeviceId` | `0x81bc8c` | 随机设备 ID 的保存与复用 |
| `_sharedAesKey` | `0x81b48c` | 原始密钥文本 UTF-8 后 SHA-256 |
| `decryptClientPayloadB64` | `0x81b05c` | Base64、IV、GCM tag 与空 AAD |
| `_x` | `0x81804c` | 内置发现地址和文本常量的解混淆 |

登录是 `GET /authorization?email=…&passwd=…`，不是网站的 `POST /auth/login`。依据是 `_loginOnce` 的 `email` / `passwd` Map、`queryParameters` 命名参数及 Dio `get` 调用。成功分支要求整数 `ret == 1`。网站表单脚本是另一路证据，见 [账号协议](auth-protocol.md)。

刷新是 `POST /clientConfig`，`application/x-www-form-urlencoded` body 为 `email`、`passwd`、`platform=linux`。请求还包含 `User-Agent: 0dcloud/2026`、`X-App-Version` 和 `X-Client-Id/Ts/Nonce/Sig`。

从时间转换、随机字节循环、字符串构造、UTF-8 编码和 Hmac 构造器恢复出：

```text
timestamp = Unix 秒的十进制字符串
nonce     = 16 个安全随机字节的小写 hex，共 32 字符
device_id = 稳定保存的 32 字符随机小写 hex
message   = POST\n/clientConfig\n{timestamp}\n{nonce}\n{device_id}\n{email}
signature = lowercase_hex(HMAC_SHA256(UTF8(内置签名文本), UTF8(message)))
```

消息末尾没有换行；签名密钥文本不做 Base64 解码或预哈希；密码和 platform 不在签名消息内。GUI 额外加密本地偏好中的设备 ID，与下载配置加密不是同一层，也不要求 CLI 读取系统 machine-id。

加密响应要求 `ret == 1` 且 `enc == aes-256-gcm`：

```text
key     = SHA256(UTF8(内置 AES 文本))         # 32 字节
payload = Base64Decode(data)
payload = 12 字节 IV | 密文 | 16 字节 GCM tag
AAD     = 空字节串
```

验证 GCM tag 后才 UTF-8 解码、解析 YAML。非 AES 分支的 `_decodePlainClientPayload` 尝试 Base64→UTF-8，失败或空结果时使用原文本。密文认证失败不能回退成明文配置。

另对 nonce、设备 ID、AES 派生、HMAC、hex 输出和明文分支逐个生成 `client-*.check.txt`，核对 tagged integer、循环边界、参数寄存器及调用目标。完整字段定义见 [原生协议](native-client-protocol.md)。本文不抄录共享密钥原文，也不包含用户密码或配置内容。

## 7. 恢复 API 发现与混淆格式

沿 `_x`、混淆 getter、公开发现函数追踪，恢复 OSS、DNS TXT 和腾讯 COS 等候选来源。观察到的配置字段按 `api_url`、`api_url_fallback`、`api_urls` 顺序收集，递归处理列表、去重并移除尾部斜杠。

TXT 分支 `_fetchConfigFromDohTxt` 入口为 `0x81890c`：遍历 DoH JSON 的 `Answer`，只收集 `type == 16` 的 TXT，先拼接各记录的 data，再清理引号、空白，然后解析配置。回退路径为：

```text
拼接 TXT → 清理 → Base64 解码 → profileXor → UTF-8 → JSON Map
profileXor: output[i] = input[i] XOR key[i % 16]
```

不是逐条 TXT 独立解码，也不是看到 TXT 中的字符串就跟随为下载 URL。直接 JSON 分支也存在，但 GUI 的全局去引号行为会破坏普通带引号 JSON，不能忽略 Base64+XOR 分支。

2026-10-28 公开 OSS 配置成功提供客户端 API `http://8.134.141.98:7985`；当时 COS 返回 403。发现结果只作为候选展示，不自动把账号发送到新地址。该 HTTP 地址经用户明确授权才做凭据验证；签名与响应加密不保护 HTTP 请求里的邮箱密码。

GUI 的“刷新”由原生协议直接取得配置，因此不要求用户手动填写标准订阅 URL。`clientconfig://app2` 是内部来源标记，不是可直接下载的 URL。

## 8. 从静态结论到独立实现与真实验证

恢复协议后，在 `odcloud_sh/client_api.py` 独立实现发现、登录、签名、设备 ID、响应解密和配置保存。网站 Cookie 与客户端 Cookie 分开保存；账号从环境变量读取；失败时保留原配置，不把 HTML 错误页或未认证密文写成订阅。

验证按层进行：

1. 用独立 HMAC 测试向量、真实 GCM 认证/错误 tag、TXT 编码与字段顺序测试确认实现。
2. 用户授权后，从 `.zshrc` 环境变量加载账号，对确认的 API 真实登录和刷新。
3. 得到 22 个 AnyTLS 节点、3 个订阅代理组和 559 条规则；官方 Mihomo 启动后还会显示其 `GLOBAL` 组。
4. 使用官方 Mihomo v1.19.33 compatible，安装所需 GeoSite/Country 数据库，实际代理访问 Google 返回 204、首页返回 200。
5. 标准网站订阅独立返回 504；原生刷新不依赖这条失败路径。

之后 CLI 增加的 Dashboard、连接/断开和 TUN 控制是本项目实现，不是从样本恢复的 GUI 源码。TUN 实测发现系统 DNS 被自动路由接管后超时，修复为排除系统 IPv4 DNS 地址。服务器已在 TUN 开启时用 `curl --noproxy '*' -4` 验证 Google；66 项测试覆盖 DNS 修复，后续密码登录完成后共 68 项测试。详细时间线与限制见 [验证记录](validation.md)。

## 9. 证据保留位置与分析边界

原分析工作区主要文件：

| 文件组 | 内容 |
|---|---|
| `analysis/0dcloud-2.0.30/rea-*.json`、`evidence-index.json` | REA 原始结果、operation、证据标识与限制 |
| `REPORT.md`、`core-findings.md` | 最初静态报告与 Go 核心追踪；其中早期“尚未恢复协议”的结论已被后续工作补充 |
| `core.functions.*`、`core.main.disasm.txt`、`core.*xrefs.txt` | Go 函数表、main 反汇编和命令调用交叉引用 |
| `snapshot-metadata.tsv`、`client-*.disasm.txt`、`client-*.check.txt` | Dart 对象池、代码边界与协议机器码证据 |
| `discovery-format-findings.md`、`doh-txt.disasm.txt`、`profile-xor.disasm.txt` | 发现格式和回退路径证据 |
| `analysis/tools/snapshot_reader.cc`、`disassemble_snapshot.py` | 专用快照读取和注释反汇编源码 |
| `analysis/tools/dart-*.log` | 匹配版本运行库的配置与构建日志 |

本次仅上传本文，原 deb、提取二进制、Dart 源码/运行库、大型元数据和真实配置不随文档提交。文档中的样本摘要、函数位置和证据标识用于定位既有结果，不意味着远端仓库已包含所有原始证据。

尚未完成的工作包括：完整还原 GUI 源码与所有依赖修改、逐一核对诊断上传字段与脱敏、确认所有 GUI 默认开关与 Linux 提权/异常恢复逻辑，以及服务端的时钟窗口、nonce 防重放和设备绑定策略。静态能力不等于实际触发；通过登录和代理验证也不等于已经证明整个原客户端安全。
