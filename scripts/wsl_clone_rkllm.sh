#!/bin/bash
# 克隆 rknn-llm v1.3.1 —— 避开大仓库 TLS 中断问题
# 策略：加大缓冲 + 只取浅层 + 失败重试
set +e

PROXY="http://172.28.240.1:10090"
export http_proxy="$PROXY"
export https_proxy="$PROXY"

WORK="$HOME/rkllm-work"
mkdir -p "$WORK"
cd "$WORK" || exit 1

# 关键：加大 HTTP 缓冲，解决 "RPC failed / early EOF"
git config --global http.postBuffer 524288000
git config --global http.lowSpeedLimit 0
git config --global http.lowSpeedTime 999999
# 关掉 HTTP/2（GnuTLS 解码错误常见诱因）
git config --global http.version HTTP/1.1

rm -rf rknn-llm

echo "=== 尝试克隆（最多 3 次）==="
for i in 1 2 3; do
    echo "--- 第 $i 次尝试 ---"
    if git clone https://github.com/airockchip/rknn-llm.git \
        --branch release-v1.3.1 --depth 1 --single-branch; then
        echo "✓ 克隆成功"
        break
    fi
    echo "✗ 第 $i 次失败，清理后重试"
    rm -rf rknn-llm
    sleep 5
done

if [ ! -d rknn-llm/.git ]; then
    echo "✗✗ 三次克隆均失败"
    exit 1
fi

cd rknn-llm || exit 1
echo "=== 版本确认 ==="
git log --oneline -1

echo "=== 仓库大小 ==="
du -sh . 2>/dev/null

echo "=== 找 wheel ==="
find . -name "*.whl" 2>/dev/null

exit 0
