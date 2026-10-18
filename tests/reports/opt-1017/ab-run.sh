#!/usr/bin/env bash
# A/B 测量：同一脚本、同一机器、同一安静窗口，分别对 BEFORE(e18c079 worktree) 与 AFTER(当前树) 跑。
# 用法: bash .testscratch/perf-bench-ab.sh <tree-dir> <label> <out-dir>
#   例: bash .testscratch/perf-bench-ab.sh /home/xgl/qa-real/baseline-tree before-quiet /home/xgl/qa-real/out/before
#       bash .testscratch/perf-bench-ab.sh "$PWD" after-quiet /home/xgl/qa-real/out/after
# 安静窗口守卫：loadavg(1min) > 4 直接 ABORT（Lead 要求 load<4 窗口）。
set -u
TREE=${1:?usage: perf-bench-ab.sh <tree-dir> <label> <out-dir>}
LABEL=${2:?}
OUT=${3:?}
cd "$TREE" || { echo "cannot cd $TREE"; exit 2; }
mkdir -p "$OUT"
LOAD=$(cut -d" " -f1 /proc/loadavg)
if awk "BEGIN{exit !($LOAD > 4.0)}"; then
  echo "ABORT: loadavg=$LOAD > 4.0，不是安静窗口（Lead 要求 load<4）" | tee "$OUT/aborted.txt"
  exit 3
fi
echo "=== AB run tree=$TREE label=$LABEL load=$LOAD $(date -Is) ===" | tee "$OUT/run.log"
START=$(date +%s)

echo "--- [1/3] 官方基线组：EVSD btrfs + HTTP(一次性/续传分段/下载/Range) + 续传" | tee -a "$OUT/run.log"
PYTHONPATH=src timeout 2400 python3 -u tools/bench_transfer.py all \
  --sizes 64,512,1024 --streams 1,2,4,8 --repeats 5 --warmups 1 \
  --resume-size-mib 512 --resume-repeats 2 \
  --json "$OUT/all.json" --md "$OUT/all.md" --label "$LABEL:all-btrfs" >>"$OUT/run.log" 2>&1
echo "    rc=$?" | tee -a "$OUT/run.log"

echo "--- [2/3] 协议/CPU 上限组：EVSD tmpfs（64/512×1,4 + 1GiB×4）" | tee -a "$OUT/run.log"
PYTHONPATH=src timeout 1200 python3 -u tools/bench_transfer.py evsd-loopback \
  --sizes 64,512 --streams 1,4 --repeats 5 --warmups 1 --scratch /dev/shm/perf-bench-ab \
  --json "$OUT/tmpfs-small.json" --md "$OUT/tmpfs-small.md" --label "$LABEL:tmpfs-small" >>"$OUT/run.log" 2>&1
echo "    rc=$?" | tee -a "$OUT/run.log"
PYTHONPATH=src timeout 1200 python3 -u tools/bench_transfer.py evsd-loopback \
  --sizes 1024 --streams 4 --repeats 5 --warmups 1 --scratch /dev/shm/perf-bench-ab \
  --json "$OUT/tmpfs-1g.json" --md "$OUT/tmpfs-1g.md" --label "$LABEL:tmpfs-1g" >>"$OUT/run.log" 2>&1
echo "    rc=$?" | tee -a "$OUT/run.log"

echo "--- [3/3] 2 GiB 分阶段 CPU 账本（tmpfs）" | tee -a "$OUT/run.log"
PYTHONPATH=src timeout 1200 python3 -u tools/bench_transfer.py cpu-split \
  --size-mib 2048 --streams 4 --repeats 3 --scratch /dev/shm/perf-bench-ab \
  --json "$OUT/cpu-split.json" --md "$OUT/cpu-split.md" --label "$LABEL:cpu-split-2g" >>"$OUT/run.log" 2>&1
echo "    rc=$?" | tee -a "$OUT/run.log"

END=$(date +%s)
echo "=== AB DONE tree=$TREE label=$LABEL elapsed=$((END-START))s load=$(cut -d' ' -f1 /proc/loadavg) $(date -Is) ===" | tee -a "$OUT/run.log"
