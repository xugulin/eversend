# Linux 端实现：`/api/upload` 断点续传（opt-1017 阶段 2 · Lead）

## 1. 问题（来自真机基线与审计）

手机→电脑的大文件走 `POST /api/upload`，**一次请求传完**：
- 100 MiB 传到 73 MiB 时连接被系统掐断（真机 A2 首次运行日志 `Software caused connection abort`），已收字节全废、重发从 0 开始；
- 电脑端重启/手机切后台/切 Wi-Fi 等任何中断都等于"从头再来"；
- 对照：电脑→手机方向早就有 HTTP `Range` 断点续传（服务端 `_stream_file` 支持 206），只有上传方向没有。

## 2. 改动（`src/eversend/web/server.py`，+约 230 行）

| 位置 | 改动 |
|---|---|
| `WebUI.__init__` | 新增 `_resume_locks`（同一 uploadId 串行写）、`_resume_done`（完成响应幂等快照，有界 64 条） |
| `WebUI.state()` | 顶层新增 `caps = {uploadResume: true, uploadMaxBytes, downloadRange: true}`，旧客户端忽略 |
| `WebUI` 新方法 | `valid_resume_id`（16–64 位十六进制，防路径穿越）、`resume_spool`、`resume_lock`、`promote_resume_spool`、`remember_resume_done`、`resume_completed`、`sweep_resume_spools`（>24h 清理，启动时跑一次） |
| `_Handler._receive_upload` | 带 `uploadId` 时走续传分支：`have=spool 现有字节`；`offset!=have` → 409+真实 offset；追加写；未满 `X-EverSend-Total` → 202+offset；收满 → 走原有 land/finish/share/start_send 流程（旧语义不变）；完成响应加 `complete/resumeId` 并登记幂等 |
| `_Handler._append_upload_body` | 续传追加写（失败**保留**已收字节，不删 spool） |
| `_Handler._resume_conflict` | 409 用 `_fail` 发（关连接 + 排空未读 body，避免 keep-alive 解析错位） |
| `_Handler` GET 路由 | 新增 `GET /api/upload/status?uploadId=` → `{offset, complete}`，断线重连后客户端据此续传 |
| `WebUI.start()` | 启动时清理过期续传 spool |

**向后兼容**：没有 `uploadId` 的请求走原一次性路径，响应字段不变；旧客户端完全无感。
客户端接口规范与伪代码：`tests/reports/opt-1017/http-upload-resume-api.md`（已发 android-device）。

## 3. 验证

| 测试 | 结果 |
|---|---|
| 新增 `tests/test_http_resume.py`（20 项断言：三段续传 sha256、409 恢复、完成幂等重放不重复落盘、旧路径回归、非法 id/缺 total/超长、status 接口） | ✅ 全部通过 |
| 既有 `src/eversend/web/selftest.py`（356 项，真引擎 + 真 HTTP + 真 SSE） | ✅ All 356 checks passed（EXIT=0） |

复现：

```bash
cd eversend && PYTHONPATH=src python3 tests/test_http_resume.py
cd eversend && PYTHONPATH=src python3 -u src/eversend/web/selftest.py
```

## 4. 性能影响

- 旧路径（无 uploadId）代码路径几乎不变（多一次 header 解析），无回退风险；perf-bench 会复测 HTTP 基准确认。
- 续传路径多一次 `os.path.getsize` + per-id 锁，相对网络传输可忽略。
- 真机收益（待新 APK 落地后复测）：手机→电脑 100 MiB 中途断连，从断点续传而非从 0 重传；断线重连恢复字节比 = 1 - 已收/总。

## 5. 已知边界/后续

- 服务端只按 uploadId 存 spool；客户端需保证同一文件重试链复用同一 id（接口文档已写）。
- 已完成的 uploadId 重放返回缓存响应；服务端进程重启后该缓存丢失，此时 spool 已定稿、`status` 返回 offset=0，客户端会重传并触发 `unique_path` 落成 `(1)` 文件 —— 这是极端边界（完成响应丢失 + 电脑端重启），阶段 3 可加"完成记录持久化"，本轮不做。
- 真机验收：等 android-device 的新 APK 实现客户端侧后，由 Lead 用 `--only A7` + 人为断网实验验证。
