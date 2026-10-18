# `/api/upload` 断点续传接口 v1（opt-1017）—— 客户端实现依据

Linux 端已实现并测试（`tests/test_http_resume.py` 全绿）。旧的"无 uploadId 一次性上传"**行为完全不变**。
手机端（android-device）按本文实现即可。

## 0. 能力宣告（客户端先判断）

`GET /api/state` 顶层新增：

```json
"caps": {"uploadResume": true, "uploadMaxBytes": 34359738368, "downloadRange": true}
```

- `uploadResume=true` ⇒ 本服务端支持续传上传；旧服务端没有这个字段/为 false ⇒ 手机端**必须**退回旧的一次性 POST。
- `downloadRange=true` 即现有 Range 下载能力（不变）。

## 1. 请求格式

```
POST /api/upload?name=<urlencoded 文件名>&deviceId=<目标设备id>&uploadId=<hex>&offset=<已确认字节>
Header: X-EverSend-Token: <CSRF 令牌>
Header: X-EverSend-Total: <文件总字节数>
Header: Content-Type: application/octet-stream
Header: Content-Length: <本次 body 字节数>
Body: 文件字节 [offset, offset+Content-Length)
```

- `uploadId`：客户端生成，16–64 位**小写十六进制**（建议 `32` 位随机 hex）。同一文件同一目标的重试链复用**同一个** id。
- `offset`：客户端"认为服务端已有"的字节数（首次 0；续传时用 `202.offset` 或 `GET /api/upload/status` 的值）。
- body 可以是**剩余字节的前缀**（服务端允许一次没传完，返回 202 继续）。
- `deviceId`：目标设备（发给自己 = 本机 device id；当前手机端发文件给电脑就是电脑 device id，见 `/api/state` 的 devices）。

## 2. 响应

| HTTP | 场景 | JSON |
|---|---|---|
| `202` | 已收下但未到 `X-EverSend-Total`（客户端主动分段或本次 body 就是前缀） | `{"ok":true,"complete":false,"offset":<服务端已有>,"total":<总大小>,"resumeId":"<id>","name":"<文件名>"}` |
| `200/202` | 收满并完成（语义 = 旧接口成功） | 旧字段原样 + `{"complete":true,"resumeId":"<id>","offset":<总大小>,"total":<总大小>}`。**会落盘/发布/开始转发** |
| `409` | offset 与服务端已有字节不一致 | `{"ok":false,"error":"偏移不匹配：服务端已有 N 字节，请从该偏移继续","offset":<N>,"total":<总大小>,"resumeId":"<id>","complete":false}`；服务端会 `Connection: close`，客户端应重连后用 `offset` 续 |
| `400` | uploadId 非法 / 缺 `X-EverSend-Total` / body 超过剩余 | `{"ok":false,"error":"中文原因"}` |
| `413` | 总大小超过 `caps.uploadMaxBytes` / 磁盘空间不足 | 同上 |

## 3. 辅助接口：问服务端已有几字节（断线后推荐）

```
GET /api/upload/status?uploadId=<hex>
```

```json
{"ok": true, "complete": false, "resumeId": "...", "offset": 73400320}
{"ok": true, "complete": true,  "resumeId": "...", "offset": <总大小>, "total": <总大小>}
```

未知 id ⇒ `{"ok":true,"complete":false,"offset":0}`。只读、无副作用、不需要令牌（GET 无 CSRF 校验；uploadId 是 128 位随机串）。

## 4. 推荐客户端算法（伪代码）

```kotlin
val total = size                      // 文件总字节
val id = randomHex(32)
var have = 0L
var attempts = 0
while (have < total && attempts++ < 6) {
    try {
        val conn = open("/api/upload?name=$name&deviceId=$id&uploadId=$uid&offset=$have")
        conn.setRequestProperty("X-EverSend-Total", total)
        conn.setFixedLengthStreamingMode(total - have)      // 流式，不整份进内存
        conn.outputStream.use { out ->
            resolver.openInputStream(uri)!!.use { src ->
                src.skip(have)                              // 或 channel.position(have)
                copyLoop(src, out, 64KiB) { sent -> publishProgress(have + sent) }
            }
        }
        val code = conn.responseCode
        val json = readJson(conn)
        if (code == 200 || code == 202) {
            if (json.optBoolean("complete")) return done(json)
            have = json.getLong("offset")                   // 没传完：继续
        } else if (code == 409) {
            have = json.getLong("offset")                   // 服务端比我们记得多：跟它走
        } else throw IOException(json.optString("error", "HTTP $code"))
    } catch (e: IOException) {
        // 断线：先问服务端，再续（不要从 0 重来）
        have = try { statusOf(uid).offset } catch (_: Exception) { have }
        delay(backoff(attempts))                            // 0.5s,1s,2s,4s,8s
    }
}
```

关键点：
- `have` 永远以**服务端**为准（202.offset / status.offset / 409.offset），不要用"本地已写出多少"——socket 缓冲里可能没到。
- 每段都用 `setFixedLengthStreamingMode(total - have)`，不把文件读进内存。
- `complete=true` 后再重发同一 id（幂等重放）不会重复落盘：服务端返回缓存的完成响应。
- 旧服务端（无 `caps.uploadResume`）用原来的 `conn.setFixedLengthStreamingMode(total)` 一次传完。

## 5. 服务端测试覆盖（已全绿，可真机上复查）

`tests/test_http_resume.py`：三段续传 sha256 一致、409 恢复、完成幂等重放（不重复落盘）、旧路径回归、
非法 id/缺 total/超长校验、状态查询接口。

curl 复现（服务端已起，token 从页面 HTML 里取）：

```bash
# 第 1 段（500 KiB）
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  -H "X-EverSend-Token: $TOKEN" -H "X-EverSend-Total: 2097152" \
  --data-binary @<(head -c 524288 src.bin) \
  "http://192.168.110.41:52119/api/upload?name=x.bin&deviceId=<电脑设备id>&uploadId=$(openssl rand -hex 16)&offset=0"
# 之后带 offset 续、或 GET /api/upload/status?uploadId=... 查已有字节
```

## 6. 兼容性矩阵

| 客户端 | 服务端 | 行为 |
|---|---|---|
| 旧（无 uploadId） | 新 | 一次性上传，不变 ✅ |
| 新（先查 caps） | 旧（无 caps） | 退回一次性上传 ✅ |
| 新（带 uploadId） | 新 | 断点续传 ✅ |
| 新（带 uploadId） | 旧（理论上） | 旧服务端把 body 当整文件 → 会得到不完整文件。**所以必须先查 caps 再启用** ⚠️ |
