#!/bin/bash
# 安装 RKLLM-Toolkit（自动匹配 Python cp 版本）
set +e

PROXY="http://172.28.240.1:10090"
export http_proxy="$PROXY"
export https_proxy="$PROXY"

PKG="$HOME/rkllm-work/rknn-llm/rkllm-toolkit/packages"
cd "$PKG" || { echo "✗ 找不到 packages 目录"; exit 1; }

echo "=== 本机 Python ==="
python3 --version
PYVER=$(python3 -c 'import sys; print(f"cp{sys.version_info.major}{sys.version_info.minor}")')
echo "需要: $PYVER"

echo "=== 可用 wheel ==="
ls -la *.whl

WHL=$(ls rkllm_toolkit-*-${PYVER}-${PYVER}-linux_x86_64.whl 2>/dev/null | head -1)
if [ -z "$WHL" ]; then
    echo "✗ 没有匹配 $PYVER 的 wheel"
    exit 1
fi
echo "=== 选用: $WHL ==="

echo "=== 查看依赖 ==="
cat requirements.txt 2>/dev/null | head -30

echo "=== 安装 ==="
pip3 install --user "./$WHL" 2>&1 | tail -25
RC=${PIPESTATUS[0]}
echo "pip 退出码: $RC"

echo "=== 验证 import ==="
python3 -c "from rkllm.api import RKLLM; print('✓ rkllm import OK'); print('RKLLM:', RKLLM)" 2>&1

exit 0
