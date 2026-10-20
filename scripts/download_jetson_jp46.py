#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""断点续传下载 NVIDIA Jetson Nano SD 卡镜像（走本地 HTTP 代理）。

背景：项目锁定的板子是 **Jetson Nano B01 (4GB) 开发者套件**，官方镜像为
JetPack 4.6 = L4T R32.6.1 的 SD 卡镜像。PC 上的 curl/schannel 在沙箱里
无法建立 TLS（SEC_E_NO_CREDENTIALS），所以用 Python 的 OpenSSL 走代理下载。

用法：
    python scripts/download_jetson_jp46.py                # 默认下载 JP46 到 firmware/
    python scripts/download_jetson_jp46.py --url <URL>    # 换镜像（如 JP461）
    python scripts/download_jetson_jp46.py --no-proxy     # 直连

特性：
  * 先 HEAD 取 Content-Length，校验与预期大小一致；
  * 支持 Range 断点续传，网络中断自动重试（指数退避），重跑即续传；
  * 每 20 秒打印进度/速度/ETA，全部写入日志文件；
  * 完成后计算 SHA256 并落盘 <zip>.sha256，同时用 zipfile 校验压缩包完整性。
"""

import argparse
import hashlib
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile

DEFAULTS = {
    # JetPack 4.6.1 (L4T R32.7.1) —— 最新 4.x，备选
    "jp461": "https://developer.nvidia.com/embedded/l4t/r32_release_v7.1/jp_4.6.1_b110_sd_card/jeston_nano/jetson-nano-jp461-sd-card-image.zip",
    # JetPack 4.6 (L4T R32.6.1) —— 官方技术员确认可用于 B01
    "jp46": "https://developer.nvidia.com/embedded/l4t/r32_release_v6.1/jeston_nano/jetson-nano-jp46-sd-card-image.zip",
}
# 2026-10-20 实测 Content-Length（字节）
EXPECTED_SIZE = {
    "jp46": 6533894369,
}
PROXY = "http://127.0.0.1:10090"
CHUNK = 4 * 1024 * 1024
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


def opener(proxy: str | None):
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def head(op, url: str, timeout=60, retries: int = 6):
    """取 Content-Length。代理偶发掐断 TLS（SSLEOFError），必须重试。"""
    last = None
    for i in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
            with op.open(req, timeout=timeout) as r:
                return int(r.headers.get("Content-Length") or 0), r.status
        except (urllib.error.URLError, OSError) as e:
            last = e
            wait = min(30, 3 * i)
            print(f"[head retry {i}/{retries}] {type(e).__name__}: {e} — {wait}s", flush=True)
            time.sleep(wait)
    raise SystemExit(f"[fatal] HEAD 失败: {last}")


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(8 * 1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def download(op, url: str, dst: str, total: int, max_retries: int = 50):
    part = dst + ".part"
    attempt = 0
    while True:
        done = os.path.getsize(part) if os.path.exists(part) else 0
        if total and done == total:
            break
        if total and done > total:
            print(f"[warn] {part} 比预期大，删除重下", flush=True)
            os.remove(part)
            done = 0
        headers = {"User-Agent": UA, "Accept-Encoding": "identity"}
        if done:
            headers["Range"] = f"bytes={done}-"
        try:
            req = urllib.request.Request(url, headers=headers)
            with op.open(req, timeout=120) as r:
                if done and r.status != 206:
                    print(f"[warn] 服务器不支持续传 (HTTP {r.status})，从头开始", flush=True)
                    done = 0
                    mode = "wb"
                else:
                    mode = "ab" if done else "wb"
                t0 = time.time()
                base = done
                last_log = 0.0
                with open(part, mode) as f:
                    while True:
                        b = r.read(CHUNK)
                        if not b:
                            break
                        f.write(b)
                        done += len(b)
                        now = time.time()
                        if now - last_log >= 20:
                            last_log = now
                            speed = (done - base) / max(now - t0, 1e-6) / 1048576
                            pct = (done / total * 100) if total else 0
                            eta = ((total - done) / (speed * 1048576) / 60) if speed > 0.01 and total else 0
                            print(
                                f"[{pct:5.1f}%] {done/1048576:8.1f}/{total/1048576:.1f} MiB "
                                f"{speed:5.2f} MiB/s ETA {eta:5.1f} min",
                                flush=True,
                            )
            if total and done < total:
                print(f"[warn] 连接提前结束 ({done}/{total})，续传", flush=True)
                raise IOError("short read")
            break
        except (urllib.error.URLError, IOError, TimeoutError, OSError) as e:
            attempt += 1
            if attempt > max_retries:
                raise SystemExit(f"[fatal] 重试 {max_retries} 次仍失败: {e}")
            wait = min(60, 5 * attempt)
            print(f"[retry {attempt}] {type(e).__name__}: {e} — {wait}s 后续传", flush=True)
            time.sleep(wait)
    if os.path.exists(dst):
        os.remove(dst)
    os.replace(part, dst)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default="jp46", choices=sorted(DEFAULTS))
    ap.add_argument("--url", default=None, help="覆盖下载地址")
    ap.add_argument("--out", default=None, help="输出文件路径")
    ap.add_argument("--proxy", default=PROXY)
    ap.add_argument("--no-proxy", action="store_true")
    args = ap.parse_args()

    url = args.url or DEFAULTS[args.key]
    name = url.rsplit("/", 1)[-1]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dst = args.out or os.path.join(root, "firmware", name)
    os.makedirs(os.path.dirname(dst), exist_ok=True)

    proxy = None if args.no_proxy else args.proxy
    op = opener(proxy)
    print(f"URL   : {url}", flush=True)
    print(f"PROXY : {proxy or '(direct)'}", flush=True)
    print(f"OUT   : {dst}", flush=True)

    total, status = head(op, url)
    print(f"HEAD  : HTTP {status}, {total} bytes ({total/1073741824:.2f} GiB)", flush=True)
    exp = EXPECTED_SIZE.get(args.key)
    if exp and total and exp != total:
        print(f"[warn] 大小与记录值 {exp} 不一致，可能已换版 — 继续按服务器值下载", flush=True)

    download(op, url, dst, total)
    print(f"[ok] 下载完成 {dst} ({os.path.getsize(dst)} bytes)", flush=True)

    digest = sha256_of(dst)
    with open(dst + ".sha256", "w", encoding="ascii") as f:
        f.write(f"{digest}  {name}\n")
    print(f"[sha256] {digest}", flush=True)

    print("[zip] 校验压缩包完整性 ...", flush=True)
    ok = zipfile.ZipFile(dst).testzip() is None
    print(f"[zip] {'OK — 压缩包无损' if ok else '损坏！'} ", flush=True)


if __name__ == "__main__":
    sys.exit(main())
