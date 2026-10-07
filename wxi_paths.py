"""微信洞察 · 路径与个人配置

程序目录（CODE）和数据目录（DATA）分开：
- 自用：两者都是 ~/wechat-insights，和以前完全一样。
- 打包给朋友的 App：程序在 App 里（只读），数据放在 WXI_HOME（~/Library/Application Support/微信洞察）。

分类、线索、「我是谁」这些个人口径放在 DATA/config.json，没写就用下面的通用默认值。
"""

import json
import os

CODE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.expanduser(os.environ.get("WXI_HOME") or CODE)
CARDS = os.path.join(DATA, "cards")
RAW = os.path.join(DATA, "raw")
PACKAGED = bool(os.environ.get("WXI_HOME"))


def data(*parts):
    return os.path.join(DATA, *parts)


def load_cfg():
    path = data("config.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def save_cfg(cfg):
    with open(data("config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


DEFAULT_CATEGORIES = {
    "legal":    {"name": "法务·纠纷",   "color": "#c2410c"},
    "company":  {"name": "公司·财税",   "color": "#b45309"},
    "security": {"name": "安全·风险",   "color": "#dc2626"},
    "venture":  {"name": "创业·项目",   "color": "#7c3aed"},
    "invest":   {"name": "投资·理财",   "color": "#0891b2"},
    "work":     {"name": "工作·职业",   "color": "#475569"},
    "interest": {"name": "兴趣·圈子",   "color": "#4f46e5"},
    "family":   {"name": "家庭",        "color": "#16a34a"},
    "people":   {"name": "人情·朋友",   "color": "#0d9488"},
    "health":   {"name": "健康",        "color": "#65a30d"},
    "life":     {"name": "生活·琐事",   "color": "#64748b"},
}
# 这两个分类是程序自己用的，个人配置里写不写都会有
BUILTIN_CATEGORIES = {
    "inbox":    {"name": "消息提醒",    "color": "#0ea5e9"},
    "private":  {"name": "私密",        "color": "#6b7280"},
}
DEFAULT_THREADS = {
    "work":     "工作",
    "venture":  "项目·合作",
    "money":    "钱款往来",
    "family":   "家庭安排",
    "people":   "人情往来",
    "health":   "健康",
    "security": "账号与安全",
    "life":     "生活琐事",
}


def categories(cfg=None):
    cfg = load_cfg() if cfg is None else cfg
    cats = dict(cfg.get("categories") or DEFAULT_CATEGORIES)
    cats.update(BUILTIN_CATEGORIES)
    return cats


def threads(cfg=None):
    cfg = load_cfg() if cfg is None else cfg
    return dict(cfg.get("threads") or DEFAULT_THREADS)


def persona(cfg=None):
    """给 AI 的「用户是谁」说明，和需要特别注意的个人口径。"""
    cfg = load_cfg() if cfg is None else cfg
    who = cfg.get("persona") or "用户本人（聊天里写作 me）"
    notes = cfg.get("persona_notes") or []
    return who, notes
