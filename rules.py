"""微信洞察 · 不依赖 AI 的规则提炼（被 server.py 调用）

每次拉取数据后都会跑一遍，不需要登录 Claude：
1. 「待回复」：私聊里最后一条不是我发的 → 一张提醒卡；我回复了之后自动标为完成。
2. 「有人 @我」：群里 @了我 → 一张提醒卡；我在群里接话后自动完成。
3. 朋友圈：只在不用 AI 时启用，按关键词挑出可能有意义的动态（换工作、演出、开业、结婚……）。
提醒卡都放在 cards/inbox.json，id 按会话固定，不会越积越多。
"""

import hashlib
import json
import os
import re
from datetime import datetime

import wxi_paths as P

INBOX = os.path.join(P.CARDS, "inbox.json")

SNS_KEYWORDS = {
    "work": ["入职", "离职", "新工作", "跳槽", "加入了", "创业", "招聘", "招人", "内推", "求职", "辞职", "裸辞", "转行"],
    "music": ["巡演", "开票", "演出", "首发", "新歌", "新专", "专辑", "上线", "音乐节", "live", "Live", "LIVE", "首唱", "签约"],
    "venture": ["开业", "开张", "融资", "发布会", "上市", "新品", "合伙人", "签约"],
    "people": ["结婚", "订婚", "领证", "婚礼", "宝宝", "出生", "生日", "毕业", "搬家", "乔迁", "住院", "手术", "去世", "节哀", "求助", "帮忙", "帮转", "回国"],
}
SNS_ADS = ["代购", "下单", "包邮", "折扣", "优惠券", "私信我", "限时", "秒杀", "团购", "加盟", "招代理", "红包", "抽奖", "直播间"]


def sns_keywords(cfg=None):
    """朋友圈关键词：内置通用词 + config.json 的 sns_keywords_extra（比如自己所在的城市「来<城市名>」）。"""
    cfg = P.load_cfg() if cfg is None else cfg
    kw = {k: list(v) for k, v in SNS_KEYWORDS.items()}
    for k, extra in (cfg.get("sns_keywords_extra") or {}).items():
        kw.setdefault(k, []).extend(extra)
    return kw


def _rid(prefix, chat):
    return f"{prefix}-{hashlib.md5(chat.encode()).hexdigest()[:8]}"


def _load():
    if os.path.exists(INBOX):
        with open(INBOX, encoding="utf-8") as f:
            return json.load(f)
    return {"period": {"type": "inbox", "id": "inbox", "label": "消息提醒", "start": "2000-01-01",
                       "end": "2999-12-31", "extracted_until": ""},
            "summary": "不经过 AI、按规则自动生成的提醒：待回复的私聊、群里 @你 的消息、朋友圈动态。你回复之后会自动标为完成。",
            "cards": []}


def _now():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _ev(line, chat):
    m = re.match(r"^\[(.{16})\]\s(?:([^:：]{1,40}):\s)?(.*)$", line, re.S)
    if not m:
        return [line[:16], "", line[:60], chat]
    who = "我" if m.group(2) == "me" else (m.group(2) or "")
    return [m.group(1), who, m.group(3)[:60], chat]


def apply_rules(meta, cfg, use_sns=False, log=print):
    """meta: extract.py 写出的 meta.json 内容。返回 {added, changed, resolved}。"""
    doc = _load()
    by_id = {c["id"]: c for c in doc["cards"]}
    skip = set(cfg.get("inbox_skip", [])) | set(cfg.get("skip_chats", []))
    vip = set(cfg.get("vip_chats", []))
    added, changed, resolved = [], [], []

    def upsert(card, stamp=None):
        """提醒卡 id = 会话前缀 + 最后一条消息时间：有新消息就换新卡（旧卡移除），
        这样你在页面上点过「完成」的旧提醒，不会把新来的消息也一起藏掉。"""
        prefix = card["id"]
        if stamp:
            digits = re.sub(r"\D", "", stamp)
            card["id"] = f"{prefix}-{digits[4:12]}"
        if card["id"] in by_id:
            by_id[card["id"]].update(card)
            changed.append(card["id"])
            return
        if stamp:
            doc["cards"][:] = [c for c in doc["cards"] if not c["id"].startswith(prefix + "-")]
        doc["cards"].append(card)
        by_id[card["id"]] = card
        added.append(card["id"])

    def resolve(prefix):
        """我已经回复：直接移除这条提醒（不进「已完成」，免得刷屏）。"""
        before = len(doc["cards"])
        doc["cards"][:] = [c for c in doc["cards"] if not c["id"].startswith(prefix + "-")]
        if len(doc["cards"]) < before:
            resolved.append(prefix)

    for ch in meta.get("chats", []):
        name, typ = ch["chat"], ch["type"]
        if name in skip or typ == "work":
            continue
        tail = ch.get("tail") or []
        if typ == "private":
            cid = _rid("r", name)
            if ch.get("last_from_me"):
                resolve(cid)
                continue
            level = "focus" if (ch.get("mine") or name in vip) else "info"
            upsert({"id": cid, "date": ch["last_time"][:10], "level": level, "kind": "todo", "category": "inbox",
                    "title": f"待回复：{name}", "status": "open",
                    "summary": f"{name} 最后一条消息在 {ch['last_time'][5:]}，你还没回。最近几条：" +
                               " / ".join(_ev(l, name)[2] for l in tail[-3:]),
                    "action": "看一眼是否需要回复；回复之后这张卡会在下次更新时自动完成。",
                    "people": [name], "chats": [name], "evidence": [_ev(l, name) for l in tail[-3:]],
                    "tags": ["自动提醒", "待回复"], "auto": True,
                    "query": {"chats": [name], "keywords": [], "start": ch["last_time"][:10], "end": ch["last_time"][:10]}},
                   stamp=ch["last_time"])
        elif typ == "group" and ch.get("at_me"):
            cid = _rid("a", name)
            if ch.get("last_from_me"):
                resolve(cid)
                continue
            upsert({"id": cid, "date": ch["last_time"][:10], "level": "focus", "kind": "todo", "category": "inbox",
                    "title": f"群里有人 @你：{name}", "status": "open",
                    "summary": f"「{name}」里有 {ch['at_me']} 条消息 @了你，最近一条在 {ch['last_time'][5:]}。",
                    "action": "去群里看一下要不要接话。", "people": [], "chats": [name],
                    "evidence": [_ev(l, name) for l in tail[-3:]], "tags": ["自动提醒", "@我"], "auto": True,
                    "query": {"chats": [name], "keywords": [], "start": ch["last_time"][:10], "end": ch["last_time"][:10]}},
                   stamp=ch["last_time"])
        elif typ == "group" and ch.get("mine") and ch.get("last_from_me"):
            resolve(_rid("a", name))

    if use_sns:
        avail = set(P.categories())
        # 朋友圈关键词桶 -> 卡片分类：优先用匹配的分类，没有就退到通用分类，最后一定落到 people
        bucket_cat = {"work": "work", "venture": "venture", "people": "people",
                      "music": "music" if "music" in avail else ("interest" if "interest" in avail else "people")}
        for it in meta.get("sns_items", []):
            body = it["line"].split(": ", 1)[-1]
            if any(w in body for w in SNS_ADS) or it["name"] in skip:
                continue
            bucket = next((k for k, ws in sns_keywords(cfg).items() if any(w in body for w in ws)), None)
            if not bucket:
                continue
            cat = bucket_cat.get(bucket, "people")
            if cat not in avail:
                cat = "people"
            short = re.sub(r"[\[\]]", "", body)[:22]
            cid = _rid("s", it["name"] + it["time"])
            if cid in by_id:
                continue
            upsert({"id": cid, "date": it["time"][:10], "level": "info", "kind": "event", "category": cat,
                    "title": f"朋友圈｜{it['name']}：{short}", "status": "info",
                    "summary": body[:200], "action": "无需处理（可以点赞或私信问候）",
                    "people": [it["name"]], "chats": [f"朋友圈·{it['name']}"],
                    "evidence": [[it["time"], it["name"], body[:60], "朋友圈"]], "tags": ["朋友圈", "关键词"], "auto": True,
                    "query": {"chats": [f"朋友圈·{it['name']}"], "keywords": [], "start": it["time"][:10], "end": it["time"][:10]}})

    os.makedirs(P.CARDS, exist_ok=True)
    with open(INBOX, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    log(f"规则提醒：新增 {len(added)}，更新 {len(changed)}，已回复自动完成 {len(resolved)}")
    return {"added": added, "changed": changed, "resolved": resolved}
