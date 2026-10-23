# 更新日志

## 1.1.1 (2026-10-23)

- 通知渠道改为自由组合：仅 PushPlus、仅邮箱、或两者同时启用
- 新增 `pushplus_enabled` 开关，与 `smtp_enabled` 相互独立
- `/qqwatch` 状态输出增加当前启用的通知渠道
- 插件作者改为 GTFaiz，补充更新日志

## 1.1.0 (2026-10-23)

- 新增 QQ 邮箱 SMTP 通知渠道，可与 PushPlus 并行
- 邮件使用 `multipart/related` + `cid` 内嵌截图，兼容性优于微信模板
- `/qqwatch test` 按渠道逐个发送并回报各自结果
- `smtp_to` 留空时默认发送到发件邮箱

## 1.0.0 (2026-10-22)

- 初始版本：监控 SnowLuma 的 QQ 登录状态
- 掉线时通过 PushPlus 推送提醒，附带 noVNC 桌面截图、SSH 隧道命令与 VNC 密码
- 支持离线重复提醒、恢复通知，以及 `/qqwatch`、`/qqwatch test` 指令
- 配套主机侧截屏服务 `host/qqwatch-screen.py` 与 systemd 单元
