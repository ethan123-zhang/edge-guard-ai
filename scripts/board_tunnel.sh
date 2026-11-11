#!/bin/bash
# 鲁班猫公网入口：Cloudflare 快速隧道，把主人端 8766 暴露成公网 https 网址。
#
# 用法（板上，仓库已同步到 /home/cat/sentinel-master）：
#   bash scripts/board_tunnel.sh start    # 手动起（后台）
#   bash scripts/board_tunnel.sh stop
#   bash scripts/board_tunnel.sh status
#   bash scripts/board_tunnel.sh url      # 打印当前公网地址
#   bash scripts/board_tunnel.sh run      # 前台监督循环（systemd 用，别手动跑）
#
# 事实与边界：
# - 出站连接：板子没有公网 IP、路由器不开端口也能用；不改校园网任何设置。
# - 快速隧道地址每次重启都会变（https://xxx.trycloudflare.com）：新地址写进
#   data/public_url.txt，并在配了企业微信 webhook 时往群里推一条，免得地址变了没人知道。
# - 固定网址要 Cloudflare 账号 + 一个域名，改命名隧道；步骤见 docs/04-部署与运维.md。
set -u

APP_DIR="${MASTER_DIR:-/home/cat/sentinel-master}"
BIN="${CLOUDFLARED_BIN:-/usr/local/bin/cloudflared}"
PORT="${MASTER_PORT:-8766}"
LOG="$APP_DIR/logs/tunnel.log"
URL_FILE="$APP_DIR/data/public_url.txt"
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

pids() { pgrep -f '[c]loudflared tunnel --url' || true; }

wait_url() {
    local i url
    for i in $(seq 1 40); do
        url=$(grep -o 'https://[a-z0-9-]*[.]trycloudflare[.]com' "$LOG" 2>/dev/null | head -1)
        if [ -n "$url" ]; then
            echo "$url"
            return 0
        fi
        sleep 1
    done
    return 1
}

notify() {
    local url="$1"
    python3 - "$APP_DIR/config/master.local.yaml" "$url" <<'PY' 2>/dev/null || true
import json, sys, urllib.request

try:
    import yaml
    with open(sys.argv[1], encoding="utf-8") as handle:
        cfg = yaml.safe_load(handle) or {}
except Exception:
    cfg = {}
webhook = ((cfg.get("push") or {}).get("relay") or {}).get("url") or ""
if not webhook:
    print("[tunnel] 没有配置企业微信 webhook，跳过地址通知")
    raise SystemExit(0)
text = ("哨兵管家公网地址已更新：\n%s\n"
        "（隧道重启后旧地址失效；手机「设置」里的服务器地址改成这个。）" % sys.argv[2])
payload = json.dumps({"msgtype": "text", "text": {"content": text}},
                     ensure_ascii=False).encode("utf-8")
request = urllib.request.Request(webhook, data=payload,
                                 headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(request, timeout=8) as response:
        print("[tunnel] 企业微信通知：%s" % response.read().decode("utf-8", "replace"))
except Exception as exc:
    print("[tunnel] 企业微信通知失败：%s" % exc)
PY
}

publish() {
    mkdir -p "$APP_DIR/data"
    printf '%s\n' "$1" > "$URL_FILE"
    echo "[tunnel] 公网地址：$1"
    echo "[tunnel] 已写入 $URL_FILE"
    notify "$1"
}

run_forever() {
    mkdir -p "$APP_DIR/logs" "$APP_DIR/data"
    if [ ! -x "$BIN" ]; then
        echo "[tunnel] 找不到 $BIN：先装上 cloudflared（见 docs/04-部署与运维.md）"
        exit 2
    fi
    while :; do
        : > "$LOG"
        "$BIN" tunnel --url "http://127.0.0.1:$PORT" --no-autoupdate >> "$LOG" 2>&1 &
        local pid=$!
        local url
        if url=$(wait_url); then
            publish "$url"
        else
            echo "[tunnel] 40 秒没拿到地址，看 $LOG"
        fi
        # cloudflared 退出（网络抖动 / 被杀）就重来，重来后会发新地址
        wait "$pid"
        echo "[tunnel] cloudflared 已退出，3 秒后重连"
        sleep 3
    done
}

case "${1:-status}" in
start)
    if pids | grep -q .; then
        echo "[tunnel] 已在跑：$(cat "$URL_FILE" 2>/dev/null || echo 地址未知)"
        exit 0
    fi
    mkdir -p "$APP_DIR/logs"
    setsid nohup bash "$SELF" run >> "$APP_DIR/logs/tunnel.out" 2>&1 < /dev/null &
    echo "[tunnel] 已启动，pid=$!（地址约 10 秒后出现在 $URL_FILE）"
    ;;
stop)
    for p in $(pgrep -f '[b]oard_tunnel.sh run'); do kill "$p" 2>/dev/null; done
    for p in $(pids); do kill "$p" 2>/dev/null; done
    sleep 1
    echo "[tunnel] 已停止"
    ;;
run)
    run_forever
    ;;
url)
    cat "$URL_FILE" 2>/dev/null || { echo "[tunnel] 还没有地址：先 start"; exit 1; }
    ;;
status)
    if pids | grep -q .; then
        echo "[tunnel] 运行中：$(pids | tr '\n' ' ')"
    else
        echo "[tunnel] 没在跑"
    fi
    [ -f "$URL_FILE" ] && echo "[tunnel] 地址：$(cat "$URL_FILE")"
    ;;
*)
    echo "用法：bash scripts/board_tunnel.sh start|stop|status|url" >&2
    exit 2
    ;;
esac