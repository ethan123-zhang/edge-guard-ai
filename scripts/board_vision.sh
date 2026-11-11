#!/bin/bash
# Jetson 视觉节点：常驻启停（鲁班猫主人端那边是 scripts/board_master.sh，同一套路）
#
# 用法（板上，仓库已同步到 /home/jetson/sentinel-vision）：
#   bash scripts/board_vision.sh start      # 默认开摄像头 /dev/video0
#   bash scripts/board_vision.sh start /home/jetson/sentinel-quality-20261106/xxx.mp4   # 用视频当源
#   bash scripts/board_vision.sh stop
#   bash scripts/board_vision.sh status
#
# 密钥不进仓库：写 $APP_DIR/.env（chmod 600）里一行 SENTINEL_VISION_KEY=...，
# 必须与鲁班猫 ~/sentinel-master/config/master.local.yaml 的 security.vision_key 一致。
#
# 两个板端环境事实（2026-11-07/11-11 实测，写在最显眼处）：
#   1. 板上 OpenCV/numpy 必须 OPENBLAS_CORETYPE=ARMV8，否则 import cv2 直接 Illegal instruction
#   2. 摄像头必须显式 backend=v4l2 + MJPG：这个 L4T 的 CAP_ANY 会先试 GStreamer，
#      不认 FOURCC，1280x720 会掉到 9 fps（这些都写在 vision/config/board.yaml 里了）
set -u

APP_DIR="${VISION_DIR:-/home/jetson/sentinel-vision}"
PROFILE="${PROFILE:-board}"
SOURCE="${2:-${SOURCE:-0}}"

cd "$APP_DIR" || { echo "[vision] 找不到 $APP_DIR"; exit 1; }
mkdir -p logs

# 密钥：优先环境变量，其次 .env（不导入 git）
if [ -z "${SENTINEL_VISION_KEY:-}" ] && [ -f .env ]; then
    # shellcheck disable=SC1091
    . ./.env
fi
if [ -z "${SENTINEL_VISION_KEY:-}" ]; then
    echo "[vision] 缺少 SENTINEL_VISION_KEY：写 $APP_DIR/.env 或 export 后再启"
    exit 2
fi
# 关键：从 .env 读到的变量只是当前 shell 的局部变量，不 export 子进程（python）拿不到
export SENTINEL_VISION_KEY

export OPENBLAS_CORETYPE=ARMV8 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1

pids() {
    # 方括号写法避免 pgrep 匹配到执行脚本自己的命令行
    pgrep -f 'vision_sim[.]py --profile' || true
}

case "${1:-status}" in
    start)
        if [ -n "$(pids)" ]; then
            echo "[vision] 已在运行：$(pids)"
            exit 0
        fi
        echo "[vision] 启动：profile=$PROFILE source=$SOURCE → logs/vision.log"
        # -u 必须加：重定向到文件时 stdout 是块缓冲，不加的话 tail -f 看不到任何日志，
        # 现场排障会以为"进程没起来"（2026-11-11 实测）。
        setsid nohup python3 -u scripts/vision_sim.py --profile "$PROFILE" \
            --no-window --source "$SOURCE" >> logs/vision.log 2>&1 < /dev/null &
        sleep 3
        echo "[vision] pid=$(pids)"
        tail -3 logs/vision.log
        ;;
    stop)
        for pid in $(pids); do
            kill "$pid" 2>/dev/null && echo "[vision] 已停 pid=$pid"
        done
        ;;
    status)
        if [ -n "$(pids)" ]; then
            echo "[vision] 运行中 pid=$(pids)"
        else
            echo "[vision] 未运行"
        fi
        tail -5 logs/vision.log 2>/dev/null
        ;;
    *)
        echo "用法：bash scripts/board_vision.sh {start [source]|stop|status}"
        exit 1
        ;;
esac