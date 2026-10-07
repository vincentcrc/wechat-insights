#!/bin/bash
# 微信洞察 · 打包成可发给朋友的 Mac 安装包（.pkg）
# 用法：bash ~/wechat-insights/build_pkg.sh
# 产物：~/wechat-insights/dist/微信洞察-<版本>.pkg
#
# 打进包里的只有「程序」：Python 运行环境 + 本工具代码 + 取密钥脚本。
# 不含任何个人数据：你的聊天、卡片、Claude 令牌都不会进包。
# 朋友装好后第一次打开会自己读取他们本机的微信、在他们电脑上生成数据。
set -euo pipefail

VERSION="${1:-1.0}"
CODE="$HOME/wechat-insights"
UVPY="$(/usr/bin/python3 -c "import os;print(os.path.realpath(os.path.expanduser('~/.local/share/uv/python/cpython-3.12-macos-aarch64-none')))")"
VENV_SP="$HOME/.wechat-cli/venv/lib/python3.12/site-packages"
KEYTOOLS="$HOME/.claude/skills/wechat-cli/scripts"
ICON="$CODE/pkg/icon.icns"

STAGE="$(mktemp -d /tmp/wxi-pkg.XXXX)"
APP="$STAGE/root/Applications/微信洞察.app"
RES="$APP/Contents/Resources"
DIST="$CODE/dist"
mkdir -p "$RES/app" "$RES/keytools" "$APP/Contents/MacOS" "$STAGE/scripts" "$DIST"

echo "[1/6] 拷贝 Python 运行环境（arm64）"
[ -x "$UVPY/bin/python3.12" ] || { echo "找不到打包用的 Python：$UVPY"; exit 1; }
cp -R "$UVPY" "$RES/py"
cp -R "$VENV_SP/." "$RES/py/lib/python3.12/site-packages/"
# 瘦身：删缓存、测试、无用的大件
find "$RES/py" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$RES/py" -name "*.pyc" -delete 2>/dev/null || true
rm -rf "$RES/py/lib/python3.12/test" "$RES/py/lib/python3.12/idlelib" \
       "$RES/py/lib/python3.12/tkinter" "$RES/py/lib/python3.12/turtledemo" \
       "$RES/py/lib/python3.12/site-packages/numpy"* 2>/dev/null || true

echo "[2/6] 拷贝工具代码（不含个人数据）"
cp "$CODE/server.py" "$CODE/extract.py" "$CODE/fastupdate.py" "$CODE/build.py" \
   "$CODE/rules.py" "$CODE/wxi_paths.py" "$CODE/CLAUDE.md" "$CODE/index.html" "$RES/app/"
cp "$KEYTOOLS/machmem.py" "$KEYTOOLS/extract_keys_macos.py" "$RES/keytools/"
[ -f "$ICON" ] && cp "$ICON" "$RES/icon.icns" || true

echo "[3/6] 写入启动器与首次引导"
cp "$CODE/pkg/微信洞察.launcher" "$APP/Contents/MacOS/微信洞察"
cp "$CODE/pkg/setup.sh" "$RES/setup.sh"
chmod +x "$APP/Contents/MacOS/微信洞察" "$RES/setup.sh" "$RES/py/bin/"* 2>/dev/null || true

echo "[4/6] 写入 Info.plist"
cat > "$APP/Contents/PkgInfo" <<< "APPL????"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>微信洞察</string>
  <key>CFBundleDisplayName</key><string>微信洞察</string>
  <key>CFBundleIdentifier</key><string>io.github.wechat-insights</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleSignature</key><string>????</string>
  <key>CFBundleExecutable</key><string>微信洞察</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST

echo "[5/6] 写入安装脚本（去隔离标记）"
cat > "$STAGE/scripts/postinstall" <<'POST'
#!/bin/bash
APP="/Applications/微信洞察.app"
/usr/bin/xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true
chmod +x "$APP/Contents/MacOS/微信洞察" "$APP/Contents/Resources/setup.sh" 2>/dev/null || true
chmod +x "$APP/Contents/Resources/py/bin/"* 2>/dev/null || true
exit 0
POST
chmod +x "$STAGE/scripts/postinstall"

echo "[6/6] 打包"
OUT="$DIST/微信洞察-$VERSION.pkg"
pkgbuild --root "$STAGE/root" --identifier io.github.wechat-insights \
  --version "$VERSION" --install-location / --scripts "$STAGE/scripts" "$OUT" >/dev/null
rm -rf "$STAGE"
echo
echo "✅ 完成：$OUT"
du -sh "$OUT"
echo "把这个 .pkg 发给朋友即可。第一次打开 App 若被拦，右键 App →「打开」。"
