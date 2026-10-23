#!/bin/bash
# jetson-build-opencv.sh — 在 Jetson Nano（JetPack 4.6 / CUDA 10.2）上自编 OpenCV 4.5.4
#
# 为什么需要：JP46 自带 OpenCV 4.1.1 —— 有 dnn、**没有** cv2.FaceDetectorYN / FaceRecognizerSF
# （这两个在 objdetect 里，4.5.4 才进主线）。V1（YuNet + SFace）必须 ≥4.5.4。
#
# 策略：
#   先编 **不带 CUDA** 的最小集合（约 40–60 分钟）→ 立刻可答 V1「能不能跑通 + 单帧多快」；
#   若 CPU 版太慢，再编 CUDA 版（-DWITH_CUDA=ON -DCUDA_ARCH_BIN=5.3，预计 4–6 小时）。
#   Nano 只有 4GB 内存，务必先有 swap（本脚本会检查）。
#
# 用法（在板子上）：
#   bash jetson-build-opencv.sh              # 安装依赖 + 下载 + 编译 + 安装
#   OPENCV_VER=4.5.4 JOBS=4 bash jetson-build-opencv.sh
#   SKIP_APT=1 bash jetson-build-opencv.sh   # 依赖已装好时跳过 apt
#
# 日志：~/opencv-build.log（本脚本自己 tee）
set -o pipefail

OPENCV_VER="${OPENCV_VER:-4.5.4}"
JOBS="${JOBS:-4}"
SRC="${SRC:-$HOME/opencv-src}"
BUILD="$SRC/build"
JOBS_LOW=$(( JOBS < 4 ? JOBS : 4 ))
LOG="$HOME/opencv-build.log"

exec > >(tee -a "$LOG") 2>&1
echo "===== $(date)  开始编 OpenCV $OPENCV_VER（JOBS=$JOBS）====="

# ---------- 0. 前置检查 ----------
AVAIL_KB=$(df --output=avail / | tail -1 | tr -d ' ')
echo "根分区可用: $((AVAIL_KB/1024/1024)) GiB"
if [ "$AVAIL_KB" -lt 6000000 ]; then
  echo "!! 可用空间不足 6 GiB，先扩分区（docs/04 §2.0）"; exit 2
fi
SWAP_KB=$(free | awk '/^Swap:/{print $2}')
echo "Swap: $((SWAP_KB/1024)) MiB"
if [ "$SWAP_KB" -lt 2000000 ]; then
  echo "!! Swap 不足 2 GiB，编译会 OOM。先: sudo fallocate -l 4G /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile"
  exit 3
fi

# ---------- 1. 依赖 ----------
if [ "${SKIP_APT:-0}" != "1" ]; then
  echo "---- apt ----"
  sudo apt-get update || echo "!! apt-get update 失败（先解决网络）"
  sudo apt-get install -y --no-install-recommends \
    build-essential cmake git pkg-config \
    python3-dev python3-numpy \
    libjpeg8-dev libpng-dev libtiff5-dev \
    libavcodec-dev libavformat-dev libswscale-dev libv4l-dev \
    libeigen3-dev || { echo "!! 依赖安装失败"; exit 4; }
  ls /usr/include/python3.6m/Python.h >/dev/null 2>&1 || { echo "!! 仍缺 Python.h"; exit 4; }
fi

# ---------- 2. 源码 ----------
mkdir -p "$SRC"
cd "$SRC" || exit 5
if [ ! -d "$SRC/opencv-$OPENCV_VER" ]; then
  echo "---- 下载 OpenCV $OPENCV_VER ----"
  URLS=(
    "https://github.com/opencv/opencv/archive/refs/tags/$OPENCV_VER.tar.gz"
    "https://downloads.sourceforge.net/project/opencvlibrary/$OPENCV_VER/opencv-$OPENCV_VER.tar.gz"
    "https://codeload.github.com/opencv/opencv/tar.gz/refs/tags/$OPENCV_VER"
  )
  ok=0
  for u in "${URLS[@]}"; do
    echo "  尝试 $u"
    if wget -c -T 30 -t 3 -O opencv.tar.gz "$u"; then ok=1; break; fi
  done
  [ "$ok" = "1" ] || { echo "!! 下载失败（网络/白名单问题）"; exit 6; }
  tar xzf opencv.tar.gz || { echo "!! 解压失败"; exit 6; }
fi
cd "$SRC/opencv-$OPENCV_VER" || exit 5

# ---------- 3. cmake ----------
mkdir -p "$BUILD" && cd "$BUILD" || exit 7
echo "---- cmake configure ----"
cmake -D CMAKE_BUILD_TYPE=RELEASE \
      -D CMAKE_INSTALL_PREFIX=/usr/local \
      -D OPENCV_EXTRA_MODULES_PATH= \
      -D BUILD_LIST=core,imgproc,imgcodecs,videoio,dnn,objdetect,calib3d,features2d,flann,highgui,python3 \
      -D WITH_CUDA=OFF \
      -D WITH_GTK=OFF -D WITH_QT=OFF \
      -D WITH_FFMPEG=ON -D WITH_V4L=ON -D WITH_LIBV4L=ON \
      -D ENABLE_NEON=ON -D CPU_BASELINE=NEON \
      -D BUILD_opencv_python3=ON \
      -D PYTHON3_EXECUTABLE=/usr/bin/python3 \
      -D PYTHON3_INCLUDE_DIR=/usr/include/python3.6m \
      -D PYTHON3_NUMPY_INCLUDE_DIRS=/usr/lib/python3/dist-packages/numpy/core/include \
      -D BUILD_TESTS=OFF -D BUILD_PERF_TESTS=OFF -D BUILD_EXAMPLES=OFF \
      -D BUILD_DOCS=OFF -D BUILD_JAVA=OFF \
      -D OPENCV_GENERATE_PKGCONFIG=ON \
      .. || { echo "!! cmake 配置失败"; exit 7; }
grep -E 'To be built:' -A3 CMakeCache.txt 2>/dev/null | head -5

# ---------- 4. 编译 ----------
echo "---- make -j$JOBS_LOW（Nano 4 核，预计 40–60 分钟）---- $(date)"
make -j"$JOBS_LOW" || { echo "!! 编译失败（看上面第一条 error）"; exit 8; }
echo "---- 编译完成 $(date) ----"

# ---------- 5. 安装 + 验证 ----------
echo "---- make install ----"
sudo make install || { echo "!! 安装失败"; exit 9; }
sudo ldconfig

echo "---- 验证 ----"
python3 - <<'PYEOF'
import sys
try:
    import cv2
except Exception as e:
    print("!! import cv2 失败:", e); sys.exit(1)
print("cv2.__version__            =", cv2.__version__)
print("has cv2.dnn                =", hasattr(cv2, "dnn"))
print("has cv2.FaceDetectorYN     =", hasattr(cv2, "FaceDetectorYN"))
print("has cv2.FaceRecognizerSF   =", hasattr(cv2, "FaceRecognizerSF"))
try:
    print("cuda devices               =", cv2.cuda.getCudaEnabledDeviceCount())
except Exception:
    print("cuda devices               = (无 cuda 模块)")
PYEOF

echo "===== $(date)  全部完成 ====="
echo "提示：系统自带的 4.1.1 在 /usr/lib/aarch64-linux-gnu，新编的在 /usr/local —— python3 会优先加载 /usr/local（dist-packages）"
