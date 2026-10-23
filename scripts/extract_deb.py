#!/usr/bin/env python3
"""从 .deb（ar 归档）里只抽出指定路径前缀的文件。

用法：
    python scripts/extract_deb.py <deb 路径> <dest 目录> <成员路径前缀>

例：
    python scripts/extract_deb.py firmware/bsp3273/Linux_for_Tegra/kernel/nvidia-l4t-kernel_*.deb \
        firmware/modules-4.9.299 ./lib/modules/4.9.299-tegra

为什么自己写：Windows 上没有 `ar`/`dpkg-deb`，而 .deb 只是 ar + 压缩 tar。
ar 层用标准库拆；载荷层视压缩格式分两条路：
  * gz / bz2 / xz —— 标准库 tarfile 直接读进内存；
  * zstd（R32.7.x 起 NVIDIA 改用 data.tar.zst）—— 标准库不支持，落盘后交给
    系统自带 bsdtar（Windows 10+ 的 tar.exe，libarchive 编译了 zstd）。
"""
import io
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

AR_MAGIC = b"!<arch>\n"


def iter_members(fh):
    """逐个返回 (名字, 内容字节) —— ar 归档格式，60 字节定长头。"""
    assert fh.read(8) == AR_MAGIC, "不是 ar 归档，确认传入的是 .deb"
    while True:
        hdr = fh.read(60)
        if len(hdr) < 60:
            return
        name = hdr[0:16].decode("ascii", "replace").strip().rstrip("/")
        size = int(hdr[48:58].decode("ascii").strip())
        data = fh.read(size)
        if size % 2:  # ar 成员按偶数字节对齐
            fh.read(1)
        yield name, data


def main():
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    deb, dest, prefix = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3].strip("./").rstrip("/")

    with open(deb, "rb") as fh:
        members = dict(iter_members(fh))

    payload_name = next((n for n in members if n.startswith("data.tar")), None)
    if payload_name is None:
        sys.exit(f"没找到 data.tar.*，实际成员：{list(members)}")

    payload = members[payload_name]
    print(f"[+] {deb.name} -> {payload_name} ({len(payload) / 1e6:.1f} MB)")

    dest.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    if payload_name.endswith(".zst"):
        # 标准库不认 zstd，落盘交给 bsdtar
        with tempfile.NamedTemporaryFile(suffix=".tar.zst", delete=False) as tmp:
            tmp.write(payload)
            tmp_path = tmp.name
        try:
            cmd = ["tar", "-xf", tmp_path, "-C", str(dest), "./" + prefix]
            print(f"[+] 交给 bsdtar：{' '.join(cmd[:2])} …")
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                sys.exit(f"[!] bsdtar 失败（{r.returncode}）：{r.stderr.strip()}")
        finally:
            Path(tmp_path).unlink(missing_ok=True)
    else:
        with tarfile.open(fileobj=io.BytesIO(payload)) as tf:
            wanted = [
                m for m in tf
                if (n := m.name.lstrip("./")) == prefix or n.startswith(prefix + "/")
            ]
            if not wanted:
                sys.exit(f"[!] 载荷里没有 {prefix}")
            tf.extractall(dest, members=[m for m in wanted if m.isfile() or m.isdir()])

    out = dest / prefix
    files = [p for p in out.rglob("*") if p.is_file()] if out.exists() else []
    total = sum(p.stat().st_size for p in files)
    print(f"[+] {len(files)} 个文件，{total / 1e6:.1f} MB -> {out}  用时 {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
