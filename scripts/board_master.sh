#!/bin/bash
# 鲁班猫上启动/重启主人端（部署目录 ~/sentinel-master）。
# 用法：bash scripts/board_master.sh
# 说明：用 pgrep 的模式写成 'master[.]server'，避免把执行这条命令的 shell 自己匹配掉。
cd ~/sentinel-master || exit 1
for p in $(pgrep -f 'master[.]server'); do kill "$p" 2>/dev/null; done
sleep 1
setsid nohup python3 -m master.server > master.log 2>&1 < /dev/null &
echo "master started, pid=$!"
sleep 2
pgrep -af 'master[.]server' | head -2
ss -ltn | grep -E ':(8765|8766)' || echo "警告：端口没监听"