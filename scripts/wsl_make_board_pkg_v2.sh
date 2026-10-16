#!/bin/bash
# ============================================================================
# 重新打包板端最小包（含 llm_demo 提示词补丁）
# ----------------------------------------------------------------------------
# 在原有 wsl_make_board_pkg.sh 的基础上，额外做三件事：
#   1. 用本仓库 board-pkg/src/llm_demo.cpp 覆盖官方 demo 源码（关键！）
#   2. 打包 prompts/ 目录（系统提示词）
#   3. 打包 verify-persona.sh（板端复验脚本）
#
# 用法：bash scripts/wsl_make_board_pkg_v2.sh
# 依赖：$HOME/rkllm-work/rknn-llm（官方仓库，已克隆）
# ============================================================================
set -e

SRC="$HOME/rkllm-work/rknn-llm"
# 本仓库在本机（Windows）的路径，通过 /mnt/e 访问
REPO="/mnt/e/哨兵管家"
OUT="$HOME/board-pkg"
NAME="rkllm-board-v1.3.1"

if [ ! -d "$SRC" ]; then
    echo "✗ 找不到官方仓库：$SRC"
    echo "  请先按 docs/04 克隆 rknn-llm（Toolkit 1.3.1）"
    exit 1
fi
if [ ! -d "$REPO" ]; then
    echo "✗ 找不到本仓库：$REPO"
    echo "  如果你不在 WSL 里，请在 Windows 端用 scripts/make_board_pkg.ps1"
    exit 1
fi
if [ ! -f "$REPO/board-pkg/src/llm_demo.cpp" ]; then
    echo "✗ 找不到补丁源码：$REPO/board-pkg/src/llm_demo.cpp"
    exit 1
fi

rm -rf "$OUT"
mkdir -p "$OUT/$NAME"

cd "$SRC"

echo "=== 1. 拷贝 deploy 源码 ==="
mkdir -p "$OUT/$NAME/examples/rkllm_api_demo"
cp -r examples/rkllm_api_demo/deploy "$OUT/$NAME/examples/rkllm_api_demo/"

echo "=== 2. 用补丁版覆盖官方 llm_demo.cpp ==="
cp "$REPO/board-pkg/src/llm_demo.cpp" \
   "$OUT/$NAME/examples/rkllm_api_demo/deploy/src/llm_demo.cpp"
# 自检：确认覆盖成功（必须含补丁标志）
if ! grep -q "system_prompt_file" "$OUT/$NAME/examples/rkllm_api_demo/deploy/src/llm_demo.cpp"; then
    echo "✗ 覆盖后仍找不到补丁标志，中止"
    exit 1
fi
echo "✓ 已覆盖，且确认含补丁标志"

echo "=== 3. 拷贝 prompts 与复验脚本 ==="
mkdir -p "$OUT/$NAME/prompts"
cp "$REPO/board-pkg/prompts/system-prompt.txt"          "$OUT/$NAME/prompts/"
cp "$REPO/board-pkg/prompts/system-prompt.template.txt" "$OUT/$NAME/prompts/"
cp "$REPO/board-pkg/verify-persona.sh"                  "$OUT/$NAME/"
chmod +x "$OUT/$NAME/verify-persona.sh"

echo "=== 4. 拷贝 runtime（Linux/aarch64）==="
mkdir -p "$OUT/$NAME/rkllm-runtime/Linux/librkllm_api/include"
mkdir -p "$OUT/$NAME/rkllm-runtime/Linux/librkllm_api/aarch64"
cp rkllm-runtime/Linux/librkllm_api/include/rkllm.h \
   "$OUT/$NAME/rkllm-runtime/Linux/librkllm_api/include/"
cp rkllm-runtime/Linux/librkllm_api/aarch64/librkllmrt.so \
   "$OUT/$NAME/rkllm-runtime/Linux/librkllm_api/aarch64/"

echo "=== 5. 写板端本机编译脚本 ==="
cp "$REPO/board-pkg/build-on-board.sh" "$OUT/$NAME/build-on-board.sh"
chmod +x "$OUT/$NAME/build-on-board.sh"

echo "=== 6. 打 tar.gz ==="
cd "$OUT"
tar czf "$NAME.tar.gz" "$NAME"

echo
echo "=== 结果 ==="
ls -la "$OUT/$NAME.tar.gz"
echo
echo "=== 包内结构 ==="
tar tzf "$NAME.tar.gz"
echo
echo "=== 关键内容自检 ==="
echo "--- llm_demo.cpp 是否含补丁 ---"
tar xzfO "$NAME.tar.gz" "$NAME/examples/rkllm_api_demo/deploy/src/llm_demo.cpp" | grep -c "system_prompt_file" || true
echo "--- 提示词首行 ---"
tar xzfO "$NAME.tar.gz" "$NAME/prompts/system-prompt.txt" | head -1
echo
echo "完成后把包拷回 Windows："
echo "  cp $OUT/$NAME.tar.gz /mnt/e/哨兵管家/board-pkg/"
