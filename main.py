"""QQ 登录守护插件。

监控 SnowLuma 容器里 QQ 客户端的登录状态（通过其 OneBot HTTP 接口），
掉线时通过 PushPlus 推送提醒，并附带 noVNC 桌面实时截图与重新登录提示。

截屏由主机侧的 ``qqwatch-screen.service`` 提供（AstrBot 容器没有 Docker 权限，
但可以访问 docker0 网关）。
"""

import asyncio
import base64
import time
from datetime import datetime

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register(
    "astrbot_plugin_qq_login_watch",
    "dsh",
    "监控 SnowLuma 的 QQ 登录状态，掉线时推送提醒并附 noVNC 实时截图",
    "1.0.0",
)
class QQLoginWatch(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.conf = config if config is not None else {}
        self._task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        self._online: bool | None = None
        self._account: str = ""
        self._last_check: float = 0.0
        self._last_ok: float = 0.0
        self._last_notify: float = 0.0
        self._offline_since: float = 0.0
        self._last_error: str = ""

    # ------------------------------------------------------------------ utils
    def _c(self, key: str, default=None):
        try:
            value = self.conf.get(key, default)
        except Exception:  # noqa: BLE001 - config may be a plain dict
            value = default
        return default if value is None else value

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    # ------------------------------------------------------------- lifecycle
    async def initialize(self):
        if not self._c("enabled", True):
            logger.info("[qq-login-watch] 插件已禁用，不启动监控")
            return
        interval = max(15, int(self._c("check_interval_seconds", 60)))
        self._task = asyncio.create_task(self._loop(), name="qq-login-watch")
        logger.info("[qq-login-watch] 监控已启动，检查间隔 %ss", interval)

    async def terminate(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _loop(self):
        while True:
            try:
                await self._check_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                logger.warning("[qq-login-watch] 本轮检查异常: %s", exc)
            await asyncio.sleep(max(15, int(self._c("check_interval_seconds", 60))))

    # ----------------------------------------------------------------- check
    async def _check_once(self):
        base = str(self._c("snowluma_api_base", "http://192.168.0.2:3000")).rstrip("/")
        token = str(self._c("snowluma_access_token", "")).strip()
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        self._last_check = time.time()
        ok = False
        account = ""
        err = ""

        try:
            session = await self._get_session()
            async with session.post(
                f"{base}/get_login_info",
                json={},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    info = data.get("data") or {}
                    if data.get("retcode") == 0 and info.get("user_id"):
                        ok = True
                        account = str(info["user_id"])
                    else:
                        err = f"retcode={data.get('retcode')}"
                elif resp.status in (401, 403):
                    # token 配错不是掉线，避免误报，直接跳过本轮状态判定
                    logger.error(
                        "[qq-login-watch] OneBot 鉴权失败 HTTP %s，请检查 snowluma_access_token",
                        resp.status,
                    )
                    self._last_error = f"HTTP {resp.status} (鉴权失败)"
                    return
                else:
                    err = f"HTTP {resp.status}"
        except Exception as exc:  # noqa: BLE001 - network errors mean offline
            err = type(exc).__name__

        self._last_error = err
        if account:
            self._account = account

        now = time.time()
        previous = self._online
        self._online = ok

        if ok:
            self._last_ok = now
            if previous is False:
                self._offline_since = 0.0
                logger.info("[qq-login-watch] QQ 已恢复在线: %s", self._account)
                if self._c("notify_on_recovery", True):
                    await self._push(
                        "✅ QQ 已恢复在线",
                        "<h3>✅ QQ 已恢复在线</h3>"
                        f"<p>时间：{self._fmt(now)}<br/>账号：{self._account or '未知'}</p>",
                    )
            return

        if previous is not False:
            self._offline_since = now
            self._last_notify = now
            logger.warning("[qq-login-watch] 检测到 QQ 掉线: %s", err)
            await self._notify_offline(err)
            return

        renotify = float(self._c("offline_renotify_minutes", 30)) * 60
        if renotify > 0 and now - self._last_notify >= renotify:
            self._last_notify = now
            logger.warning("[qq-login-watch] QQ 仍离线，重复提醒")
            await self._notify_offline(err)

    @staticmethod
    def _fmt(ts: float) -> str:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

    # ----------------------------------------------------------- screenshot
    async def _fetch_shot(self) -> bytes | None:
        if not self._c("attach_screenshot", True):
            return None
        url = str(self._c("screenshot_url", "")).strip()
        if not url:
            return None
        params = {"token": str(self._c("screenshot_token", "")).strip()}
        try:
            session = await self._get_session()
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 200:
                    return await resp.read()
                logger.warning("[qq-login-watch] 截图服务返回 HTTP %s", resp.status)
        except Exception as exc:  # noqa: BLE001 - screenshot is best effort
            logger.warning("[qq-login-watch] 截图失败: %s", exc)
        return None

    # ---------------------------------------------------------------- push
    async def _notify_offline(self, err: str):
        shot = await self._fetch_shot()
        novnc = str(self._c("novnc_url", "")).strip()
        vnc_pwd = str(self._c("vnc_password", "")).strip()

        parts = [
            "<h3>⚠️ QQ 掉线提醒</h3>",
            f"<p>时间：{self._fmt(time.time())}<br/>"
            f"账号：{self._account or '未知'}<br/>"
            f"原因：{err or 'OneBot 接口不可用'}</p>",
        ]
        ssh_host = str(self._c("ssh_host", "")).strip()
        ssh_port = int(self._c("ssh_port", 22))
        if novnc:
            parts.append(f'<p>重新登录：<a href="{novnc}">{novnc}</a></p>')
        elif ssh_host:
            cmd = (
                f"ssh -p {ssh_port} -L 6081:127.0.0.1:6081 root@{ssh_host}"
            )
            parts.append(
                "<p>noVNC 未对公网开放，请先建 SSH 隧道：</p>"
                f'<p><code>{cmd}</code></p>'
                "<p>然后浏览器打开 http://127.0.0.1:6081/vnc.html</p>"
            )
        if vnc_pwd:
            parts.append(f"<p>VNC 密码：{vnc_pwd}</p>")
        parts.append("<p>打开后用手机 QQ 扫码，或在登录框输入账号密码。</p>")
        if shot:
            parts.append(
                '<p><img src="data:image/jpeg;base64,'
                + base64.b64encode(shot).decode()
                + '" style="max-width:100%"/></p>'
            )
        else:
            parts.append("<p>（本次未取到截图，请直接打开 noVNC 查看。）</p>")

        await self._push("QQ 掉线提醒", "".join(parts))

    async def _push(self, title: str, content: str) -> bool:
        token = str(self._c("pushplus_token", "")).strip()
        if not token:
            logger.warning("[qq-login-watch] 未配置 pushplus_token，跳过推送")
            return False
        payload = {
            "token": token,
            "title": title,
            "content": content,
            "template": "html",
        }
        try:
            session = await self._get_session()
            async with session.post(
                "https://www.pushplus.plus/send",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status == 200 and body.get("code") == 200:
                    logger.info("[qq-login-watch] 推送成功: %s", title)
                    return True
                logger.error("[qq-login-watch] 推送失败: %s %s", resp.status, body)
        except Exception as exc:  # noqa: BLE001
            logger.error("[qq-login-watch] 推送异常: %s", exc)
        return False

    # ------------------------------------------------------------- commands
    @filter.command("qqwatch")
    async def qqwatch(self, event: AstrMessageEvent):
        """QQ 登录守护：查看状态或发送测试推送。用法：/qqwatch [status|test]"""
        parts = (event.message_str or "").strip().split(maxsplit=1)
        sub = parts[1].strip().lower() if len(parts) > 1 else "status"

        if sub.startswith("test"):
            if self._c("command_admin_only", True) and not event.is_admin():
                yield event.plain_result("仅管理员可用。")
                return
            shot = await self._fetch_shot()
            content = (
                "<h3>🔔 测试推送</h3>"
                f"<p>时间：{self._fmt(time.time())}</p>"
                f"<p>截图：{'已附带' if shot else '未获取到'}</p>"
            )
            if shot:
                content += (
                    '<p><img src="data:image/jpeg;base64,'
                    + base64.b64encode(shot).decode()
                    + '" style="max-width:100%"/></p>'
                )
            sent = await self._push("QQ 登录守护测试", content)
            yield event.plain_result("测试推送已发送。" if sent else "推送失败，请查看日志。")
            return

        state = {None: "未知", True: "在线", False: "离线"}[self._online]
        lines = [
            f"状态：{state}",
            f"账号：{self._account or '未知'}",
            f"最近检查：{self._fmt(self._last_check) if self._last_check else '尚未检查'}",
        ]
        if self._online is False:
            lines.append(f"离线开始：{self._fmt(self._offline_since)}")
            lines.append(f"最后错误：{self._last_error or '未知'}")
        lines.append(f"检查间隔：{self._c('check_interval_seconds', 60)}s")
        yield event.plain_result("\n".join(lines))
