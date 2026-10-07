#!/usr/bin/env python3
"""微信洞察 · 构建脚本：把 cards/*.json 合并校验成 data.js，供 index.html 离线读取。

用法：python3 build.py（数据目录见 wxi_paths.py）
"""

import glob
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wxi_paths as P  # noqa: E402

ROOT = P.DATA
# 分类和线索是个人口径，放在 config.json（categories / threads），没写就用 wxi_paths 里的通用默认值
CATEGORIES = P.categories()

LEVELS = {
    "major": {"name": "重大", "rank": 0},
    "focus": {"name": "需关注", "rank": 1},
    "info":  {"name": "参考", "rank": 2},
}

KINDS = {"todo": "待办", "risk": "风险", "decision": "决定", "event": "事件", "info": "信息"}

THREADS = P.threads()

STATUSES = {"open", "watch", "done", "info"}


def main():
    periods, cards, errors = [], [], []
    seen = set()
    for path in sorted(glob.glob(os.path.join(ROOT, "cards", "*.json"))):
        name = os.path.basename(path)
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except json.JSONDecodeError as e:
            errors.append(f"{name}: JSON 格式错误 {e}")
            continue
        p = doc["period"]
        p["summary"] = doc.get("summary", "")
        p["file"] = name
        periods.append(p)
        for c in doc.get("cards", []):
            cid = c.get("id")
            where = f"{name}:{cid}"
            if not cid or cid in seen:
                errors.append(f"{where}: id 缺失或重复")
            seen.add(cid)
            for key in ("date", "level", "category", "title", "summary"):
                if not c.get(key):
                    errors.append(f"{where}: 缺字段 {key}")
            if c.get("category") not in CATEGORIES:
                errors.append(f"{where}: 未知分类 {c.get('category')}")
            if c.get("level") not in LEVELS:
                errors.append(f"{where}: 未知级别 {c.get('level')}")
            if c.get("status", "info") not in STATUSES:
                errors.append(f"{where}: 未知状态 {c.get('status')}")
            if c.get("thread") and c["thread"] not in THREADS:
                errors.append(f"{where}: 未知线索 {c['thread']}（先在 config.json 的 threads 里登记）")
            c["period"] = p["id"]
            c["period_type"] = p["type"]
            c.setdefault("status", "info")
            c.setdefault("evidence", [])
            c.setdefault("people", [])
            c.setdefault("chats", [])
            c.setdefault("tags", [])
            cards.append(c)

    if errors:
        print("构建失败：\n  " + "\n  ".join(errors), file=sys.stderr)
        sys.exit(1)

    latest = max((p.get("extracted_until", "") for p in periods), default="")
    try:
        with open(os.path.join(ROOT, "extract_state.json"), encoding="utf-8") as f:
            latest = max(latest, json.load(f).get("extracted_until", ""))
    except (OSError, json.JSONDecodeError):
        pass
    data = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "extracted_until": latest,
        "root": ROOT,
        "categories": CATEGORIES,
        "levels": LEVELS,
        "kinds": KINDS,
        "threads": THREADS,
        "periods": sorted(periods, key=lambda p: p["start"], reverse=True),
        "cards": cards,
    }
    out = os.path.join(ROOT, "data.js")
    os.makedirs(ROOT, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write("// 由 build.py 生成，请勿手改；改 cards/*.json 后重新运行 build.py\n")
        f.write("window.WXI_DATA = ")
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write(";\n")
    by_level = {k: sum(1 for c in cards if c["level"] == k) for k in LEVELS}
    print(f"OK  {len(periods)} 个周期 / {len(cards)} 张卡片  {by_level}  → {out}")


if __name__ == "__main__":
    main()
