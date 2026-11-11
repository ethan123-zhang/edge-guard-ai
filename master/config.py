"""主人端配置（铁律④：阈值/端口/密钥外置，不进代码）。

和 `vision/config.py` 同一套路：`DEFAULTS` → 叠加 `config/master.yaml` →
可选叠加 `config/master.local.yaml`（gitignore）→ `validate()` 返回普通 dict。

密钥只从环境变量或本地覆盖文件读：
- `SENTINEL_VISION_KEY`：Jetson 上传照片时的 `X-Vision-Key`
- `SENTINEL_TOKEN_SALT` / `SENTINEL_PHOTO_SECRET`：设备 token 哈希盐、照片 URL 签名密钥
配置文件里给的是**占位空串**，空串时对应功能拒绝服务而不是放行。
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import yaml
except ImportError:  # pragma: no cover - 板端 apt 装 python3-yaml 即可
    yaml = None

MASTER_DIR = Path(__file__).resolve().parent
PROJECT_DIR = MASTER_DIR.parent
CONFIG_DIR = PROJECT_DIR / "config"


class ConfigError(ValueError):
    """配置缺失或取值非法。"""


DEFAULTS: Dict[str, Any] = {
    "profile": "pc",
    "server": {
        "host": "0.0.0.0",
        "port": 8766,
        # 视觉节点入站（docs/02 §1：视觉节点 → 对话节点 8765），协议保持原样
        "vision_port": 8765,
        "backlog": 32,
        "thread_pool": 16,
        "shutdown_grace_s": 3,
    },
    "paths": {
        # 相对仓库根；与 dialog 侧共用一个库，旧库直接升级
        "db": "data/sentinel.db",
        "snapshots": "data/snapshots",
        "web": "master/web",
    },
    "reception": {
        # 访客沉默多久算离场（毫秒）——只影响收口与统计，不写 departed_at
        "silence_timeout_ms": 45000,
        # 一次接待最长多久（毫秒），防键盘入口忘记收尾
        "session_timeout_ms": 300000,
        # 明确收尾的固定说法（收口动作之一，见设计 §4.5）
        "farewell_phrases": [
            "我走了", "我先走了", "那我走了", "拜拜", "再见",
            "没事了", "不用了", "就这样吧", "好的谢谢", "好谢谢",
            "谢谢，麻烦了", "辛苦了", "好的，我放门口了",
        ],
    },
    "photo": {
        "max_bytes": 2097152,
        "sign_ttl_s": 600,
    },
    "outbox": {
        "retry_base_ms": 1000,
        "retry_max_ms": 60000,
        "batch": 20,
        "poll_interval_ms": 500,
    },
    "push": {
        # 已定：局域网直连（WebSocket 广播）。relay/vendor 等通道定了再插适配器
        "adapter": "lan",
    },
    "security": {
        # 空串 = 未配置 → 对应入口返回 503，不静默放行
        "vision_key": "",
        "token_salt": "",
        "photo_secret": "",
        # 允许登记的设备名长度上限（防把日志写爆）
        "max_device_name": 64,
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _read_yaml(path: Path) -> Dict[str, Any]:
    if yaml is None:
        raise ConfigError(
            "缺少 PyYAML：Ubuntu 上 `sudo apt-get install -y python3-yaml`，"
            "Windows 上 `python -m pip install pyyaml`"
        )
    with open(str(path), "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError("%s 顶层必须是 mapping（key: value）" % path)
    return data


def load(path: Optional[Any] = None, config_dir: Optional[Any] = None) -> Dict[str, Any]:
    """读配置。`path` 给了就读单个文件，否则按 `config_dir` 下 base+local 叠加。"""
    if path is not None:
        cfg = _deep_merge(DEFAULTS, _read_yaml(Path(path)))
    else:
        cdir = Path(config_dir) if config_dir else CONFIG_DIR
        base_path = cdir / "master.yaml"
        if not base_path.exists():
            raise ConfigError("找不到配置文件：%s" % base_path)
        cfg = _deep_merge(DEFAULTS, _read_yaml(base_path))
        local_path = cdir / "master.local.yaml"
        if local_path.exists():
            cfg = _deep_merge(cfg, _read_yaml(local_path))

    # 密钥：环境变量优先（部署时用 systemd Environment= / 命令行 export）
    for key, env in (("vision_key", "SENTINEL_VISION_KEY"),
                     ("token_salt", "SENTINEL_TOKEN_SALT"),
                     ("photo_secret", "SENTINEL_PHOTO_SECRET")):
        env_value = os.environ.get(env)
        if env_value:
            cfg["security"][key] = env_value

    validate(cfg)
    return cfg


def _need(cond: bool, message: str) -> None:
    if not cond:
        raise ConfigError(message)


def validate(cfg: Dict[str, Any]) -> None:
    server = cfg.get("server") or {}
    _need(1 <= int(server.get("port", 0)) <= 65535,
          "server.port 必须在 1..65535，收到 %r" % (server.get("port"),))
    _need(1 <= int(server.get("vision_port", 0)) <= 65535,
          "server.vision_port 必须在 1..65535，收到 %r" % (server.get("vision_port"),))
    _need(int(server.get("port")) != int(server.get("vision_port")),
          "server.port 与 server.vision_port 不能相同")
    _need(int(server.get("thread_pool", 0)) >= 1, "server.thread_pool 必须 >= 1")

    paths = cfg.get("paths") or {}
    for key in ("db", "snapshots", "web"):
        _need(str(paths.get(key) or "").strip() != "",
              "paths.%s 不能为空（相对仓库根或绝对路径）" % key)

    reception = cfg.get("reception") or {}
    _need(int(reception.get("silence_timeout_ms", 0)) > 0,
          "reception.silence_timeout_ms 必须为正整数")
    _need(int(reception.get("session_timeout_ms", 0)) > 0,
          "reception.session_timeout_ms 必须为正整数")
    _need(int(reception["session_timeout_ms"]) >= int(reception["silence_timeout_ms"]),
          "reception.session_timeout_ms 不能小于 silence_timeout_ms")

    photo = cfg.get("photo") or {}
    _need(int(photo.get("max_bytes", 0)) > 0, "photo.max_bytes 必须为正整数")
    _need(int(photo.get("sign_ttl_s", 0)) > 0, "photo.sign_ttl_s 必须为正整数")

    outbox = cfg.get("outbox") or {}
    _need(int(outbox.get("retry_base_ms", 0)) > 0, "outbox.retry_base_ms 必须为正整数")
    _need(int(outbox.get("retry_max_ms", 0)) >= int(outbox["retry_base_ms"]),
          "outbox.retry_max_ms 不能小于 retry_base_ms")
    _need(int(outbox.get("batch", 0)) >= 1, "outbox.batch 必须 >= 1")

    push = cfg.get("push") or {}
    _need(str(push.get("adapter") or "").strip() != "", "push.adapter 不能为空")


def resolve_project(rel_path: Any) -> Path:
    """把配置里的相对路径按仓库根解析成绝对路径。"""
    path = Path(str(rel_path))
    if path.is_absolute():
        return path
    return (PROJECT_DIR / path).resolve()


def db_path(cfg: Dict[str, Any]) -> Path:
    return resolve_project(cfg["paths"]["db"])


def snapshots_dir(cfg: Dict[str, Any]) -> Path:
    return resolve_project(cfg["paths"]["snapshots"])


def web_dir(cfg: Dict[str, Any]) -> Path:
    return resolve_project(cfg["paths"]["web"])