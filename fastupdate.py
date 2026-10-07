"""微信洞察 · 快速更新引擎（被 server.py 调用）

思路：把 digest 和「现有卡片索引」直接放进一次 Claude 调用里，让它只输出一个小 JSON 补丁，
由这里负责分配 id、落盘、合并。不让 Claude 逐个读写文件，也不开长思考，所以快很多。
digest 很大时按会话切块并行处理。
"""

import glob
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import wxi_paths as P

ROOT = P.DATA
CARDS = P.CARDS
STATE_FILE = P.data("extract_state.json")


def build_system(cfg=None):
    """提炼用的系统提示词。分类、线索和「用户是谁」来自 config.json，自用和朋友版共用一套规则。"""
    cfg = P.load_cfg() if cfg is None else cfg
    cats = " / ".join(f"{k} {v['name']}" for k, v in P.categories(cfg).items() if k not in ("inbox", "private"))
    ths = ", ".join(P.threads(cfg))
    who, notes = P.persona(cfg)
    extra = "".join(f"\n   {n}" for n in notes)
    return f"""你是「微信洞察」的信息提炼器。输入是用户（{who}）的一段微信聊天摘录和现有的卡片索引。
任务：找出值得记录的新事项，或已有事项的新进展，只输出一个 JSON 对象，不要任何其它文字。

输出格式：
{{"new": [卡片], "update": [更新]}}

卡片字段：date(YYYY-MM-DD，事情发生日期), due(可选), level(major 重大/focus 需关注/info 参考), kind(todo/risk/decision/event/info),
category({cats}), thread(可选，只能从这些里选：{ths}),
title(一句话结论≤30字), summary(2–4句，含具体时间人物数字), action(下一步；没有写「无需处理」), people[], chats[](必须与摘录里 ## 标题中的会话名完全一致),
evidence[[时间,发言人(本人写「我」),原话≤60字,会话名]], tags[], status(open/watch/done/info), query{{chats[],keywords[],start,end}}。

更新格式：{{"id": "现有卡片id", "set": {{只写需要改的字段，如 summary/action/status/due/level/title}}, "add_evidence": [[...]]}}

规则：
1. 已有卡片（见索引）能对上的，一律用 update，不要重复新建。
2. 私聊和「我发言/@我」的群要逐个认真过一遍，凡是下面这些都要记：约定的时间、地点和行程；钱、转账、合同、发票、税；别人请我做的事和我答应的事；截止日期；重要决定或变化；法律、安全、健康风险；项目或合作的进展；对我有用的机会。
   一件事一张卡，同一件事在不同会话里出现要合并。闲聊、表情、段子、广告、群发营销不要。旁观群只摘和我本人直接有关的通知或机会（比如小区、公司、我参与的团体）。
3. 私密或亲密关系类的内容一律不要写。{extra}
3.1 卡片的归属（chats/people）一律以摘录里 ## 会话标题中的人为准。标着「[转发的聊天记录·历史内容，非本会话对话]」的行，或内容里出现的「某人的聊天记录」「A和B的聊天记录」，是用户转发进本会话的历史记录，不是用户和那个第三方的对话——绝不能据此新建或归并成「和某第三方的对话」卡片，也不要把第三方当成本条消息的发言人。
4. 投资类只记事实并注明风险，不给买卖建议。
   「## [朋友圈] 朋友圈·某人」是好友发的朋友圈：只挑对用户有意义的——朋友的人生大事（结婚、生子、换工作、创业、搬家、生病）、朋友在做的项目或活动、明确的求助或机会、和用户的项目或行业直接相关的消息。
   广告、微商、转发鸡汤、普通晒图一律不要。朋友圈卡片的 chats 写「朋友圈·某人」，category 多用 people，level 通常是 info，确实需要行动时才用 focus。
5. 没有值得记录的就输出 {{"new": [], "update": []}}。
6. JSON 字符串里不要出现英文双引号，中文引号用「」。"""


def load_all():
    docs = {}
    for p in sorted(glob.glob(os.path.join(CARDS, "*.json"))):
        with open(p, encoding="utf-8") as f:
            docs[p] = json.load(f)
    return docs


def card_index(docs, since_days=28):
    cut = (datetime.now() - timedelta(days=since_days)).strftime("%Y-%m-%d")
    rows = []
    for doc in docs.values():
        for c in doc.get("cards", []):
            if c.get("status") in ("open", "watch") or c.get("date", "") >= cut:
                rows.append(f"{c['id']} | {c['date']} | {c['level']}/{c.get('status')} | {c['title']} | 会话:{'、'.join(c.get('chats', [])[:3])} | {c['summary'][:70]}")
    return "\n".join(rows)


def user_state():
    try:
        with open(P.data("state.json"), encoding="utf-8") as f:
            st = json.load(f).get("state", {})
    except Exception:  # noqa: BLE001
        return ""
    lines = []
    for cid, v in st.items():
        bits = []
        if v.get("status"):
            bits.append("状态=" + v["status"])
        if v.get("note"):
            bits.append("备注=" + v["note"][:80])
        if bits:
            lines.append(f"{cid}: {'；'.join(bits)}")
    return "\n".join(lines)


def chunk_digest(path, max_chars=18000):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    head, _, body = text.partition("\n## ")
    sections = ["## " + s for s in body.split("\n## ")] if body else []
    chunks, cur = [], ""
    for sec in sections:
        if len(sec) > max_chars:
            sec = sec[:1000] + "\n…（中间省略）…\n" + sec[-(max_chars - 1200):]
        if cur and len(cur) + len(sec) > max_chars:
            chunks.append(cur)
            cur = ""
        cur += sec + "\n"
    if cur:
        chunks.append(cur)
    return head.strip(), chunks


def parse_json(text):
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    else:
        a, b = text.find("{"), text.rfind("}")
        text = text[a:b + 1] if a >= 0 else "{}"
    return json.loads(text)


def call_claude(run, user_prompt, system=None, effort="low", model=None, timeout=600):
    """run: server 传进来的执行器，负责二进制路径、环境变量和登录失败识别。返回 (text, cost)。"""
    extra = ["--tools", "", "--effort", effort, "--no-session-persistence", "--system-prompt", system or build_system()]
    if model:
        extra += ["--model", model]
    return run(user_prompt, extra, timeout)


def next_id(docs, week_id):
    n = 0
    prefix = "w" + week_id.split("-W")[1] + "-"
    for doc in docs.values():
        for c in doc.get("cards", []):
            if c["id"].startswith(prefix):
                try:
                    n = max(n, int(c["id"][len(prefix):]))
                except ValueError:
                    pass
    return f"{prefix}{n + 1:02d}"


def week_of(date_s):
    d = datetime.strptime(date_s[:10], "%Y-%m-%d")
    y, w, _ = d.isocalendar()
    mon = d - timedelta(days=d.weekday())
    return f"{y}-W{w:02d}", mon, mon + timedelta(days=6)


def week_doc(docs, date_s, extracted_until):
    wid, mon, sun = week_of(date_s)
    path = os.path.join(CARDS, f"{wid}.json")
    if path not in docs:
        docs[path] = {"period": {"type": "week", "id": wid, "label": f"第{int(wid.split('-W')[1])}周",
                                 "start": mon.strftime("%Y-%m-%d"), "end": sun.strftime("%Y-%m-%d"),
                                 "extracted_until": extracted_until}, "summary": "", "cards": []}
    return path, docs[path]


def apply_patches(patches, extracted_until, valid_cats, valid_threads, log, advance=True):
    os.makedirs(CARDS, exist_ok=True)
    docs = load_all()
    by_id = {c["id"]: (p, c) for p, d in docs.items() for c in d.get("cards", [])}
    added, changed, touched = [], [], set()
    seen_titles = set()
    for patch in patches:
        for u in patch.get("update", []) or []:
            hit = by_id.get(u.get("id"))
            if not hit:
                continue
            path, c = hit
            for k, v in (u.get("set") or {}).items():
                if k in ("summary", "action", "status", "due", "level", "title", "kind") and v:
                    if k == "status" and v == "done" and c.get("status") != "done":
                        c["done_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                    elif k == "status" and v != "done":
                        c.pop("done_at", None)
                    c[k] = v
            ev = u.get("add_evidence") or []
            have = {tuple(e[:3]) for e in c.get("evidence", [])}
            c.setdefault("evidence", []).extend(e for e in ev if isinstance(e, list) and len(e) >= 3 and tuple(e[:3]) not in have)
            if c["id"] not in [x["id"] for x in changed]:
                changed.append({"id": c["id"], "title": c["title"], "level": c["level"]})
            touched.add(path)
        for c in patch.get("new", []) or []:
            try:
                datetime.strptime(c.get("date", "")[:10], "%Y-%m-%d")
            except ValueError:
                log(f"跳过一张日期不合法的卡片：{c.get('title')}")
                continue
            if c.get("category") not in valid_cats or not c.get("title") or not c.get("summary"):
                log(f"跳过一张字段不全的卡片：{c.get('title')}")
                continue
            key = re.sub(r"\W", "", c["title"])[:14]
            if key in seen_titles:
                continue
            seen_titles.add(key)
            if c.get("thread") not in valid_threads:
                c.pop("thread", None)
            c["level"] = c.get("level") if c.get("level") in ("major", "focus", "info") else "info"
            c["status"] = c.get("status") if c.get("status") in ("open", "watch", "done", "info") else "info"
            path, doc = week_doc(docs, c["date"], extracted_until)
            c["id"] = next_id(docs, doc["period"]["id"])
            for k in ("people", "chats", "evidence", "tags"):
                c.setdefault(k, [])
            doc["cards"].append(c)
            added.append({"id": c["id"], "title": c["title"], "level": c["level"]})
            touched.add(path)
    if advance:  # 当前周文件记录抽取进度（手动加卡时不推进）
        path, doc = week_doc(docs, datetime.now().strftime("%Y-%m-%d"), extracted_until)
        doc["period"]["extracted_until"] = extracted_until
        touched.add(path)
    for p in touched:
        per = docs[p]["period"]
        if advance and per["start"] <= extracted_until[:10] <= per["end"]:
            per["extracted_until"] = extracted_until
        with open(p, "w", encoding="utf-8") as f:
            json.dump(docs[p], f, ensure_ascii=False, indent=2)
    if advance:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"extracted_until": extracted_until}, f)
    return added, changed, sorted(touched)


def refresh_summaries(run, paths, log, effort="low", model=None):
    """用一次很小的调用，给受影响的周重写 2–4 句总结。"""
    docs = {p: json.load(open(p, encoding="utf-8")) for p in paths}
    items = []
    for p, d in docs.items():
        cards = sorted(d["cards"], key=lambda c: ({"major": 0, "focus": 1, "info": 2}[c["level"]], c["date"]))
        items.append(f"### {d['period']['id']}\n旧总结：{d.get('summary', '')}\n" +
                     "\n".join(f"- [{c['level']}/{c.get('status')}] {c['date'][5:]} {c['title']}" for c in cards[:25]))
    prompt = ("为下面每一周写 2–4 句中文总结，突出重大和需关注事项，已完成的少提。只输出 JSON：{\"周id\": \"总结\"}\n\n" + "\n\n".join(items))
    try:
        text, _ = call_claude(run, prompt, system="你是简洁的中文编辑，只输出 JSON。", effort=effort, model=model, timeout=180)
        sums = parse_json(text)
    except Exception as e:  # noqa: BLE001
        log(f"周总结没更新：{e}")
        return
    for p, d in docs.items():
        s = sums.get(d["period"]["id"])
        if isinstance(s, str) and s.strip():
            d["summary"] = s.strip()
            with open(p, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)


def run_fast(run, digest_path, extracted_until, valid_cats, valid_threads, log, effort="medium", model=None, workers=5):
    head, chunks = chunk_digest(digest_path)
    if not chunks:
        log("这段时间没有新的聊天")
        added, changed, touched = apply_patches([], extracted_until, valid_cats, valid_threads, log)
        return {"added": added, "changed": changed, "summary": "这段时间没有新的聊天。"}
    docs = load_all()
    index, state = card_index(docs), user_state()
    today = datetime.now().strftime("%Y-%m-%d")
    log(f"摘录 {sum(len(c) for c in chunks) // 1000}k 字，分 {len(chunks)} 块并行提炼")

    def one(i_chunk):
        i, chunk = i_chunk
        prompt = (f"今天是 {today}。{head}\n\n【现有卡片索引】（id | 日期 | 级别/状态 | 标题 | 会话 | 摘要）\n{index}\n\n"
                  + (f"【用户在页面上的处理标记】\n{state}\n\n" if state else "")
                  + f"【聊天摘录 第{i + 1}/{len(chunks)}块】\n{chunk}")
        for attempt in (1, 2):
            text, cost = call_claude(run, prompt, effort=effort, model=model)
            try:
                patch = parse_json(text)
                log(f"第 {i + 1} 块完成：新增 {len(patch.get('new', []))}，更新 {len(patch.get('update', []))}")
                return patch, cost or 0
            except json.JSONDecodeError:
                log(f"第 {i + 1} 块返回格式不对，重试一次" if attempt == 1 else f"第 {i + 1} 块放弃")
        return {}, 0

    # 登录续期的并发保护在 server.make_runner 里（AUTH_LOCK）：需要续期时只放一个调用去续，其余等它成功后并行
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(one, enumerate(chunks)))
    patches = [r[0] for r in results]
    cost = sum(r[1] for r in results)
    added, changed, touched = apply_patches(patches, extracted_until, valid_cats, valid_threads, log)
    if added or changed:
        log("更新周总结")
        refresh_summaries(run, touched, log, effort="low", model=model)
    lv = {"major": "重大", "focus": "需关注", "info": "参考"}
    summ = [f"新增 {len(added)} 张、更新 {len(changed)} 张卡片。"]
    summ += [f"- {lv[c['level']]}：{c['title']}" for c in added if c["level"] != "info"]
    return {"added": added, "changed": changed, "summary": "\n".join(summ), "cost": cost}
