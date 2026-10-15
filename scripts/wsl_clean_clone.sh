#!/bin/bash
# 清理未完成的克隆
set +e

echo "=== 杀掉残留 git 进程 ==="
pkill -f "git clone" 2>/dev/null && echo "已杀 git clone" || echo "无 git clone 进程"
pkill -f "index-pack" 2>/dev/null && echo "已杀 index-pack" || echo "无 index-pack 进程"
sleep 2

echo "=== 删除未完成目录 ==="
if [ -d "$HOME/rkllm-work/rknn-llm" ]; then
    rm -rf "$HOME/rkllm-work/rknn-llm"
    echo "已删除 $HOME/rkllm-work/rknn-llm"
else
    echo "目录不存在，无需删除"
fi

echo "=== 当前状态 ==="
ls -la "$HOME/rkllm-work/" 2>&1
echo "=== 剩余 git 进程 ==="
pgrep -af git || echo "无"
exit 0
