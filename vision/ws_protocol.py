"""最小 RFC6455 实现（纯标准库）。

**为什么不用 `websockets` 库**：板端不能保证能 pip install（断网/没编译环境，
见 memory/04 的坑），而协议本身只需要 ~100 行。自己实现的好处是
「PC 上跑通 = 板子上一定跑得通」，代价是不支持扩展（压缩、多子协议）——
本项目不需要。

只实现文本帧收发 + 握手 + ping/pong/close 的处理，够用就行。
"""
import base64
import hashlib
import os
import struct

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WsError(RuntimeError):
    """握手/帧格式错误或连接断开。"""


def accept_key(client_key):
    """按 RFC6455 §4.2.2 算 Sec-WebSocket-Accept。"""
    digest = hashlib.sha1((client_key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def encode_frame(payload, opcode=OP_TEXT, mask=False):
    """打一个完整帧。客户端必须 mask=True，服务端必须 False（服务端 mask 会被客户端断开）。"""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    header = bytearray()
    header.append(0x80 | (opcode & 0x0F))     # FIN=1
    length = len(payload)
    mask_bit = 0x80 if mask else 0x00
    if length < 126:
        header.append(mask_bit | length)
    elif length < 65536:
        header.append(mask_bit | 126)
        header += struct.pack("!H", length)
    else:
        header.append(mask_bit | 127)
        header += struct.pack("!Q", length)

    if not mask:
        return bytes(header) + payload

    key = os.urandom(4)
    header += key
    masked = bytes(byte ^ key[i % 4] for i, byte in enumerate(payload))
    return bytes(header) + masked


def recv_exact(sock, size):
    buf = b""
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise WsError("连接被对端关闭")
        buf += chunk
    return buf


def recv_frame(sock):
    """读一个帧，返回 `(opcode, payload_bytes)`。分片帧不合并（本项目消息都很小）。"""
    first, second = recv_exact(sock, 2)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", recv_exact(sock, 8))[0]
    mask_key = recv_exact(sock, 4) if masked else None
    payload = recv_exact(sock, length) if length else b""
    if mask_key:
        payload = bytes(byte ^ mask_key[i % 4] for i, byte in enumerate(payload))
    return opcode, payload


def recv_text(sock):
    """读下一条文本消息，自动处理 ping/close，返回 str（对端关闭时返回 None）。"""
    while True:
        opcode, payload = recv_frame(sock)
        if opcode == OP_TEXT:
            return payload.decode("utf-8", "replace")
        if opcode == OP_CLOSE:
            return None
        if opcode == OP_PING:
            sock.sendall(encode_frame(payload, OP_PONG))
        # pong / 其它控制帧：忽略


# ---------------------------------------------------------------- 握手

def client_handshake_request(host, port, path="/", extra_headers=None):
    """返回 `(请求字节, Sec-WebSocket-Key)`。"""
    client_key = base64.b64encode(os.urandom(16)).decode("ascii")
    lines = [
        "GET %s HTTP/1.1" % (path or "/"),
        "Host: %s:%d" % (host, int(port)),
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Key: %s" % client_key,
        "Sec-WebSocket-Version: 13",
    ]
    for key, value in (extra_headers or {}).items():
        lines.append("%s: %s" % (key, value))
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"), client_key


def read_http_head(sock):
    """读到 `\\r\\n\\r\\n` 为止，返回 `(起始行, headers dict)`。"""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise WsError("握手阶段连接被对端关闭")
        data += chunk
        if len(data) > 65536:
            raise WsError("握手请求头异常大（>64KB）")
    head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    lines = head.split("\r\n")
    start_line = lines[0] if lines else ""
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    return start_line, headers


def verify_server_response(start_line, headers, client_key):
    if "101" not in start_line:
        raise WsError("握手被拒：%s" % start_line)
    if headers.get("sec-websocket-accept") != accept_key(client_key):
        raise WsError("Sec-WebSocket-Accept 不匹配（中间有代理篡改？）")


def server_handshake(sock):
    """服务端握手，返回 `(请求起始行, headers)`；失败抛 WsError。"""
    start_line, headers = read_http_head(sock)
    if headers.get("upgrade", "").lower() != "websocket":
        raise WsError("不是 WebSocket 升级请求：%s" % start_line)
    client_key = headers.get("sec-websocket-key")
    if not client_key:
        raise WsError("缺少 Sec-WebSocket-Key")
    response = [
        "HTTP/1.1 101 Switching Protocols",
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Accept: %s" % accept_key(client_key),
        "",
        "",
    ]
    sock.sendall(("\r\n".join(response)).encode("latin-1"))
    return start_line, headers
