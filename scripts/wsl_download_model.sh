#!/bin/bash
# 下载 Qwen3-0.6B 模型（多路径尝试 + 断点续传友好）
set +e

export http_proxy="http://172.28.240.1:10090"
export https_proxy="$PROXY"

DEST="$HOME/sentinel-models/Qwen3-0.6B"
mkdir -p "$HOME/sentinel-models"
cd "$HOME/sentinel-models" || exit 1

echo "=== 探测 GPU（决定转换用 cpu 还是 cuda）==="
nvidia-smi 2>/dev/null | head -12 || echo "无 nvidia-smi"
python3 -c "import torch; print('torch.cuda.is_available =', torch.cuda.is_available()); print('device_count =', torch.cuda.device_count())"
if python3 -c "import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)"; then
    python3 -c "import torch; print('GPU 型号:', torch.cuda.get_device_name(0))"
fi

echo
echo "=== 方式 A：modelscope SDK（推荐，支持续传）==="
python3 -m pip install --user -q modelscope 2>&1 | tail -3

# modelscope 装在 ~/.local/bin，不在 PATH 里
export PATH="$HOME/.local/bin:$PATH"

if [ -d "$DEST" ] && [ -n "$(ls -A "$DEST" 2>/dev/null)" ]; then
    echo "· $DEST 已存在，跳过"
else
    # 优先用 python -m 调用，避免 PATH 问题
    if python3 -m modelscope.cli.cli download --model Qwen/Qwen3-0.6B --local_dir "$DEST" 2>&1 | tail -25; then
        echo "· python -m 方式完成"
    else
        echo "· python -m 失败，试 PATH 里的 modelscope"
        modelscope download --model Qwen/Qwen3-0.6B --local_dir "$DEST" 2>&1 | tail -25
    fi
fi

echo
echo "=== 结果 ==="
if [ -d "$DEST" ]; then
    du -sh "$DEST"
    ls -la "$DEST"
else
    echo "✗ 未下载成功"
fi
exit 0
