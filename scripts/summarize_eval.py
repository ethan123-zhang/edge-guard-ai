"""把 scenario_eval 的 JSON 汇总成一张表（给报告/文档用）。"""
import glob
import json
import os
import sys


def main(pattern):
    rows = []
    for path in sorted(glob.glob(pattern)):
        with open(path, "r", encoding="utf-8") as handle:
            doc = json.load(handle)
        cos = doc.get("cosine") or {}
        rows.append({
            "video": doc.get("video"),
            "scenario": doc.get("scenario"),
            "attempts": doc.get("report_attempts"),
            "events": doc.get("events"),
            "suppressed": doc.get("suppressed"),
            "by_id": doc.get("events_by_identity"),
            "sessions": doc.get("sessions"),
            "matrix": (cos.get("matrix") or {}),
            "same": (cos.get("same_person_own") or {}),
            "diff": (cos.get("different_person_own") or {}),
            "passed": doc.get("passed"),
            "problems": doc.get("problems") or [],
        })
    print("%-30s %-11s %3s %3s %3s %-22s %3s %-24s %s"
          % ("video", "scenario", "att", "evt", "sup", "by_identity", "ses", "matrix", "verdict"))
    for row in rows:
        print("%-30s %-11s %3s %3s %3s %-22s %3s %-24s %s"
              % (row["video"], row["scenario"], row["attempts"], row["events"],
                 row["suppressed"], row["by_id"], row["sessions"],
                 "R{s}/W{d}/r{s2}/w{d2}".format(
                     s=row["matrix"].get("reuse_same", 0), d=row["matrix"].get("reuse_diff", 0),
                     s2=row["matrix"].get("reject_same", 0),
                     d2=row["matrix"].get("reject_diff", 0)),
                 "PASS" if row["passed"] else "FAIL"))
        if row["problems"]:
            for text in row["problems"]:
                print("    ! %s" % text)
        if row["same"]:
            print("    same-person own: %s" % row["same"])
        if row["diff"]:
            print("    diff-person own: %s" % row["diff"])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "logs/eval_*_q007.json")