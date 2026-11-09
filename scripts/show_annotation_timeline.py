"""打印标注草稿的身份时间线（不重新检测，只读 .json），用于人工核对子例边界。"""
import json
import os
import sys


def main(paths):
    for path in paths:
        with open(path, "r", encoding="utf-8") as handle:
            doc = json.load(handle)
        print("\n=== %s（%s）" % (doc["video"], doc["scenario"]))
        print("  " + doc.get("notes", "")[:200])
        line = []
        for row in doc["frames"]:
            identities = [face["identity"] for face in row["faces"]]
            line.append("%5.1fs:%s" % (row["t_ms"] / 1000.0,
                                       ",".join(identities) if identities else "-"))
        # 每行 8 段，便于读
        for start in range(0, len(line), 8):
            print("  " + "  ".join(line[start:start + 8]))


if __name__ == "__main__":
    main(sys.argv[1:])