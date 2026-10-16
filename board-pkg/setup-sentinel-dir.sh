#!/bin/bash
# ============================================================================
# 哨兵管家 —— 板端目录归整：把 LLM 相关的一切收进 ~/sentinel/
# ----------------------------------------------------------------------------
# 在【板子上】运行。作用：建立统一目录结构，便于后续 dialog/ 代码直接长在里面。
#
# 预期最终结构：
#   ~/sentinel/
#   ├── models/          Qwen3-0.6B_W8A8_RK3588.rkllm
#   ├── prompts/         §5.1 系统提示词
#   ├── rkllm/           编译产出（llm_demo + lib/librkllmrt.so）
#   ├── rkllm-src/       runtime 源码包（可重编译）
#   └── logs/            运行日志（含 tok/s 记录）
#
# 用法：bash setup-sentinel-dir.sh
# 安全：默认【只移动不删除】，且移动前检查目标是否已存在，避免覆盖。
# ============================================================================
set -u

SENT="$HOME/sentinel"

echo "============================================================"
echo " 板端目录归整 → $SENT"
echo "============================================================"
echo

# ---------- 1. 现状盘点 ----------
echo "--- 1. 归整前现状 ---"
for p in "$HOME/models" "$HOME/rkllm-board-v1.3.1" "$SENT"; do
    if [ -e "$p" ]; then
        cnt=$(find "$p" -type f 2>/dev/null | wc -l)
        sz=$(du -sh "$p" 2>/dev/null | cut -f1)
        echo "  [有] $p  ($cnt 个文件, $sz)"
    else
        echo "  [无] $p"
    fi
done
echo

# ---------- 2. 建目录 ----------
echo "--- 2. 建立目录结构 ---"
mkdir -p "$SENT"/{models,prompts,rkllm,rkllm-src,logs}
echo "  ✓ 已建: $SENT/{models,prompts,rkllm,rkllm-src,logs}"
echo

# 安全移动函数：目标存在就跳过，不覆盖
safe_move() {
    local src="$1" dst="$2"
    if [ ! -e "$src" ]; then
        echo "  - 跳过（源不存在）: $src"
        return 0
    fi
    if [ -e "$dst" ]; then
        echo "  ! 已存在，未覆盖: $dst"
        echo "    （源仍在 $src，请自行确认哪个是要留的）"
        return 0
    fi
    mv "$src" "$dst" && echo "  ✓ 移动: $src → $dst"
}

# ---------- 3. 迁移模型 ----------
echo "--- 3. 迁移模型 ---"
if [ -f "$HOME/models/Qwen3-0.6B_W8A8_RK3588.rkllm" ]; then
    safe_move "$HOME/models/Qwen3-0.6B_W8A8_RK3588.rkllm" "$SENT/models/"
else
    echo "  ! 未找到 ~/models/Qwen3-0.6B_W8A8_RK3588.rkllm"
    echo "    请先用 scp/拖拽把模型传到 ~/models/ 或直接传到 $SENT/models/"
fi
# 若模型直接传在别处，这里做一次兜底查找
if [ ! -f "$SENT/models/Qwen3-0.6B_W8A8_RK3588.rkllm" ]; then
    found=$(find "$HOME" -maxdepth 2 -name "Qwen3-0.6B_W8A8_RK3588.rkllm" 2>/dev/null | head -1)
    [ -n "$found" ] && safe_move "$found" "$SENT/models/"
fi
echo

# ---------- 4. 迁移编译源码包 ----------
echo "--- 4. 迁移 rkllm 源码/编译目录 ---"
if [ -d "$HOME/rkllm-board-v1.3.1" ]; then
    # 把编译产出单独链接到 rkllm/，方便固定路径引用
    DEMO_DIR="$HOME/rkllm-board-v1.3.1/examples/rkllm_api_demo/deploy/install/demo_Linux_aarch64"
    if [ -d "$DEMO_DIR" ]; then
        echo "  ✓ 找到编译产出: $DEMO_DIR"
        echo "    （保持原位，下面在 $SENT/rkllm/ 建软链，避免复制大文件）"
        ln -sfn "$DEMO_DIR" "$SENT/rkllm/demo_Linux_aarch64"
        echo "  ✓ 软链: $SENT/rkllm/demo_Linux_aarch64 → $DEMO_DIR"
    else
        echo "  ! 编译产出不存在，请先跑 build-on-board.sh"
    fi
    if [ -d "$HOME/rkllm-board-v1.3.1/prompts" ]; then
        for f in "$HOME/rkllm-board-v1.3.1/prompts"/*.txt; do
            [ -f "$f" ] && safe_move "$f" "$SENT/prompts/"
        done
    fi
else
    echo "  - 跳过（未找到 ~/rkllm-board-v1.3.1）"
fi
echo

# ---------- 5. 校验模型完整性 ----------
echo "--- 5. 模型完整性校验 ---"
M="$SENT/models/Qwen3-0.6B_W8A8_RK3588.rkllm"
if [ -f "$M" ]; then
    sz=$(stat -c%s "$M")
    echo "  文件: $M"
    echo "  大小: $sz 字节（应为 933035324）"
    if [ "$sz" = "933035324" ]; then
        echo "  ✓ 大小正确"
    else
        echo "  ✗ 大小不符 —— 传输可能不完整！"
        echo "    请重传后再继续（断点续传见下方说明）"
    fi
    echo "  正在算 SHA256（约需 10~30 秒）..."
    actual=$(sha256sum "$M" | cut -d' ' -f1)
    expect="8064c9a9b78b7c1068e26dd8e9901109c715b0af29907379c66379dc412b7679"
    echo "  实际: $actual"
    if [ "$actual" = "$expect" ]; then
        echo "  ✓✓ SHA256 完全一致，模型可用于复验"
    else
        echo "  ✗✗ SHA256 不一致！模型文件损坏，必须重传"
        echo "      切勿在此状态下跑复验，否则会把传输问题误判成模型问题"
    fi
else
    echo "  ! 模型不在 $SENT/models/，请先传输"
fi
echo

# ---------- 6. 结果 ----------
echo "============================================================"
echo " 归整后结构"
echo "============================================================"
if command -v tree >/dev/null 2>&1; then
    tree -L 2 "$SENT"
else
    find "$SENT" -maxdepth 2 | sort | sed "s|$HOME|~|"
fi
echo
echo "磁盘占用:"
du -sh "$SENT" 2>/dev/null
df -h "$HOME" | tail -1
echo
echo "后续运行命令（模型已在新位置）："
echo "  cd $SENT/rkllm/demo_Linux_aarch64"
echo "  export LD_LIBRARY_PATH=./lib:\$LD_LIBRARY_PATH"
echo "  export RKLLM_LOG_LEVEL=1"
echo "  ./llm_demo $SENT/models/Qwen3-0.6B_W8A8_RK3588.rkllm 48 2048 \\"
echo "      $SENT/prompts/system-prompt.txt off"
