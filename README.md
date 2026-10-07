# 微信洞察 · WeChat Insights

> 把本机 Mac 版微信的聊天和朋友圈，整理成按 **日 / 周 / 月** 归档的交互式卡片：哪些事要跟进、哪些是重大事项、谁还在等你回复，一眼看清。
>
> *Turn your local macOS WeChat chats and Moments into day/week/month insight cards — runs entirely on your Mac.*

**数据只在你自己的电脑上处理。** 默认不使用 AI、不联网；AI 提炼是可选的（Claude / 本地模型），开启后才会把相关聊天片段发给你选择的模型。

## 下载安装

| 平台 | 状态 | 下载 |
|---|---|---|
| **macOS · Apple 芯片（M 系列）** | ✅ 可用 | [最新 Release](https://github.com/vincentcrc/wechat-insights/releases/latest) 里的 `.pkg` |
| macOS · Intel 芯片 | 🚧 计划中 | 见下方「平台支持」 |
| Windows | 🚧 计划中 | 见下方「平台支持」 |

下载 `.pkg` 后双击安装；第一次打开若被 Gatekeeper 拦（“来自身份不明的开发者”），右键点 App →「打开」。首次打开会图形化引导：给微信加调试签名（必要时弹一次管理员密码）→ 读取聊天库密钥 → 建好数据目录。也可以[从源码运行](#快速开始从源码运行)。

---

## 功能

- **卡片**：把聊天提炼成卡片，分三档——重大 / 需关注 / 参考；每张卡有摘要、下一步行动、原话证据、相关会话。
- **周期视图**：日 / 周 / 月 / 全部切换；时间线、线索（同一件事的来龙去脉）、已完成（按完成时间倒序，可撤销）。
- **📨 待回复与 @我**：私聊里对方最后发言、群里有人 @你，自动生成提醒；你回复后自动消失。不需要 AI。
- **朋友圈**：按周期浏览好友动态，筛选「值得看」（换工作、结婚、开业、求助……）、隐藏广告、按好友过滤，一键做成卡片。
- **页面内操作**：直接在页面里拉取最新数据、查看原始消息、向 Claude 提问。
- **提炼方式三选一**：
  - `不用 AI`（默认）：几秒完成，只做规则提醒，完全离线。
  - `Claude`：提炼最准；需要你自己的 Claude 账号和 [Claude Code](https://claude.com/claude-code) 命令行。
  - `本地模型`：通过 [Ollama](https://ollama.com) 运行，完全离线。

## 运行要求

- macOS 12+，**Apple 芯片（M 系列）**
- Mac 版微信 **4.1.x**（已登录）
- 依赖 [wechat-cli](https://github.com/freestylefly/wechat-cli)（Apache-2.0）读取并解密本机微信数据库；需要先按它的说明拿到本机数据库密钥。

## 平台支持

| 平台 | 状态 | 说明 |
|---|---|---|
| macOS · Apple Silicon | ✅ 可用 | 开发和验证都在此平台 |
| macOS · Intel (x86_64) | 🚧 计划中 | 需要重新打包一套 x86_64 的 Python，并针对 Intel 版微信重新校准读取密钥的那一步；缺一台 Intel Mac 来构建和验证，欢迎有条件的朋友 PR 或提供测试。 |
| Windows | 🚧 计划中 | 不是简单重打包：读取密钥、授权、首次引导和安装包格式都需要换成 Windows 的实现（底层 `wechat-cli` 已支持 Windows），需要在 Windows 上开发和测试。欢迎 PR。 |

> 目前只在 Apple Silicon + 微信 4.1.x 上验证过。其它芯片或差异较大的微信版本可能读不到密钥。

## 快速开始（从源码运行）

```bash
git clone https://github.com/vincentcrc/wechat-insights.git
cd wechat-insights
cp config.example.json config.json      # 按需修改

# 用装了 wechat_cli 的 Python 启动本地服务（只监听 127.0.0.1）
~/.wechat-cli/venv/bin/python server.py
# 浏览器打开 http://127.0.0.1:8765/ ，点右上角「⟳ 更新数据」
```

命令行抽取（可选）：

```bash
~/.wechat-cli/venv/bin/python extract.py --period week          # 本周
~/.wechat-cli/venv/bin/python extract.py --since "2026-10-01 09:00"
python3 build.py                                                 # 校验卡片并生成 data.js
```

## 打包成 Mac 安装包（.pkg）

```bash
bash build_pkg.sh            # 产物：dist/微信洞察-1.0.pkg
```

安装包自带 Python 运行环境，安装后首次打开会图形化引导：给微信加调试签名（必要时弹一次管理员密码）→ 读取聊天库密钥 → 建好数据目录。数据放在 `~/Library/Application Support/微信洞察`，安装包里不含任何个人数据。

> 打包需要本机已装好 wechat-cli 及其 macOS 取密钥脚本（本仓库不包含这部分代码）。安装包未经 Apple 公证，首次打开需右键 →「打开」。

## 目录结构

| 文件 | 作用 |
|---|---|
| `server.py` | 本地服务（127.0.0.1，带 token 校验）：页面、拉数据、问 Claude |
| `extract.py` | 从微信数据库按周期抽取聊天和朋友圈，生成 digest |
| `fastupdate.py` | 快速提炼：一次调用返回 JSON 补丁，分块并行 |
| `rules.py` | 不用 AI 的规则提醒（待回复 / @我 / 朋友圈关键词） |
| `build.py` | 校验 `cards/*.json` 并生成 `data.js` |
| `wxi_paths.py` | 程序目录 / 数据目录分离，读取个人配置 |
| `index.html` | 单页应用（无构建、无依赖） |
| `CLAUDE.md` | 给 Claude 的卡片编写规则（深度模式） |
| `pkg/` · `build_pkg.sh` | 安装包的启动器、首次引导和打包脚本 |
| `setup_token.sh` | 把 Claude 长期令牌存进 macOS 钥匙串（可选） |

## 个人配置（config.json）

| 字段 | 说明 |
|---|---|
| `persona` / `persona_notes` | 告诉 AI「你是谁」以及需要特别注意的口径 |
| `categories` / `threads` | 自定义卡片分类和线索；不写就用通用默认值 |
| `inbox_skip` / `sns_skip` / `skip_chats` | 不生成提醒 / 不读朋友圈 / 不抽取的会话 |
| `vip_chats` | 重要联系人，待回复提醒会提升级别 |
| `ai_provider` | `none` / `claude` / `ollama` |
| `sns_keywords_extra` | 追加朋友圈「值得看」的关键词 |

`config.json`、`cards/`、`raw/`、`state.json`、`data.js` 都是个人数据，已在 `.gitignore` 里排除。

## 隐私与免责声明

- 本项目只用于**查看和整理你自己账号**的聊天数据，请尊重聊天中其他人的隐私，不要用于获取他人数据。
- 读取数据库密钥需要给本机微信加调试签名（ad-hoc 重签），这会改变微信的代码签名；微信自动更新后需重新设置。请自行评估风险。
- 私密、亲密关系类聊天按规则不会做成卡片；你也可以用 `inbox_skip` / `skip_chats` 排除指定会话。
- 本项目与腾讯 / 微信官方无关。

## 致谢

- [wechat-cli](https://github.com/freestylefly/wechat-cli)：本机微信数据查询与解密（基于 [wechat-decrypt](https://github.com/ylytdeng/wechat-decrypt)）。
