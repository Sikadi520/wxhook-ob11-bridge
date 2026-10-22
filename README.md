# 微信 ⇄ OneBot v11 ⇄ AstrBot 桥接

把 **PC 微信小号** 接进 [AstrBot](https://github.com/AstrBotDevs/AstrBot)，让它和 QQ 机器人共用同一个大模型、同一套记忆、同一批插件。

收发全部在微信进程内完成：**不启动模拟器、不开虚拟机、不接管鼠标键盘、不要求微信窗口可见**。桥接本体是一个 Python 进程，开销在几十 MB 量级。

> 这是 [wxhook](https://github.com/miloira/wxhook)（DLL 注入）+ OneBot v11 + AstrBot `aiocqhttp` 适配器 之间的一层胶水，不是微信协议实现。

---

## 版本与登录

### 支持的微信版本

`wxhook` 上游已经覆盖 **3.9.x 到 4.1.x**，最新支持到 4.1.15.56。各版本安装包在这里下载：

- [tom-snow/wechat-windows-versions](https://github.com/tom-snow/wechat-windows-versions/releases) —— 含 [WeChat 3.9.5.81](https://github.com/tom-snow/wechat-windows-versions/releases/download/v3.9.5.81/WeChatSetup-3.9.5.81.exe)
- 本仓库的适配层**实测跑通的是 3.9.5.81**；4.x 理论上可用，未验证，欢迎贴日志

**装完第一件事：关掉自动升级。** 3.9.x 会自己升到 4.x，升上去就白装了：

1. 登录后 → 设置 → 通用设置 → 取消「有更新时自动升级微信」
2. 把安装目录里的 `WeChatUpdate.exe` 改名成 `WeChatUpdate.exe.bak`
3. `reg add "HKCU\Software\Tencent\WeChat" /v NeedUpdateType /t reg_dword /d 0 /f`

### 提示「当前客户端版本过低」

腾讯对老客户端加了服务端版本门禁。社区的做法是**在扫码前**给微信进程打一个 `x64 → x32` 的内存补丁，再配合特定的扫码顺序：

1. 把 `config.json` 的 `patch_x64_to_x32` 改成 `true`
2. 启动 `start.bat`，微信会停在扫码界面（补丁在进程启动时打一次）
3. 手机扫码 → **先不要点确认**
4. 再扫一次
5. 点确认登录

补丁的来源是 [alingalingling/Astrbot-wechat-bot-](https://github.com/alingalingling/Astrbot-wechat-bot-) 里的 `wechat_patcher.py`；本仓库的 `patch_audit.py` 是它的审计加强版。

登录成功后**把 `patch_x64_to_x32` 改回 `false`**：补丁只在进程启动时执行一次，之后的会话恢复不需要它，而带着补丁跑大约每小时会崩一次（[设计说明 §7](docs/设计说明.md#7-关于-x64--x32-内存补丁)）。

> 成功率随腾讯策略变化，不是每次都过；过不去就等几天再试。

---

## 架构

```
手机（主号）
    │  微信服务器
    ▼
微信 3.9.5.81（小号，独立进程）
    │  进程内函数调用（不碰键鼠、不占剪贴板）
    ▼
wxhook.dll
    │
    ▼
main.py ─── bridge.py ─── ob11.py      ← 一个 Python 进程
    │  OneBot v11 over WebSocket
    ▼
AstrBot  aiocqhttp 适配器（:11229）
    │
    ▼
大模型 / 插件 / 记忆
```

整条链路只有一跳。对比之下，常见的 `WeFlow → 桥 → OneBot → AstrBot` 至少多两个进程。

---

## 能做什么

| 方向 | 支持 | 备注 |
| --- | :---: | --- |
| 收 · 私聊 / 群聊文本 | ✅ | |
| 收 · 图片 | ✅ | 私聊事件自带 base64；群聊没有，走 `download_attachment` 落盘再读 |
| 收 · 引用回复 | ✅ | `type=49` 拍平成 `[引用 张三:原文]` 再送给模型 |
| 收 · 群 @ | ✅ | 以 `signature.msgsource.atuserlist` 为准，不靠文本猜 |
| 发 · 文本 | ✅ | |
| 发 · 图片 | ✅ | base64 内嵌，单帧上限 64 MiB |
| 发 · 文件 / 音频 | ✅ | `record` 段降级成文件（微信里点开照样能播） |
| 发 · GIF 表情 | ✅ | 走 `sendCustomEmotion`，GIF 才是动的 |
| 小程序 / 链接 / 转账卡片 | ⚠️ | 只记一条日志，不解析 |

**群聊策略**：所有群消息都会推给 AstrBot（供群聊上下文记录「群友在聊什么」），但只有 **@ 了机器人** 的那条才带 `at` 段、才会触发回复。也就是「看得见，不插嘴」。

---

## 快速开始

### 前置条件

- Windows 10 / 11，x64
- 微信 **3.9.5.81**（[下载](https://github.com/tom-snow/wechat-windows-versions/releases/download/v3.9.5.81/WeChatSetup-3.9.5.81.exe)），且有个能正常登录的小号
- 已安装并运行 [AstrBot](https://astrbot.app/)，并在里面建好 `aiocqhttp` 适配器（默认端口 11229）
- [uv](https://docs.astral.sh/uv/)（`start.bat` 用它拉起依赖）

### 步骤

```bat
git clone https://github.com/Sikadi520/wxhook-ob11-bridge.git
cd wxhook-ob11-bridge
copy config.example.json config.json
```

1. **确认微信没有在运行** —— 脚本会自己启动它。
2. **确认 AstrBot 在跑**：`netstat -ano | findstr :11229` 能看到 `LISTENING`。
3. 右键 `start.bat` → 以管理员身份运行（要注入 DLL，必须提权）。
4. 微信窗口自己弹出来，用小号扫码。

### 启动日志应该长这样

| 顺序 | 日志 |
| --- | --- |
| 1 | `正在连接 AstrBot: ws://127.0.0.1:11229/ws` |
| 2 | `已结束残留的 WeChat.exe 进程` / `已重置端口状态文件` |
| 3 | `微信已启动,wxhook.dll 已注入` |
| 4 | `已连接到 AstrBot` |
| 5 | `已登录: 昵称 (wxid_xxx)` |
| 6 | `收到消息 [私聊] 来自 wxid_xxx: ...` |

第 5 行出现之前，`send_text` 也会返回 `code=1`，但消息其实发不出去 —— 所以登录后会在同目录写一个 `ready.json`，外部程序（比如启动致辞插件）可以轮询它。

---

## 配置

`config.json` 由 `config.example.json` 复制而来，字段说明见 [`docs/配置与排障.md`](docs/配置与排障.md)。

最该先改的三个：

```jsonc
{
  "astrbot_ob_url": "ws://127.0.0.1:11229/ws",  // 必须带 /ws
  "allowed_wxids": ["wxid_你的主号"],            // 空 = 谁都能触发,务必填
  "bot_nicknames": ["小助手"]                    // 群里 @ 它时的昵称
}
```

> `allowed_wxids` 留空意味着**任何能给这个小号发消息的人**都能触发 AstrBot。如果你的 AstrBot 开了 shell / python 工具，等于把电脑交出去。详见 [`docs/安全与风险.md`](docs/安全与风险.md)。

---

## 常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| 提示「当前客户端版本过低」 | 服务端门禁。按 [版本与登录](#提示当前客户端版本过低) 的补丁 + 扫码顺序处理 |
| 群里 `/签到` 之类指令没反应 | `bot_nicknames` 没配，@ 前缀没被剥掉。见 [`docs/设计说明.md`](docs/设计说明.md#1-群里的--分隔符不是空格) |
| 群里 @ 别人它也回 | 老版本靠文本猜。本仓库用 `atuserlist` 判定，确认你在用最新代码 |
| `WinError 10043` / 一条消息都收不到 | 上次的注入进程占着 18999，`main.py` 启动时会自动清理 |
| 图片发出去了但是不下载 / 模型 400 | 大图分块落盘，抓到半截文件。本仓库按精确文件名 + 文件头校验 |
| 文件发出去显示「发送中断」 | 源文件被提前删了。`send_file_keep_seconds` 控制延迟删除 |
| GIF 发出去不动 | 走了图片通道。`gif_as_emotion` 要开 |
| 连发好几条回复 | `buffer_seconds` 调大 |

更多见 [`docs/配置与排障.md`](docs/配置与排障.md)。

---

## 微信 4.x

上游 `miloira/wxhook` 已支持到 **4.1.15.56**，但 PyPI 上的 `0.0.10` 只认 3.9.5.81。想上 4.x，把依赖换成 GitHub 版：

```bat
uv run --with "wxhook @ git+https://github.com/miloira/wxhook" --with websockets python main.py
```

或者直接写进 `requirements.txt`：

```text
wxhook @ git+https://github.com/miloira/wxhook
```

本仓库的适配层是按 `0.0.10` 的 API 写的（`Bot(...)`、`send_text`、`download_attachment`、`send_emotion`），4.x 下这些调用是否完全一致**没有验证过**。跑通或跑不通都欢迎开 issue 贴日志。

---

## 设计要点

代码注释里写了大量「为什么」，几个关键设计单独抽出来放在 [`docs/设计说明.md`](docs/设计说明.md)：

- OneBot 的 `user_id` 用 `crc32(wxid)` 而不是 `hash()` —— 否则 AstrBot 一重启，`user_id` 就变，对话记忆全断
- WebSocket 单帧上限抬到 64 MiB —— 默认 1 MiB 会让一张 base64 图片直接触发 `1009` 断连
- 群 @ 的权威来源是 `atuserlist`，不是正文里的 `@昵称`
- 微信 @ 的分隔符是 `U+2005`，不是空格
- 发文件和发图片都不能发完立刻删源文件 —— wxhook 是异步上传

---

## 风险与免责

- **封号风险**：DLL 注入违反微信用户协议，用小号，别用主号。
- **版本兼容**：上游支持 3.9.x–4.1.x，本仓库实测 3.9.5.81；大版本升级仍可能需要改适配层。
- **需要管理员权限**：注入 DLL 必须提权。
- **重启 = 重来**：重启电脑后要重新跑 `start.bat`（会话还在的话不用重新扫码）。
- **AstrBot agent 权限很大**：开了 shell / python 工具时，`allowed_wxids` 不能留空。

本项目仅供学习交流，不保证可用性。使用造成的一切后果由使用者自负。

---

## 致谢

- [miloira/wxhook](https://github.com/miloira/wxhook) —— 注入层
- [AstrBotDevs/AstrBot](https://github.com/AstrBotDevs/AstrBot) —— 机器人框架
- OneBot v11 协议

## License

[MIT](LICENSE)
