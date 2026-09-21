# 两个参考项目的深度解剖：LocalSend 与 RustDesk

这份文档是整个项目的**第一性依据**。EverSend 的每一个设计决定，要么是在抄这两个项目验证过的正确做法，要么是在修它们暴露出来的错误做法。所有结论都来自逐行阅读源码（只读，未改动其任何一个文件），并标注了文件与行号出处。

---

## 0. 一句话结论

| 项目 | 它擅长什么 | 它在大文件/稳定性上栽在哪 |
|---|---|---|
| **LocalSend** | 局域网发现（UDP 组播 + HTTP 回注册）、零配置、协议简单 | **没有任何断点续传**；整文件一个 HTTP POST，断了从头再来；全局只允许一个会话；校验和只在**整个文件写完之后**才核对，错了就整文件重传（最多 3 次） |
| **RustDesk** | 互联网穿透（会合 + TCP 打洞 + 中继）、端到端加密身份体系 | 文件传输是**每 tick 一块**的定时器模型（块固定 128 KiB，被控端 5 ms/tick）→ **约 26–29 MB/s 硬顶**；块号 `blk_id` 恒为 0（没有偏移）；无逐块 ACK/滑窗/重传；续传只靠 `(size, mtime 秒)`，无内容哈希，偏移还被 `uint32` 截断在 4 GiB |

**EverSend 的定位**：拿 LocalSend 的"零配置局域网"体验，换掉它的传输层；拿 RustDesk 的穿透思路，换掉它的文件传输层。

---

## 1. LocalSend 解剖

### 1.1 它是什么

Flutter UI + **Rust 协议内核**（`packages/core`，14225 行）。通信全在 Rust 侧，Dart 只做 UI 和编排（`app/AGENTS.md`）。

### 1.2 发现机制（值得照抄的部分）

`packages/core/src/multicast/mod.rs`：

- 组播组 `224.0.0.167`（`mod.rs:28`）。注释说明为什么选 `224.0.0.0/24`：**部分 Android 设备只能收到这个网段的组播**。
- 端口复用 HTTP 端口 `53317`（`mod.rs:39`）。
- **每个网卡一个 socket**（`socket.rs`）：`SO_REUSEPORT`/`SO_REUSEADDR` + `IP_MULTICAST_IF`。原因很实在——一个 socket 只能从一个网卡发包。
- 公告不是"一问一答"，而是**单向下发 + HTTP 回连**：收到公告的设备主动向公告方发 `POST /api/localsend/v2/register`（`mod.rs:1-9` 模块注释）。**UDP 只用来喊，从不用来答**。这让发现过程完全无状态，丢包只意味着下次重试。
- 公告连发 3 次，间隔 100/500/2000 ms（`mod.rs:45-49`）——既对抗单包丢失，也给刚启动的对端留出监听时间。
- 自己的公告会回环收到，用 fingerprint 过滤（`mod.rs:376`）。
- 连续 10 次接收错误才放弃该网卡（`mod.rs:58`），因为 Windows 上"上一次 send 的 ICMP 错误会在下一次 recv 时浮出来"。
- 兜底：`discover_staged` 先试已知地址，宽限期内没有确认就**扫整个 /24 网段**，并发 50（`discovery/mod.rs:320-340`）。这是给"完全不转发组播的企业网络"准备的。

**EverSend 采纳**：每网卡一 socket、组播只喊不答、公告连发、连续错误容忍、子网扫描兜底。**追加**：UDP 广播（IGMP snooping 剪枝时组播收不到，广播往往能到）+ mDNS（很多 AP 会可靠转发 224.0.0.251 却丢自定义组）。

### 1.3 HTTP v2 协议

| 端点 | 作用 |
|---|---|
| `POST /api/localsend/v2/register` | 回注册（发现） |
| `GET /api/localsend/v2/info` | 查设备信息 |
| `POST /api/localsend/v2/prepare-upload` | 发起传输 → 返回 `sessionId` + `{fileId: token}` |
| `POST /api/localsend/v2/upload?sessionId=&fileId=&token=` | 上传**单个文件**（body 是裸字节流） |
| `POST /api/localsend/v2/cancel?sessionId=` | 取消 |
| `POST /api/localsend/v2/prepare-download` / `GET .../download` | 反向下载（网页发送用） |

`FileDto`：`id / fileName / size / fileType / sha256? / preview? / metadata?`（`model/transfer.rs:126`）。

### 1.4 安全模型

- 每设备自签证书，**强制双向 mTLS**（`http/server/mod.rs:373`，服务网页时客户端证书可选，否则强制）。
- 身份 = **客户端证书 DER 的 SHA-256 大写十六进制**（`http/server/mod.rs:544`）。
- 证书指纹**钉死在 TLS 握手阶段**（`http/client/server_cert_verifier.rs`）：指纹不匹配的对端在握手期就被拒，请求根本发不出去。
- `register` 时如果 payload 里的 fingerprint 和证书指纹不一致，**直接不产生事件**（`http/server/v2.rs:163-166`），防伪造。
- v3 引入了 nonce 交换（`/api/localsend/v3/nonce`）作为额外的重放防护。

**EverSend 采纳**：身份不可伪造、指纹在握手期校验、指纹不匹配直接拒绝。**改进**：不用 TLS（去掉 TLS 记录层与每连接握手开销），改用 X25519 + AEAD，并把会话密钥绑定到双方 nonce，让重放握手必然得到不同密钥。

### 1.5 它为什么传不好大文件 —— 逐条根因

这是本项目最有价值的一段，因为它就是用户报的"大文件传输失败、断流"。

1. **完全没有断点续传。**
   `POST /upload` 的 body 就是整个文件（`http/client/v2.rs:233`）。`save.rs` 把 body 直接写盘，最后校验总字节数。中途断线 → `SaveResult::Failed` → 文件状态置 `Failed`。
   注意 `v2.rs:646` 的 `MAX_UPLOAD_ATTEMPTS = 3`：**它只对"校验和不匹配"生效**（`finalize_file` 里 `HashMismatch` 分支），网络中断根本不在重试范围内。20 GB 的文件传到 99% 断了，只能从 0 开始。

2. **全局只有一个会话槽。**
   `V2State.session: Mutex<Option<SessionStateV2>>`（`http/server/mod.rs:63`），第二个 `prepare-upload` 直接 `409 Conflict`（`v2.rs:258`）。一次卡住的会话会挡住所有后续传输。

3. **上传严格绑定源 IP。**
   `session.sender_ip != client_info.ip` → `403 Invalid token or IP address`（`v2.rs:387`）。
   这是"Linux 传不了 Windows"的高概率元凶：Windows 常有多个适配器（Wi-Fi + Hyper-V vEthernet + WSL + VPN），`prepare-upload` 和 `upload` 这两个**独立 TCP 连接**完全可能从不同源 IP 出去；IPv6 的 scope id 也被算进 `PeerIp` 参与比较。

4. **校验和只在最后核对，错了整文件重来。**
   `save.rs` 边写边算哈希，但比对发生在整个文件落盘之后。不匹配 → `422` → 整个文件重传。

5. **TLS 与 HTTP 的固定开销。**
   每个文件一次 mTLS 连接、每次响应都要 `verify_cert_from_res` 重新验链；TLS 记录约 16 KiB，写侧靠 512 KiB 合并缓冲缓解（`save.rs` `WRITE_BUFFER_SIZE`），但读侧每帧仍是 TLS 记录大小。

6. **Dart/Rust 之间的信道是 `try_send`，会丢事件。**
   `register` 事件通道满了直接丢弃（`v2.rs:179`，"Dropped a register event"）。进度事件同样可能丢（`save.rs` 的 `progress_tx.try_send`）。这不影响数据正确性，但 UI 会出现"进度不动"的观感。

7. **续传能力受限于平台。**
   Android 走 SAF 文件描述符（`FileUploadTarget::Fd`），没有路径也就没有"在同一个文件上继续写"的自然语义。

**EverSend 的对策**：见本文第 3 节，逐条对着修。

### 1.6 LocalSend 的 WebRTC 远程传输

`packages/core/src/webrtc/`，配一个 Axum WebSocket 信令服务器（`server/src/controller/ws_controller.rs`）：

- 信令按 **IP 房间**分组（同一公网 IP 的客户端互相可见）。
- SDP 用 **zlib 压缩 + base64（无填充）**，放在 WebSocket 消息里（`webrtc/signaling.rs:80`）。
- 数据通道 `ordered: true`、可靠、**16 KiB 分片**（`webrtc/webrtc.rs:1288`），靠 `buffered_amount()` 轮询做背压（`wait_buffer_empty`，`webrtc.rs:1282`）。
- 用同一个 6 位 PIN 做端到端确认。

**EverSend 的看法**：WebRTC 数据通道的 16 KiB 分片 + SCTP 层，吞吐远不如裸 TCP 多流；而且它需要 SDP/ICE/DTLS 一整套。远程传输我选择"自建会合 + TCP 打洞 + 中继"（见 `REMOTE_DESIGN.md`），把 WebRTC 留作可选第三条腿。

---

## 2. RustDesk 解剖

完整报告见 `rustdesk_远程传输架构研究报告.md`（681 行，含全部 file:line 取证）。这里只列对设计有决定性的部分。

### 2.1 端口与消息集

| 端口 | 用途 |
|---|---|
| 21115 TCP | NAT 类型测试 + 本机 console |
| 21116 TCP/UDP | **hbbs 会合**（UDP 只收注册类，TCP 承载打洞信令） |
| 21117 TCP | **hbbr 中继** |
| 21118/21119 TCP | 上述两者的 WebSocket 版本 |
| 21119 UDP | 客户端之间的局域网发现广播（不经服务器） |

核心消息：`RegisterPeer` / `RegisterPk` / `PunchHoleRequest` / `PunchHole` / `PunchHoleSent` / `PunchHoleResponse` / `RequestRelay` / `RelayResponse` / `FetchLocalAddr` / `LocalAddr` / `TestNatRequest`。

### 2.2 打洞的精髓

**服务器只做介绍人，永远只告诉对端"我观测到的你的地址"**，客户端从不自己上报 IP。

- 控制端在 21116 上发 `PunchHoleRequest`，然后**把这条 TCP 连接挂住不关**，作为 `PunchHoleResponse` 的回程通道（`server/src/rendezvous_server.rs:509-521`）。这是很多人实现打洞时踩的第一个坑：连接一关，答复就没了。
- 服务器把控制端的**观测地址**用 `AddrMangle` 编码后 UDP 推给被控端（`PunchHole`）。
- 被控端回 `PunchHoleSent`，服务器再把被控端的观测地址回给控制端。
- `AddrMangle` 不是加密，只是混淆：IPv4 会把 IP、端口和微秒时间戳混进一个 u128 后剥掉高位零字节（1–16 字节变长）；IPv6 是明文 18 字节。同一地址每次编码都不同，可防简单重放。

### 2.3 TCP 打洞的关键细节（最值得复用）

被控端拿到控制端地址后（`src/rendezvous_mediator.rs:1192-1208` + `1543-1668`）：

1. **新建一条到 hbbs 的 TCP 连接，取它的 `local_addr`** —— 这是"本机访问外网那块网卡的私网 IP + 临时端口"，比枚举网卡可靠得多，天然排除 Docker/VPN 网卡。
2. 用这条连接把 `PunchHoleSent` 发给服务器，然后 drop。
3. 在**同一个 `local_addr`** 上用 `SO_REUSEADDR/SO_REUSEPORT` 起监听，然后在 18 s 窗口内**一边反复 connect（打洞）一边 accept**。
4. 打洞节奏 150 ms 起每次 ×1.5、封顶 2 s；截止后仍给在飞的 connect 留 `PUNCH_GRACE=3000 ms`——因为**中途掐断会毁掉对端已经建立的连接**。
5. **accept 不过滤来源地址**（NAT 可能换外部地址池）。

控制端则用"它到 hbbs 那条 TCP 连接的 local_addr"去打洞（`src/client.rs:1460-1470`）——**复用同一个本地端口，NAT 映射才能对上**。

### 2.4 同内网的本地地址交换（`FetchLocalAddr`/`LocalAddr`）

服务器判定双方同内网后，让被控端**新建一条到服务器的 TCP 连接**，取 `socket.local_addr()` 当候选地址回给控制端（`src/rendezvous_mediator.rs:764-868`）。这比枚举网卡准确得多，也天然过滤掉虚拟网卡。

**EverSend 的局域网发现用的正是同一个思想**：与其猜哪块网卡对，不如枚举真实接口 + 每个接口都发一遍 + 让对端用收到包的那个接口地址回连。

### 2.5 中继（hbbr）

- 配对键是 **uuid**（`RequestRelay.uuid`），先到者存起来等最多 30 s，第二个同 uuid 到达即配对（`server/src/relay_server.rs:461-498`）。
- 双方都不是 WebSocket 时**双侧 `set_raw()`**，退化成纯字节管道——不解密、不解析、不注入。
- 30 s 无数据断开；单连限速 128 Mbit/s、全局 1024 Mbit/s；**中继看不到明文**（端到端加密在客户端之间）。
- 定位很清楚：**稳定但慢的兜底**。

### 2.6 它的安全短板（新实现必须补掉）

- **开源 hbbs 根本没有 `KeyExchange`**（服务端零命中），客户端只在带 `token`/`switch_code`（Pro 特性）时才尝试 `secure_tcp` → **自建部署下会合信令是明文的**。
- `PunchHoleResponse.pk`（服务器签名的对端公钥）**不可伪造但可被剥离**，从而把会话降级到非加密。
- 客户端↔客户端那层是真的：`SignedId`（用长期 Ed25519 私钥签 一次性 X25519 公钥 + DTLS 指纹）→ 控制端验签并要求 `id == peer_id` → 回 `PublicKey{自己的临时 X25519 pk, 用 X25519 box 封的 32 B 会话密钥}`。
- nonce 是 24 字节里的前 8 字节小端计数器，收发各一个，**不上线**；但**载荷 ≤ 1 字节的帧既不解密也不校验**（`hbb_common/src/tcp.rs`）——这是 0 长心跳能穿过加密层的原因，也是风险点。

### 2.7 它为什么传文件慢 —— 逐条根因

1. **每 tick 一块的定时器模型。**
   块大小固定 `BUF_SIZE = 128 * 1024`（`libs/base/src/fs.rs:956`）。控制端 `MILLI1 = 1 ms`/tick，被控端 CM 侧 `MILLI5 = 5 ms`/tick（`src/ui_cm_interface.rs:491`）→ 理论 **26–29 MB/s**。这不是网络限制，是实现限制。

2. **块没有编号。**
   `blk_id` 在 Rust 代码里**从未赋值、从未读取**（全仓 grep 只命中 `offset_blk`）→ 线上恒为 0，块是纯顺序的、没有 `(file_id, offset)`。收端无法乱序重组、无法并行、无法只补缺口。

3. **没有应用层 ACK / 滑窗 / 重传。**
   收方收一块写一块，发方不等确认，流控完全靠 TCP 反压。

4. **多文件串行。**
   `handle_read_jobs` 每个 tick 只处理第一个非挂起 job 就 `break`（`fs.rs:1350-1382`）。

5. **续传只靠元数据，没有内容哈希。**
   `<目标>.download` + `<目标>.digest`（内容只有 `{size, modified}`，**modified 是秒级 mtime，不是哈希**）。源文件被原地改而 size/mtime 恰好不变 → **静默损坏**。
   而且续传偏移是 `uint32` 的 `offset_blk` → **截断在 4 GiB**。

6. **解压失败被吞成空 Vec。**
   `compress.rs:37-39` 的 `.unwrap_or_default()` → 静默写 0 字节。必须改成显式错误。

7. **文件 I/O 与网络 I/O 分属两个进程**，块要在 IPC 上再拷一次（换来"以服务身份跑网络、以用户身份跑文件"的权限分离）。

**EverSend 的对策**：块带 `(file_id, offset, length)`、收端 `pwrite`、应用层信用窗口由接收方驱动、多流并行、逐块 CRC + 整文件 BLAKE2b、分块级修复。详见下一节。

---

## 3. EverSend 的设计取舍：抄什么，改什么

### 3.1 直接采纳（两个项目都验证过的）

| 做法 | 出处 |
|---|---|
| 每网卡一个组播 socket | LocalSend `multicast/socket.rs` |
| 组播只喊不答，答复走 TCP | LocalSend `multicast/mod.rs` 模块注释 |
| 公告连发 3 次 | LocalSend `ANNOUNCE_DELAYS` |
| 连发失败容忍 N 次再放弃网卡 | LocalSend `MAX_CONSECUTIVE_RECEIVE_ERRORS` |
| 子网扫描兜底 | LocalSend `scan_subnet` |
| 服务器只告诉对端"我观测到的地址" | RustDesk 打洞模型 |
| 用"连外网那块网卡的 local_addr"当候选 | RustDesk `FetchLocalAddr` |
| 握手期校验身份，不匹配直接拒 | LocalSend 证书指纹钉死 |
| 会话密钥绑定双方 nonce | RustDesk `SignedId`/`PublicKey` 思路 |
| 中继是零信任的哑管道 | RustDesk hbbr |
| 错误必须显式，不能吞 | RustDesk 解压吞异常的教训（反例） |

### 3.2 明确改掉（两个项目都做错的地方）

| 它们的做法 | EverSend 的做法 | 为什么 |
|---|---|---|
| 整文件一个请求，无续传 | 固定分片 + 追加式日志 + `.part` 文件 | 20 GB 断在 99% 不该从 0 开始 |
| 全局单会话槽 | 并发会话，每会话独立 | 一个卡住的会话不该堵住所有传输 |
| 上传绑定源 IP | 绑定**已认证的设备身份**（Ed25519） | 多网卡/多 IP 是常态，不是攻击 |
| 校验和最后核对，错了整传 | 逐块 CRC32（在途+落盘）+ 整文件 BLAKE2b；不匹配只重传坏块 | 100 GB 坏一个扇区不该重传 100 GB |
| 每 tick 一块（128 KiB） | 无定时器，`recv` 完成即发下一块 | 定时器模型把速度锁死在 26 MB/s |
| 块无编号 | 块带 `(seq, index, offset, length, crc)` | 乱序/并行/补缺口全都免费获得 |
| 无 ACK/滑窗 | **接收方驱动**的信用窗口（拉取模型） | 见 3.3 |
| 多文件串行 | 文件内多流并行；文件按大小降序 | 长尾文件放最后会浪费已建立的并行度 |
| HTTP + mTLS | 自研二进制分帧 + X25519/AEAD | 去掉 TLS 记录层与每文件握手 |
| 明文信令可降级 | 会合通道也强制握手（设计约束） | 不重复 RustDesk 的明文降级 |

### 3.3 最核心的一个决定：接收方驱动

**每一块都是接收方要来的，不是发送方推的。**

三个性质直接掉出来：

1. **续传免费。** 续传日志在接收方手里，重连后它只是"不请求已有的块"。不需要交换位图、不需要协商、不需要"从头再来"。
2. **负载自动均衡。** 所有流从一个共享队列取任务，快流自然拿得多，不需要调参。一条流死了，它在飞的块回到队列被别的流接走。
3. **发送方无状态。** 它只回答"第几块是什么"，无论多少条流，内存里永远只有一个块。

再叠一条：**块按文件顺序发放**（共享递增队列）。这让多条流合起来是**从前往后顺序写盘**——在机械硬盘上这是 120 MB/s 与 20 MB/s 的差距——并且让整文件哈希可以边传边算（`IncrementalFileHasher`），正常情况完全不需要传完再读一遍。

### 3.4 稳定性是设计出来的，不是测出来的

`tests/test_resilience.py` 里有一个会**主动破坏连接**的代理（每 0.35 s 用 `SO_LINGER=0` 发 RST 杀掉一条随机连接，同时限速 6 MB/s）。它逼出了四个真实缺陷：

1. **控制连接一死就全盘放弃。** → 改成控制消息可以走任意存活的数据流；发送方在自己的连接池变小而自己重连（supervisor）。
2. **最后一条流死掉时，接收方没有任何通道可以求援。** → 发送方主动补流，不依赖接收方请求。
3. **"本地 send 没抛异常"完全不能证明消息送达。** 被网络杀死的连接照样能接受写入，RST 随后才回来。→ 完成报告在 3 秒窗口内**在所有通道上重复发送**。
4. **摘要回复没人读。** 控制连接已死时，发送方把整文件摘要回到数据流上，而数据流的读线程早已退出——**整整 60 秒超时才被发现**（63.2 s → 2.0 s）。→ 谁发的请求谁读回复。

这四个都是"在完美回环上永远测不出来"的问题。

### 3.5 跨系统测试逼出来的缺陷

上面那四个至少还能在单机上复现。下面这些**只有"真的换一个操作系统跑"才会出现**，
而它们的共同点是：在开发机上永远是绿的。

1. **Windows 上根本没有 `os.pread` / `os.pwrite`。** 不是"慢一点"，是这两个名字在
   `os` 模块里不存在。整个接收路径都建立在这两个函数上，所以 Windows 版接收第一个
   分片就 `AttributeError`——一个字都收不了。→ `core/fileio.py`：POSIX 用单描述符 +
   真 `pread/pwrite`；Windows 用**每线程一个描述符** + `lseek`（文件位置是描述符级
   状态，所以线程之间不会互相踩）。
2. **Windows 的 `os.open` 默认是文本模式**，每个 `0x0A` 会被悄悄写成 `0x0D 0x0A`。
   落盘文件比源文件大几千字节，而**程序自己的校验和发现不了**——因为读的时候文本模式
   又原样翻译了回来。→ 所有二进制描述符一律带 `O_BINARY`。
3. **Windows 没有 `socket.sendmsg`**；而 Linux 上 `sendmsg` 在设置了 socket 超时的
   情况下会**短写**（CPython 转成非阻塞 + select）：实测阻塞 socket 一次写满
   524288 字节，`settimeout(120)` 之后只写了 32741 字节，其余**静默丢失**。
   GitHub runner 的 `net.core.wmem_max` 是默认的 212992，所以这个洞在 CI 上必现。
   → `sendmsg_all()` 循环到写完为止；Windows 上退回 `sendall`。
4. **手动接受的传输在 Windows 上会在传完最后一个字节后失败。** `decide()` 被调用两次
   （一次是 offer 刚到，界面要拿续传状态显示"还有 3.2 GB"；一次是用户点接受），
   每次都打开一套 `.part` 文件，第一套被丢掉却没人关。于是传输跑到最后，`.part`
   文件还被自己开着，`os.replace` 报 `ERROR_SHARING_VIOLATION`（共享冲突），
   **文件落不了盘**。POSIX 允许重命名一个还开着的文件，所以 Linux 上一切正常，
   唯一的痕迹是每个被接受的文件泄漏两个描述符。→ 决定只算一次
   （`ReceiveSession._decision`）。
5. **macOS 上 `getaddrinfo(自己的主机名)` 能阻塞几十秒**，而且没法设超时。它挂在
   `/api/state` 上，于是"手机打开页面"的第一个请求就超时；Linux 上同一个调用是
   微秒级。→ 解析放后台线程，最多等 0.35 秒，慢的结果留给下一次轮询。
6. **413 的响应会被 RST 吃掉。** 拒绝一个超大请求体之后直接关连接，内核发现有没读
   的数据就回 RST，而 RST 会把刚写出去的响应丢掉——客户端报"连接被重置"而不是 413。
   这本质上是响应与请求体剩余部分的赛跑，所以偶发。→ 按 `Content-Length` 把剩下的
   读掉（有上限，恶意的大上传不会被读到底）再关。

这一节的教训不是"多测"，而是**"在同一个平台上多测"和"换个平台测"是两种不同的测试**：
前者证明代码逻辑对，后者证明代码里那些"我以为所有系统都一样"的假设对。

---

## 4. 关键数字对照

| 指标 | LocalSend | RustDesk（文件传输） | EverSend |
|---|---|---|---|
| 分块大小 | 无（整文件流） | 固定 128 KiB | 1–16 MiB，按文件大小自动选（目标 ~4096 块） |
| 块偏移/编号 | 无 | `blk_id` 恒 0 | `(seq, index, offset, length)` |
| 断点续传 | **无** | 有（仅 size+mtime 秒，4 GiB 截断） | 有（追加日志 + 逐块 CRC） |
| 完整性校验 | 整文件 SHA-256（事后） | 无内容哈希 | 逐块 CRC32 + 整文件 BLAKE2b |
| 校验失败处理 | 整文件重传（≤3 次） | 无 | **只重传坏块** |
| 并行流 | 每文件可并行，会话串行 | 单连接，多文件串行 | 默认 4 条，最多 16 条，自动降为 2（机械盘） |
| 应用层流控 | 无（靠 TCP） | 无（靠 TCP） | 接收方驱动的信用窗口 |
| 传输层 | HTTP + mTLS | 自研帧 + XSalsa20-Poly1305 | 自研帧 + X25519/AES-GCM 或 ChaCha20 |
| 局域网发现 | 组播 + 子网扫描 | UDP 广播 | 组播 + 广播 + mDNS + 子网扫描 + 手动/二维码 |
| 实测吞吐（本机） | — | ~26–29 MB/s（设计上限） | **110 MiB/s @ 加密开启**（4 流，tmpfs） |
| 抗断连 | 失败即重来 | 无块级重传 | 8 次 RST 杀连接后仍**逐字节一致**完成 |

---

## 5. 参考项目的文件索引（便于复核）

**LocalSend**（`localsend/localsend-main/`）
- `packages/core/src/multicast/mod.rs` —— 组播发现
- `packages/core/src/discovery/mod.rs` —— 分阶段发现 + 子网扫描
- `packages/core/src/http/server/v2.rs` —— 接收端协议与单会话槽
- `packages/core/src/http/client/v2.rs` —— 发送端协议
- `packages/core/src/http/server/common/save.rs` —— 落盘与校验（续传缺失的证据）
- `packages/core/src/http/server/common/session.rs` —— 会话/文件状态机
- `packages/core/src/crypto/*` —— 证书、哈希、v3 token
- `packages/core/src/webrtc/*` —— 远程传输与 16 KiB 分片

**RustDesk**（`rustdesk/rustdesk-master/`，另见合稿报告）
- `src/rendezvous_mediator.rs` —— 注册、打洞、中继申请（2118 行）
- `src/client.rs` —— 连接超时预算、多传输竞速、中继回退
- `src/common.rs` —— NAT 类型自测、UDP 探针
- `libs/base/src/fs.rs` —— 文件传输块模型（慢的根源）
- `src/port_forward_mux.rs` —— 应用层信用窗口的现成范例

> 说明：`libs/hbb_common/` 是未拉取的 git submodule，其内容（`rendezvous.proto`、`bytes_codec.rs`、`tcp.rs` 等）由子调研从上游 `rustdesk/hbb_common` 只读克隆补齐后取证。整个研究过程未修改参考项目的任何文件。
