# QQ 登录守护（astrbot_plugin_qq_login_watch）

监控 SnowLuma 容器里 QQ 客户端的登录状态。QQ 掉线时通过 **PushPlus（微信）** 或 **QQ 邮箱 SMTP** 提醒，附带 **noVNC 桌面实时截图**、SSH 隧道命令和 VNC 密码，让你能第一时间扫码或输密码重新登录。

> 起因：QQ 跑在服务器的 noVNC 里，noVNC 端口不对公网开放。一旦 QQ 掉线，既收不到消息也不知道，只能等发现时再想办法。这个插件把「掉线」变成一条能立刻行动的微信推送。

## 功能

- 每 60 秒检查一次 QQ 登录状态（可配置），掉线立即推送
- 通知双通道：PushPlus 微信推送 与 QQ 邮箱 SMTP，可单独启用或同时启用
- 推送内容包含：时间、账号、掉线原因、SSH 隧道命令、VNC 密码、当前桌面截图
- 离线期间按设定间隔重复提醒（默认 30 分钟），避免漏看
- 重新登录成功后推送一条恢复通知
- 指令 `/qqwatch` 查看状态，`/qqwatch test` 发送测试推送

## 工作原理

```
┌─────────────┐   1. POST /get_login_info    ┌──────────────┐
│   AstrBot   │ ───────────────────────────▶ │   SnowLuma   │
│  (插件轮询)  │ ◀─────────────────────────── │  OneBot HTTP │
└──────┬──────┘        2. 掉线 = 接口失效      └──────────────┘
       │
       │ 3. GET /shot?token=...
       ▼
┌──────────────────────┐   4. docker exec xwd   ┌──────────────┐
│ 主机 qqwatch-screen   │ ─────────────────────▶ │  SnowLuma    │
│  (systemd, :8765)     │ ◀───────────────────── │  Xvfb :1     │
└──────┬───────────────┘      5. JPEG           └──────────────┘
       │
       │ 6. PushPlus 推送（含截图 base64）
       ▼
    微信通知
```

AstrBot 容器没有 Docker 权限，无法直接截 SnowLuma 的屏，所以由主机上的 `qqwatch-screen` 服务代劳；该服务只监听 docker0 网关，并用 token 鉴权。

## 运行环境

### 主机

- Linux + systemd + Docker（插件与 SnowLuma 均以容器运行）
- ImageMagick（提供 `convert`，用于 xwd → JPEG）
- Python 3.8+（运行 `qqwatch-screen.py`，仅用标准库）
- 能 `docker exec` 进入 SnowLuma 容器

### AstrBot

- AstrBot **>= 4.24**（实测 4.29.1）
- 容器内可访问：
  - SnowLuma 的 OneBot HTTP 端口（默认 `192.168.0.2:3000`）
  - 主机 docker0 网关（默认 `192.168.0.1:8765`）
  - `https://www.pushplus.plus`（使用 PushPlus 时）或 `smtp.qq.com:465`（使用邮件时）

### SnowLuma

- SnowLuma 镜像自带 Xvfb、x11vnc、noVNC，QQ 运行在 `DISPLAY=:1`
- OneBot HTTP 服务端已开启，并知道其 `accessToken`
- 建议给 SnowLuma 容器加 `--restart unless-stopped`，避免宿主机重启后 QQ 不在线

### 通知渠道（二选一或同时启用）

**PushPlus（微信）**

- 账号需完成**实名认证**（未实名会返回 `code=905`）

**QQ 邮箱 SMTP**

- 登录 QQ 邮箱 → 设置 → 账户 → 开启「SMTP 服务」→ 生成**授权码**
- 使用授权码而非 QQ 登录密码
- 默认 `smtp.qq.com:465`（SSL）；也可用 587 + STARTTLS

## 安装

### 1. 主机侧截图服务

```bash
# 1) 安装依赖
apt-get update && apt-get install -y imagemagick

# 2) 部署脚本与 token（token 自行生成，任意高强度字符串）
install -m 755 host/qqwatch-screen.py /usr/local/bin/qqwatch-screen.py
umask 077 && head -c 32 /dev/urandom | xxd -p -c 32 > /etc/qqwatch-screen.token

# 3) 确认 docker0 网关地址（默认 192.168.0.1，不一致请改 service 里的 QQWATCH_BIND）
ip -4 addr show docker0 | grep inet

# 4) 安装 systemd 服务
install -m 644 host/qqwatch-screen.service /etc/systemd/system/qqwatch-screen.service
systemctl daemon-reload
systemctl enable --now qqwatch-screen.service

# 5) 自测（应返回 JPEG 字节数）
curl -s -o /tmp/shot.jpg -w '%{http_code} %{size_download}\n' \
  "http://192.168.0.1:8765/shot?token=$(cat /etc/qqwatch-screen.token)"
```

脚本支持的环境变量：`QQWATCH_BIND`、`QQWATCH_PORT`、`QQWATCH_CONTAINER`、`QQWATCH_DISPLAY`、`QQWATCH_WIDTH`、`QQWATCH_QUALITY`。

### 2. AstrBot 插件

```bash
cp -r astrbot_plugin_qq_login_watch /path/to/astrbot/data/plugins/
# 重启 AstrBot 使其加载插件
docker restart astrbot
```

在 AstrBot 的插件配置页填写以下必填项（其余保持默认即可）：

| 配置项 | 说明 |
| --- | --- |
| `snowluma_api_base` | SnowLuma OneBot 地址，默认 `http://192.168.0.2:3000` |
| `snowluma_access_token` | SnowLuma OneBot 的 access token |
| `screenshot_url` | 截屏服务地址，默认 `http://192.168.0.1:8765/shot` |
| `screenshot_token` | 与 `/etc/qqwatch-screen.token` 一致 |
| `pushplus_token` | PushPlus 的用户 token（留空则不启用微信推送） |
| `smtp_enabled` | 是否启用邮件提醒，默认 `false` |
| `smtp_host` / `smtp_port` | 默认 `smtp.qq.com` / `465` |
| `smtp_ssl` | 465 端口填 `true`；587 端口填 `false`（自动 STARTTLS） |
| `smtp_user` | 发件邮箱，如 `123456789@qq.com` |
| `smtp_password` | 邮箱 SMTP **授权码**（不是登录密码） |
| `smtp_from` | 发件人，留空则用 `smtp_user` |
| `smtp_to` | 收件人，多个用逗号分隔 |
| `ssh_host` / `ssh_port` | 服务器公网 IP / SSH 端口，用于生成隧道命令 |
| `vnc_password` | 可选，填写后会显示在推送里 |

`novnc_url` 与 `ssh_host` 二选一：填了 `novnc_url` 就推链接；否则用 `ssh_host` 生成 `ssh -L` 隧道命令。

### 3. 验证

在 QQ 里发送：

- `/qqwatch` —— 看状态
- `/qqwatch test` —— 按已启用的渠道各发一条测试消息，并回报每个渠道的成功/失败

## 指令

| 指令 | 说明 |
| --- | --- |
| `/qqwatch` | 显示在线状态、账号、最近检查时间、掉线原因 |
| `/qqwatch test` | 立即发送一条测试推送（默认仅管理员） |

## 掉线后怎么操作

推送里会给出：

```
ssh -p 22 -L 6081:127.0.0.1:6081 root@<服务器IP>
```

在本地执行后，浏览器打开 `http://127.0.0.1:6081/vnc.html`，输入推送里的 VNC 密码，然后用手机 QQ 扫码或输入账号密码登录。

## 已知限制

1. **二维码约 2 分钟过期**：收到推送请尽快扫码；过期需在 noVNC 里点「刷新」再截。
2. **QQ 最小化到托盘时截图是空桌面**：Electron 窗口被隐藏后不重绘。真实掉线时 QQ 会自行弹出登录窗，届时截图正常。
3. **PushPlus 的微信模板对内嵌 base64 图片的渲染不保证**：若图片未显示，可启用邮件通道——邮件用 `cid` 内嵌图片，兼容性更好。
4. 截图服务依赖 `docker exec`，SnowLuma 容器名变更时需同步修改 `QQWATCH_CONTAINER`。

## 安全说明

- 截图服务只监听 docker0 网关（`192.168.0.1`），不对公网开放，且需要 token。
- `vnc_password` 会出现在微信推送内容里，介意的话留空。
- 不要把 noVNC 的 6081 端口直接暴露到公网；推荐 SSH 隧道。
- SnowLuma 的 OneBot token、PushPlus token、邮箱 SMTP 授权码都保存在 AstrBot 的插件配置中，注意 `data/config/` 的权限。
- 建议使用专用邮箱或邮箱别名发送，便于随时吊销授权码。

## 目录结构

```
astrbot_plugin_qq_login_watch/
├── main.py                 # 插件实现
├── metadata.yaml           # 插件元信息
├── _conf_schema.json       # 配置项定义
├── README.md
└── host/
    ├── qqwatch-screen.py       # 主机侧截屏 HTTP 服务
    └── qqwatch-screen.service  # systemd 单元
```

## License

Apache-2.0
