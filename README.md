# astrbot_plugin_wekit

让 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 接管**安卓端微信** —— 通过 [WeKit(wcx)](https://github.com/Johnny520/wcx) 暴露的 REST API 收发消息。

WeKit 是一个基于 Xposed 的微信增强模块，自带「API + MCP 服务器」功能，对外提供 HTTP 接口调用微信能力。本插件把它封装成一个 AstrBot 平台适配器。

> ⚠️ **风险提示**：Xposed 注入微信属于对抗性方案，有封号风险。请使用备用机 + 小号，不要用于主力账号。

## 工作模式：主动轮询

```
AstrBot（VPS / 任意能连到手机的机器）
  ├─ 轮询  GET  {base}/api/conversations/{conv_id}/history   → 发现新消息 → 交给 LLM
  └─ 发送  POST {base}/api/messages/text                     → 回复进群
手机（root + LSPosed + wcx，开「API + MCP 服务器」）
```

**前提**：AstrBot 所在机器**能访问到手机**的 API（同局域网 / 端口映射 / 内网穿透）。手机端只需打开开关，不需要写任何脚本。

## 手机端准备

1. 安装 wcx，在 LSPosed 里把作用域勾选为微信
2. 打开 **wcx → 系统与隐私 → 「API + MCP 服务器」** 开关
   （会 Toast：`REST API 服务器启动于 http://0.0.0.0:30010/api`）
3. 记下并修改 `api_auth_token`
4. 让该地址可以从 AstrBot 机器访问（端口映射 / frp / WireGuard 等）

## 插件部署

1. 将本目录放到 AstrBot 的 `data/plugins/astrbot_plugin_wekit/`
2. WebUI → 平台 → 添加「WeKit 微信适配器」，填写：
   - `base_url`：手机 API 地址，如 `http://127.0.0.1:30010`
   - `token`：手机上设置的 `api_auth_token`
   - `conv_ids`：要接管的会话，逗号分隔。群为 `xxx@chatroom`，私聊为 `wxid_xxx`
   - `poll_interval`：轮询间隔秒数（默认 5）
   - `page_size`：每次拉取条数（默认 10，需大于轮询间隔内可能新增的消息数）
3. 重启 AstrBot，日志出现 `启动轮询` 和 `基线建立` 即正常

**启动前的历史消息不会补发** —— 插件启动时会先拉一次建立基线。

## 消息格式

**收到**：
```json
{"sender": "成员昵称", "content": "正文", "type": "text"}
```
- 群消息已拆好昵称；自己发的消息 `sender` 为 `<myself>`，会被过滤不上报
- `type` 可能是 `text` / `quote` 等，非文本类型的 `content` 是原始 XML 或 `<type:image>` 这类占位符

**发送**：
```
POST {base}/api/messages/text
{"type": "text", "convId": "<会话ID>", "content": "回复内容"}
```

## 过滤机制：交给 AstrBot，不在插件里做

插件**原样上报所有消息**，不自己做白名单 / 唤醒 / 限流判断。这些由 AstrBot 的消息流水线统一处理，配置都在 WebUI → 平台设置：

| 需求 | 配置项 |
|---|---|
| 只让指定群生效 | `enable_id_white_list` + `id_whitelist` |
| 管理员豁免白名单 | `wl_ignore_admin_on_group` / `wl_ignore_admin_on_friend` |
| 唤醒词（群里被 @ 或点名才回） | `wake_prefix` |
| 私聊也要唤醒词 | `friend_message_needs_wake_prefix` |
| 回复频率限制 | `rate_limit` / `rate_limit_strategy` |

这也是本插件天然支持多会话的原因 —— `conv_ids` 配多个，白名单里放行哪些就生效哪些。

## 已知限制

WeKit 的 `history` 接口**不返回 `msgSvrId`**，由此带来三个限制：

- **无法引用回复** —— 只能发纯文本，回复不带被引用消息，群里有经验的人能看出是机器人
- **看不到图片内容** —— 图片消息的 `content` 是 `<type:image>` 占位符，LLM 拿不到图
- **连发相同内容会漏一条** —— 去重用 `sender|content|type` 的 MD5 内容指纹，同一人连续发两条完全相同的文本，第二条会被判为重复而丢弃

要突破这三个限制，需要给 wcx 加 webhook 推送（在 `onMessage` 钩子里主动 POST 给 AstrBot），那样能同时拿到 `msgSvrId`，引用回复和看图说话都能解锁。

其他注意：

- 手机必须常亮联网、微信在后台且 wcx 模块生效
- 轮询有延迟（默认 5 秒），且断网期间的消息会被基线下一次拉取时补齐

## 常见问题

**日志报 `400 Invalid auth header`**
token 含非法字符。ktor 的 bearer 认证器只接受 RFC 7235 token 字符（`A-Z a-z 0-9 - _ . ~`），`?` `!` `%` `&` 等都会让它直接返 400，**根本走不到密码比对**。改一个只用合法字符的 token。

**日志报 `401`**
token 填错了，或者手机上改了 `api_auth_token` 但插件这边没同步。

**日志报 `history xxx 返回 404`**
`conv_ids` 里的会话 ID 不对。可以先 `GET /api/conversations/current` 拿到微信当前打开会话的 ID 作参考。

**日志一直 `轮询异常: Cannot connect to host`**
网络不通。先在手提/服务器上 `curl -H "Authorization: Bearer <token>" http://<地址>/api/self/info` 验证链路。

## 目录结构

```
astrbot_plugin_wekit/
├── main.py             # 插件入口，Star 子类（触发平台注册）
├── wekit_adapter.py    # 平台适配器：轮询收消息 + 发送
├── wekit_event.py      # 消息事件类：转发 send 到适配器
└── _conf_schema.json   # WebUI 表单元数据
```

## 兼容性

- AstrBot v4.29.0-beta.1 实测通过
- 需要 `aiohttp`（AstrBot 已内置）

## 致谢

- 原始轮询版适配器由社区作者「阿福」编写，本仓库在其基础上修复了若干无法加载的问题并补充文档
- 参考了 AstrBot 内置 `webchat` / `weixin_oc` 适配器的写法

## License

MIT
