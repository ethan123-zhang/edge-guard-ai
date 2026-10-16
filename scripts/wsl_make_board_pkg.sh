#!/bin/bash
# 为板端准备最小编译包
# 只打包板端编译 llm_demo 真正需要的文件，而不是 773M 的整个仓库。
set -e

SRC="$HOME/rkllm-work/rknn-llm"
OUT="$HOME/board-pkg"
NAME="rkllm-board-v1.3.1"

rm -rf "$OUT"
mkdir -p "$OUT/$NAME"

cd "$SRC"

echo "=== 拷贝 deploy 源码 ==="
mkdir -p "$OUT/$NAME/examples/rkllm_api_demo"
cp -r examples/rkllm_api_demo/deploy "$OUT/$NAME/examples/rkllm_api_demo/"

echo "=== 拷贝 runtime（Linux/aarch64 部分）==="
mkdir -p "$OUT/$NAME/rkllm-runtime/Linux/librkllm_api/include"
mkdir -p "$OUT/$NAME/rkllm-runtime/Linux/librkllm_api/aarch64"
cp rkllm-runtime/Linux/librkllm_api/include/rkllm.h \
   "$OUT/$NAME/rkllm-runtime/Linux/librkllm_api/include/"
cp rkllm-runtime/Linux/librkllm_api/aarch64/librkllmrt.so \
   "$OUT/$NAME/rkllm-runtime/Linux/librkllm_api/aarch64/"

echo "=== 写一个板端本机编译脚本（覆盖官方的交叉编译版）==="
cat > "$OUT/$NAME/build-on-board.sh" <<'EOS'
#!/bin/bash
# 在鲁班猫5 上【本机】编译 llm_demo
# 说明：官方 build-linux.sh 用的是交叉编译器路径（~/opts/gcc-arm-...），
#       那是给 x86 PC 交叉编译用的。在板子上编译要改用系统 gcc/g++。
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
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

echo
echo "=== 产出 ==="
ls -la "$ROOT/examples/rkllm_api_demo/deploy/install/demo_Linux_aarch64/" 2>/dev/null || \
ls -la "$ROOT"/examples/rkllm_api_demo/deploy/install/*/ 2>/dev/null
EOS
chmod +x "$OUT/$NAME/build-on-board.sh"

echo "=== 打 tar.gz ==="
cd "$OUT"
tar czf "$NAME.tar.gz" "$NAME"

echo
echo "=== 结果 ==="
ls -la "$OUT/"
echo
echo "--- 包大小对比 ---"
echo "原仓库:  $(du -sh "$SRC" | cut -f1)"
echo "最小包:  $(du -sh "$OUT/$NAME.tar.gz" | cut -f1)"
echo
echo "=== 包内结构 ==="
tar tzf "$NAME.tar.gz" | head -25
