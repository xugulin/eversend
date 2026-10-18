# QA 报告索引（`tests/reports/`）

这个目录里既有 **QA 的验收产物**，也有**实现方自己跑我的套件留下的报告**（文件名带实现方名字）。
导航如下。原始数据一律是同名的 `.json`。

## 先看这三份

| 文件 | 是什么 |
|---|---|
| `final_qa_verdict.md` | **QA 终局判定**：结论、每条 C 项的判定口径与证据、"未由 QA 独立复现"清单、**11 条校准记录**、证据索引、风险建议 |
| `candidate-final.md` / `.json` | **权威数据**：最近一次全量运行（17 项，含每条子断言的期望/实际） |
| `screenshot_index.md` | 截图索引 + 重拍配方 + 两个截图目录的语义 + **冻结树规则** |

## 专题报告

| 文件 | 用途 |
|---|---|
| `C2_typography_fixlist.md` | C2 的**可施工清单**：`文件:行号 + 实际 + 期望 + 负责人` |
| `readme_screenshots_audit.md` | README 图片引用 vs 磁盘实况（断言 = `--only E6`） |
| `manual_veth_scan.md` | netns + veth 下验主动扫描的**手工**配方与判读（不能进 CI） |
| `final.md` / `final.json` | Lead 跑的那次全量（脚本自动生成，未改写） |

## 复验记录（`recheck-*` 是我按 `--only` 逐条复跑留下的）

| 文件 | 复验内容 |
|---|---|
| `recheck-c1c2.md` | C1（安卓两条修完）、C2（10 处字面量清零） |
| `recheck-c5.md` | C5（失败行「重试」）—— 证明上一轮红是撞上中间状态 |
| `recheck-c11.md` | C11（真按键 + 模式药丸） |
| `recheck-c13c14.md` / `recheck-c14.md` | C13/C14 |
| `recheck-e4.md` / `recheck-e456.md` / `recheck-e5.md` | E4/E5/E6 的判据修正后复跑 |

`smoke-*` 是我开发断言时的中间产物（保留是为了让"断言怎么一步步收紧的"可追溯）。

## 别人跑的报告

`records-data-*.md`、`shell-*.md`、`chat-final-C6.md`、`web-fix.md` 是实现方用我的套件自测留下的。
它们**不是** QA 的结论 —— QA 的结论只看 `final_qa_verdict.md` 与它引用的权威数据。

## 怎么自己再跑一遍

```bash
cd eversend
# 全量（约 8 分钟，含 E4 的七套子测试）
xvfb-run -a -s "-screen 0 1280x900x24" .venv/bin/python tests/test_enterprise_acceptance.py --label my-run

# 单条复验（例如只跑 C13/C14）
xvfb-run -a -s "-screen 0 1280x900x24" .venv/bin/python tests/test_enterprise_acceptance.py \
    --only C13,C14 --label recheck-mine

# 跑全量时把最耗时的 test_resilience 也带上
EVERSEND_QA_FULL_LEGACY=1 xvfb-run -a ... --label my-run
```

**开跑前**：`git status --short` 看一眼有没有文件刚被改过 ——
对着"别人还在写"的树跑出来的结论只能叫候选，不能当交付证据。
