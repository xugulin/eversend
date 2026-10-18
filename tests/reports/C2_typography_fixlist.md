# C2 排版 · 可施工清单（逐处替换用）

来源：`tests/reports/smoke-c2.json`（2026-09-25 19:05:56，HEAD `c3245e6`，load average `[8.37, 8.05, 7.72]`）

判据：间距/边距落在 **4/8/12/16/20/24/32**；字号取 **11/12/13/15/17/22**；圆角取 **6/10/14/999**。按 Lead 裁决 **0 与 1px（发丝线）合规**；Qt 样式默认值（本机量到 margin 9/11、spacing 6）已排除，不算应用的选择。

## 归属

| 文件:行 | 实际 | 期望 | 负责人 |
|---|---|---|---|
| `chat_view.py:114` | `setSpacing(6)` | 4 或 8 | desktop-chat |
| `chat_view.py:122` | `setContentsMargins(2, 2, 2, 2)` | 0 或 4 | desktop-chat |
| `chat_view.py:150` | `setSpacing(2)` | 0 或 4 | desktop-chat |
| `chat_view.py:346` | `setContentsMargins(16, 14, 16, 14)` | `(16, 12, 16, 12)` | desktop-chat |
| `chat_view.py:921` | `setSpacing(6)` | 4 或 8 | desktop-chat |
| `sidebar.py:161` | `setSpacing(theme.SPACE["sm"] - 2)` = 6 | `theme.SPACE["sm"]`(8) 或 `"xs"`(4) | desktop-shell |
| `sidebar.py:387` | 同上 | 同上 | desktop-shell |
| `widgets.py:108` | `setContentsMargins(2, 2, 2, 2)` | 0 或 4 | desktop-shell |
| `widgets.py:109` | `setSpacing(2)` | 0 或 4 | desktop-shell |
| `widgets.py:581` | `setSpacing(2)` | 0 或 4 | desktop-shell |
| `widgets.py:382` | `setSpacing(1)` | ✅ 合规（发丝线），不用改 | — |

## 原始输出

### C2.1a 运行期布局 margin 是 4 的倍数（检查 92 个布局，违规 3 处）

- 期望：落在 4 的倍数（0/1 发丝线合规；Qt 样式默认 [6, 9, 11] 不算应用选择）
- 实际：`Top8：['SegmentedControl#Segmented margins=(2, 2, 2, 2)', 'QWidget#- margins=(16, 14, 16, 14)', 'QWidget#- margins=(2, 2, 2, 2)']`

### C2.1b 运行期布局 spacing 是 4 的倍数（检查 92 个布局，违规 1 处）

- 期望：落在 4 的倍数（Qt 样式默认 [6, 9, 11] 不算应用选择）
- 实际：`Top8：['SegmentedControl#Segmented spacing=2']`

### C2.1c 源码里的间距字面量都在刻度上（检查 137 个字面量，违规 12 处）

- 期望：4 的倍数（0/1 合规）
- 实际：`Top10：['chat_view.py:114 setSpacing(6) → [6]', 'chat_view.py:122 setContentsMargins(2, 2, 2, 2) → [2, 2, 2, 2]', 'chat_view.py:150 setSpacing(2) → [2]', 'chat_view.py:346 setContentsMargins(16, 14, 16, 14) → [14, 14]', 'chat_view.py:921 setSpacing(6) → [6]', 'sidebar.py:161 setSpacing(theme.SPACE["sm"] - 2) → [2]', 'sidebar.py:387 setSpacing(theme.SPACE["sm"] - 2) → [2]', 'widgets.py:108 setContentsMargins(2, 2, 2, 2) → [2, 2, 2, 2]', 'widgets.py:109 setSpacing(2) → [2]', 'widgets.py:581 setSpacing(2) → [2]']`

### C2.2 字号只取类型刻度 11/12/13/15/17/22（检查 0 处，违规 0 处） ✅
- Top10：[]

### C2.3 圆角只取 token 6/10/14/999（检查 0 处，违规 0 处） ✅
- Top10：[]
