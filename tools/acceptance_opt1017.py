#!/usr/bin/env python3
"""阶段 3 全量验收编排器（opt-1017）。

把 docs/ENTERPRISE_CHARTER.md §8 的验收命令 + 本轮新增的测试串成**一次可复跑**的运行：
每一步的 stdout/stderr 落盘、退出码记录、汇总成 Markdown/JSON，并在报告头记录
"冻结树"信息（HEAD、工作树变更清单、关键文件 mtime、负载）—— 宪章要求 final 必须
对着冻结的树跑，跑之前确认没有文件晚于开跑时刻。

用法（在 eversend/ 下，图形用现有 :99 Xvfb）::

    DISPLAY=:99 QT_QPA_PLATFORM=xcb \
    EVERSEND_ADB_SERIAL=2430d3c EVERSEND_REAL_WORK=/home/xgl/qa-real \
    python3 tools/acceptance_opt1017.py --label final

    # 只跑部分 / 跳过真机
    python3 tools/acceptance_opt1017.py --only loopback,web --no-realdevice

说明：
* 真机用例会独占默认端口 52117/52118/52119，跑之前请确认没有别的电脑端实例；
* 每步有独立超时；失败不中断后续步骤（但要人工判断某步失败是否导致依赖步骤假红）；
* EVERSEND_TEST_TMP 默认指向 eversend/.testscratch，避免 /tmp tmpfs 被大文件打满。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OPT = REPO / "tests" / "reports" / "opt-1017"
LOGS = OPT / "acceptance-logs"


def step(
    name: str,
    title: str,
    argv: list[str],
    *,
    timeout: float,
    env: dict[str, str],
    expected: str = "",
) -> dict:
    LOGS.mkdir(parents=True, exist_ok=True)
    log_path = LOGS / f"{name}.log"
    started = time.time()
    print(f"\n=== [{name}] {title} ===", flush=True)
    try:
        proc = subprocess.run(
            argv, cwd=str(REPO), env=env, capture_output=True, timeout=timeout,
        )
        code = proc.returncode
        output = (proc.stdout or b"") + (proc.stderr or b"")
    except subprocess.TimeoutExpired as exc:
        code = 124
        output = (exc.stdout or b"") + (exc.stderr or b"") + "\n[超时]\n".encode("utf-8")
    elapsed = time.time() - started
    log_path.write_bytes(output)
    text = output.decode("utf-8", "replace")
    tail = "\n".join([line for line in text.splitlines() if line.strip()][-15:])
    passed = code == 0
    print(f"  {'PASS' if passed else 'FAIL'} exit={code} {elapsed:.1f}s log={log_path}", flush=True)
    if not passed:
        print("  ---- 末尾 15 行 ----")
        print("\n".join("  " + line for line in tail.splitlines()))
    return {
        "name": name,
        "title": title,
        "argv": argv,
        "exit": code,
        "elapsed": round(elapsed, 1),
        "log": str(log_path),
        "tail": tail,
        "passed": passed,
        "expected": expected,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="opt-1017 阶段3全量验收")
    parser.add_argument("--label", default="final", help="报告 label")
    parser.add_argument("--only", default="", help="逗号分隔步骤名（默认全部）")
    parser.add_argument("--no-realdevice", action="store_true", help="跳过真机用例")
    parser.add_argument("--skip-compile", action="store_true", help="跳过 compileall")
    args = parser.parse_args(argv)

    OPT.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env.setdefault("PYTHONPATH", "src")
    env.setdefault("EVERSEND_TEST_TMP", str(REPO / ".testscratch"))
    env.setdefault("DISPLAY", ":99")
    env.setdefault("QT_QPA_PLATFORM", "xcb")
    env.setdefault("PYTHONUNBUFFERED", "1")
    # 宪章 §8 第 3 条：绝对吞吐阈值在多任务机器上只会制造噪声。loopback 的
    # 60 MiB/s 地板在 load>10 时会假红（真实吞吐交给 perf-bench 的 A/B 测量），
    # 验收只要求"明显不是坏的"（5 MiB/s）。
    env.setdefault("EVERSEND_MBPS_FLOOR", "5")

    python = sys.executable
    steps: list[tuple[str, str, list[str], float, str]] = []
    if not args.skip_compile:
        steps.append((
            "compile", "编译检查（src/tests/tools 顶层）",
            [python, "-m", "compileall", "-q", "src", "tests"], 300.0, "",
        ))
    steps += [
        ("loopback", "内核 e2e（loopback，含 2GiB 吞吐）",
         [python, "tests/test_loopback.py"], 1200.0, "test result"),
        ("resilience", "恶劣网络/中断恢复",
         [python, "tests/test_resilience.py"], 1200.0, "test result"),
        ("concurrency", "并发传输",
         [python, "tests/test_concurrency.py"], 900.0, "test result"),
        ("chat", "聊天/记录模型",
         [python, "tests/test_chat.py"], 600.0, "test result"),
        ("http-resume", "可续传上传（opt-1017 新增）",
         [python, "tests/test_http_resume.py"], 300.0, "全部通过"),
        ("web", "网页版 356 项自检",
         [python, "-u", "src/eversend/web/selftest.py"], 1200.0, "All .* checks passed"),
        ("gui-render", "Qt 离屏渲染",
         [python, "tests/test_gui_render.py"], 900.0, ""),
        ("gui-actions", "Qt 真点击交互",
         [python, "tests/test_gui_actions.py"], 1800.0, ""),
        ("enterprise", "企业级验收 C1-C14",
         [python, "tests/test_enterprise_acceptance.py", "--label", f"opt-{args.label}"], 2400.0, ""),
    ]
    if not args.no_realdevice:
        steps.append((
            "realdevice", "MI9 真机 A0-A9（APK↔Linux）",
            [python, "tests/test_real_device.py", "--label", f"opt-{args.label}"], 2400.0, "",
        ))

    wanted = {item.strip() for item in args.only.split(",") if item.strip()}
    if wanted:
        steps = [s for s in steps if s[0] in wanted]

    # ---- 冻结树快照 -------------------------------------------------------
    def git(*cmd: str) -> str:
        try:
            return subprocess.run(["git", *cmd], cwd=str(REPO), capture_output=True,
                                  text=True, timeout=30).stdout.strip()
        except Exception:
            return ""

    head = git("log", "-1", "--format=%h %s")
    changed = [line for line in git("status", "--short").splitlines() if line.strip()]
    tracked = [line[3:] for line in changed if not line.startswith("??")]
    mtimes = {}
    for rel in tracked:
        path = REPO / rel
        if path.is_file():
            mtimes[rel] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime))
    freeze = {
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "head": head,
        "changed_count": len(changed),
        "changed": changed[:80],
        "tracked_mtimes": mtimes,
        "loadavg": os.getloadavg(),
        "python": sys.version.split()[0],
        "label": args.label,
        "cmdline": [sys.executable, __file__, *sys.argv[1:]],
    }
    started_at = time.time()
    results = []
    for name, title, argv_step, timeout, expected in steps:
        results.append(step(name, title, argv_step, timeout=timeout, env=env, expected=expected))

    total = time.time() - started_at
    # 跑的过程中树被改了 → 报告里明确写出来（final 不能对着写入中的树）
    after = [line for line in git("status", "--short").splitlines() if line.strip()]
    freeze["changed_after"] = after[:80]
    freeze["duration_s"] = round(total, 1)

    payload = {"freeze": freeze, "results": results,
               "passed": sum(1 for r in results if r["passed"]),
               "failed": sum(1 for r in results if not r["passed"]),
               "total": len(results)}
    json_path = OPT / f"acceptance-{args.label}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")

    lines = [
        f"# 全量验收报告 · {args.label}",
        "",
        f"- 时间：{freeze['when']}　·　耗时：{total/60:.1f} 分钟　·　Python {freeze['python']}",
        f"- HEAD：`{freeze['head']}`　·　工作树变更：{freeze['changed_count']} 项",
        f"- 负载：{freeze['loadavg']}（{os.cpu_count()} 核）",
        "",
        "## 结果",
        "",
        "| 步骤 | 内容 | 结果 | 耗时 | 日志 |",
        "|---|---|---|---|---|",
    ]
    for item in results:
        mark = "✅ PASS" if item["passed"] else f"❌ FAIL(exit={item['exit']})"
        log_name = Path(item["log"]).name
        lines.append(
            f"| {item['name']} | {item['title']} | {mark} | {item['elapsed']}s | "
            f"`{log_name}` |"
        )
    lines += [
        "",
        f"**{payload['passed']} / {payload['total']} 步通过**",
        "",
        "## 冻结树证据",
        "",
        "```",
        f"HEAD: {head}",
        f"变更文件数: {len(changed)}",
        *[f"  {line}" for line in changed[:40]],
        "```",
        "",
        "## 失败步骤明细",
        "",
    ]
    failures = [r for r in results if not r["passed"]]
    if not failures:
        lines.append("（无）")
    else:
        for item in failures:
            lines += [f"### {item['name']}（exit={item['exit']}）", "", "```",
                      item["tail"], "```", ""]
    md_path = OPT / f"acceptance-{args.label}.md"
    md_path.write_text("\n".join(lines) + "\n", "utf-8")
    print(f"\n验收报告：{md_path}")
    print(f"{payload['passed']}/{payload['total']} 步通过")
    return 0 if payload["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
