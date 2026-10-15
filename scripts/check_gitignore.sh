#!/usr/bin/env bash
# ============================================================
# check_gitignore.sh — 哨兵管家 推送前自检
#
# 用途：一条命令确认「没有该忽略的文件会被推上去」
#       并找出「新目录里没被 .gitignore 覆盖」的漏网之鱼。
#
# 用法：  bash scripts/check_gitignore.sh
# 退出码：0 = 安全可推；1 = 发现可疑文件，先处理
# ============================================================

set -u
cd "$(dirname "$0")/.." || exit 2

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; NC=$'\033[0m'
fail=0

echo "=============================================="
echo " 哨兵管家 — 推送前自检"
echo "=============================================="

# ---------- 1. 检查隐私/大文件是否被跟踪 ----------
echo
echo "[1] 检查敏感文件是否已被 git 跟踪"
echo "----------------------------------------------"

# 这些模式一旦出现在已跟踪列表里就是事故
PATTERNS='\.rkllm$|\.rknn$|\.onnx$|\.gguf$|\.pt$|\.pth$|\.bin$|\.npy$|\.npz$|\.db$|\.sqlite|\.wav$|\.mp3$|\.pcm$|\.jpg$|\.png$|\.mp4$|\.log$'

tracked_bad=$(git ls-files | grep -Ei "$PATTERNS" || true)

if [ -n "$tracked_bad" ]; then
    echo "${RED}✗ 以下敏感文件已被 git 跟踪（会推到 GitHub）：${NC}"
    echo "$tracked_bad" | sed 's/^/    /'
    echo
    echo "  处理：先加 .gitignore 规则，再执行"
    echo "    git rm --cached <文件>"
    fail=1
else
    echo "${GRN}✓ 无敏感文件被跟踪${NC}"
fi

# ---------- 2. 找出未跟踪但不该入库的文件 ----------
echo
echo "[2] 扫描工作区中「未被忽略」的可疑文件"
echo "----------------------------------------------"

# --others：未跟踪；--exclude-standard：排除 .gitignore 已覆盖的
# 剩下的就是"既没被跟踪、也没被忽略"的文件 → 下次 git add . 会进去
suspect=$(git ls-files --others --exclude-standard \
    | grep -Ei "$PATTERNS" || true)

if [ -n "$suspect" ]; then
    echo "${YLW}! 以下文件未被 .gitignore 覆盖，git add . 时会被推上去：${NC}"
    echo "$suspect" | head -40 | sed 's/^/    /'
    n=$(echo "$suspect" | wc -l)
    echo
    echo "  共 $n 个。处理方式："
    echo "    ① 若是隐私/大文件 → 在 .gitignore 补规则"
    echo "    ② 若是文档/代码   → 可正常入库，忽略本提示"
    fail=1
else
    echo "${GRN}✓ 无可疑的未跟踪文件${NC}"
fi

# ---------- 3. 列出当前所有目录，提示复核覆盖情况 ----------
echo
echo "[3] 当前工作区目录（新目录需确认已被 .gitignore 覆盖）"
echo "----------------------------------------------"

find . -type d \
    -not -path './.git*' \
    -not -path './node_modules*' \
    -not -path '*/__pycache__*' \
    -not -path './venv*' -not -path './.venv*' \
    -printf '    %p\n' 2>/dev/null | sort

echo
echo "  提示：对照 memory/00-maintenance-rule.md 的覆盖表，"
echo "        名字不在表内的自定义目录（如 datasets/、face_gallery/）需手工补规则。"

# ---------- 4. 与远端同步状态 ----------
echo
echo "[4] 与远端同步状态"
echo "----------------------------------------------"

git fetch origin --quiet 2>/dev/null
local_rev=$(git rev-parse --short main 2>/dev/null)
remote_rev=$(git rev-parse --short origin/main 2>/dev/null)
pending=$(git log origin/main..main --oneline 2>/dev/null | wc -l)

echo "    本地 main : $local_rev"
echo "    远端 main : $remote_rev"
echo "    未推送    : $pending 条"

if [ "$pending" -gt 0 ]; then
    echo "${YLW}  ! 有未推送的提交，收尾时要执行 git push origin main${NC}"
    fail=1
else
    echo "${GRN}✓ 已与远端同步${NC}"
fi

# ---------- 结论 ----------
echo
echo "=============================================="
if [ "$fail" -eq 0 ]; then
    echo "${GRN} 结论：安全，可以推送${NC}"
else
    echo "${RED} 结论：发现问题，请先处理上述项${NC}"
fi
echo "=============================================="

exit $fail
