#!/usr/bin/env python3
"""微信洞察 · 本地服务（只监听 127.0.0.1，数据不出本机，AI 提炼除外）

让页面可以直接：拉取新数据、查看原始消息、向 Claude 提问 / 让 Claude 更新卡片。
必须用 wechat-cli 的 venv 解释器运行（要 import wechat_cli）：
  ~/.wechat-cli/venv/bin/python ~/wechat-insights/server.py
然后打开 http://127.0.0.1:8765/ 。启动器 /Applications/微信洞察.app 会自动做这两步。
"""

import glob
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CODE = os.path.dirname(os.path.abspath(__file__))   # 程序目录（App 里只读）
sys.path.insert(0, CODE)
import wxi_paths as P  # noqa: E402
import extract  # noqa: E402

ROOT = P.DATA   # 数据目录（卡片、状态、朋友圈都写这里；自用时与 CODE 相同）
os.makedirs(P.CARDS, exist_ok=True)
os.makedirs(P.RAW, exist_ok=True)

HOST, PORT = "127.0.0.1", int(os.environ.get("WXI_PORT", "8765"))
PY_VENV = sys.executable
TOKEN = secrets.token_urlsafe(18)
IDLE_EXIT = 6 * 3600  # 6 小时没有任何请求就自动退出，启动器会按需拉起
STATIC = {"/": "index.html", "/index.html": "index.html", "/data.js": "data.js"}
DATA_STATIC = {"/data.js": ROOT}  # data.js 是每台机器各自生成的，放数据目录；index.html 在程序目录


def seed_data_dir():
    """首次运行把程序目录里的规则文件复制到数据目录，并保证 data.js 存在，页面不至于空白。"""
    import shutil
    src = os.path.join(CODE, "CLAUDE.md")
    dst = os.path.join(ROOT, "CLAUDE.md")
    if os.path.exists(src) and not os.path.exists(dst) and os.path.abspath(src) != os.path.abspath(dst):
        shutil.copy(src, dst)
    if not os.path.exists(os.path.join(ROOT, "data.js")):
        subprocess.run([PY_VENV, os.path.join(CODE, "build.py")], capture_output=True, text=True, cwd=ROOT)
last_hit = time.time()


def claude_bin():
    cfg = extract.load_cfg()
    for c in (cfg.get("claude_bin"), shutil.which("claude"),
              os.path.expanduser("~/.claude/local/claude"), os.path.expanduser("~/.npm-global/bin/claude"),
              os.path.expanduser("~/.local/bin/claude"), "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
        if c and os.path.exists(c):
            return c
    return None


def system_proxy():
    """Anthropic 在国内直连会 403，Claude 调用要走本机代理：优先 config.json 的 proxy，其次 macOS 系统代理。"""
    cfg = extract.load_cfg()
    if cfg.get("proxy"):
        return cfg["proxy"]
    try:
        out = subprocess.run(["scutil", "--proxy"], capture_output=True, text=True, timeout=5).stdout
        kv = dict(re.findall(r"^\s*(\w+)\s*:\s*(\S+)", out, re.M))
        if kv.get("HTTPSEnable") == "1" and kv.get("HTTPSProxy"):
            return f"http://{kv['HTTPSProxy']}:{kv.get('HTTPSPort', '80')}"
        if kv.get("HTTPEnable") == "1" and kv.get("HTTPProxy"):
            return f"http://{kv['HTTPProxy']}:{kv.get('HTTPPort', '80')}"
    except Exception:  # noqa: BLE001
        pass
    return None


def login_cmd():
    px = system_proxy()
    pre = f"HTTPS_PROXY={px} HTTP_PROXY={px} " if px else ""
    return f"env -u ANTHROPIC_BASE_URL -u ANTHROPIC_AUTH_TOKEN {pre}claude auth login --claudeai"


KC_SERVICE, KC_ACCOUNT = "wechat-insights-claude", "oauth-token"


def long_token():
    """用户用 setup_token.sh 存进钥匙串的长期令牌（claude setup-token 生成，一年有效，不需要续期）。"""
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", KC_SERVICE, "-a", KC_ACCOUNT, "-w"],
                           capture_output=True, text=True, timeout=5)
        t = r.stdout.strip()
        return t if r.returncode == 0 and t else None
    except Exception:  # noqa: BLE001
        return None


def clean_env():
    """去掉宿主（Claude 桌面端会话）和 shell 里可能注入的 ANTHROPIC_* 网关变量，让 CLI 用个人登录；并补上系统代理。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "ANTHROPIC"))}
    px = system_proxy()
    if px and not (env.get("HTTPS_PROXY") or env.get("https_proxy")):
        env["HTTPS_PROXY"] = env["HTTP_PROXY"] = px
    cb = claude_bin()
    if cb:
        env["PATH"] = os.path.dirname(cb) + os.pathsep + env.get("PATH", "/usr/bin:/bin")
    env.setdefault("LANG", "en_US.UTF-8")
    tok = long_token()
    if tok:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    return env


PROBE = {"ok": None, "at": None, "checking": False}
# 登录凭证每 8 小时过期一次，过期后第一次调用会去续期。多个调用同时续期会互相把 refresh token 作废，
# 所以「距上次成功超过 7 小时」时，所有调用都要排队，直到有一次成功（续期完成）为止。
AUTH_LOCK = threading.Lock()
LAST_OK = {"t": 0.0}
AUTH_RE = re.compile(r"authenticat|log ?in|401|OAuth", re.I)


def _need_serial():
    return not long_token() and time.time() - LAST_OK["t"] > 7 * 3600


def probe_claude():
    """真正发一句极短的请求，确认登录可用（auth status 只看本地有没有凭证，过期了也会显示已登录）。"""
    cb = claude_bin()
    if not cb or PROBE["checking"]:
        return
    PROBE["checking"] = True
    try:
        with AUTH_LOCK:
            for attempt in (1, 2):
                r = subprocess.run([cb, "-p", "--output-format", "json", "--tools", "", "--no-session-persistence"],
                                   input="只回复 OK", capture_output=True, text=True, timeout=90, env=clean_env(), cwd=ROOT)
                d = json.loads((r.stdout or "{}").strip().splitlines()[-1])
                PROBE["ok"] = bool(d.get("subtype") == "success" and not d.get("is_error"))
                if PROBE["ok"]:
                    LAST_OK["t"] = time.time()
                    break
                time.sleep(3)
    except Exception:  # noqa: BLE001
        PROBE["ok"] = False
    PROBE["at"] = datetime.now().strftime("%H:%M:%S")
    PROBE["checking"] = False


def claude_status():
    cb = claude_bin()
    if not cb:
        return {"installed": False, "loggedIn": False}
    return {"installed": True, "bin": cb, "login_cmd": login_cmd(), "long_token": bool(long_token()),
            "setup_cmd": "bash ~/wechat-insights/setup_token.sh", "loggedIn": PROBE["ok"] is not False, "checked": PROBE["ok"] is not None,
            "checking": PROBE["checking"], "checked_at": PROBE["at"]}


# ---------------- 本地模型（Ollama，可选） ----------------

OLLAMA = "http://127.0.0.1:11434"


def ollama_status():
    import urllib.request
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=2) as r:
            models = [m["name"] for m in json.loads(r.read()).get("models", [])]
        want = extract.load_cfg().get("ollama_model", "qwen3:8b")
        return {"running": True, "models": models, "model": want, "ready": any(m.startswith(want) for m in models)}
    except Exception:  # noqa: BLE001
        return {"running": False, "models": [], "model": extract.load_cfg().get("ollama_model", "qwen3:8b"), "ready": False}


def make_ollama_runner(job):
    """和 make_runner 同样的接口：(prompt, extra_args, timeout) -> (text, cost)。数据完全不出本机。"""
    import urllib.request

    def run(prompt, extra, timeout):
        system = extra[extra.index("--system-prompt") + 1] if "--system-prompt" in extra else ""
        model = extract.load_cfg().get("ollama_model", "qwen3:8b")
        body = {"model": model, "stream": False, "format": "json", "think": False,
                "options": {"num_ctx": 32768, "temperature": 0.2},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        req = urllib.request.Request(OLLAMA + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            raise ConnectionError(f"本地模型没有响应（{e}）。请确认 Ollama 已打开，并已下载 {model}。")
        return d.get("message", {}).get("content", ""), 0
    return run


# ---------------- 任务 ----------------

JOBS, JOB_LOCK = {}, threading.Lock()


class Job:
    def __init__(self, kind, title, steps):
        self.id = uuid.uuid4().hex[:10]
        self.kind, self.title = kind, title
        self.steps = [{"name": s, "status": "pending"} for s in steps]
        self.log, self.status, self.result, self.error = [], "running", {}, None
        self.started, self.ended, self.proc, self.procs = time.time(), None, None, []
        JOBS[self.id] = self

    def step(self, i, status):
        self.steps[i]["status"] = status

    def say(self, text):
        self.log.append({"t": datetime.now().strftime("%H:%M:%S"), "text": text})
        self.log = self.log[-400:]

    def view(self):
        return {"id": self.id, "kind": self.kind, "title": self.title, "steps": self.steps, "log": self.log,
                "status": self.status, "result": self.result, "error": self.error,
                "elapsed": int((self.ended or time.time()) - self.started)}


def running_job():
    return next((j for j in JOBS.values() if j.status == "running"), None)


def cards_snapshot():
    snap = {}
    for p in glob.glob(os.path.join(ROOT, "cards", "*.json")):
        try:
            with open(p, encoding="utf-8") as f:
                doc = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        for c in doc.get("cards", []):
            snap[c["id"]] = (hashlib.md5(json.dumps(c, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                             c.get("title"), c.get("level"))
    return snap


def write_state(state):
    if isinstance(state, dict):
        with open(os.path.join(ROOT, "state.json"), "w", encoding="utf-8") as f:
            json.dump({"saved_at": datetime.now().isoformat(timespec="seconds"), "state": state}, f,
                      ensure_ascii=False, indent=1)


def run_build(job):
    r = subprocess.run([PY_VENV, os.path.join(CODE, "build.py")],
                       capture_output=True, text=True, timeout=60, cwd=ROOT)
    msg = (r.stdout + r.stderr).strip()
    job.say(msg.splitlines()[-1] if msg else "build 完成")
    return r.returncode == 0, msg


def describe_tool(name, inp):
    path = inp.get("file_path") or inp.get("path") or inp.get("pattern") or ""
    short = os.path.relpath(path, ROOT) if path.startswith("/") else path
    return {"Read": "读取", "Write": "写入", "Edit": "修改", "MultiEdit": "修改", "Glob": "查找", "Grep": "搜索"}.get(name, name) + " " + short


def persona_brief():
    """把 config.json 里的「用户是谁」和分类、线索口径拼成一段说明，放在给 Claude 的指令前面。
    这样 CLAUDE.md 可以保持通用（能公开），个人口径只留在本机的 config.json。"""
    cfg = P.load_cfg()
    who, notes = P.persona(cfg)
    cats = "、".join(f"{k} {v['name']}" for k, v in P.categories(cfg).items() if k not in ("inbox", "private"))
    ths = "、".join(f"{k}（{v}）" for k, v in P.threads(cfg).items())
    lines = [f"用户：{who}。"] + [f"注意：{n}" for n in notes]
    lines += [f"category 可选值：{cats}。", f"thread 可选值：{ths}（要新线索就在 config.json 的 threads 里登记）。"]
    return "\n".join(lines) + "\n\n"


def run_claude(job, prompt, tools, timeout=1800):
    cb = claude_bin()
    if not cb:
        raise RuntimeError("没找到 Claude 命令行（claude）。")
    cmd = [cb, "-p", "--output-format", "stream-json", "--verbose", "--effort", "medium", "--permission-mode", "acceptEdits",
           "--allowedTools", ",".join(tools), "--disallowedTools", "Bash,WebFetch,WebSearch,Task,Agent"]
    model = extract.load_cfg().get("claude_model")
    if model:
        cmd += ["--model", model]
    job.proc = subprocess.Popen(cmd, cwd=ROOT, env=clean_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
    job.proc.stdin.write(prompt)
    job.proc.stdin.close()
    final, last_text, deadline = None, "", time.time() + timeout
    for line in job.proc.stdout:
        if time.time() > deadline:
            job.proc.kill()
            raise RuntimeError("Claude 处理超时。")
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            job.say(f"Claude 已启动（{ev.get('model', '')}）")
        elif t == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    job.say(describe_tool(block.get("name", ""), block.get("input", {})))
                elif block.get("type") == "text" and block.get("text", "").strip():
                    last_text = block["text"].strip()
        elif t == "result":
            final = ev
    job.proc.wait()
    if not final:
        raise RuntimeError("Claude 没有返回结果。")
    text = final.get("result") or last_text
    if final.get("is_error") or final.get("subtype") != "success":
        if re.search(r"authenticat|log ?in|401|OAuth", text or "", re.I):
            PROBE["ok"] = False
            raise PermissionError("Claude 命令行未登录或登录已过期。")
        raise RuntimeError(text or "Claude 执行失败。")
    PROBE["ok"] = True
    cost = final.get("total_cost_usd")
    return text, cost


def make_runner(job):
    """给 fastupdate 用的单次调用执行器（可并行），返回 (text, cost)。"""
    def run(prompt, extra, timeout):
        cb = claude_bin()
        if not cb:
            raise RuntimeError("没找到 Claude 命令行（claude）。")
        model = extract.load_cfg().get("claude_model")
        cmd = [cb, "-p", "--output-format", "json", *extra] + (["--model", model] if model and "--model" not in extra else [])
        def once():
            p = subprocess.Popen(cmd, cwd=os.path.join(ROOT, "raw"), env=clean_env(), stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            job.procs.append(p)
            try:
                out, err = p.communicate(prompt, timeout=timeout)
            except subprocess.TimeoutExpired:
                p.kill()
                raise RuntimeError("Claude 处理超时。")
            try:
                return json.loads(out.strip().splitlines()[-1])
            except (json.JSONDecodeError, IndexError):
                raise RuntimeError("Claude 没有返回结果：" + (err or out)[-300:])

        def attempt():
            d = once()
            text = d.get("result") or ""
            if (d.get("is_error") or d.get("subtype") != "success") and AUTH_RE.search(text):
                job.say("登录凭证正在续期，3 秒后重试")
                time.sleep(3)  # 可能别的进程刚续期完、钥匙串已更新，重读一次通常就好
                d = once()
            if d.get("subtype") == "success" and not d.get("is_error"):
                LAST_OK["t"] = time.time()
            return d

        d = None
        if _need_serial():  # 需要续期时排队；排到时再看一眼，别人已经续期成功就直接并行跑
            with AUTH_LOCK:
                if _need_serial():
                    d = attempt()
        if d is None:
            d = attempt()
        text = d.get("result") or ""
        if d.get("is_error") or d.get("subtype") != "success":
            if AUTH_RE.search(text):
                PROBE["ok"] = False
                raise PermissionError("Claude 命令行未登录或登录已过期。")
            raise RuntimeError(text or "Claude 执行失败。")
        PROBE["ok"] = True
        LAST_OK["t"] = time.time()
        return text, d.get("total_cost_usd")
    return run


def job_update(job, body):
    try:
        mode = body.get("mode", "since")
        today = datetime.now().strftime("%Y-%m-%d")
        if mode == "today":
            args = ["--period", "day"]
        elif mode == "week":
            args = ["--period", "week"]
        elif mode == "lastweek":
            args = ["--period", "week", "--ago", "1"]
        elif mode == "month":
            args = ["--period", "month"]
        elif mode == "custom":
            args = ["--start", body["start"], "--end", body.get("end") or today]
        else:  # since：从上次抽取的那一分钟到现在（增量）
            since = body.get("since") or today + " 00:00"
            args = ["--since", since[:16]] if len(since) >= 16 else ["--start", since[:10], "--end", today]
        job.step(0, "running")
        job.say("从微信本地数据库抽取：" + " ".join(args))
        r = subprocess.run([PY_VENV, os.path.join(CODE, "extract.py"), *args], capture_output=True, text=True,
                           timeout=600, cwd=ROOT)
        if r.returncode != 0:
            raise RuntimeError("抽取失败：" + (r.stderr or r.stdout)[-600:])
        info = json.loads(r.stdout.strip().splitlines()[-1])
        with open(os.path.join(info["out"], "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        chats = meta["chats"]
        job.result["extract"] = {
            "label": info["label"], "period": meta["period"], "chats": len(chats), "messages": info["messages"],
            "at_me": [c["chat"] for c in chats if c.get("at_me")],
            "unreplied": [{"chat": c["chat"], "time": c.get("last_time")} for c in chats
                          if c["type"] == "private" and not c.get("last_from_me")][:30],
            "top": [{"chat": c["chat"], "type": c["type"], "count": c["count"], "mine": c["mine"],
                     "at_me": c["at_me"]} for c in chats[:25]],
        }
        job.say(f"抽取完成：{len(chats)} 个会话 / {info['messages']} 条")
        job.step(0, "done")

        write_state(body.get("state"))
        import build
        import fastupdate
        import rules
        cfg = extract.load_cfg()
        provider = body.get("provider") or cfg.get("ai_provider", "claude")
        if not body.get("ai", True):
            provider = "none"
        job.result["extract"]["sns"] = meta.get("sns", {})
        if meta.get("sns", {}).get("posts"):
            job.say(f"朋友圈：{meta['sns']['posts']} 条（{meta['sns']['authors']} 位好友）")
        job.result["rules"] = rules.apply_rules(meta, cfg, use_sns=(provider == "none"), log=job.say)
        job.result["provider"] = provider
        until = meta["period"]["extracted_until"]

        def finish_without_ai(warning=None):
            # 不用 AI 时不推进「AI 抽取进度」：规则提醒可以反复生成，下次用 AI 时这段聊天仍会被提炼
            job.step(1, "skipped" if not warning else "error")
            if warning:
                rules.apply_rules(meta, cfg, use_sns=True, log=job.say)
                job.result["warning"] = warning
            job.step(2, "running")
            ok, msg = run_build(job)
            if not ok:
                raise RuntimeError("卡片校验未通过：" + msg[-600:])
            job.step(2, "done")
            job.status = "done"

        if provider == "none":
            finish_without_ai()
            return
        job.step(1, "running")
        if not body.get("deep") or provider == "ollama":
            runner = make_ollama_runner(job) if provider == "ollama" else make_runner(job)
            try:
                res = fastupdate.run_fast(runner, os.path.join(info["out"], "digest.md"), until,
                                          set(build.CATEGORIES), set(build.THREADS), job.say,
                                          effort=cfg.get("claude_effort", "medium"), model=cfg.get("claude_model"),
                                          workers=1 if provider == "ollama" else 5)
            except PermissionError as e:
                job.say("Claude 登录失效，本次先只生成规则提醒")
                finish_without_ai({"code": "auth", "message": str(e)})
                return
            except ConnectionError as e:
                job.say(str(e))
                finish_without_ai({"code": "ollama", "message": str(e)})
                return
            job.step(1, "done")
            job.step(2, "running")
            ok, msg = run_build(job)
            if not ok:
                raise RuntimeError("卡片校验未通过：" + msg[-600:])
            job.result.update(res)
            job.step(2, "done")
            job.status = "done"
            return
        before = cards_snapshot()
        rel = os.path.relpath(info["out"], ROOT)
        prompt = (persona_brief() + f"请按本目录 CLAUDE.md 的规则更新「微信洞察」卡片。\n"
                  f"本次抽取：{info['label']}，原文在 {rel}/digest.md，统计在 {rel}/meta.json。\n"
                  f"用户处理状态在 state.json。已有卡片在 cards/ 下，先读和本次日期相关的周文件与当月月报，再读 digest。\n"
                  f"写完就结束，不要运行任何命令。")
        text, cost = run_claude(job, prompt, ["Read", "Glob", "Grep", "Edit", "Write", "MultiEdit"])
        job.step(1, "done")
        job.step(2, "running")
        ok, msg = run_build(job)
        if not ok:
            job.say("卡片格式有问题，让 Claude 修正一次")
            text2, _ = run_claude(job, f"build.py 校验失败，请修正 cards/ 下的 JSON：\n{msg[-1500:]}",
                                  ["Read", "Glob", "Grep", "Edit", "Write", "MultiEdit"], timeout=600)
            ok, msg = run_build(job)
            if not ok:
                raise RuntimeError("卡片校验仍未通过：" + msg[-600:])
        after = cards_snapshot()
        added = [k for k in after if k not in before]
        changed = [k for k in after if k in before and after[k][0] != before[k][0]]
        job.result.update({"summary": text, "cost": cost,
                           "added": [{"id": k, "title": after[k][1], "level": after[k][2]} for k in added],
                           "changed": [{"id": k, "title": after[k][1], "level": after[k][2]} for k in changed]})
        job.step(2, "done")
        job.status = "done"
    except PermissionError as e:
        job.status, job.error = "error", {"code": "auth", "message": str(e)}
    except Exception as e:  # noqa: BLE001
        job.status, job.error = "error", {"code": "fail", "message": str(e)}
    finally:
        for s in job.steps:
            if s["status"] == "running":
                s["status"] = "error" if job.status == "error" else "done"
        job.ended = time.time()


def job_ask(job, body):
    try:
        job.step(0, "running")
        chats, kws = body.get("chats") or [], body.get("keywords") or []
        start, end = body.get("start"), body.get("end") or datetime.now().strftime("%Y-%m-%d")
        ctx = extract.collect_messages(chats, start, end, (), limit=600) if chats else {"chats": [], "missing": []}
        os.makedirs(os.path.join(ROOT, "raw", "ask"), exist_ok=True)
        path = os.path.join(ROOT, "raw", "ask", f"{datetime.now():%Y%m%d-%H%M%S}-{job.id}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# 提问上下文 {start} ~ {end}\n关键词提示：{'、'.join(kws) or '无'}\n")
            for c in ctx["chats"]:
                f.write(f"\n## {c['chat']}（{c['count']} 条，显示最近 {len(c['lines'])} 条）\n\n" + "\n".join(c["lines"]) + "\n")
        n = sum(c["count"] for c in ctx["chats"])
        job.say(f"已取出 {len(ctx['chats'])} 个会话、{n} 条原始消息" + (f"；找不到：{'、'.join(ctx['missing'])}" if ctx["missing"] else ""))
        job.step(0, "done")
        write_state(body.get("state"))
        job.step(1, "running")
        cards = body.get("cards") or []
        allow = bool(body.get("allow_edit"))
        prompt = (persona_brief() + f"用户在「微信洞察」页面上提问。\n问题：{body.get('question', '').strip() or '这些事情最新进展到哪了？有没有我漏回或要跟进的？'}\n\n"
                  f"相关卡片（{len(cards)} 张）：\n" + "\n".join(f"- {c}" for c in cards[:40]) +
                  f"\n\n原始聊天记录在 {os.path.relpath(path, ROOT)}（{start} ~ {end}）。卡片数据在 cards/，规则见 CLAUDE.md，用户处理状态在 state.json。\n"
                  "请基于原文回答，引用关键原话时标注时间和发言人；原文里没有的就明说没找到，不要猜。\n"
                  + ("回答后，如有新进展，请按 CLAUDE.md 规则更新相关卡片（保留 id），并在回答末尾列出改了哪些卡片。"
                     if allow else "只回答，不要修改任何文件。") + "\n用中文，简洁，分点。")
        tools = ["Read", "Glob", "Grep"] + (["Edit", "Write", "MultiEdit"] if allow else [])
        before = cards_snapshot()
        text, cost = run_claude(job, prompt, tools, timeout=900)
        job.step(1, "done")
        job.result.update({"answer": text, "cost": cost})
        if allow:
            job.step(2, "running")
            ok, msg = run_build(job)
            if not ok:
                raise RuntimeError("Claude 改过的卡片没通过校验：" + msg[-600:])
            after = cards_snapshot()
            job.result["changed"] = [{"id": k, "title": after[k][1]} for k in after
                                     if k not in before or after[k][0] != before[k][0]]
            job.step(2, "done")
        else:
            job.step(2, "skipped")
        job.status = "done"
    except PermissionError as e:
        job.status, job.error = "error", {"code": "auth", "message": str(e)}
    except Exception as e:  # noqa: BLE001
        job.status, job.error = "error", {"code": "fail", "message": str(e)}
    finally:
        for s in job.steps:
            if s["status"] == "running":
                s["status"] = "error" if job.status == "error" else "done"
        job.ended = time.time()


# ---------------- HTTP ----------------

class Handler(BaseHTTPRequestHandler):
    server_version = "wxi/1"

    def log_message(self, *a):
        pass

    def _host_ok(self):
        return self.headers.get("Host", "") in (f"{HOST}:{PORT}", f"localhost:{PORT}")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _auth(self):
        return self._host_ok() and self.headers.get("X-WXI-Token") == TOKEN

    def do_GET(self):
        global last_hit
        last_hit = time.time()
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        path = self.path.split("?")[0]
        if path in STATIC:
            fp = os.path.join(DATA_STATIC.get(path, CODE), STATIC[path])
            if not os.path.exists(fp):
                return self._send(404, {"error": "missing " + STATIC[path]})
            with open(fp, "rb") as f:
                data = f.read()
            if STATIC[path] == "index.html":
                inject = f'<script>window.WXI_SERVER={{token:"{TOKEN}"}};</script>'.encode()
                data = data.replace(b'<script src="data.js"></script>',
                                    inject + f'<script src="data.js?t={int(time.time())}"></script>'.encode())
                return self._send(200, data, "text/html; charset=utf-8")
            return self._send(200, data, "application/javascript; charset=utf-8")
        if path == "/api/ping":
            return self._send(200, {"ok": True})
        if not self._auth():
            return self._send(403, {"error": "forbidden"})
        if path == "/api/status":
            j = running_job()
            return self._send(200, {"ok": True, "claude": claude_status(), "ollama": ollama_status(),
                                    "provider": extract.load_cfg().get("ai_provider", "claude"),
                                    "running": j.view() if j else None})
        if path == "/api/state":
            try:
                with open(os.path.join(ROOT, "state.json"), encoding="utf-8") as f:
                    return self._send(200, json.load(f))
            except (OSError, json.JSONDecodeError):
                return self._send(200, {"state": {}})
        m = re.match(r"^/api/jobs/(\w+)$", path)
        if m:
            j = JOBS.get(m.group(1))
            return self._send(200, j.view()) if j else self._send(404, {"error": "no job"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        global last_hit
        last_hit = time.time()
        if not self._auth():
            return self._send(403, {"error": "forbidden"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad json"})
        path = self.path.split("?")[0]
        if path == "/api/state":
            write_state(body.get("state"))
            return self._send(200, {"ok": True})
        if path == "/api/messages":
            try:
                end = body.get("end") or datetime.now().strftime("%Y-%m-%d")
                start = body.get("start") or (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
                r = extract.collect_messages(body.get("chats") or [], start, end, body.get("keywords") or [],
                                             int(body.get("limit") or 300))
                r.update({"start": start, "end": end})
                return self._send(200, r)
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": str(e)})
        if path == "/api/sns":
            try:
                end = body.get("end") or datetime.now().strftime("%Y-%m-%d")
                start = body.get("start") or (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
                return self._send(200, extract.sns_feed(start, end, body.get("q") or "", body.get("author") or None))
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": str(e)})
        if path == "/api/sns/card":
            # 不经过 AI，把一条朋友圈直接做成卡片（放进对应日期的周文件）
            try:
                import build
                import fastupdate
                it = body["item"]
                text = re.sub(r"\s+", " ", " ".join(x for x in (it.get("text"), it.get("title"), it.get("finder")) if x)).strip() or f"[{it.get('kind', '动态')}]"
                card = {"date": it["time"][:10], "level": body.get("level") or "info", "kind": "event",
                        "category": body.get("category") or "people", "title": f"朋友圈｜{it['name']}：{text[:22]}",
                        "summary": f"{it['name']} {it['time'][5:]} 发朋友圈：{text[:200]}" + (f"（📍{it['loc']}）" if it.get("loc") else ""),
                        "action": body.get("action") or "无需处理", "status": "info" if (body.get("level") or "info") == "info" else "open",
                        "people": [it["name"]], "chats": [f"朋友圈·{it['name']}"],
                        "evidence": [[it["time"], it["name"], text[:60], "朋友圈"]], "tags": ["朋友圈", "手动"],
                        "query": {"chats": [f"朋友圈·{it['name']}"], "keywords": [], "start": it["time"][:10], "end": it["time"][:10]}}
                added, _, _ = fastupdate.apply_patches([{"new": [card]}], "", set(build.CATEGORIES), set(build.THREADS),
                                                       lambda *_: None, advance=False)
                r = subprocess.run([PY_VENV, os.path.join(CODE, "build.py")], capture_output=True, text=True, cwd=ROOT)
                return self._send(200, {"ok": r.returncode == 0, "added": added})
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": str(e)})
        if path in ("/api/update", "/api/ask"):
            with JOB_LOCK:
                if running_job():
                    return self._send(409, {"error": "已有任务在运行", "job": running_job().view()})
                if path == "/api/update":
                    job = Job("update", "更新数据", ["抽取聊天", "Claude 提炼卡片", "校验并生成"])
                    target = job_update
                else:
                    job = Job("ask", "问 Claude", ["取原始消息", "Claude 分析", "更新卡片"])
                    target = job_ask
            threading.Thread(target=target, args=(job, body), daemon=True).start()
            return self._send(200, job.view())
        if path == "/api/claude-check":
            threading.Thread(target=probe_claude, daemon=True).start()
            return self._send(200, {"ok": True})
        if path == "/api/cancel":
            j = running_job()
            for p in ([j.proc] + j.procs) if j else []:
                if p and p.poll() is None:
                    p.kill()
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})


def idle_watch(srv):
    while True:
        time.sleep(300)
        if time.time() - last_hit > IDLE_EXIT and not running_job():
            srv.shutdown()
            return


def main():
    seed_data_dir()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    threading.Thread(target=idle_watch, args=(srv,), daemon=True).start()
    threading.Thread(target=probe_claude, daemon=True).start()
    print(f"微信洞察服务已启动：http://{HOST}:{PORT}/", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
