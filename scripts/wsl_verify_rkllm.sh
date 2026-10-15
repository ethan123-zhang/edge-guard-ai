#!/bin/bash
# 校验转换产出的 .rkllm 文件完整性
set +e

OUT_DIR="$HOME/sentinel-models/rkllm-out"
F="$OUT_DIR/Qwen3-0.6B_W8A8_RK3588.rkllm"

echo "=== 目录内容 ==="
ls -la "$OUT_DIR"

if [ ! -f "$F" ]; then
    echo "✗ 文件不存在: $F"
    exit 1
fi

echo
echo "=== 文件信息 ==="
stat -c '路径: %n%n大小: %s 字节%n修改: %y' "$F"

echo
echo "=== 大小换算 ==="
SIZE=$(stat -c %s "$F")
echo "字节: $SIZE"
echo "MB  : $(echo "scale=1; $SIZE/1048576" | bc 2>/dev/null || python3 -c "print(f'{$SIZE/1048576:.1f}')")"

echo
echo "=== 文件头 16 字节（确认是有效 rkllm 容器）==="
od -A x -t x1z "$F" | head -2

echo
echo "=== 文件尾 16 字节（确认完整写入，未被截断）==="
tail -c 16 "$F" | od -A x -t x1z

echo
echo "=== SHA256（拷到板子后可核对）==="
sha256sum "$F"

echo
echo "=== 校准数据 ==="
cat "$OUT_DIR/data_quant.json"

exit 0
