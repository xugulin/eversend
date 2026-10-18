#!/bin/sh
# EverSend（韧传）源码树启动器 —— 供 packaging/linux/eversend.desktop 调用。
#
# 从应用菜单/桌面启动时，工作目录是 $HOME 或 /，而 EverSend 的数据目录默认取
# 「EVERSEND_HOME，否则当前目录」。所以这里必须先把 EVERSEND_HOME 固定到一个
# 稳定位置（默认 $HOME/.local/share/eversend），否则 data/ 和 received/ 会散落
# 到主目录甚至根目录。
set -eu

# 取脚本自身的**物理**路径：允许从别的目录调用、允许通过符号链接调用，
# 路径里带空格/中文也成立（与绿色包 run.sh 同一套逻辑，见 docs/PACKAGING.md §6.1）。
SELF=$0
hops=0
while [ -L "$SELF" ]; do
    hops=$((hops + 1))
    if [ "$hops" -gt 40 ]; then
        echo "eversend-launcher: 符号链接层数过多（>40），放弃解析" >&2
        exit 2
    fi
    link=$(readlink "$SELF")
    case $link in
        /*) SELF=$link ;;
        *) SELF=$(dirname "$SELF")/$link ;;
    esac
done
HERE=$(cd -P "$(dirname "$SELF")" && pwd)
ROOT=$(cd -P "$HERE/../.." && pwd)

# 源码运行需要 src 在 PYTHONPATH 上（仓库没有 pyproject，不 pip 安装）。
PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH

# 数据目录：调用方显式设置时尊重它（测试/便携包），否则固定到 XDG 数据目录。
: "${EVERSEND_HOME:=$HOME/.local/share/eversend}"
export EVERSEND_HOME
mkdir -p "$EVERSEND_HOME" 2>/dev/null || true

# README 的源码运行方式用 .venv；从菜单启动时 PATH 里的 python3 不一定装了
# PySide6，所以仓库里存在 .venv 就优先用它（可用 EVERSEND_PYTHON 覆盖）。
if [ -z "${EVERSEND_PYTHON:-}" ] && [ -x "$ROOT/.venv/bin/python" ]; then
    EVERSEND_PYTHON=$ROOT/.venv/bin/python
fi
PYTHON=${EVERSEND_PYTHON:-python3}
exec "$PYTHON" -m eversend "$@"
