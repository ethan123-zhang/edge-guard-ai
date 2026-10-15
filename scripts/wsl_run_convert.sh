#!/bin/bash
# 启动 Qwen3-0.6B → RKLLM 转换（长任务）
set +e

export http_proxy="http://172.28.240.1:10090"
export https_proxy="$PROXY"
# 让 toolkit 能下载它需要的 tokenizer 等资源
export HF_ENDPOINT="https://hf-mirror.com"

LOG="$HOME/sentinel-models/convert.log"
mkdir -p "$HOME/sentinel-models"

echo "=== 开始转换 $(date '+%F %T') ===" | tee "$LOG"
echo "日志: $LOG"

cd "$HOME/sentinel-models" || exit 1

# 日志同时写文件和控制台
python3 -u /mnt/e/哨兵管家/scripts/convert_qwen3_rkllm.py 2>&1 | tee -a "$LOG"

RC=${PIPESTATUS[0]}
echo
echo "=== 转换结束 $(date '+%F %T') 退出码=$RC ===" | tee -a "$LOG"

if [ $RC -eq 0 ]; then
    echo "=== 产出文件 ==="
    ls -la "$HOME/sentinel-models/rkllm-out/" 2>&1
fi
exit $RC
