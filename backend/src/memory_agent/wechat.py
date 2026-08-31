from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx2


WECHAT_API_BASE_URL = "https://api.weixin.qq.com"
ACCESS_TOKEN_PATH = "/cgi-bin/token"
SUBSCRIBE_MESSAGE_PATH = "/cgi-bin/message/subscribe/send"
INVALID_ACCESS_TOKEN_CODES = {40001, 40014, 41001, 42001}


@dataclass(frozen=True)
class WeChatSendResult:
    errcode: int | None
    errmsg: str | None


class WeChatClientError(Exception):
    """A WeChat API failure that can be classified by the reminder domain."""

    def __init__(
        self,
        message: str,
        *,
        errcode: int | None = None,
        uncertain: bool,
        phase: str = "subscribe_send",
    ) -> None:
        super().__init__(message)
        self.errcode = errcode
        self.errmsg = message
        self.uncertain = uncertain
        self.phase = phase


class WeChatClient:
    """Small official HTTP client for the two reminder-related WeChat APIs."""

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        timeout_seconds: float = 10.0,
        miniprogram_state: str = "developer",
        lang: str = "zh_CN",
        client: Any | None = None,
        token_safety_seconds: int = 200,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.timeout_seconds = timeout_seconds
        self.miniprogram_state = miniprogram_state
        self.lang = lang
        self.client = client
        self.token_safety_seconds = token_safety_seconds
        self.access_token_cache_key = f"wechat:access_token:{app_id}"
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        if force_refresh:
            self._invalidate_access_token()
        if self._token is not None and time.monotonic() < self._token_expires_at:
            return self._token

        async with self._token_lock:
            if self._token is not None and time.monotonic() < self._token_expires_at:
                return self._token

            response = await self._request(
                "GET",
                ACCESS_TOKEN_PATH,
                params={
                    "grant_type": "client_credential",
                    "appid": self.app_id,
                    "secret": self.app_secret,
                },
                phase="access_token",
            )
            payload = self._json(
                response, label="access token response", phase="access_token"
            )
            errcode = self._optional_errcode(payload)
            if errcode is not None and errcode != 0:
                raise WeChatClientError(
                    self._errmsg(payload, "WeChat access token request failed") or "",
                    errcode=errcode,
                    uncertain=False,
                    phase="access_token",
                )
            token = payload.get("access_token")
            expires_in = payload.get("expires_in")
            if not isinstance(token, str) or not token:
                raise WeChatClientError(
                    "WeChat access token response is missing access_token",
                    uncertain=True,
                    phase="access_token",
                )
            if not isinstance(expires_in, int) or expires_in <= 0:
                raise WeChatClientError(
                    "WeChat access token response has an invalid expires_in",
                    uncertain=True,
                    phase="access_token",
                )
            # WeChat normally returns 7200 seconds. Cache 7000 seconds to avoid
            # using a token right at its expiry boundary.
            ttl = max(1, expires_in - self.token_safety_seconds)
            self._token = token
            self._token_expires_at = time.monotonic() + ttl
            return token

    async def send_subscribe_message(
        self,
        openid: str,
        template_id: str,
        page: str | None,
        data: dict[str, dict[str, str]],
    ) -> WeChatSendResult:
        payload: dict[str, Any] = {
            "touser": openid,
            "template_id": template_id,
            "data": data,
            "miniprogram_state": self.miniprogram_state,
            "lang": self.lang,
        }
        if page:
            payload["page"] = page

        access_token = await self.get_access_token()
        response = await self._request_json(
            "POST",
            SUBSCRIBE_MESSAGE_PATH,
            params={"access_token": access_token},
            json_payload=payload,
        )
        if response.errcode in INVALID_ACCESS_TOKEN_CODES:
            # A token rejection means WeChat did not accept this send. Refresh
            # once and retry once; never loop on token failures.
            access_token = await self.get_access_token(force_refresh=True)
            response = await self._request_json(
                "POST",
                SUBSCRIBE_MESSAGE_PATH,
                params={"access_token": access_token},
                json_payload=payload,
            )
        return response

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    def _invalidate_access_token(self) -> None:
        self._token = None
        self._token_expires_at = 0.0

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str],
        json_payload: dict[str, Any],
    ) -> WeChatSendResult:
        response = await self._request(
            method,
            path,
            params=params,
            json_payload=json_payload,
        )
        payload = self._json(
            response, label="WeChat response", phase="subscribe_send"
        )
        errcode = self._optional_errcode(payload)
        return WeChatSendResult(
            errcode=errcode,
            errmsg=self._errmsg(payload, None),
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str],
        json_payload: dict[str, Any] | None = None,
        phase: str = "subscribe_send",
    ) -> Any:
        if self.client is None:
            self.client = httpx2.AsyncClient(
                base_url=WECHAT_API_BASE_URL,
                timeout=self.timeout_seconds,
            )
        try:
            return await self.client.request(
                method,
                path,
                params=params,
                json=json_payload,
            )
        except httpx2.TimeoutException as exc:
            raise WeChatClientError(
                f"WeChat request timed out after {self.timeout_seconds}s",
                uncertain=True,
                phase=phase,
            ) from exc
        except httpx2.HTTPError as exc:
            raise WeChatClientError(
                f"WeChat HTTP request failed: {type(exc).__name__}",
                uncertain=True,
                phase=phase,
            ) from exc

    def _json(self, response: Any, *, label: str, phase: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise WeChatClientError(
                f"WeChat returned a non-JSON {label}",
                uncertain=True,
                phase=phase,
            ) from exc
        if not isinstance(payload, dict):
            raise WeChatClientError(
                f"WeChat returned an invalid {label}",
                uncertain=True,
                phase=phase,
            )
        return payload

    @staticmethod
    def _optional_errcode(payload: dict[str, Any]) -> int | None:
        value = payload.get("errcode", payload.get("errCode"))
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise WeChatClientError(
                "WeChat returned an invalid errcode",
                uncertain=True,
                phase="subscribe_send",
            ) from exc

    @staticmethod
    def _errmsg(payload: dict[str, Any], default: str | None) -> str | None:
        value = payload.get("errmsg", payload.get("errMsg"))
        if value is None:
            return default
        return str(value)[:500]
