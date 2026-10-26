"""QQ 登录守护插件。

监控 SnowLuma 容器里 QQ 客户端的登录状态（通过其 OneBot HTTP 接口），
掉线时通过 PushPlus 推送提醒，并附带 noVNC 桌面实时截图与重新登录提示。

截屏由主机侧的 ``qqwatch-screen.service`` 提供（AstrBot 容器没有 Docker 权限，
但可以访问 docker0 网关）。
"""

import asyncio
import base64
import io
import smtplib
import ssl
import time
from datetime import datetime
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

# PushPlus 单条内容上限 2 万字（超出返回 code=999），留出余量给 HTML 正文
PUSHPLUS_MAX_CONTENT = 18000
# 内嵌到 PushPlus 的截图最长边与 JPEG 质量，base64 后约 1 万字
PUSHPLUS_IMG_WIDTH = 640
PUSHPLUS_IMG_QUALITY = 50

SMTP_AUTH_HINT = (
    "SMTP 认证失败：请检查 smtp_user 是否为完整邮箱地址、"
    "smtp_password 是否为邮箱「授权码」（不是登录密码），"
    "并确认已在邮箱设置中开启 SMTP 服务"
)


@register(
    "astrbot_plugin_qq_login_watch",
    "GTFaiz",
    "监控 SnowLuma 的 QQ 登录状态，掉线时通过 PushPlus 或邮箱提醒并附 noVNC 实时截图",
    "1.1.2",
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

    @staticmethod
    def _shrink_jpeg(
        shot: bytes,
        max_width: int = PUSHPLUS_IMG_WIDTH,
        quality: int = PUSHPLUS_IMG_QUALITY,
    ) -> bytes:
        """压缩截图，避免 base64 后撑爆 PushPlus 的 2 万字上限。"""
        try:
            from PIL import Image

            with Image.open(io.BytesIO(shot)) as img:
                img = img.convert("RGB")
                if img.width > max_width:
                    img.thumbnail((max_width, 100000), Image.LANCZOS)
                out = io.BytesIO()
                img.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
            return out.getvalue()
        except Exception as exc:  # noqa: BLE001 - 压图失败就退回原图
            logger.debug("[qq-login-watch] 截图压缩失败，使用原图: %s", exc)
            return shot

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
        if not shot:
            parts.append("<p>（本次未取到截图，请直接打开 noVNC 查看。）</p>")

        await self._notify_all("QQ 掉线提醒", "".join(parts), shot)

    async def _push(
        self, title: str, content: str, shot: bytes | None = None
    ) -> tuple[bool, str]:
        """PushPlus 推送，返回 (是否成功, 说明)。"""
        token = str(self._c("pushplus_token", "")).strip()
        if not token:
            logger.warning("[qq-login-watch] 未配置 pushplus_token，跳过推送")
            return False, "未配置 pushplus_token"

        with_image = ""
        if shot:
            with_image = content + self._img_tag(self._shrink_jpeg(shot), data_uri=True)
        if with_image and len(with_image) <= PUSHPLUS_MAX_CONTENT:
            ok, detail = await self._post_push(token, title, with_image)
            if ok:
                return True, ""
            if "999" not in detail:
                return False, detail
            logger.warning(
                "[qq-login-watch] PushPlus 返回 999，降级为纯文字重试（内容 %s 字）",
                len(with_image),
            )
        elif with_image:
            logger.warning(
                "[qq-login-watch] PushPlus 内容超限（%s 字），省略截图",
                len(with_image),
            )

        text_only = content + (
            "<p>（截图较大已省略，请打开 noVNC 查看。）</p>" if shot else ""
        )
        if len(text_only) > PUSHPLUS_MAX_CONTENT:
            text_only = text_only[: PUSHPLUS_MAX_CONTENT - 20] + "…"
        ok, detail = await self._post_push(token, title, text_only)
        return (True, "截图已省略") if ok else (False, detail)

    async def _post_push(self, token: str, title: str, content: str) -> tuple[bool, str]:
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
                    return True, ""
                detail = f"HTTP {resp.status} {body}"
                logger.error("[qq-login-watch] 推送失败: %s", detail)
                return False, detail
        except Exception as exc:  # noqa: BLE001
            logger.error("[qq-login-watch] 推送异常: %s", exc)
            return False, f"{type(exc).__name__}: {exc}"

    # ---------------------------------------------------------------- email
    @staticmethod
    def _img_tag(shot: bytes | None, *, data_uri: bool) -> str:
        if not shot:
            return ""
        if data_uri:
            return (
                '<p><img src="data:image/jpeg;base64,'
                + base64.b64encode(shot).decode()
                + '" style="max-width:100%"/></p>'
            )
        return '<p><img src="cid:qqscreenshot" style="max-width:100%"/></p>'

    async def _send_email(
        self, title: str, html: str, shot: bytes | None
    ) -> tuple[bool, str]:
        host = str(self._c("smtp_host", "smtp.qq.com")).strip()
        port = int(self._c("smtp_port", 465))
        use_ssl = bool(self._c("smtp_ssl", True))
        user = str(self._c("smtp_user", "")).strip()
        password = str(self._c("smtp_password", "")).strip()
        sender = str(self._c("smtp_from", "")).strip()
        raw_to = str(self._c("smtp_to", "")).replace(";", ",")
        recipients = [
            addr.strip() for addr in raw_to.split(",") if "@" in addr.strip()
        ]
        if not recipients and user:
            recipients = [user]  # 未填收件人时默认发给自己

        if not (user and password and recipients):
            logger.warning(
                "[qq-login-watch] SMTP 配置不完整（smtp_user / smtp_password / smtp_to），跳过邮件"
            )
            return False, "SMTP 配置不完整"
        if "@" not in user:
            logger.error("[qq-login-watch] smtp_user 需填写完整邮箱地址，当前为 %r", user)
            return False, "smtp_user 不是完整邮箱地址"
        if not sender or "@" not in sender:
            if sender:
                logger.warning(
                    "[qq-login-watch] smtp_from=%r 不是有效邮箱，改用 smtp_user", sender
                )
            sender = user  # QQ 邮箱要求 From 与登录账号一致

        root = MIMEMultipart("related")
        root["Subject"] = title
        root["From"] = sender
        root["To"] = ", ".join(recipients)
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText("QQ 登录守护提醒，请使用支持 HTML 的客户端查看。", "plain", "utf-8"))
        alt.attach(MIMEText(html + self._img_tag(shot, data_uri=False), "html", "utf-8"))
        root.attach(alt)
        if shot:
            image = MIMEImage(shot, "jpeg")
            image.add_header("Content-ID", "<qqscreenshot>")
            image.add_header("Content-Disposition", "inline", filename="qq-desktop.jpg")
            root.attach(image)

        try:
            await asyncio.to_thread(
                self._smtp_send, host, port, use_ssl, user, password, root
            )
            logger.info("[qq-login-watch] 邮件已发送: %s", title)
            return True, ""
        except Exception as exc:  # noqa: BLE001
            logger.error("[qq-login-watch] 邮件发送失败: %s", exc)
            return False, str(exc)

    @staticmethod
    def _smtp_login(server: smtplib.SMTP, user: str, password: str) -> None:
        """优先用 AUTH PLAIN，好让服务器把 535 的真实原因返回而不是直接断开。"""
        server.ehlo_or_helo_if_needed()
        response = base64.b64encode(f"\0{user}\0{password}".encode()).decode()
        code, resp = server.docmd("AUTH", "PLAIN " + response)
        if code in (235, 503):  # 认证成功 / 已认证
            return
        if code in (500, 501, 504):  # 服务器不支持 initial-response，退回标准登录
            server.login(user, password)
            return
        raise smtplib.SMTPAuthenticationError(code, resp)

    @classmethod
    def _smtp_send(
        cls, host: str, port: int, use_ssl: bool, user: str, password: str, msg
    ) -> None:
        try:
            if use_ssl:
                server = smtplib.SMTP_SSL(host, port, timeout=25)
            else:
                server = smtplib.SMTP(host, port, timeout=25)
        except (OSError, ssl.SSLError) as exc:
            raise RuntimeError(
                f"连接 {host}:{port} 失败（{type(exc).__name__}: {exc}），"
                "请检查端口是否被封、smtp_host/smtp_port/smtp_ssl 是否匹配"
            ) from exc
        try:
            if not use_ssl:
                server.starttls(context=ssl.create_default_context())
            cls._smtp_login(server, user, password)
            server.send_message(msg)
        except smtplib.SMTPAuthenticationError as exc:
            raise RuntimeError(
                f"{SMTP_AUTH_HINT}；服务器返回 {exc.smtp_code} "
                f"{exc.smtp_error!r}"
            ) from exc
        except smtplib.SMTPServerDisconnected as exc:
            raise RuntimeError(
                f"{SMTP_AUTH_HINT}；服务器在认证阶段直接断开连接"
            ) from exc
        except smtplib.SMTPException as exc:
            raise RuntimeError(f"SMTP 发送失败: {exc}") from exc
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001 - best effort
                pass

    async def _notify_all(
        self, title: str, html: str, shot: bytes | None
    ) -> dict[str, tuple[bool, str]]:
        """向所有已启用的渠道发送，返回 {渠道: (是否成功, 说明)}。"""
        results: dict[str, tuple[bool, str]] = {}
        pushplus_on = bool(self._c("pushplus_enabled", True)) and bool(
            str(self._c("pushplus_token", "")).strip()
        )
        if pushplus_on:
            results["pushplus"] = await self._push(title, html, shot)
        if bool(self._c("smtp_enabled", False)):
            results["email"] = await self._send_email(title, html, shot)
        if not results:
            logger.warning("[qq-login-watch] 未启用任何通知渠道（PushPlus / SMTP）")
        return results

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
            results = await self._notify_all("QQ 登录守护测试", content, shot)
            if not results:
                yield event.plain_result("未启用任何通知渠道，请先配置 PushPlus token 或 SMTP。")
                return
            names = {"pushplus": "PushPlus", "email": "邮件"}
            detail = "；".join(
                f"{names.get(k, k)}：{'成功' if ok else '失败'}"
                + (f"（{msg}）" if msg else "")
                for k, (ok, msg) in results.items()
            )
            yield event.plain_result(f"测试推送 —— {detail}")
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
        channels = []
        if bool(self._c("pushplus_enabled", True)) and str(
            self._c("pushplus_token", "")
        ).strip():
            channels.append("PushPlus")
        if bool(self._c("smtp_enabled", False)):
            channels.append("邮件")
        lines.append("通知渠道：" + ("、".join(channels) if channels else "未启用"))
        lines.append(f"检查间隔：{self._c('check_interval_seconds', 60)}s")
        yield event.plain_result("\n".join(lines))
