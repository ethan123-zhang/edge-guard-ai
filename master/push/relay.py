"""公网推送适配器：企业微信群机器人 webhook（可选通用 HTTP 模板）。

为什么是这个通道（2026-11-11 用户拍板）：免费、不用应用审核、国内网络稳，
手机装企业微信就能弹通知；代价是 20 条/分钟限频、消息进群、只能带文字摘要。

设计约束：
- 只发**摘要文字**（时间/身份/来意/越权原话），原图仍走局域网看板；
  要发图得先 `upload_media` 换 media_id，见 `_send_image` 预留。
- 消息内容由 payload（协议 §4.1/§4.2）决定，这里**不改协议字段**，只做呈现。
- 返回 `(ok, error)`：HTTP 200 且 `errcode == 0` 才算"平台受理"；
  `45009`（限频）当可重试失败，交给 outbox 退避。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from master.push import PushAdapter

WECOM_DEFAULT_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"


def _fmt_time(ms) -> str:
    if not ms:
        return "—"
    return time.strftime("%m-%d %H:%M:%S", time.localtime(int(ms) / 1000))


def report_text(payload):
    """把 `visit_report` 压成一条手机上一眼能读的摘要。"""
    person = payload.get("person") or {}
    dialog = payload.get("dialog") or {}
    timing = payload.get("timing") or {}
    security = payload.get("security") or {}
    who = person.get("name") or "陌生访客"
    lines = [
        "【到访】%s" % _fmt_time(timing.get("arrived_at") or payload.get("ts")),
        "身份：%s%s" % (who, "（第 %s 次）" % person.get("visit_count") if person.get("visit_count") else ""),
        "来意：%s" % (dialog.get("purpose") or "未说明"),
    ]
    if dialog.get("caller_name"):
        lines.append("自称：%s" % dialog["caller_name"])
    if dialog.get("summary"):
        lines.append("摘要：%s" % dialog["summary"])
    if security.get("privilege_escalation"):
        lines.append("⚠ 本次含越权请求：%s" % "、".join(security.get("escalation_phrases") or []))
    return "\n".join(lines)


def alert_text(payload):
    """把 `security_alert` 压成一条告警（越权原话必须保留）。"""
    lines = [
        "【越权告警】%s" % _fmt_time(payload.get("ts")),
        "原话：%s" % (payload.get("phrase") or ""),
        "回复：%s" % (payload.get("reply_given") or ""),
        "到访：%s" % (payload.get("visit_id") or ""),
    ]
    return "\n".join(lines)


class RelayAdapter(PushAdapter):
    name = "relay"

    def __init__(self, url="", provider="wecom", timeout_s=5.0, log=None):
        self.url = str(url or "").strip()
        self.provider = str(provider or "wecom").strip().lower()
        self.timeout_s = float(timeout_s)
        self._log = log or (lambda message: None)

    def build_body(self, payload):
        """按 provider 组装请求体。目前只实现企业微信文本消息。"""
        msg_type = payload.get("type")
        text = report_text(payload) if msg_type == "visit_report" else alert_text(payload)
        if self.provider == "wecom":
            return {"msgtype": "text", "text": {"content": text}}
        # 通用模板：`{text}` 直接放 JSON，方便以后换飞书/钉钉/PushPlus 时只改配置
        return {"text": text}

    def send(self, message_key, payload):
        if not self.url:
            return False, "公网推送未配置（push.relay.url 为空）"
        body = json.dumps(self.build_body(payload), ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.url, data=body, method="POST")
        request.add_header("Content-Type", "application/json; charset=utf-8")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            return False, "HTTP %s" % exc.code
        except (urllib.error.URLError, OSError) as exc:
            return False, "网络错误：%s" % exc

        if status != 200:
            return False, "HTTP %s" % status
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            return False, "响应不是 JSON"
        errcode = data.get("errcode")
        if errcode in (0, None):
            return True, None
        if errcode == 45009:
            return False, "企业微信限频（20 条/分钟）"
        return False, "企业微信错误 %s：%s" % (errcode, data.get("errmsg"))

    def _send_image(self, payload):
        """预留：企业微信发图要先 `upload_media` 换 media_id，再发 image 消息。

        当前先只发文字摘要：原图只在内网看板取，公网通道不传访客照片
        （也顺带降低隐私面）。真要做时在这里实现：
        POST https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key=...&type=file
        → {"media_id": ...} → {"msgtype":"image","image":{"base64":...,"md5":...}}
        """
        raise NotImplementedError