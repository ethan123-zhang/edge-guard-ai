#!/bin/bash
# 在鲁班猫5 上【本机】编译 llm_demo（哨兵管家补丁版）
#
# 说明1：官方 build-linux.sh 用的是交叉编译器路径（~/opts/gcc-arm-...），
#        那是给 x86 PC 交叉编译用的。在板子上编译要改用系统 gcc/g++。
# 说明2：本包内的 src/llm_demo.cpp 已被替换为「支持系统提示词」的补丁版，
#        编译前会自检，避免官方原版被误编译进去、导致复验结论无效。
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
SRC="$ROOT/examples/rkllm_api_demo/deploy/src/llm_demo.cpp"

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
echo "=== 编译后自检：用法是否含新参数 ==="
if [ -x "$DEMO" ]; then
    "$DEMO" 2>&1 | head -3
    if "$DEMO" 2>&1 | grep -q "system_prompt_file"; then
        echo "✓ 补丁已生效，可以跑复验："
        echo "  $ROOT/verify-persona.sh"
    else
        echo "✗ 用法里没有 system_prompt_file，补丁没进去，请排查"
        exit 1
    fi
fi
