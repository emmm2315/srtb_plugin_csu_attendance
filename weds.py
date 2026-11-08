"""WEDS（微校）到课 / 打卡平台客户端。

已实测确认的事实：
  * 接口 https://wedsk12.weds.com.cn:8081/atd/weekRank 存在，POST，
    Authorization: "Token <token>"。
  * token 无效时返回 {"Msg": "token失效", "Code": 401}（HTTP 状态仍是 200）。
  * 教务系统的 /jsxsd/External/accessToken.do 发放的 JWT **不被此平台接受**
    （已实测，回 401）。该 token 属于"互认互选"外部对接。
  * 平台的 /user/login 需要微信 wx.login() 的 code（回 code=601 参数不全），
    无法用学号密码直接换取 token。

因此 token 需要由使用者提供一次，插件负责缓存、校验与失效提醒。
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import httpx

DEFAULT_BASE = "https://wedsk12.weds.com.cn:8081"
DEFAULT_ORGA_ID = "10001"
SUCCESS_CODE = "600"

# 与微信小程序抓包一致
MP_UA = (
    "Mozilla/5.0 (Linux; Android 13; Mi 9T Build/TQ3A.230901.001; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/116.0.0.0 "
    "Mobile Safari/537.36 XWEB/1160117 MMWEBSDK/20250904 MMWEBID/1094 "
    "MicroMessenger/8.0.65.2942(0x28004142) WeChat/arm64 Weixin GPVersion/1 "
    "NetType/WIFI Language/zh_CN ABI/arm64 MiniProgramEnv/android"
)
MINIPROGRAM_REFERER = "https://servicewechat.com/wx416b36c668206e1d/29/page-frame.html"

# 平台把业务错误放在 body 里，HTTP 状态码不可靠
_TOKEN_INVALID_CODES = {401}


class WedsError(Exception):
    """WEDS 接口调用错误。"""


class TokenInvalid(WedsError):
    """token 失效或不被接受。"""


class WedsClient:
    def __init__(
        self,
        base: str = DEFAULT_BASE,
        timeout: int = 20,
        proxy: str = "",
        logger=None,
    ) -> None:
        self.base = (base or DEFAULT_BASE).rstrip("/")
        self.timeout = max(5, int(timeout or 20))
        self.proxy = (proxy or "").strip()
        self.log = logger
        self._client: httpx.AsyncClient | None = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                proxy=self.proxy or None,
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json;charset=utf-8",
            "Authorization": "Token " + (token or ""),
            "Charset": "utf-8",
            "Referer": MINIPROGRAM_REFERER,
            "User-Agent": MP_UA,
        }

    async def post(self, path: str, token: str, body: dict[str, Any]) -> dict[str, Any]:
        """调用平台接口，返回解析后的 JSON。token 失效时抛 TokenInvalid。"""
        url = self.base + path
        client = self._ensure_client()
        try:
            response = await client.post(url, headers=self._headers(token), json=body)
        except httpx.HTTPError as exc:
            raise WedsError("请求 %s 失败: %s" % (path, exc)) from exc

        text = response.text
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            if self.log:
                self.log.warning("[CSU] 接口 %s 返回非 JSON: %s", path, text[:200])
            raise WedsError(
                "接口 %s 返回了非 JSON 内容（可能是网络被劫持或地址有误）: %s"
                % (path, text[:200])
            ) from None

        self._raise_if_token_invalid(data, path)
        return data

    @staticmethod
    def _raise_if_token_invalid(data: dict[str, Any], path: str) -> None:
        if not isinstance(data, dict):
            return
        code = data.get("Code", data.get("code"))
        msg = str(data.get("Msg") or data.get("msg") or "")
        if code in _TOKEN_INVALID_CODES or "失效" in msg:
            raise TokenInvalid(msg or "token 已失效")

    # ── 业务接口 ──────────────────────────────────────────────────────────

    async def week_rank(
        self,
        token: str,
        user_serial: str,
        orga_id: str = DEFAULT_ORGA_ID,
        filter_week: int = 0,
    ) -> dict[str, Any]:
        """查询周维度到课 / 打卡统计。

        返回接口的 Data 部分，结构（实测 + 参考实现确认）::

            {
              "ClassNum": "10", "WeekNow": "3", "WeekNum": "21",
              "Week": [{"WeekName": "星期日", "Rq": "2026-09-20"}, ...],   # 周日~周六
              "Rank": [
                {"CourseName": "大学物理B（二）", "TeachName": "周良玉",
                 "ClassLxBz": "未到" | "正常" | "调课" | null,   # 到勤情况
                 "Class": 3, "ClassEnd": 4,                      # 起止节次
                 "ClassRoom": "A座421", "Rq": "2026-09-22"},
                ...
              ]
            }
        """
        data = await self.post(
            "/atd/weekRank",
            token,
            {
                "orgaId": str(orga_id),
                "userSerial": str(user_serial),
                "filterWeek": filter_week,
            },
        )
        return self._extract_data(data, "/atd/weekRank")

    @staticmethod
    def _extract_data(payload: dict[str, Any], path: str) -> dict[str, Any]:
        """校验业务码并取出 Data。成功码是字符串 '600'。"""
        if not isinstance(payload, dict):
            raise WedsError("接口 %s 返回格式异常" % path)

        code = payload.get("Code", payload.get("code"))
        if str(code) != str(SUCCESS_CODE):
            msg = payload.get("Msg") or payload.get("msg") or "未知错误"
            raise WedsError("接口返回异常 (Code=%s): %s" % (code, msg))

        data = payload.get("Data", payload.get("data"))
        if not isinstance(data, dict):
            raise WedsError("接口 %s 未返回 Data 字段" % path)
        return data

    async def verify_token(
        self, token: str, user_serial: str, orga_id: str = DEFAULT_ORGA_ID
    ) -> tuple[bool, str]:
        """校验 token 是否可用。返回 (是否有效, 说明)。"""
        if not token:
            return False, "未提供 token"
        try:
            data = await self.week_rank(token, user_serial, orga_id)
            count = len(data.get("Rank") or [])
            return True, "token 可用（返回 %d 条记录）" % count
        except TokenInvalid as exc:
            return False, "token 失效（%s）" % exc
        except WedsError as exc:
            return False, "校验失败: %s" % exc

    async def check_token(
        self, token: str, user_serial: str, orga_id: str = DEFAULT_ORGA_ID
    ) -> str:
        """token 体检。返回状态码:

        ok        —— token 有效
        invalid   —— token 失效，需要重新获取
        unset     —— 未配置 token
        nouser    —— 缺少可用的学号，无法校验
        error     —— 网络或接口异常（不代表 token 失效）
        """
        if not token:
            return "unset"
        if not user_serial:
            return "nouser"
        try:
            await self.week_rank(token, user_serial, orga_id)
            return "ok"
        except TokenInvalid:
            return "invalid"
        except WedsError as exc:
            if self.log:
                self.log.warning("[CSU] token 体检遇到接口异常: %s", exc)
            return "error"
        except Exception as exc:  # noqa: BLE001
            if self.log:
                self.log.warning("[CSU] token 体检遇到网络异常: %s", exc)
            return "error"


def decode_token_payload(token: str) -> dict[str, Any]:
    """解析 WEDS token 的 payload（不校验签名）。

    实测该 token 是「整体再做一次 base64 的 JWT」::

        原始: ZXlKcmFXUWlPaUl3SWl3aWRIbHdJam9pU2xkVUlpd2lZV3huSWpvaVNGTXlOVF...
        解一层: eyJraWQiOiIwIiwidHlwIjoiSldUIiwiYWxnIjoiSFMyNTYifQ.eyJpYXQiOjE3MDAwMDAwMDAs...
                └──────────── header ────────────┘ └──────────── payload ────────────┘

    （上面为合成示例，非真实 token。）

    payload 里值得关注的字段：
        exp      过期时间（Unix 秒）—— 通常签发后约 15 天
        iat      签发时间
        iss      "weds"
        aud      "H5"
    """
    if not token:
        return {}

    candidates: list[str] = []
    try:
        decoded = base64.b64decode(token + "=" * (-len(token) % 4)).decode("utf-8")
        if decoded.count(".") == 2:
            candidates.append(decoded)
    except Exception:
        pass
    candidates.append(token)  # 也兼容普通 JWT

    for candidate in candidates:
        parts = candidate.split(".")
        if len(parts) != 3:
            continue
        try:
            raw = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
            payload = json.loads(raw)
            if isinstance(payload, dict):
                return payload
        except Exception:
            continue
    return {}


def parse_ts(value: Any) -> float | None:
    """尽量把平台返回的时间字段解析成时间戳。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 1e11 else v
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S"):
        try:
            return time.mktime(time.strptime(text, fmt))
        except ValueError:
            continue
    if text.isdigit():
        return parse_ts(int(text))
    return None
