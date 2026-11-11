"""主人端（上位机）包：单端口 8766 的 HTTP API + WebSocket + 照片 + 待发队列。

对应文档：[主人端-设计与接口-2026-11-11](../docs/主人端-设计与接口-2026-11-11.md)

模块分工：
- `config.py`   配置加载（DEFAULTS → config/master.yaml → 环境变量）
- `store.py`    三张新表 + dialog_turn 迁移（只做 SQL）
- `auth.py`     设备 token 哈希、照片 URL 签名
- `outbox.py`   待发消息：稳定键、退避重试、三种观测点
- `snapshots.py`照片原子落盘与 msg_id 绑定
- `reception.py`接待会话：规则优先于模型，汇总只发一次
- `ws.py`       主人端连接注册与广播
- `api.py`      HTTP 路由与鉴权
- `server.py`   单端口监听、Upgrade 分流、线程池、优雅退出
- `push/`       推送适配器（lan 已定；relay/vendor 占位）
"""