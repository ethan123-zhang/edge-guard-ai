#!/bin/bash
# 在鲁班猫5 上【本机】编译 llm_demo（哨兵管家补丁版）
#
# 说明1：官方 build-linux.sh 用的是交叉编译器路径（~/opts/gcc-arm-...），
#        那是给 x86 PC 交叉编译用的。在板子上编译要改用系统 gcc/g++。
# 说明2：本包内的 src/llm_demo.cpp 已被替换为「支持系统提示词」的补丁版，
#        编译前会自检，避免官方原版被误编译进去、导致复验结论无效。
# 说明3：本包在 Windows 上打包，Unix 执行位丢失（Windows tar 的已知限制）。
#        本脚本会在开头【自动补齐】同目录所有 .sh 的执行位，
#        所以只要用 `bash build-on-board.sh` 启动一次，后面 `./xxx.sh` 就都能用。
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
SRC="$ROOT/examples/rkllm_api_demo/deploy/src/llm_demo.cpp"

echo "=== 自动补齐执行位（Windows 打的包会缺 +x）==="
FIXED=""
for s in "$ROOT"/*.sh; do
    [ -f "$s" ] || continue
    if [ ! -x "$s" ]; then
        chmod +x "$s" 2>/dev/null && FIXED="$FIXED $(basename "$s")"
    fi
done
if [ -n "$FIXED" ]; then
    echo "  ✓ 已补 +x:$FIXED"
else
    echo "  ✓ 执行位本来就正常"
fi

echo "=== 检查源码是否为补丁版 ==="
if [ ! -f "$SRC" ]; then
    echo "✗ 找不到源码：$SRC"
    exit 1
fi
if grep -q "system_prompt_file" "$SRC"; then
    echo "✓ 是补丁版（支持 system_prompt_file 参数）"
else
    echo "✗ 这是官方原版源码，不支持系统提示词！"
    echo "  请确认你解压的是 v2 包，且 src/llm_demo.cpp 已被替换。"
    exit 1
fi

cd "$ROOT/examples/rkllm_api_demo/deploy"

BUILD_DIR="$ROOT/build"
mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"

echo "=== 检查依赖 ==="
which cmake >/dev/null 2>&1 || { echo "✗ 缺 cmake: sudo apt install -y cmake"; exit 1; }
which g++   >/dev/null 2>&1 || { echo "✗ 缺 g++:   sudo apt install -y build-essential"; exit 1; }
echo "✓ cmake: $(cmake --version | head -1)"
echo "✓ g++:   $(g++ --version | head -1)"

echo "=== 配置 ==="
# 关键：用系统编译器，不指定交叉编译工具链
cmake "$ROOT/examples/rkllm_api_demo/deploy" \
    -DCMAKE_SYSTEM_PROCESSOR=aarch64 \
    -DCMAKE_SYSTEM_NAME=Linux \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_POSITION_INDEPENDENT_CODE=ON

echo "=== 编译 ==="
make -j4
make install

DEMO="$ROOT/examples/rkllm_api_demo/deploy/install/demo_Linux_aarch64/llm_demo"

echo
echo "=== 产出 ==="
ls -la "$ROOT/examples/rkllm_api_demo/deploy/install/demo_Linux_aarch64/" 2>/dev/null || \
ls -la "$ROOT"/examples/rkllm_api_demo/deploy/install/*/ 2>/dev/null

echo
echo "=== 编译后自检：补丁是否编进二进制 ==="
# 注意：这里【不能】直接跑 ./llm_demo，因为它依赖 ./lib/librkllmrt.so，
# 未设 LD_LIBRARY_PATH 时会以「cannot open shared object file」失败，
# 那样就分不清是「补丁没进去」还是「只是没设库路径」——曾被误判过一次。
# 所以先用 strings 查字符串（不依赖动态库），这是最可靠的判据。
if [ -x "$DEMO" ]; then
    if command -v strings >/dev/null 2>&1; then
        cnt=$(strings "$DEMO" 2>/dev/null | grep -c "system_prompt_file" || true)
        echo "  二进制内 'system_prompt_file' 出现次数: $cnt"
    else
        # 没有 strings 就退回 grep 二进制
        cnt=$(grep -c "system_prompt_file" "$DEMO" 2>/dev/null || true)
        echo "  二进制内 'system_prompt_file' 匹配行数: $cnt"
    fi

    if [ "${cnt:-0}" -ge 1 ]; then
        echo "  ✓ 补丁已编入二进制"
        echo
        echo "  下面附带打印真实用法（已自动设好 LD_LIBRARY_PATH）："
        ( cd "$(dirname "$DEMO")" && LD_LIBRARY_PATH=./lib "$DEMO" 2>&1 | head -4 ) || true
        echo
        echo "  ✓ 可以跑复验：$ROOT/verify-persona.sh"
    else
        echo "  ✗ 二进制里找不到 system_prompt_file —— 编译进去的是官方原版！"
        echo "    请确认: $SRC 含该字符串（本脚本开头已查过，正常应通过）"
        exit 1
    fi
fi
