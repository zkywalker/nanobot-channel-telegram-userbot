# nanobot-channel-telegram-userbot

nanobot 的 Telegram Userbot 插件，使用 [Telethon](https://github.com/LonamiWebs/Telethon) 通过 MTProto 协议以**用户账号**（非 Bot）接入 Telegram。

## 与 Bot API 的区别

| | Bot API (内置) | Userbot (本插件) |
|---|---|---|
| 账号类型 | Bot 账号 | 普通用户账号 |
| 协议 | HTTP Bot API | MTProto |
| 消息历史 | 无法获取 | 可获取任意聊天记录 |
| 已读回执 | 不支持 | 支持 |
| Emoji 回应 | 不支持 | 支持 |
| 消息转发/删除 | 受限 | 完全支持 |
| 定时消息 | 不支持 | 支持 |
| 群组搜索 | 不支持 | 支持 |

> **Warning**
> 使用用户账号进行自动化可能违反 Telegram 服务条款，建议使用专用小号。

## 安装

### 前置要求

- Python 3.10+
- 已安装 [nanobot](https://github.com/HKUDS/nanobot)（需使用[支持插件 channel 的 fork](https://github.com/zkywalker/nanobot)）
- Telegram API 凭据（从 https://my.telegram.org 获取 `api_id` 和 `api_hash`）

### 1. 克隆本仓库

```bash
cd /path/to/your/projects
git clone https://github.com/zkywalker/nanobot-channel-telegram-userbot.git
```

### 2. 运行安装脚本

```bash
cd nanobot-channel-telegram-userbot
python install.py
```

安装脚本会自动完成：
- 安装 Python 依赖（`telethon`、`pysocks`）到 nanobot 的运行环境中
- 自动检测 nanobot 的安装位置（支持 `uv tool`、`pip`、editable install）
- 在 nanobot 的 `channels/` 目录下创建 symlink

> 也可以手动指定 nanobot 源码目录：`python install.py --nanobot-dir /path/to/nanobot`

### 3. 首次认证

```bash
python auth.py --api-id YOUR_API_ID --phone +8613800138000

# 会提示输入 api_hash（隐藏输入）和验证码
# session 文件保存到 ~/.nanobot/nanobot_userbot.session
```

> auth.py 会自动检测系统代理环境变量（`all_proxy`、`https_proxy`、`http_proxy`），也可以手动指定：
> ```bash
> python auth.py --api-id 12345 --phone +8613800138000 --proxy socks5://127.0.0.1:1080
> ```

其他可选参数：

```bash
# 自定义 session 名
python auth.py --api-id 12345 --phone +8613800138000 --session my_account

# 导出 StringSession（适合 Docker / 无状态部署）
python auth.py --api-id 12345 --phone +8613800138000 --export-string
```

### 4. 配置 nanobot

在 `~/.nanobot/config.json` 中添加：

```json
{
  "channels": {
    "telegramUserbot": {
      "enabled": true,
      "apiId": 12345678,
      "apiHash": "your_api_hash_here",
      "sessionName": "nanobot_userbot",
      "allowFrom": ["*"]
    }
  }
}
```

### 5. 启动

```bash
nanobot gateway
```

## 配置项

| JSON Key | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | `false` | 是否启用 |
| `apiId` | int | `0` | Telegram API ID |
| `apiHash` | string | `""` | Telegram API Hash |
| `sessionName` | string | `"nanobot_userbot"` | SQLite session 文件名（不含 .session） |
| `sessionString` | string | `""` | 预认证的 StringSession（替代文件方式） |
| `phone` | string | `""` | 手机号（交互式登录用） |
| `proxy` | string | `null` | 代理 URL，如 `socks5://127.0.0.1:1080` |
| `allowFrom` | list | `[]` | 允许的用户 ID 或用户名，`["*"]` 允许所有人 |
| `groupPolicy` | string | `"mention"` | 群组策略：`"mention"` 需要 @提及，`"open"` 回复所有消息 |
| `replyToMessage` | bool | `false` | 是否引用原消息回复 |
| `reactionEmoji` | string | `""` | 收到消息时的 emoji 回应，如 `"👀"`，留空禁用 |
| `autoDisclosure` | string | `""` | 附加到每条回复末尾的文字，如 `"[AI]"` |

## 卸载

```bash
python uninstall.py
```

仅移除 symlink，不修改任何文件。`~/.nanobot/` 下的 session 文件会保留。

## 架构

```
nanobot-channel-telegram-userbot/
├── channel/
│   ├── telegram_userbot.py  # TelegramUserbotChannel + TelegramUserbotConfig
│   └── utils.py             # 独立工具函数（可被其他项目复用）
├── install.py               # 安装（依赖 + symlink）
├── uninstall.py             # 移除 symlink
├── auth.py                  # 认证工具
└── requirements.txt
```

**无侵入设计**：插件自带 `TelegramUserbotConfig`，通过 nanobot 的插件 channel 机制（`ChannelsConfig(extra="allow")` + `config_class`）加载，不需要修改 nanobot 的 `schema.py`。

## License

MIT
