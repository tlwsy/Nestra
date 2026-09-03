"""Thin async Apprise adapter; encrypted target values come from the caller."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from urllib.parse import urlsplit

from ..core.crypto import Crypto
from ..core.errors import NotifyTransient, TargetRejected

ALLOWED_APPRISE_SCHEMES = frozenset(
    {
        "discord",
        "dingtalk",
        "feishu",
        "gchat",
        "guilded",
        "line",
        "msteams",
        "opsgenie",
        "pagerduty",
        "pover",
        "pbul",
        "slack",
        "tgram",
        "wecombot",
        "whatsapp",
        "wxteams",
    }
)


def validate_target_url(url: str) -> None:
    """Allow only Apprise plugins whose destination host is fixed by the plugin."""
    if (
        not 4 <= len(url) <= 4096
        or any(char.isspace() or ord(char) < 32 for char in url)
        or urlsplit(url).scheme.lower() not in ALLOWED_APPRISE_SCHEMES
    ):
        raise TargetRejected("不支持自定义网络目标；请选择受支持的固定云服务")


class AppriseClient:
    def __init__(self, crypto: Crypto) -> None:
        self.crypto = crypto

    def _notify_sync(
        self,
        encrypted_target: bytes,
        *,
        body: str,
        title: str,
        body_format: str,
        attachments: Sequence[str],
    ) -> None:
        try:
            import apprise
        except ImportError as exc:
            raise TargetRejected("Apprise 未安装；请安装 notify extra") from exc

        target_url = self.crypto.decrypt(encrypted_target)
        validate_target_url(target_url)
        instance = apprise.Apprise()
        try:
            added = instance.add(target_url)
        except Exception as exc:
            raise TargetRejected("Apprise 目标 URL 非法") from exc
        finally:
            target_url = ""  # Do not retain the decrypted credential longer than needed.
        if not added:
            raise TargetRejected("Apprise 目标 URL 非法")
        try:
            result = instance.notify(
                body=body,
                title=title,
                body_format=body_format,
                attach=list(attachments) or None,
            )
        except Exception as exc:
            raise NotifyTransient("Apprise 调用失败") from exc
        if result is not True:
            raise NotifyTransient("Apprise 渠道未确认投递")

    async def notify(
        self,
        encrypted_target: bytes,
        *,
        body: str,
        title: str,
        body_format: str = "text",
        attachments: Sequence[str] = (),
    ) -> None:
        await asyncio.to_thread(
            self._notify_sync,
            encrypted_target,
            body=body,
            title=title,
            body_format=body_format,
            attachments=attachments,
        )

    send = notify
