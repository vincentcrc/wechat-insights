#!/usr/bin/env python3
"""微信洞察 · 抽取脚本（只读，数据不出本机）

按周期把私聊 / 群聊 / 企微会话的消息导出成一份 digest.md，供 Claude 阅读后提炼卡片。
公众号、服务号（contact.verify_flag != 0）和系统占位会话一律跳过。

用法（必须用装了 wechat_cli 的解释器；自用是 ~/.wechat-cli/venv，App 版自带）：
  ~/.wechat-cli/venv/bin/python ~/wechat-insights/extract.py --period week            # 本周（周一至今）
  ~/.wechat-cli/venv/bin/python ~/wechat-insights/extract.py --period week --ago 1    # 上周
  ~/.wechat-cli/venv/bin/python ~/wechat-insights/extract.py --period day             # 今天
  ~/.wechat-cli/venv/bin/python ~/wechat-insights/extract.py --period month           # 本月
  ~/.wechat-cli/venv/bin/python ~/wechat-insights/extract.py --start 2026-09-01 --end 2026-09-10
  可选：--chat "群名"（可重复，只抽这些会话）  --max-per-chat 400  --no-groups
输出：<数据目录>/raw/<周期ID>/digest.md 与 meta.json
"""

import argparse
import json
import os
import re
from html import unescape as html_unescape
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta

from wechat_cli.core.context import AppContext
from wechat_cli.core.contacts import get_contact_names, get_self_username
from wechat_cli.core.messages import (
    _build_history_line,
    _iter_table_contexts,
    _load_name2id_maps,
    resolve_chat_context,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wxi_paths as P  # noqa: E402

ROOT = P.DATA
SYS_USERS = {"filehelper", "notifymessage", "fmessage", "weixin", "qqmail",
             "floatbottle", "medianote", "newsapp", "qqsafe", "tmessage"}
# 这些消息类型对提炼价值很低，直接丢弃
NOISE_MARKERS = ("微信版本不支持展示该内容", "微信版本不支援顯示此內容", "[表情]", "[动画表情]", "[拍一拍]", "sysmsgtemplate", "加入了群聊", "加入群聊", "撤回了一条消息")
# 聚焦模式：我没参与、没被 @ 的群只保留命中这些信号词的消息
SIGNAL_WORDS = ("@所有人", "通知", "公告", "截止", "报名", "演出", "排练", "巡演", "音乐节", "招募", "合同",
                "付款", "转账", "打款", "发票", "报销", "费用", "价格", "预算", "门票", "票", "开会", "会议",
                "明天", "后天", "下周", "周六", "周日", "号", "点前", "地址", "定了", "确定", "取消", "改期",
                "招聘", "内推", "岗位", "融资", "上线", "发布", "签约", "版权", "商标", "急", "重要", "务必")


def period_range(kind, ago, now):
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "day":
        start = today - timedelta(days=ago)
        end = start + timedelta(days=1)
        pid = start.strftime("%Y-%m-%d")
        label = f"{start:%m-%d} 日报"
    elif kind == "week":
        monday = today - timedelta(days=today.weekday()) - timedelta(weeks=ago)
        start, end = monday, monday + timedelta(days=7)
        y, w, _ = start.isocalendar()
        pid = f"{y}-W{w:02d}"
        label = f"{y} 第{w}周（{start:%m-%d}~{(end - timedelta(days=1)):%m-%d}）"
    elif kind == "month":
        y, m = today.year, today.month - ago
        while m <= 0:
            y, m = y - 1, m + 12
        start = datetime(y, m, 1)
        end = datetime(y + (m == 12), m % 12 + 1, 1)
        pid = f"{y}-{m:02d}"
        label = f"{y}年{m}月 月报"
    else:
        raise ValueError(kind)
    return start, min(end, now), end, pid, label


def classify(username, verify_flag):
    if "@chatroom" in username:
        return "group"
    if username.startswith("gh_") or verify_flag:
        return "mp"
    if "holder" in username or username in SYS_USERS:
        return "sys"
    if "@openim" in username:
        return "work"
    return "private"


def load_contact_meta(app):
    path = app.cache.get(os.path.join("contact", "contact.db"))
    meta = {}
    with closing(sqlite3.connect(path)) as c:
        for u, vf, notify in c.execute("SELECT username, verify_flag, chat_room_notify FROM contact"):
            meta[u] = {"verify_flag": vf or 0, "notify": notify}
    return meta


def load_sessions(app):
    path = app.cache.get(os.path.join("session", "session.db"))
    with closing(sqlite3.connect(path)) as c:
        return [{"username": r[0], "ts": r[1] or 0}
                for r in c.execute("SELECT username, last_timestamp FROM SessionTable")]


_RE_VOICE = re.compile(r"<msg><voicemsg[^>]*?voicelength=\"(\d+)\".*", re.S)
_RE_VIDEO = re.compile(r"<\?xml[^>]*>\s*<msg>\s*<videomsg.*", re.S)
_RE_LOC = re.compile(r"<\?xml.*?<location[^>]*?label=\"([^\"]*)\"[^>]*?poiname=\"([^\"]*)\".*", re.S)
_RE_CARD = re.compile(r"<\?xml.*?<msg[^>]*?nickname=\"([^\"]*)\".*", re.S)
_RE_TITLE = re.compile(r"<msg>.*?<title>([^<]*)</title>.*", re.S)
_RE_REVOKE = re.compile(r"<\?xml.*?<content>([^<]*)</content>.*", re.S)


_RE_FWDREC = re.compile(r"\[链接/文件\]\s*([^\n]*?的聊天记录)")


def tidy(line):
    """把语音 / 视频 / 位置 / 名片等 XML 压缩成一句话，省阅读成本"""
    # 合并转发进来的「某人的聊天记录 / A和B的聊天记录」：标题里会出现第三方名字。
    # 明确标成「转发的历史记录」，免得被当成「我和这个第三方的对话」而归错人。
    line = _RE_FWDREC.sub(r"[转发的聊天记录·历史内容，非本会话对话] \1", line)
    if "<" not in line:
        return line
    line = _RE_VOICE.sub(lambda m: f"(约{int(m.group(1)) // 1000}秒，未转文字)", line)
    line = _RE_VIDEO.sub("(视频)", line)
    line = _RE_LOC.sub(lambda m: f"{m.group(2)}｜{m.group(1)}", line)
    line = _RE_REVOKE.sub(lambda m: m.group(1), line)
    line = _RE_CARD.sub(lambda m: f"[名片] {m.group(1)}", line)
    line = _RE_TITLE.sub(lambda m: f"《{m.group(1)}》", line)
    line = line.replace('<?xml version="1.0"?> ', "")
    line = re.sub(r"<(\?xml|msg)\b.*", "(媒体)", line, flags=re.S)
    return line


def fetch_lines(ctx, names, fn, start_ts, end_ts, cap):
    out = []
    for t in _iter_table_contexts(ctx):
        with closing(sqlite3.connect(t["db_path"])) as conn:
            id_map = _load_name2id_maps(conn)
            rows = conn.execute(
                f"SELECT local_id, local_type, create_time, real_sender_id, message_content,"
                f" WCDB_CT_message_content FROM [{t['table_name']}]"
                f" WHERE create_time >= ? AND create_time < ? ORDER BY create_time DESC LIMIT ?",
                (start_ts, end_ts, cap)).fetchall()
            for row in rows:
                try:
                    out.append(_build_history_line(row, t, names, id_map, fn))
                except Exception:
                    pass
    out.sort(key=lambda x: x[0])
    return [line for _, line in out]


# ---------------- 朋友圈 ----------------
SNS_KIND = {"1": "图片", "2": "文字", "3": "链接", "4": "音乐", "5": "视频", "15": "视频", "28": "视频号", "34": "视频号", "54": "视频", "7": "图片"}


def _xml_text(xml, tag):
    m = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.S)
    if not m:
        return ""
    t = m.group(1)
    t = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", t, flags=re.S)
    return html_unescape(re.sub(r"<[^>]+>", "", t)).strip()


def sns_items(app, names, s_ts, e_ts, me="", skip=(), author=None):
    """读取本机微信缓存的朋友圈（只有在电脑版微信里刷到过的才有正文）。"""
    path = app.cache.get(os.path.join("sns", "sns.db"))
    if not path:
        return []
    out = []
    with closing(sqlite3.connect(path)) as c:
        for tid, user, xml in c.execute("SELECT tid, user_name, content FROM SnsTimeLine"):
            m = re.search(r"<createTime>(\d+)</createTime>", xml or "")
            if not m or not (s_ts <= int(m.group(1)) < e_ts) or user == me:
                continue
            name = names.get(user, user)
            if user in skip or name in skip or (author and author not in (user, name)):
                continue
            obj = xml.split("<ContentObject>", 1)[-1]
            kind = SNS_KIND.get(_xml_text(obj, "type"), "动态")
            title = _xml_text(obj, "title")
            if re.search(r"微信版本不支|版本不支援", title):
                title = ""
            finder = re.search(r"<finderFeed>.*?<nickname>(.*?)</nickname>.*?<desc>(.*?)</desc>", obj, re.S)
            loc = re.search(r'poiName="([^"]*)"', xml)
            out.append({"tid": str(tid), "ts": int(m.group(1)), "user": user, "name": name,
                        "text": _xml_text(xml.split("<ContentObject>", 1)[0], "contentDesc"),
                        "kind": kind, "title": title,
                        "finder": f"{html_unescape(finder.group(1))}：{html_unescape(re.sub(r'<[^>]+>', '', finder.group(2)))[:120]}" if finder else "",
                        "loc": html_unescape(loc.group(1)) if loc and loc.group(1) else ""})
    out.sort(key=lambda x: x["ts"])
    return out


def sns_index_count(app, s_ts, e_ts, me=""):
    """微信本地索引里这段时间一共有多少条朋友圈（含没缓存正文的），用来提示覆盖率。"""
    path = app.cache.get(os.path.join("sns", "sns.db"))
    if not path:
        return 0
    with closing(sqlite3.connect(path)) as c:
        try:
            return c.execute("SELECT COUNT(DISTINCT tid) FROM SnsTopItem_1 WHERE create_time >= ? AND create_time < ? AND username != ?",
                             (s_ts, e_ts, me)).fetchone()[0]
        except sqlite3.Error:
            return 0


def sns_feed(start, end, q="", author=None):
    """给页面「朋友圈」视图用：按时间范围取朋友圈，附带关键词标签和广告标记。"""
    import rules
    app = AppContext()
    names = get_contact_names(app.cache, app.decrypted_dir)
    me = get_self_username(app.db_dir, app.cache, app.decrypted_dir)
    cfg = load_cfg()
    skip = set(cfg.get("sns_skip", [])) | set(cfg.get("inbox_skip", []))
    s_ts = int(datetime.strptime(start, "%Y-%m-%d").timestamp())
    e_ts = int((datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)).timestamp())
    items = sns_items(app, names, s_ts, e_ts, me=me, skip=skip, author=author)
    out = []
    for it in items:
        body = " ".join(x for x in (it["text"], it["title"], it["finder"]) if x)
        if q and q.lower() not in (body + it["name"]).lower():
            continue
        tags = [k for k, ws in rules.sns_keywords(cfg).items() if any(w in body for w in ws)]
        out.append({**it, "time": datetime.fromtimestamp(it["ts"]).strftime("%Y-%m-%d %H:%M"),
                    "tags": tags, "ad": any(w in body for w in rules.SNS_ADS)})
    out.sort(key=lambda x: -x["ts"])
    authors = {}
    for it in out:
        authors[it["name"]] = authors.get(it["name"], 0) + 1
    return {"items": out, "cached": len(items), "indexed": sns_index_count(app, s_ts, e_ts, me),
            "authors": sorted(authors.items(), key=lambda x: -x[1])[:60], "start": start, "end": end}


def sns_line(it):
    t = datetime.fromtimestamp(it["ts"]).strftime("%Y-%m-%d %H:%M")
    bits = [it["text"] or "（无文字）", f"[{it['kind']}]"]
    if it["title"] and it["title"] != it["text"]:
        bits.append(f"《{it['title'][:80]}》")
    if it["finder"]:
        bits.append(f"转发视频号 {it['finder']}")
    if it["loc"]:
        bits.append(f"📍{it['loc']}")
    return f"[{t}] {it['name']}: " + re.sub(r"\s+", " ", " ".join(bits))


def load_cfg():
    return P.load_cfg()


def collect_messages(chats, start, end, keywords=(), limit=300):
    """供 server.py 调用：按会话 + 时间范围 (+ 关键词) 取原始消息。start/end 为 YYYY-MM-DD，含首尾。"""
    app = AppContext()
    names = get_contact_names(app.cache, app.decrypted_dir)
    s_ts = int(datetime.strptime(start, "%Y-%m-%d").timestamp())
    e_ts = int((datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)).timestamp())
    out, missing = [], []
    for q in chats:
        if q.startswith("朋友圈"):
            who = q.split("·", 1)[1] if "·" in q else None
            items = sns_items(app, names, s_ts, e_ts, author=who)
            lines = [sns_line(i) for i in items]
            if keywords:
                lines = [l for l in lines if any(k.lower() in l.split("] ", 1)[-1].lower() for k in keywords)]
            out.append({"chat": q, "count": len(lines), "lines": lines[-limit:]})
            continue
        ctx = resolve_chat_context(q, app.msg_db_keys, app.cache, app.decrypted_dir)
        if not ctx or not ctx["db_path"]:
            missing.append(q)
            continue
        lines = [tidy(l) for l in fetch_lines(ctx, names, app.display_name_fn, s_ts, e_ts, 2000)
                 if not any(m in l for m in NOISE_MARKERS)]
        if keywords:
            lines = [l for l in lines if any(k.lower() in l.split("] ", 1)[-1].lower() for k in keywords)]
        out.append({"chat": ctx["display_name"], "count": len(lines), "lines": lines[-limit:]})
    return {"chats": out, "missing": missing}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", choices=["day", "week", "month"], default="week")
    ap.add_argument("--ago", type=int, default=0, help="往前推几个周期，0=当前")
    ap.add_argument("--since", help="增量：从这个时间点（YYYY-MM-DD HH:MM）到现在")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--chat", action="append", default=[])
    ap.add_argument("--max-per-chat", type=int, default=400)
    ap.add_argument("--no-groups", action="store_true")
    ap.add_argument("--full", action="store_true", help="关闭聚焦模式，所有群保留全部消息")
    ap.add_argument("--focus-cap", type=int, default=60, help="聚焦模式下旁观群最多保留几条")
    ap.add_argument("--no-sns", action="store_true", help="不读取朋友圈")
    a = ap.parse_args()

    now = datetime.now()
    if a.since:
        start = datetime.strptime(a.since[:16], "%Y-%m-%d %H:%M")
        end = full_end = now
        pid = f"inc-{start:%Y%m%d-%H%M}"
        label = f"增量 {start:%m-%d %H:%M} 起"
        kind = "custom"
    elif a.start:
        start = datetime.strptime(a.start, "%Y-%m-%d")
        end = datetime.strptime(a.end, "%Y-%m-%d") + timedelta(days=1) if a.end else now
        end = min(end, now)
        full_end = end
        pid = f"custom-{start:%Y%m%d}-{(end - timedelta(days=1)):%Y%m%d}"
        label = f"自定义 {start:%m-%d}~{(end - timedelta(days=1)):%m-%d}"
        kind = "custom"
    else:
        start, end, full_end, pid, label = period_range(a.period, a.ago, now)
        kind = a.period
    s_ts, e_ts = int(start.timestamp()), int(end.timestamp())

    app = AppContext()
    names = get_contact_names(app.cache, app.decrypted_dir)
    me = get_self_username(app.db_dir, app.cache, app.decrypted_dir)
    cfg = load_cfg()
    my_names = {me, names.get(me, ""), *cfg.get("my_aliases", [])} - {""}
    skip = set(cfg.get("skip_chats", []))
    cmeta = load_contact_meta(app)

    targets = []
    if a.chat:
        for q in a.chat:
            ctx = resolve_chat_context(q, app.msg_db_keys, app.cache, app.decrypted_dir)
            if ctx and ctx["db_path"]:
                targets.append(ctx)
            else:
                print(f"找不到会话: {q}", file=sys.stderr)
    else:
        for s in load_sessions(app):
            if s["ts"] < s_ts:
                continue
            u = s["username"]
            kindc = classify(u, cmeta.get(u, {}).get("verify_flag", 0))
            if kindc in ("mp", "sys") or (a.no_groups and kindc == "group") \
                    or u in skip or names.get(u) in skip:
                continue
            ctx = resolve_chat_context(u, app.msg_db_keys, app.cache, app.decrypted_dir)
            if ctx and ctx["db_path"]:
                targets.append(ctx)

    chats = []
    for ctx in targets:
        u = ctx["username"]
        lines = fetch_lines(ctx, names, app.display_name_fn, s_ts, e_ts, a.max_per_chat)
        lines = [tidy(l) for l in lines if not any(m in l for m in NOISE_MARKERS)]
        if not lines:
            continue
        mine = sum(1 for l in lines if any(f"] {n}:" in l for n in my_names))
        at_me = sum(1 for l in lines if any(f"@{n}" in l for n in my_names if n != "me"))
        total = len(lines)
        typ = classify(u, cmeta.get(u, {}).get("verify_flag", 0))
        focused = False
        if not a.full and typ == "group" and not mine and not at_me and not cmeta.get(u, {}).get("notify"):
            hits, seen = [], set()
            for l in lines:
                body = l.split("] ", 1)[-1].split(": ", 1)[-1][:80]
                if body in seen or not any(w in l for w in SIGNAL_WORDS):
                    continue
                seen.add(body)
                hits.append(l)
            lines, focused = hits[-a.focus_cap:], True
            if not lines:
                continue
        last = lines[-1]
        chats.append({
            "last_time": last[1:17], "last_from_me": any(f"] {n}:" in last for n in my_names),
            "chat": ctx["display_name"], "username": u,
            "type": typ, "total": total, "focused": focused,
            "notify": cmeta.get(u, {}).get("notify"),
            "count": len(lines), "mine": mine, "at_me": at_me,
            "truncated": len(lines) >= a.max_per_chat, "lines": lines,
            "tail": [l[:200] for l in lines[-4:]],
        })

    # 私聊优先，其次我参与 / @我 多的群，再按消息量
    order = {"private": 0, "work": 2, "group": 1}
    chats.sort(key=lambda c: (order[c["type"]], -(c["mine"] * 3 + c["at_me"] * 5), -c["count"]))

    sns = [] if (a.no_sns or a.chat or not cfg.get("sns", True)) else \
        sns_items(app, names, s_ts, e_ts, me=me, skip=skip | set(cfg.get("sns_skip", [])))

    out_dir = os.path.join(P.RAW, pid)
    os.makedirs(out_dir, exist_ok=True)
    meta = {
        "period": {"type": kind, "id": pid, "label": label,
                   "start": start.strftime("%Y-%m-%d"),
                   "end": (full_end - timedelta(seconds=1)).strftime("%Y-%m-%d"),
                   "extracted_until": end.strftime("%Y-%m-%d %H:%M")},
        "me": sorted(my_names),
        "chats": [{k: v for k, v in c.items() if k != "lines"} for c in chats],
        "total_messages": sum(c["count"] for c in chats),
        "sns": {"posts": len(sns), "authors": len({i["user"] for i in sns})},
        "sns_items": [{"name": i["name"], "time": datetime.fromtimestamp(i["ts"]).strftime("%Y-%m-%d %H:%M"),
                       "line": sns_line(i)} for i in sns],
    }
    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)

    tname = {"private": "私聊", "group": "群聊", "work": "企微"}
    with open(os.path.join(out_dir, "digest.md"), "w", encoding="utf-8") as f:
        f.write(f"# 微信 digest · {label}\n\n周期 {meta['period']['start']} ~ {meta['period']['end']}，"
                f"抽取至 {meta['period']['extracted_until']}；{len(chats)} 个会话 / {meta['total_messages']} 条。"
                f"我 = {', '.join(sorted(my_names))}\n\n")
        for c in chats:
            flag = []
            if c["mine"]:
                flag.append(f"我发言{c['mine']}")
            if c["at_me"]:
                flag.append(f"@我{c['at_me']}")
            if c["type"] == "group":
                flag.append("提醒开" if c["notify"] else "免打扰")
            if c["focused"]:
                flag.append(f"旁观群·仅信号消息 {c['count']}/{c['total']}")
            if c["truncated"]:
                flag.append(f"仅最近{a.max_per_chat}条")
            f.write(f"\n## [{tname[c['type']]}] {c['chat']}（{c['count']}条；{'，'.join(flag)}）\n\n")
            for l in c["lines"]:
                f.write((l if len(l) <= 500 else l[:500] + "…") + "\n")
        by_author = {}
        for it in sns:
            by_author.setdefault(it["name"], []).append(it)
        for name, items in sorted(by_author.items(), key=lambda x: -len(x[1])):
            f.write(f"\n## [朋友圈] 朋友圈·{name}（{len(items)}条）\n\n")
            for it in items:
                f.write(sns_line(it)[:600] + "\n")
    print(json.dumps({"out": out_dir, "chats": len(chats), "messages": meta["total_messages"], "sns": len(sns),
                      "label": label}, ensure_ascii=False))


if __name__ == "__main__":
    main()
