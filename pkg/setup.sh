#!/bin/bash
# 微信洞察 · 首次设置（纯本机操作，数据不出电脑）
# 做三件事：① 给微信加调试签名（读本机微信数据的前提，必要时弹一次管理员密码）
#          ② 从运行中的微信里读出聊天库密钥（不需要密码）③ 建好数据目录
set -u
RES="$1"
PY="$RES/py/bin/python3.12"
CODE="$RES/app"
KT="$RES/keytools"
WECHAT="/Applications/WeChat.app"
KEYS="$HOME/.wechat-cli/all_keys.json"
export WXI_HOME="$HOME/Library/Application Support/微信洞察"
mkdir -p "$WXI_HOME" "$HOME/.wechat-cli"
TITLE="微信洞察 · 首次设置"

# —— osascript 小工具 ——
info(){ # 文案; 按钮(默认"继续"); 返回被点按钮名
  osascript -e "button returned of (display dialog \"$1\" buttons {${2:-\"取消\",\"继续\"}} default button (count {${2:-\"取消\",\"继续\"}}) with title \"$TITLE\" with icon note)" 2>/dev/null
}
alert(){ osascript -e "display dialog \"$1\" buttons {\"知道了\"} default button 1 with title \"$TITLE\" with icon caution" >/dev/null 2>&1; }
has_gta(){ codesign -d --entitlements - "$WECHAT" 2>/dev/null | grep -q get-task-allow; }

# 0. 欢迎
b=$(info "欢迎使用「微信洞察」。\n\n它会读取你本机电脑版微信的聊天和朋友圈，整理成卡片，帮你盯住重要的事。\n\n• 所有数据都留在这台电脑，默认不上传、不用联网。\n• 接下来需要：给微信加一个「调试签名」（可能弹一次管理员密码），再从微信里读出聊天库的密钥。\n\n请先确认：电脑版微信已经安装、已登录，并停在聊天界面。")
[ "$b" = "继续" ] || exit 1

# 1. 微信在不在
if [ ! -d "$WECHAT" ]; then
  alert "没找到「/Applications/WeChat.app」。\n请先从微信官网下载安装 Mac 版微信并登录，再重新打开本应用。"
  exit 1
fi

# 2. 调试签名
if ! has_gta; then
  b=$(info "需要先给微信加「调试签名」。\n这一步会：退出微信 → 重新签名 → 重新打开微信。\n你的聊天记录不会被改动。\n\n点「继续」后，微信会关闭再自动打开，期间可能需要输入一次电脑的管理员密码。")
  [ "$b" = "继续" ] || exit 1
  osascript -e 'quit app "WeChat"' >/dev/null 2>&1
  for i in $(seq 1 30); do pgrep -x WeChat >/dev/null || break; sleep 1; done
  if pgrep -x WeChat >/dev/null; then alert "微信没能退出，请手动按 ⌘Q 完全退出微信后重试。"; exit 1; fi
  ENT="$(mktemp /tmp/wxi_ent.XXXX).plist"
  codesign -d --entitlements :- "$WECHAT" > "$ENT" 2>/dev/null
  [ -s "$ENT" ] || printf '%s' '<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd"><plist version="1.0"><dict/></plist>' > "$ENT"
  /usr/libexec/PlistBuddy -c "Add :com.apple.security.get-task-allow bool true" "$ENT" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Set :com.apple.security.get-task-allow true" "$ENT" >/dev/null 2>&1
  SIGN="codesign --force --sign - --entitlements '$ENT' '$WECHAT'"
  # 先按普通用户签（多数人自己装的微信属于自己）；失败再用管理员
  if ! eval "$SIGN" >/dev/null 2>&1; then
    osascript -e "do shell script \"$SIGN\" with administrator privileges" >/dev/null 2>&1
  fi
  rm -f "$ENT"
  if ! has_gta; then alert "调试签名没有成功。可能微信正在运行或权限不足。\n请完全退出微信后重试，或把微信拖到「应用程序」里再试。"; exit 1; fi
  open -a "$WECHAT"
  b=$(info "微信已重新打开。\n请在微信里完成登录、进入聊天列表后，再点「继续」。\n（如果提示重新扫码登录，扫一下即可。）")
  [ "$b" = "继续" ] || exit 1
else
  # 已有签名但进程可能没带调试权限：确保微信在运行
  pgrep -x WeChat >/dev/null || open -a "$WECHAT"
fi

# 3. 读密钥（不需要密码，约 1–2 分钟）
b=$(info "马上开始读取聊天库密钥，大约需要 1–2 分钟。\n期间请保持微信开着、不要退出。\n\n点「继续」开始。")
[ "$b" = "继续" ] || exit 1
"$PY" "$KT/extract_keys_macos.py" > "$WXI_HOME/setup.log" 2>&1
N=$("$PY" - "$KEYS" <<'PY'
import json,sys
try: print(len(json.load(open(sys.argv[1]))))
except Exception: print(0)
PY
)
if [ "${N:-0}" -eq 0 ]; then
  alert "没能读到密钥（匹配 0 个）。\n常见原因：微信没登录、刚才没点进聊天界面，或你的微信版本和本工具不匹配。\n\n可以：确认微信已登录并停在聊天界面后，重新打开本应用再试一次。\n详细日志：~/Library/Application Support/微信洞察/setup.log"
  exit 1
fi

# 4. 初始化数据目录（通用配置，默认不用 AI）
"$PY" - "$WXI_HOME" <<'PY'
import json, os, sys
home = sys.argv[1]
cfg_path = os.path.join(home, "config.json")
if not os.path.exists(cfg_path):
    cfg = {
        "my_aliases": ["me"],
        "skip_chats": [], "sns": True, "sns_skip": [], "inbox_skip": [],
        "vip_chats": [], "max_per_chat": 400,
        "ai_provider": "none",
        "persona": "本工具的使用者（聊天里写作 me）",
        "persona_notes": [],
    }
    json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY

alert "设置完成！读到 $N 个聊天库密钥。\n\n接下来会打开「微信洞察」页面。第一次是空的，点右上角「⟳ 更新数据」就能把最近的聊天和朋友圈整理成卡片。\n\n默认不使用 AI（只做待回复/@我/朋友圈关键词提醒），完全离线。想要更聪明的提炼，可以在更新窗口里选 Claude（需自己登录）或本地模型。"
exit 0
