#!/bin/bash
# 微信洞察 · 设置 Claude 长期令牌（一年有效，不需要反复登录）
# 用法：bash setup_token.sh        设置 / 更换令牌
#       bash setup_token.sh --remove  删除令牌，改回普通登录
set -e
SERVICE="wechat-insights-claude"; ACCOUNT="oauth-token"

if [ "$1" = "--remove" ]; then
  security delete-generic-password -s "$SERVICE" -a "$ACCOUNT" >/dev/null 2>&1 && echo "已删除长期令牌。" || echo "本来就没有设置长期令牌。"
  exit 0
fi

# 绕开 shell 里可能设置的 ANTHROPIC_* 网关变量；国内要走本机代理，否则 Anthropic 会返回 403
unset ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN ANTHROPIC_API_KEY
PROXY_HOST=$(scutil --proxy | awk '/HTTPSProxy/ {print $3}')
PROXY_PORT=$(scutil --proxy | awk '/HTTPSPort/ {print $3}')
if [ -n "$PROXY_HOST" ] && [ -n "$PROXY_PORT" ]; then
  export HTTPS_PROXY="http://$PROXY_HOST:$PROXY_PORT" HTTP_PROXY="http://$PROXY_HOST:$PROXY_PORT"
  echo "使用代理 $HTTPS_PROXY"
else
  echo "⚠️  没检测到系统代理。如果下面登录报 403，请先打开 Clash 再重试。"
fi

CLAUDE=$(command -v claude || ls "$HOME/.claude/local/claude" 2>/dev/null || echo claude)
echo
echo "第 1 步：浏览器会打开 Claude 授权页，用你的 Claude 订阅账号登录并授权。"
echo "        完成后，终端会显示一串以 sk-ant-oat 开头的长令牌，把它完整复制下来。"
echo
"$CLAUDE" setup-token
echo
echo "第 2 步：把刚才复制的令牌粘贴到下面，然后按回车（输入时不会显示，这是正常的）。"
read -r -s -p "令牌：" TOKEN; echo
TOKEN=$(echo "$TOKEN" | tr -d '[:space:]')
case "$TOKEN" in
  sk-ant-*) ;;
  *) echo "❌ 看起来不是有效的令牌（应以 sk-ant- 开头），没有保存。"; exit 1 ;;
esac
security add-generic-password -U -s "$SERVICE" -a "$ACCOUNT" -w "$TOKEN"
unset TOKEN
echo "✅ 已存入 macOS 钥匙串（$SERVICE）。微信洞察之后会直接使用它，一年内不需要再登录。"
echo "   回到页面点「重新检测登录」即可。"
