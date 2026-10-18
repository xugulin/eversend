# 真机验收报告 · baseline

生成时间：2026-09-25 22:02:13　·　HEAD `c3245e6`　·　变更文件 164 个　·　load `[16.2, 7.2, 4.5]`

- adb 设备：`10AEBS3L74002AA`　在线：**是**
- 电脑端端口：TCP 52117 / 网页 52119 / UDP 52118
- 测试数据：`/home/xgl/qa-real`（在 /home，不在 /tmp）

| # | 检查项 | 结论 | 失败的子断言 |
|---|---|---|---|
| A0 | 现场：真机在线、三个端口空闲、测试目录就绪 | ❌ FAIL | A0.3 电脑端三个端口都空闲（同一时刻只能一个实例） |
| A1 | 电脑发小文件（<1 MB）→ 手机磁盘 + sha256 | ✅ PASS | — |
| A6 | 反作弊：界面说「已收到」时手机磁盘必须真的有（否则判红） | ❌ FAIL | A6.1 界面说已收到的文件，磁盘上都有 |

## 明细

### A0 现场：真机在线、三个端口空闲、测试目录就绪
- ✅ **A0.1 adb 设备 10AEBS3L74002AA 在线**
  - 期望：`devices 列表里是 device`
  - 实际：`List of devices attached / 10AEBS3L74002AA	device`
  - 说明：手机掉线时后续各项一律标「未判定」，不许当绿
- ✅ **A0.phone_ip**
  - 说明：inet 172.20.90.82/24
- ✅ **A0.2 手机屏幕开着（不是锁屏状态）**
  - 期望：`能读到窗口焦点`
  - 实际：`已读到`
- ❌ **A0.3 电脑端三个端口都空闲（同一时刻只能一个实例）**
  - 期望：`{'tcp': True, 'web': True, 'discovery': True}`
  - 实际：`{'tcp': False, 'web': False, 'discovery': False}`
  - 说明：TCP 52117 / 网页 52119 / UDP 52118；被占时先确认是不是别人的实例
- ✅ **A0.4 测试数据目录在 /home（不在 /tmp）**
  - 期望：`以 /home 开头`
  - 实际：`/home/xgl/qa-real`
- ✅ **A0.disk_home**
  - 说明：/home 可用 142.6 GiB
- ✅ **A0.download_before**
  - 说明：/sdcard/Download 现有 28 项：['(1)', '.801ab.cuid', '.exmu-cfg1.data', '.pending-1790865287-IMG_20260923_175527.jpg', '.pending-1790865290-IMG_20260923_175527.jpg', '.pending-1790865291-IMG_20260923_175527.jpg', '.pending-1790865301-IMG_20260923_175527.jpg', '.pending-1790865580-README.txt']
- ✅ **A0.5 产品能被 import（不是别人写作途中的中间态）**
  - 实际：`engine import ok`

### A1 电脑发小文件（<1 MB）→ 手机磁盘 + sha256
- ✅ **A1 手机已连上电脑端**
  - 期望：`（负载下未判定）`
  - 实际：`设备列表里没有 source=web 的行（App 没连上/熄屏）`

### A6 反作弊：界面说「已收到」时手机磁盘必须真的有（否则判红）
```
Traceback (most recent call last):
  File "/home/xgl/python/跨平台文件互传/eversend/tests/test_real_device.py", line 679, in run
    fn(recorder)
    ~~^^^^^^^^^^
  File "/home/xgl/python/跨平台文件互传/eversend/tests/test_real_device.py", line 604, in a06
    pend_zero = sorted(name for name in pend if disk.get(n) == 0)
  File "/home/xgl/python/跨平台文件互传/eversend/tests/test_real_device.py", line 604, in <genexpr>
    pend_zero = sorted(name for name in pend if disk.get(n) == 0)
                                                         ^
NameError: name 'n' is not defined
```
- ✅ **A6.claims**
  - 说明：界面上的完成标记 3 个，涉及文件 ['大文件测试.bin', '小文件A.bin', '进度测试.bin']
- ❌ **A6.1 界面说已收到的文件，磁盘上都有**
  - 期望：`每个都出现在 /sdcard/Download`
  - 实际：`缺失=['大文件测试.bin', '小文件A.bin', '进度测试.bin']；磁盘现有 ['(1)', '.801ab.cuid', '.exmu-cfg1.data', '.pending-1790865287-IMG_20260923_175527.jpg', '.pending-1790865290-IMG_20260923_175527.jpg', '.pending-1790865291-IMG_20260923_175527.jpg', '.pending-1790865301-IMG_20260923_175527.jpg', '.pending-1790865580-README.txt', '.pending-1790865581-README.txt', '.pending-1790865582-README.txt']`
  - 说明：责任人 android-ui：App 侧「已收到」写早了 —— 它只表示消息投递成功，不表示文件落盘。E3「无静默失败」同类问题
- ✅ **A6.2 界面说已收到的文件，磁盘上都不是 0 字节**
  - 期望：`大小 > 0`
  - 实际：`0 字节=[]`
  - 说明：实测见过 4 个 .pending-179086558x-README.txt 是 0 字节残留

