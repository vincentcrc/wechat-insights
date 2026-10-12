#!/bin/bash
# 微信洞察 · 设置 DeepSeek API Key（存进 macOS 钥匙串，不写进任何文件）
# 用法：bash setup_deepseek.sh          设置 / 更换 Key
#       bash setup_deepseek.sh --remove 删除 Key
set -e
SERVICE="wechat-insights-deepseek"; ACCOUNT="api-key"

if [ "$1" = "--remove" ]; then
  security delete-generic-password -s "$SERVICE" -a "$ACCOUNT" >/dev/null 2>&1 && echo "已删除 DeepSeek Key。" || echo "本来就没设置。"
  exit 0
fi

echo "在 https://platform.deepseek.com 申请 API Key（以 sk- 开头）。"
read -r -s -p "粘贴 DeepSeek API Key（输入时不显示）：" KEY; echo
KEY=$(echo "$KEY" | tr -d '[:space:]')
case "$KEY" in
  sk-*) ;;
  *) echo "❌ 看起来不是有效的 Key（应以 sk- 开头），没有保存。"; exit 1 ;;
esac
security add-generic-password -U -s "$SERVICE" -a "$ACCOUNT" -w "$KEY"
unset KEY
echo "✅ 已存入 macOS 钥匙串（$SERVICE）。回到页面的更新窗口选「DeepSeek」即可。"
