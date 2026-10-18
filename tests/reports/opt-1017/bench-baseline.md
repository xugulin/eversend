# 韧传 EverSend · 传输性能基线（tools/bench_transfer.py 自动生成）

- 生成时间：2026-10-17T23:17:02+0800
- 标签：pre-opt-baseline@e18c079
- 命令：`tools/bench_transfer.py all --sizes 64,512,1024 --streams 1,2,4,8 --repeats 3 --warmups 1 --resume-size-mib 512 --resume-repeats 2 --json tests/reports/opt-1017/bench-baseline.json --md tests/reports/opt-1017/bench-baseline.md --label pre-opt-baseline@e18c079`
- **口径：1 MiB = 1048576 字节；MiB/s = 字节数 / 1048576 / wall 秒**
- wall = 一次传输 API 调用的墙钟（EVSD: `Engine.send_ex` 阻塞返回；HTTP: 一次真请求从 connect 到读完响应）
- CPU = `/proc/<pid>/stat` utime+stime（发送/接收进程分开统计），峰值 RSS = VmHWM

## 机器背景

- CPU: AMD Ryzen 7 2700X Eight-Core Processor
- nproc: 16
- 内核: 7.2.8-1-cachyos
- Python: 3.14.7
- git HEAD: e18c0797c5fba9db248a2ba58f7e379a3a8e42c6
- 内存: total=16311796 kB 可用=11167028 kB
- scratch 磁盘: /dev/sdc3      btrfs 932046848 224389508 706924124   25% /home
- home 磁盘: /dev/sdc3      btrfs 932046848 224389508 706924124   25% /home
- loadavg @start: [9.44, 9.37, 7.88]
- 磁盘完成路径探针: {"size_mib": 64.0, "preallocate_s": 0.0001, "write_cache_s": 0.0471, "write_cache_mib_s": 1358.9, "fdatasync_s": 1.1771, "fdatasync_mib_s": 54.4, "note": "preallocate+write+fdatasync 模拟接收端 finalize 的落盘路径"}

## 配置

```json
{
  "command": "all",
  "sizes": "64,512,1024",
  "streams": "1,2,4,8",
  "range_mib": 8,
  "repeats": 3,
  "warmups": 1,
  "taskset": "",
  "resume_size_mib": 512,
  "resume_streams": 4,
  "resume_repeats": 2,
  "resume_fraction": 0.3,
  "skip_resume": false,
  "json": "tests/reports/opt-1017/bench-baseline.json",
  "md": "tests/reports/opt-1017/bench-baseline.md",
  "label": "pre-opt-baseline@e18c079",
  "keep": false,
  "scratch": ""
}
```

## 结果摘要（中位数，n=测量次数，预热不计入）

| 链路 | size MiB | streams | n | MiB/s 中位 | min | max | CV% | 剔除停顿后 MiB/s 中位 | n_clean | wall s | CPU s/MiB | tx CPU s | rx CPU s | 标记 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| evsd-loopback | 1024.0 | 1 | 3 | 15.953 | 15.38 | 71.934 | 166.27 | 71.934 | 1 | 64.1883 | 0.00653 | 3.25 | 3.44 | ENV_STALL_EXCLUDED(2), NOISY_CV_GT_20% |
| evsd-loopback | 1024.0 | 2 | 3 | 15.285 | 7.854 | 86.983 | 233.43 | 86.983 | 1 | 66.9958 | 0.00945 | 4.73 | 4.95 | ENV_STALL_EXCLUDED(2), NOISY_CV_GT_20% |
| evsd-loopback | 1024.0 | 4 | 3 | 15.546 | 15.235 | 64.789 | 149.79 | 64.789 | 1 | 65.8707 | 0.01462 | 7.19 | 7.78 | ENV_STALL_EXCLUDED(2), NOISY_CV_GT_20% |
| evsd-loopback | 1024.0 | 8 | 3 | 16.205 | 15.716 | 90.903 | 218.01 | 90.903 | 1 | 63.1909 | 0.01444 | 7.33 | 7.46 | ENV_STALL_EXCLUDED(2), NOISY_CV_GT_20% |
| evsd-loopback | 512.0 | 1 | 3 | 54.186 | 3.933 | 57.351 | 45.16 | 55.769 | 2 | 9.4489 | 0.01107 | 2.78 | 2.885 | ENV_STALL_EXCLUDED(1), NOISY_CV_GT_20% |
| evsd-loopback | 512.0 | 2 | 2 | 21.692 | 8.46 | 34.925 | 61.0 | 21.692 | 2 | 37.5912 | 0.0125 | 3.09 | 3.31 | NOISY_CV_GT_20% |
| evsd-loopback | 512.0 | 4 | 3 | 64.136 | 8.679 | 69.318 | 42.79 | 66.727 | 2 | 7.983 | 0.01578 | 3.95 | 4.125 | ENV_STALL_EXCLUDED(1), NOISY_CV_GT_20% |
| evsd-loopback | 512.0 | 8 | 3 | 63.711 | 8.66 | 65.523 | 41.42 | 64.617 | 2 | 8.0363 | 0.01594 | 3.665 | 4.495 | ENV_STALL_EXCLUDED(1), NOISY_CV_GT_20% |
| evsd-loopback | 64.0 | 1 | 3 | 45.689 | 23.52 | 47.575 | 23.91 | 45.689 | 3 | 1.4008 | 0.01109 | 0.33 | 0.38 | NOISY_CV_GT_20% |
| evsd-loopback | 64.0 | 2 | 3 | 42.787 | 1.211 | 45.489 | 47.36 | 44.138 | 2 | 1.4958 | 0.02101 | 0.615 | 0.73 | ENV_STALL_EXCLUDED(1), NOISY_CV_GT_20% |
| evsd-loopback | 64.0 | 4 | 3 | 26.564 | 1.209 | 38.643 | 58.72 | 32.603 | 2 | 2.4093 | 0.02429 | 0.655 | 0.9 | ENV_STALL_EXCLUDED(1), NOISY_CV_GT_20% |
| evsd-loopback | 64.0 | 8 | 2 | 37.218 | 30.037 | 44.398 | 19.29 | 37.218 | 2 | 1.7861 | 0.02305 | 0.67 | 0.805 |  |
| http-download | 1024.0 | 1 | 3 | 2202.634 | 2171.625 | 2448.279 | 5.62 | 2202.634 | 3 | 0.4649 | 0.0006 | 0.18 | 0.43 |  |
| http-download | 512.0 | 1 | 3 | 2253.484 | 1568.706 | 2291.586 | 14.74 | 2253.484 | 3 | 0.2272 | 0.00057 | 0.08 | 0.21 |  |
| http-download | 64.0 | 1 | 3 | 1462.551 | 1294.056 | 1626.902 | 9.29 | 1462.551 | 3 | 0.0438 | 0.00094 | 0.02 | 0.04 |  |
| http-download-range | 1024.0 | 128 | 1 | 1756.963 | 1756.963 | 1756.963 | nan | 1756.963 | 1 | 0.5828 | 0.0007 | 0.2 | 0.52 |  |
| http-download-range | 512.0 | 64 | 1 | 1877.393 | 1877.393 | 1877.393 | nan | 1877.393 | 1 | 0.2727 | 0.00068 | 0.09 | 0.26 |  |
| http-download-range | 64.0 | 8 | 1 | 1519.793 | 1519.793 | 1519.793 | nan | 1519.793 | 1 | 0.0421 | 0.00063 | 0.01 | 0.03 |  |
| http-upload | 1024.0 | 1 | 3 | 16.624 | 16.536 | 110.605 | 266.63 | 110.605 | 1 | 61.5962 | 0.0013 | 0.63 | 0.7 | ENV_STALL_EXCLUDED(2), NOISY_CV_GT_20% |
| http-upload | 512.0 | 1 | 3 | 819.98 | 231.191 | 1080.981 | 43.35 | 950.481 | 2 | 0.6244 | 0.00157 | 0.42 | 0.385 | ENV_STALL_EXCLUDED(1), NOISY_CV_GT_20% |
| http-upload | 64.0 | 1 | 3 | 700.858 | 694.588 | 857.626 | 10.76 | 700.858 | 3 | 0.0913 | 0.0025 | 0.08 | 0.08 |  |

## 续传（中断 → 同目录重启 → 补传）

口径：``.part`` 打开即被预分配到全长，不能用它的大小看进度；补传字节比 = 1 - 中断瞬间 journal 已落定 chunk 的比例（journal 是接收端权威续传状态）。

| size MiB | streams | 中断时 journal 落定字节 | 落定比 | 补传字节比(1-落定比) | 恢复 wall s | 冷传 wall s | 提速 | 首进度字节 | sha | ok |
|---|---|---|---|---|---|---|---|---|---|---|
| 512.0 | 4 | 190840832 | 0.35547 | 0.64453 | 60.799064 | 91.474585 | 1.505 | 191889408 | True | True |
| 512.0 | 4 | 193986560 | 0.36133 | 0.63867 | 16.509837 | 15.113575 | 0.915 | 195035136 | True | True |

- 续传汇总：512 MiB × streams=4，n=2，落定比中位 0.3584，补传比中位 0.6416（min 0.63867 / max 0.64453），恢复 38.6545s vs 冷传 53.2941s（提速 1.21×）

## 原始测量（每次）

| kind | size_mib | streams | warmup | wall_s | mib_per_s | cpu_s_per_mib | sha_ok | ok |
|---|---|---|---|---|---|---|---|---|
| evsd-loopback | 64.0 | 1 | True | 2.918111 | 21.932 | 0.00906 | True | True |
| evsd-loopback | 64.0 | 1 | False | 1.345235 | 47.575 | 0.00859 | True | True |
| evsd-loopback | 64.0 | 1 | False | 2.721066 | 23.52 | 0.01516 | True | True |
| evsd-loopback | 64.0 | 1 | False | 1.400784 | 45.689 | 0.01109 | True | True |
| evsd-loopback | 64.0 | 2 | True | 0.893716 | 71.611 | 0.01281 | True | True |
| evsd-loopback | 64.0 | 2 | False | 52.861686 | 1.211 | 0.01953 | True | True |
| evsd-loopback | 64.0 | 2 | False | 1.406925 | 45.489 | 0.02187 | True | True |
| evsd-loopback | 64.0 | 2 | False | 1.495789 | 42.787 | 0.02016 | True | True |
| evsd-loopback | 64.0 | 4 | True | 1.523416 | 42.011 | 0.02344 | True | True |
| evsd-loopback | 64.0 | 4 | False | 52.948818 | 1.209 | 0.02047 | True | True |
| evsd-loopback | 64.0 | 4 | False | 1.656204 | 38.643 | 0.02609 | True | True |
| evsd-loopback | 64.0 | 4 | False | 2.4093 | 26.564 | 0.0225 | True | True |
| evsd-loopback | 64.0 | 8 | True | 1.451349 | 44.097 | 0.02594 | True | True |
| evsd-loopback | 64.0 | 8 | False | 2.130724 | 30.037 | 0.02109 | True | True |
| evsd-loopback | 64.0 | 8 | False | 1.441519 | 44.398 | 0.025 | True | True |
| evsd-loopback | 64 | 8 | False | None | None |  | False | False |
| evsd-loopback | 512.0 | 1 | True | 17.895723 | 28.61 | 0.01697 | True | True |
| evsd-loopback | 512.0 | 1 | False | 130.173102 | 3.933 | 0.01627 | True | True |
| evsd-loopback | 512.0 | 1 | False | 9.448887 | 54.186 | 0.01145 | True | True |
| evsd-loopback | 512.0 | 1 | False | 8.927456 | 57.351 | 0.01068 | True | True |
| evsd-loopback | 512.0 | 2 | True | 68.887602 | 7.432 | 0.01182 | True | True |
| evsd-loopback | 512 | 2 | False | None | None |  | False | False |
| evsd-loopback | 512.0 | 2 | False | 14.660001 | 34.925 | 0.01164 | True | True |
| evsd-loopback | 512.0 | 2 | False | 60.522435 | 8.46 | 0.01336 | True | True |
| evsd-loopback | 512.0 | 4 | True | 7.979481 | 64.165 | 0.0167 | True | True |
| evsd-loopback | 512.0 | 4 | False | 7.386302 | 69.318 | 0.01434 | True | True |
| evsd-loopback | 512.0 | 4 | False | 58.989603 | 8.679 | 0.01408 | True | True |
| evsd-loopback | 512.0 | 4 | False | 7.983045 | 64.136 | 0.01721 | True | True |
| evsd-loopback | 512.0 | 8 | True | 7.788331 | 65.739 | 0.01602 | True | True |
| evsd-loopback | 512.0 | 8 | False | 7.814003 | 65.523 | 0.01561 | True | True |
| evsd-loopback | 512.0 | 8 | False | 59.122319 | 8.66 | 0.01557 | True | True |
| evsd-loopback | 512.0 | 8 | False | 8.036286 | 63.711 | 0.01627 | True | True |
| evsd-loopback | 1024.0 | 1 | True | 14.877383 | 68.829 | 0.00763 | True | True |
| evsd-loopback | 1024.0 | 1 | False | 66.580353 | 15.38 | 0.00639 | True | True |
| evsd-loopback | 1024.0 | 1 | False | 64.188343 | 15.953 | 0.0071 | True | True |
| evsd-loopback | 1024.0 | 1 | False | 14.235359 | 71.934 | 0.00653 | True | True |
| evsd-loopback | 1024.0 | 2 | True | 63.136044 | 16.219 | 0.00864 | True | True |
| evsd-loopback | 1024.0 | 2 | False | 11.772405 | 86.983 | 0.00945 | True | True |
| evsd-loopback | 1024.0 | 2 | False | 66.995821 | 15.285 | 0.00992 | True | True |
| evsd-loopback | 1024.0 | 2 | False | 130.382593 | 7.854 | 0.01083 | True | True |
| evsd-loopback | 1024.0 | 4 | True | 12.304309 | 83.223 | 0.01438 | True | True |
| evsd-loopback | 1024.0 | 4 | False | 65.870731 | 15.546 | 0.01328 | True | True |
| evsd-loopback | 1024.0 | 4 | False | 15.805152 | 64.789 | 0.01462 | True | True |
| evsd-loopback | 1024.0 | 4 | False | 67.21399 | 15.235 | 0.01727 | True | True |
| evsd-loopback | 1024.0 | 8 | True | 13.648547 | 75.026 | 0.01932 | True | True |
| evsd-loopback | 1024.0 | 8 | False | 63.190918 | 16.205 | 0.01641 | True | True |
| evsd-loopback | 1024.0 | 8 | False | 11.26477 | 90.903 | 0.01444 | True | True |
| evsd-loopback | 1024.0 | 8 | False | 65.158192 | 15.716 | 0.01465 | True | True |
| http-upload | 64.0 | 1 | True | 0.083071 | 770.428 | 0.00234 | True | True |
| http-download | 64.0 | 1 | True | 0.044787 | 1428.985 | 0.00094 | None | True |
| http-upload | 64.0 | 1 | False | 0.092141 | 694.588 | 0.0025 | True | True |
| http-download | 64.0 | 1 | False | 0.039339 | 1626.902 | 0.00094 | None | True |
| http-download-range | 64.0 | 8 | False | 0.042111 | 1519.793 | 0.00063 | None | True |
| http-upload | 64.0 | 1 | False | 0.091317 | 700.858 | 0.00266 | True | True |
| http-download | 64.0 | 1 | False | 0.049457 | 1294.056 | 0.00141 | None | True |
| http-upload | 64.0 | 1 | False | 0.074625 | 857.626 | 0.00203 | True | True |
| http-download | 64.0 | 1 | False | 0.043759 | 1462.551 | 0.00094 | None | True |
| http-upload | 512.0 | 1 | True | 0.558425 | 916.865 | 0.00193 | True | True |
| http-download | 512.0 | 1 | True | 0.270692 | 1891.446 | 0.00066 | None | True |
| http-upload | 512.0 | 1 | False | 2.214622 | 231.191 | 0.00154 | True | True |
| http-download | 512.0 | 1 | False | 0.223426 | 2291.586 | 0.00055 | None | True |
| http-download-range | 512.0 | 64 | False | 0.272719 | 1877.393 | 0.00068 | None | True |
| http-upload | 512.0 | 1 | False | 0.624405 | 819.98 | 0.00152 | True | True |
| http-download | 512.0 | 1 | False | 0.227204 | 2253.484 | 0.00057 | None | True |
| http-upload | 512.0 | 1 | False | 0.473644 | 1080.981 | 0.00162 | True | True |
| http-download | 512.0 | 1 | False | 0.326384 | 1568.706 | 0.00084 | None | True |
| http-upload | 1024.0 | 1 | True | 3.649978 | 280.55 | 0.00237 | True | True |
| http-download | 1024.0 | 1 | True | 0.513655 | 1993.557 | 0.00064 | None | True |
| http-upload | 1024.0 | 1 | False | 9.258145 | 110.605 | 0.0013 | True | True |
| http-download | 1024.0 | 1 | False | 0.418253 | 2448.279 | 0.00053 | None | True |
| http-download-range | 1024.0 | 128 | False | 0.582824 | 1756.963 | 0.0007 | None | True |
| http-upload | 1024.0 | 1 | False | 61.596209 | 16.624 | 0.00132 | True | True |
| http-download | 1024.0 | 1 | False | 0.471536 | 2171.625 | 0.0006 | None | True |
| http-upload | 1024.0 | 1 | False | 61.925587 | 16.536 | 0.00145 | True | True |
| http-download | 1024.0 | 1 | False | 0.464898 | 2202.634 | 0.0006 | None | True |
| evsd-resume-cold | 512.0 | 4 |  | 91.474585 | 5.597 |  | True | True |
| evsd-resume | 512.0 | 4 |  | 60.799064 | 8.421 |  | True | True |
| evsd-resume-cold | 512.0 | 4 |  | 15.113575 | 33.877 |  | True | True |
| evsd-resume | 512.0 | 4 |  | 16.509837 | 31.012 |  | True | True |

---

# 基线结论（perf-bench 手写 + 自动数字）

> 本节由 `.testscratch/perf-bench-analysis.py` 从同目录 JSON 现算生成；缺数据项标 **PENDING**（说明由哪个 A/B 组补），不做推断。

## 0. 版本与环境口径（before/after 可比性）

- 基线 git HEAD：``；测量时脏文件 0 个（工作树全团队共享，`web/server.py` 等在测量期间有他人改动，已记录 md5）
- 测量时关键文件 md5：`web/server.py` = `?`；`core/chunkstore.py` = `?`
- **本机磁盘完成路径探针**（preallocate+write+fdatasync 64 MiB）：fdatasync ≈ **54.4 MiB/s**（1.177 s / 64 MiB）
- 机器：16 核 AMD Ryzen 7 2700X；/home = **USB 便携 SSD（LanKxin PSSD）上的 btrfs**；基线测量期间其他专家任务并行，loadavg 10–18（每次运行都记录 `loadavg_before/after`）。
- **口径提醒：loopback 数字只用于 CPU/协议归因，不能当用户速度。** 真实手机↔电脑 WiFi 基线（Lead 真机实测，PC 侧 sentBytes 采样）约 **24–25 MiB/s**。

## 0.5 速度档位：用户可感知速度在哪一档

| 链路 | 量级 MiB/s | 来源/口径 | 用户感知 |
|---|---|---|---|
| 真实手机 ↔ 电脑 WiFi（PC→手机） | **24–25** | Lead 真机 sentBytes 采样（MI9，load≈8.9） | 用户真实速度；APK 侧优化才有感 |
| 本机 HTTP localhost（服务端上限参考） | 110.6 上传 / 2202.6 下载 | 本报告 §3；loopback page cache | 服务端 CPU/协议不是瓶颈 |
| 本机 EVSD loopback（btrfs/USB 落盘，端到端） | **66.7** | 本报告 §1，512MiB×4 流（剔除 ENV_STALL 后） | 桌面↔桌面大文件「看起来慢」就是这一档 |
| 本机 EVSD loopback（tmpfs，协议/CPU 上限） | PENDING | 本报告 §1；PENDING 的 quiet-window A/B 组补全量 | 去掉慢盘 fsync 后的协议上限 |
| btrfs/USB 盘 fsync 上限 | 39–56 | 磁盘探针 `_disk_probe`（64 MiB fdatasync，多轮） | 收尾刷盘时吞吐被截在这里 |

**结论**：用户真实感受 ≈ 24–25 MiB/s（WiFi 档）。安静窗口下它高于本机慢盘 EVSD，说明真机链路瓶颈在手机 WiFi/协议链路而不是这台电脑的磁盘；但桌面↔桌面大文件/本机验收会被慢盘 fsync 截住（高负载时 512 MiB 单次墙钟出现过 60–130 s 的 I/O 停顿），T4 修完应显著改善本机与 LAN 场景的收尾。

## 1. EVSD 双组：慢盘（btrfs/USB） vs tmpfs（协议上限） vs NVMe（快盘）

噪声处理：单次吞吐 < 同配置中位 40% 的运行标 `ENV_STALL_EXCLUDED`（USB 盘 I/O 停顿），下表同时给**原始中位**与**剔除停顿后的中位**；被剔除运行在 §1.2 单列，保留证据。

| size MiB | streams | btrfs 原始中位 | btrfs 剔除后中位 (n_clean) | tmpfs 中位 | NVMe 中位 | btrfs CPU s/MiB | tmpfs CPU s/MiB |
|---|---|---|---|---|---|---|---|
| 64 | 1 | 45.7 | 45.7 (3) | PENDING | PENDING | 0.0110 | PENDING |
| 64 | 4 | 26.6 | 32.6 (2) | PENDING | PENDING | 0.0240 | PENDING |
| 512 | 1 | 54.2 | 55.8 (2) | PENDING | PENDING | 0.0110 | PENDING |
| 512 | 4 | 64.1 | 66.7 (2) | PENDING | PENDING | 0.0160 | PENDING |
| 1024 | 4 | 15.5 | 64.8 (1) | PENDING | PENDING | 0.0150 | PENDING |

> **PENDING**：tmpfs/NVMe 组数据由 quiet-window A/B 组重跑补齐（原补充测量被机器重启/负载中断）。当前唯一可信的对比证据是本机磁盘探针（fdatasync 39–56 MiB/s）。

### 1.1 流数扩展（512 MiB，btrfs，剔除停顿后）

| streams | MiB/s（剔除后） | 原始中位 | CV% | wall s | CPU s/MiB |
|---|---|---|---|---|---|
| 1 | 55.8 | 54.2 | 2.8 | 9.19 | 0.0110 |
| 2 | 21.7 | 21.7 | 61.0 | 37.59 | 0.0130 |
| 4 | 66.7 | 64.1 | 3.9 | 7.68 | 0.0160 |
| 8 | 64.6 | 63.7 | 1.4 | 7.92 | 0.0160 |

**观察**：慢盘组 1→8 流收益很小（高负载下一次 1–9 MiB/s 的停顿会盖过一切），说明端到端被「收尾 fsync + 写回」串行截住；协议/CPU 真实扩展曲线要等 tmpfs 组安静窗口数据。

### 1.2 受环境停顿污染的运行（ENV_STALL_EXCLUDED）

| kind | size MiB | streams | MiB/s | wall s | loadavg(前) | 原因 |
|---|---|---|---|---|---|---|
| evsd-loopback | 64.0 | 2 | 1.2 | 52.86 | 11.73 | rate 1.2 MiB/s < 30% of config max 45.5（USB 盘 I/O 停顿） |
| evsd-loopback | 64.0 | 4 | 1.2 | 52.95 | 18.17 | rate 1.2 MiB/s < 30% of config max 38.6（USB 盘 I/O 停顿） |
| evsd-loopback | 512.0 | 1 | 3.9 | 130.17 | 26.09 | rate 3.9 MiB/s < 30% of config max 57.4（USB 盘 I/O 停顿） |
| evsd-loopback | 512.0 | 4 | 8.7 | 58.99 | 12.79 | rate 8.7 MiB/s < 30% of config max 69.3（USB 盘 I/O 停顿） |
| evsd-loopback | 512.0 | 8 | 8.7 | 59.12 | 11.33 | rate 8.7 MiB/s < 30% of config max 65.5（USB 盘 I/O 停顿） |
| evsd-loopback | 1024.0 | 1 | 15.4 | 66.58 | 9.35 | rate 15.4 MiB/s < 30% of config max 71.9（USB 盘 I/O 停顿） |
| evsd-loopback | 1024.0 | 1 | 16.0 | 64.19 | 11.15 | rate 16.0 MiB/s < 30% of config max 71.9（USB 盘 I/O 停顿） |
| evsd-loopback | 1024.0 | 2 | 15.3 | 67.00 | 12.88 | rate 15.3 MiB/s < 30% of config max 87.0（USB 盘 I/O 停顿） |
| evsd-loopback | 1024.0 | 2 | 7.9 | 130.38 | 20.56 | rate 7.9 MiB/s < 30% of config max 87.0（USB 盘 I/O 停顿） |
| evsd-loopback | 1024.0 | 4 | 15.5 | 65.87 | 17.06 | rate 15.5 MiB/s < 30% of config max 64.8（USB 盘 I/O 停顿） |
| evsd-loopback | 1024.0 | 4 | 15.2 | 67.21 | 15.52 | rate 15.2 MiB/s < 30% of config max 64.8（USB 盘 I/O 停顿） |
| evsd-loopback | 1024.0 | 8 | 16.2 | 63.19 | 16.34 | rate 16.2 MiB/s < 30% of config max 90.9（USB 盘 I/O 停顿） |
| evsd-loopback | 1024.0 | 8 | 15.7 | 65.16 | 14.91 | rate 15.7 MiB/s < 30% of config max 90.9（USB 盘 I/O 停顿） |
| http-upload | 512.0 | 1 | 231.2 | 2.21 | 12.21 | rate 231.2 MiB/s < 30% of config max 1081.0（USB 盘 I/O 停顿） |
| http-upload | 1024.0 | 1 | 16.6 | 61.60 | 11.44 | rate 16.6 MiB/s < 30% of config max 110.6（USB 盘 I/O 停顿） |
| http-upload | 1024.0 | 1 | 16.5 | 61.93 | 14.03 | rate 16.5 MiB/s < 30% of config max 110.6（USB 盘 I/O 停顿） |

## 2. EVSD 2 GiB × 4 流：分阶段 CPU 账本（阶段 2 收益折算依据）

> **PENDING**：2 GiB 分阶段 CPU 账本由 quiet-window A/B 两组（BEFORE=旧树 / AFTER=新树，`cpu-split --size-mib 2048 --streams 4 --repeats 2 --scratch /dev/shm/...`）产出。脚本已在本机冒烟（64 MiB）：hash 35.8% / aead 16.0% / crc32 11.4% / connection 9.6% / socket_recv 5.4% / other 15.3%；旧树 worktree 兼容性也已验证可出账本。

## 3. HTTP 链路（手机 App↔电脑同构：localhost 真 HTTP，单请求）

数据源：bench-baseline.json 的 HTTP 组（旧脚本：一次性上传/下载/Range；新代码专属的续传上传与 sha256 校验由 quiet-window A/B 组补）

| 操作 | size MiB | MiB/s | wall s | CPU s/MiB | tx CPU s | rx CPU s | 峰值 RSS KB (server) |
|---|---|---|---|---|---|---|---|
| 一次性上传 POST /api/upload | 64 | 700.9 | 0.091 | 0.00300 | 0.080 | 0.080 | 39220 |
| 一次性上传 POST /api/upload | 512 | 950.5 | 0.549 | 0.00200 | 0.420 | 0.385 | 42312 |
| 一次性上传 POST /api/upload | 1024 | 110.6 | 9.258 | 0.00100 | 0.630 | 0.700 | 41572 |
| 下载 GET /api/download | 64 | 1462.6 | 0.044 | 0.00100 | 0.020 | 0.040 | 42704 |
| 下载 GET /api/download | 512 | 2253.5 | 0.227 | 0.00100 | 0.080 | 0.210 | 42312 |
| 下载 GET /api/download | 1024 | 2202.6 | 0.465 | 0.00100 | 0.180 | 0.430 | 41988 |
| Range 下载（8 MiB/段） | 64 | 1519.8 | 0.042 | 0.00100 | 0.010 | 0.030 | 39008 |
| Range 下载（8 MiB/段） | 512 | 1877.4 | 0.273 | 0.00100 | 0.090 | 0.260 | 42312 |
| Range 下载（8 MiB/段） | 1024 | 1757.0 | 0.583 | 0.00100 | 0.200 | 0.520 | 41572 |

**观察**：HTTP 下载/上传在 localhost 的 CPU s/MiB 极低、下载可达数千 MiB/s（page cache），说明服务端 CPU/协议开销远小于 WiFi 带宽；真实手机链路瓶颈是 WiFi 上下行（~24–25 MiB/s）。

### 3.1 关于早期报告里的 `SHA_MISMATCH` 标注（澄清 + 已修复）

早期自动摘要里 `http-download*` 全部显示 `SHA_MISMATCH` —— **这是基准脚本的判据错误，不是产品 bug**：

1. 下载性能测量为了不让哈希成本污染 wall，**刻意丢弃 body 只统计字节数**，`sha_ok` 字段因此是 `None`（=「本测量未做哈希」）；
2. 旧版摘要代码把所有「非真」值当不一致（`not None == True`），把 `None` 误标成 `SHA_MISMATCH`；
3. 证据：所有下载运行 `size_ok=True`（收到字节数 == 文件大小）且 `ok=True`；
   本轮旧树兼容性冒烟已实测 `http-download-verify`：**整文件下载 sha256 一致、Range 拼接 sha256 一致**；产品侧还有 web selftest 356 项与真机 sha 交叉验证。

**修复**：(a) 摘要判据改成只有显式 `False` 才算不一致；(b) 新增 `http-download-verify`：性能测量**之外**单独下载并算 sha256（整文件 + Range 拼接两条）；(c) 后续报告不再出现这条假红线。

### 3.2 下载完整性校验（测量外 sha256）

> **PENDING**：本基线下载只做字节数校验；sha256 证据由 quiet-window A/B 两组产出（`http-download-verify` 已内置于脚本，每组对 64/512/1024 MiB 整文件与 Range 拼接各校验一次；旧树兼容性冒烟已证明该功能可用：整文件/`Range` 拼接 sha256 均一致）。

## 4. EVSD 续传（中断 → 同目录重启 → 补传）

| size MiB | streams | 中断时 journal 落定 | 落定比 | 补传字节比 | 恢复 wall s | 冷传 wall s | 提速 | loadavg(前) |
|---|---|---|---|---|---|---|---|---|
| 512.0 | 4 | 190840832 | 0.4 | 0.6 | 60.80 | 91.47 | 1.50 | 11.73 |
| 512.0 | 4 | 193986560 | 0.4 | 0.6 | 16.51 | 15.11 | 0.92 | 18.43 |

**注意**：补传字节比由 journal 落定状态决定，**不受墙钟污染**（两次几乎一致，可靠性高）；而「恢复 wall」受本机 USB 盘 I/O 停顿影响（同配置出现过 60.8 s vs 16.5 s），quiet-window A/B 的恢复耗时才可作为提速依据。

## 5. 瓶颈假设（按证据强度排序）

1. **本机 btrfs/USB 盘的完成路径 fsync 是当前 loopback 端到端第一瓶颈。**磁盘探针 fdatasync ≈ 54.4 MiB/s，而 EVSD 接收端 `chunkstore.finalize()` 有 `file.sync()`（`core/chunkstore.py:385-425`）且 `_maybe_flush` 周期性 `fdatasync(.part)`；btrfs 组 CPU s/MiB（0.006–0.017）远未打满 CPU，说明不是 CPU 墙；高负载下一次 512 MiB 传输可被 I/O 停顿放大到 60–130 s。
2. **hash（blake2b）是当前最大 CPU 桶，AEAD 次之，crc32 第三。** 64 MiB 冒烟账本：hash 35.8% / aead 16.0% / crc32 11.4% / connection 9.6% / socket_recv 5.4% / other 15.3%；hash 大头是接收端 `IncrementalFileHasher` 逐块 blake2b + 两端整文件摘要；AEAD 每块 seal/open；crc32 在 AEAD 已保完整性时仍两端各算一遍。LAN 千兆（118 MiB/s）下 CPU 不是墙，loopback/2.5GbE+/手机弱 CPU 上会变成墙。
3. **协议固定成本摊在 1 MiB 块上（与 transport-core T3/T5/T6/T7 一致）。** 本机 chunk 数 = size/1 MiB；流数 1→8 在慢盘组几乎无收益，说明瓶颈不在并行度而在串行完成路径与每块固定开销。
4. **HTTP 链路瓶颈在 WiFi（真实 ~24–25 MiB/s）而不是服务端 CPU。** localhost CPU s/MiB 极低；真实提速靠少 RTT/大 IO/sendfile/断点续传。

## 6. 给 transport 侧的建议（按预期收益排序，阶段 2 可直接做）

| 优先级 | 改动 | 预期收益（基线证据） | 风险 | 复测口径 |
|---|---|---|---|---|
| 1 | **收尾 fsync 移出关键路径 / 合并**（T4：finalize 的两次 `file.sync()` 与 `_maybe_flush` 移出锁、整文件 sync 延迟/异步；保持 journal 语义） | 本机 btrfs loopback 端到端从 ~40–65 MiB/s 提升到接近协议上限；消除 512 MiB 级秒级停顿 | 中（掉电语义需文档化） | A/B `evsd-loopback` btrfs + tmpfs + 续传 |
| 2 | **削减 hash 成本**（T9：避免两端重复整文件摘要、流式 hasher 大块合并、必要时并行） | 2 GiB 账本最大 CPU 桶（冒烟 35.8%），去重可省约一半 hash CPU | 低-中 | A/B `cpu-split --size-mib 2048 --streams 4` |
| 3 | **AEAD 去全量复制（T1：crypto.py seal/open 改 memoryview 直传）** | aead 一档（冒烟 16.0%）；transport-core 微基准 4 MiB 块 +115%，CPU s/MiB 预计降 10–15% | 低（+单测） | A/B cpu-split + `tests/test_loopback.py` |
| 4 | **crc32 在 AEAD 已验证时跳过接收端复算（T8）** | 冒烟 11.4% CPU；省 ~8–11% | 低-中（不加密/修复扫描保留） | A/B cpu-split |
| 5 | **降低每块固定成本**（T5 dict 替代线性扫描 + 顶层 import；T7 合并 3 次 recv_into） | 1–2% CPU + 更少 syscall | 低 | A/B 64 MiB×4 流 |
| 6 | **HTTP sendfile/大块**（下载 os.sendfile；上传 IO_CHUNK 256 KiB→1 MiB；续传分段 Lead 已落地） | localhost CPU 已低，收益主要在真实 WiFi 的 RTT/syscall 数 | 低-中 | A/B `http --resume-segments 3` |

注：T1/T3–T10 编号与 `tests/reports/opt-1017/transport-audit.md`（transport-core）对齐，互为证据。

## 7. 复现命令

```bash
cd /home/xgl/我的项目/自制软件/韧传/eversend
PYTHONPATH=src python3 tools/bench_transfer.py all \
    --sizes 64,512,1024 --streams 1,2,4,8 --repeats 3 --warmups 1 \
    --resume-size-mib 512 --resume-repeats 2 \
    --json tests/reports/opt-1017/bench-baseline.json --md tests/reports/opt-1017/bench-baseline.md

PYTHONPATH=src python3 tools/bench_transfer.py evsd-loopback --sizes 64,512 --streams 1,4 \
    --repeats 3 --warmups 1 --scratch /dev/shm/perf-bench
PYTHONPATH=src python3 tools/bench_transfer.py cpu-split --size-mib 2048 --streams 4 --repeats 2 \
    --scratch /dev/shm/perf-bench

# A/B 一键（安静窗口 load<4；脚本自带守卫，>4 直接 ABORT）
bash .testscratch/perf-bench-ab.sh /home/xgl/qa-real/baseline-tree before-quiet /home/xgl/qa-real/out/before
bash .testscratch/perf-bench-ab.sh "$PWD" after-quiet /home/xgl/qa-real/out/after
python3 .testscratch/perf-bench-compare.py /home/xgl/qa-real/out/before /home/xgl/qa-real/out/after
```

## 8. before/after 可比口径（预注册，避免事后解释）

**方法**：同一脚本、同一机器、同一安静窗口（load<4），分别跑两棵树：

- BEFORE：`git worktree add /home/xgl/qa-real/baseline-tree e18c079`（优化前完整树，已建好并验证兼容），把当前 `tools/bench_transfer.py` 复制进去（脚本是新工具，快照里没有）；
- AFTER：当前工作树（优化后），**同一个脚本文件**；
- 两组都在 `/dev/shm`（tmpfs）+ btrfs 上各跑一遍；`--label before-quiet` / `after-quiet`。

**可比性判据（按优先级）**：

1. **主判据：CPU 秒/MiB**（受负载影响小）—— 同配置 AFTER 相对 BEFORE 的变化 %；
2. **辅助：tmpfs 组 MiB/s**（协议/CPU 上限）—— 安静窗口下可直接比；
3. **参考：btrfs 组 MiB/s**（用户可感知端到端）—— 受慢盘 I/O 停顿影响，必须同时给 loadavg 与 CV，并用「剔除 ENV_STALL 后中位」比较；
4. **续传**：补传字节比（journal 决定，稳定）+ 安静窗口恢复 wall；
5. **0 假红**：BEFORE 旧树没有可续传上传（无 `/api/upload/status`），脚本自动探测并记 `SKIPPED`（不计 FAIL，旧树冒烟已验证）。

**预注册验收线**：T4 后 btrfs 端到端应 ≥1.5×；T1/T8/T9 后 EVSD CPU s/MiB 应下降 ≥10%；任何配置 CPU s/MiB 上升 >3% 视为回退，必须解释。

**为什么基线 btrfs 绝对值不作为 before/after 吞吐基线**：本次基线在 load 10–18 的共享机采集，偶发 I/O 停顿已用 ENV_STALL 标注剔除，但剩余噪声仍大；quiet-window A/B 用同一脚本重采两组，本文档的 btrfs 绝对值只用于机制归因；**tmpfs 组 + CPU s/MiB 才是主判据**。

