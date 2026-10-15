#!/bin/bash
# 用代理克隆 rknn-llm v1.3.1 并安装 RKLLM-Toolkit
set -e

PROXY="http://172.28.240.1:10090"
export http_proxy="$PROXY"
export https_proxy="$PROXY"

WORK="$HOME/rkllm-work"
mkdir -p "$WORK"
cd "$WORK"

echo "=== 1. 克隆 rknn-llm release-v1.3.1 ==="
if [ -d rknn-llm/.git ]; then
    echo "已存在，跳过克隆"
else
    git clone https://github.com/airockchip/rknn-llm.git \
        --branch release-v1.3.1 --depth 1
fi

cd rknn-llm
echo "=== 2. 确认版本 ==="
git log --oneline -1
git describe --tags 2>/dev/null || true

echo "=== 3. 列出可用 wheel（不要照抄文件名！）==="
ls -la rkllm-toolkit/packages/*.whl 2>/dev/null || {
    echo "✗ 未找到 wheel，列一下目录："
    find . -name "*.whl" 2>/dev/null | head -20
    ls -la rkllm-toolkit/ 2>/dev/null
    exit 1
}

echo "=== 4. 匹配本机 Python ==="
PYVER=$(python3 -c 'import sys; print(f"cp{sys.version_info.major}{sys.version_info.minor}")')
echo "本机 Python: $(python3 --version)  -> 需要 $PYVER"

WHL=$(ls rkllm-toolkit/packages/rkllm_toolkit-*-${PYVER}-${PYVER}-linux_x86_64.whl 2>/dev/null | head -1)
if [ -z "$WHL" ]; then
    echo "✗ 没有匹配 $PYVER 的 wheel，实际可用："
    ls rkllm-toolkit/packages/*.whl
    exit 1
fi
echo "选用: $WHL"

echo "=== 5. 安装 ==="
pip3 install --user "$WHL" 2>&1 | tail -15

echo "=== 6. 验证 import ==="
python3 -c "from rkllm.api import RKLLM; print('✓ rkllm import OK')"
echo
echo "=== 全部完成 ==="
