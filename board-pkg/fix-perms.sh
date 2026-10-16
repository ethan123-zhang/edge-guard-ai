#!/bin/bash
# ============================================================================
# 哨兵管家 —— 补执行位（在板子上跑）
# ----------------------------------------------------------------------------
# 为什么需要这个：
#   本包是在 **Windows** 上用 bsdtar 打的，而 **Windows tar 无法写入 Unix 权限位**，
#   所以包里所有 .sh 都是 -rw-rw-rw-（**没有 +x**）。
#   直接 `./build-on-board.sh` 会报：  权限不够
#
#   （对比：v1 包是在 WSL/Linux 里打的，保留了 -rwxr-xr-x，所以没有这个问题。）
#
# 用法（二选一）：
#   bash fix-perms.sh          ← 本脚本，补 +x 并打印结果
#   chmod +x *.sh              ← 等效的手工做法
# ============================================================================
set -u

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo "============================================================"
echo " 补执行位：$ROOT"
echo "============================================================"
echo

# ---------- 1. 现状 ----------
echo "--- 1. 补之前 ---"
found=0
for f in *.sh; do
    [ -f "$f" ] || continue
    found=1
    if [ -x "$f" ]; then
        echo "  [可执行] $f"
    else
        echo "  [缺 +x ] $f"
    fi
done
[ "$found" -eq 0 ] && echo "  ! 当前目录没有 .sh 文件，确认你在解压出来的 rkllm-board-v1.3.1/ 里"
echo

# ---------- 2. 补权限 ----------
echo "--- 2. 执行 chmod +x ---"
chmod +x *.sh 2>/dev/null || true
# 顺带把可能存在的子目录脚本也补上（deploy 里的官方脚本）
find "$ROOT" -name "*.sh" -type f -exec chmod +x {} \; 2>/dev/null || true
echo "  ✓ 已对全部 .sh 执行 chmod +x"
echo

# ---------- 3. 验证 ----------
echo "--- 3. 补之后 ---"
allok=1
for f in *.sh; do
    [ -f "$f" ] || continue
    if [ -x "$f" ]; then
        echo "  ✓ $f"
    else
        echo "  ✗ $f 仍然不可执行"
        allok=0
    fi
done
echo

if [ "$allok" -eq 1 ]; then
    echo "============================================================"
    echo " 完成，现在可以按顺序跑了："
    echo "============================================================"
    echo "  ./build-on-board.sh        # 编译（会自检补丁）"
    echo "  ./setup-sentinel-dir.sh    # 建 ~/sentinel/ 目录并归整"
    echo "  ./verify-persona.sh        # 人设复验（本次核心）"
    echo
    echo "  若嫌麻烦，也可以不改权限、直接用 bash 调用："
    echo "  bash build-on-board.sh"
    echo "  bash setup-sentinel-dir.sh"
    echo "  bash verify-persona.sh"
else
    echo "✗ 有文件仍不可执行，请检查文件系统是否以 noexec 挂载："
    echo "    mount | grep \" \$(df --output=target . | tail -1) \""
    exit 1
fi
