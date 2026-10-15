#!/bin/bash
# 在 WSL 里配置宿主机代理
# 用法：wsl -d Ubuntu-22.04 -- bash /mnt/e/哨兵管家/scripts/wsl_setup_proxy.sh
#
# 背景：WSL2 是独立虚拟机，里面的 127.0.0.1 指 WSL 自己，不是 Windows。
#       必须用宿主机在 WSL 网络中的网关地址（默认路由）才能访问宿主机的代理。

set -u

PROXY_HOST="172.28.240.1"
PROXY_PORT="10090"
PROXY="http://${PROXY_HOST}:${PROXY_PORT}"

echo "=== 1. 测试代理连通性 ==="
if ! curl -s -o /dev/null --max-time 20 -x "$PROXY" https://github.com; then
    echo "✗ 代理 $PROXY 不通，终止"
    exit 1
fi
echo "✓ 代理 $PROXY 可用"

echo "=== 2. 配置 git 代理 ==="
git config --global http.proxy "$PROXY"
git config --global https.proxy "$PROXY"
git config --global http.sslVerify true
echo "✓ git http.proxy = $(git config --global --get http.proxy)"

echo "=== 3. 配置 pip ==="
mkdir -p "$HOME/.pip"
cat > "$HOME/.pip/pip.conf" <<PIPCONF
[global]
proxy = ${PROXY}
index-url = https://pypi.tuna.tsinghua.edu.cn/simple
trusted-host = pypi.tuna.tsinghua.edu.cn
timeout = 120
PIPCONF
echo "✓ 已写 $HOME/.pip/pip.conf"

echo "=== 4. 配置 shell 环境变量 ==="
MARKER="# --- 宿主机代理 (sentinel) ---"
if ! grep -qF "$MARKER" "$HOME/.bashrc" 2>/dev/null; then
    cat >> "$HOME/.bashrc" <<BASHRC

$MARKER
export http_proxy="$PROXY"
export https_proxy="$PROXY"
export all_proxy="$PROXY"
export no_proxy="localhost,127.0.0.1,::1"
BASHRC
    echo "✓ 已追加到 ~/.bashrc"
else
    echo "· ~/.bashrc 已存在该配置，跳过"
fi

echo
echo "=== 结果核对 ==="
echo "git  proxy: $(git config --global --get http.proxy)"
echo "pip  proxy: $(grep -m1 proxy "$HOME/.pip/pip.conf" 2>/dev/null)"
echo "bash proxy: $(grep -c "$PROXY_HOST" "$HOME/.bashrc" 2>/dev/null) 行"
