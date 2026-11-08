"""中南大学统一身份认证（CAS）客户端。

登录链路（已实测确认）::

    GET  https://ca.csu.edu.cn/authserver/login?service=.../sso.jsp
    POST 同上（表单）→ 302 Location: http://csujwc.its.csu.edu.cn/sso.jsp?ticket=ST-...
    GET  ticket → 302 → http://csujwc.its.csu.edu.cn/jsxsd/framework/xsMain.jsp  （会话建立）

密码加密方式（与学校前端一致）:
    AES-128-CBC，明文 = 64 位随机前缀 + 密码，IV = 16 位随机串，
    key = 页面中 pwdEncryptSalt 的值，结果 base64。

教务系统的 accessToken 只用于「互认互选」外部对接，WEDS 到课平台不接受它，
因此 CAS 登录在本插件里属于诊断 / 辅助能力，核心查询不依赖它。
"""

from __future__ import annotations

import asyncio
import base64
import html as _html
import json
import random
import re
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# 学校侧固定入口
CAS_BASE = "https://ca.csu.edu.cn/authserver"
CAS_LOGIN_URL = CAS_BASE + "/login"
CAS_CAPTCHA_CHECK_URL = CAS_BASE + "/checkNeedCaptcha.htl"
CAS_CAPTCHA_IMG_URL = CAS_BASE + "/getCaptcha.htl"

JWC_ROOT = "http://csujwc.its.csu.edu.cn"
JWC_MAIN = JWC_ROOT + "/jsxsd/framework/xsMain.jsp"
# 教务系统对外发放的 JWT（"互认互选"）。注意：WEDS 到课平台不接受这个 token。
JWC_ACCESS_TOKEN_URL = JWC_ROOT + "/jsxsd/External/accessToken.do"

DEFAULT_SERVICE = JWC_ROOT + "/sso.jsp"

# 与学校前端 AES_CHARS 完全一致（注意：不含 I、L、O、U、V、0、1、9）
AES_CHARS = "ABCDEFGHJKMNPQRSTWXYZabcdefhijkmnprstwxyz2345678"

LOGIN_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class CasError(Exception):
    """CAS 登录相关错误。"""


class CaptchaRequired(CasError):
    """需要验证码但未配置自动识别。"""


def _random_string(length: int) -> str:
    return "".join(random.choice(AES_CHARS) for _ in range(length))


def _pkcs7_pad(data: bytes, block: int = 16) -> bytes:
    padding = block - len(data) % block
    return data + bytes([padding]) * padding


def encrypt_password(password: str, salt: str | None) -> str:
    """按学校前端算法加密密码。salt 为空时原样返回。"""
    if not salt:
        return password
    plaintext = _random_string(64) + password
    iv = _random_string(16).encode("utf-8")
    key = salt.strip().encode("utf-8")
    if len(key) != 16:
        # 学校页面理论上是 16 位；异常 salt 直接退回明文，避免把密码加密成错误格式。
        return password
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    encrypted = encryptor.update(_pkcs7_pad(plaintext.encode("utf-8"))) + encryptor.finalize()
    return base64.b64encode(encrypted).decode("utf-8")


def decode_jwt_payload(token: str) -> dict[str, Any]:
    """解析 JWT 的 payload（不校验签名），仅用于诊断展示。"""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


class _InputParser(HTMLParser):
    """收集页面里的 <input> 属性，替代 BeautifulSoup。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inputs: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "input":
            return
        values = {str(key).lower(): (value or "") for key, value in attrs}
        self.inputs.append(values)


def parse_login_page(html: str) -> dict[str, Any]:
    """从 CAS 登录页提取 execution 与 pwdEncryptSalt。"""
    parser = _InputParser()
    try:
        parser.feed(html or "")
    except Exception:
        pass

    info: dict[str, Any] = {"execution": None, "salt": None}
    for node in parser.inputs:
        if node.get("name") == "execution" and node.get("value"):
            info["execution"] = node["value"]
        if node.get("id") in ("pwdEncryptSalt", "pwdDefaultEncryptSalt"):
            if node.get("value") is not None:
                info["salt"] = node["value"]
    return info


class CasSession:
    """维护一个已登录教务系统的 httpx 会话。"""

    def __init__(
        self,
        username: str = "",
        password: str = "",
        service: str = DEFAULT_SERVICE,
        timeout: int = 20,
        proxy: str = "",
        use_ocr: bool = False,
        logger=None,
    ) -> None:
        self.username = (username or "").strip()
        self.password = password or ""
        self.service = service or DEFAULT_SERVICE
        self.timeout = max(5, int(timeout or 20))
        self.proxy = (proxy or "").strip()
        self.use_ocr = use_ocr
        self.log = logger

        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._logged_in = False
        self.last_login_at: float = 0.0

    # ── 生命周期 ──────────────────────────────────────────────────────────

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers={
                    "User-Agent": LOGIN_UA,
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
                timeout=httpx.Timeout(self.timeout),
                proxy=self.proxy or None,
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
        self._logged_in = False

    # ── 登录 ──────────────────────────────────────────────────────────────

    async def login(self, max_retries: int = 3) -> None:
        """执行完整 CAS 登录。成功后会话可用于访问教务系统。"""
        if not self.username or not self.password:
            raise CasError("未配置统一身份认证账号或密码")

        async with self._lock:
            client = self._ensure_client()
            for attempt in range(1, max_retries + 1):
                if self.log:
                    self.log.log("[CSU] 第 %d 次尝试登录统一身份认证…" % attempt)

                login_url = "%s?service=%s" % (CAS_LOGIN_URL, quote(self.service, safe=""))
                try:
                    response = await client.get(login_url)
                except httpx.HTTPError as exc:
                    raise CasError("访问统一身份认证失败：%s" % exc) from exc
                html = response.text

                params = parse_login_page(html)
                if not params["execution"]:
                    raise CasError("登录页解析失败：未找到 execution 参数")

                captcha = ""
                if await self._need_captcha(client):
                    captcha = await self._solve_captcha(client)
                    if not captcha:
                        raise CaptchaRequired(
                            "本次登录需要验证码，且未能自动识别。"
                            "请在配置中开启 use_ocr 并安装 ddddocr。"
                        )

                form = {
                    "username": self.username,
                    "password": encrypt_password(self.password, params["salt"]),
                    "_eventId": "submit",
                    "cllt": "userNameLogin",
                    "dllt": "generalLogin",
                    "lt": "",
                    "execution": params["execution"],
                    "captcha": captcha,
                }

                try:
                    response = await client.post(
                        login_url, data=form, follow_redirects=False
                    )
                except httpx.HTTPError as exc:
                    raise CasError("提交登录表单失败：%s" % exc) from exc

                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location", "")
                    if self.log:
                        self.log.log("[CSU] 认证通过，跳转: %s" % location)
                    await self._follow_redirects(client, location)
                    self._logged_in = True
                    self.last_login_at = time.time()
                    return

                body = response.text
                message = _extract_error(body) or "账号或密码错误"
                if self.log:
                    self.log.warning("[CSU] 登录失败: %s" % message)
                if "验证码" in message and attempt < max_retries:
                    continue
                raise CasError(message)

            raise CasError("超过最大重试次数，登录失败")

    async def _follow_redirects(self, client: httpx.AsyncClient, url: str) -> None:
        """跟随 ticket 跳转，最终落到教务系统主页面。"""
        if not url:
            raise CasError("登录响应未返回跳转地址")
        response = await client.get(url)
        if self.log:
            self.log.log("[CSU] 跳转完成: HTTP %s final=%s" % (response.status_code, response.url))

    async def _need_captcha(self, client: httpx.AsyncClient) -> bool:
        params = {"username": self.username, "_": int(time.time() * 1000)}
        try:
            response = await client.get(CAS_CAPTCHA_CHECK_URL, params=params)
            data = response.json()
            return bool(data.get("isNeed", False))
        except Exception as exc:  # 网络异常时按"不需要"处理，交给登录结果判断
            if self.log:
                self.log.warning("[CSU] 验证码检测失败: %s" % exc)
            return False

    async def _solve_captcha(self, client: httpx.AsyncClient) -> str:
        if not self.use_ocr:
            return ""
        url = "%s?%d" % (CAS_CAPTCHA_IMG_URL, int(time.time() * 1000))
        try:
            response = await client.get(url)
            image = response.content
        except Exception as exc:
            if self.log:
                self.log.warning("[CSU] 验证码下载失败: %s" % exc)
            return ""

        try:
            import ddddocr

            code = await asyncio.to_thread(
                lambda: ddddocr.DdddOcr(show_ad=False).classification(image)
            )
            if self.log:
                self.log.log("[CSU] 验证码识别结果: %r" % code)
            return code or ""
        except Exception as exc:
            if self.log:
                self.log.warning("[CSU] ddddocr 不可用: %s" % exc)
            return ""

    # ── 会话状态 ──────────────────────────────────────────────────────────

    async def ensure_login(self) -> None:
        """确保处于登录态，必要时重新登录。"""
        if self._logged_in and await self.is_alive():
            return
        await self.login()

    async def is_alive(self) -> bool:
        """会话是否仍然有效（被踢回认证页说明已失效）。"""
        try:
            client = self._ensure_client()
            response = await client.get(JWC_MAIN)
            text = response.text
            alive = "ca.csu.edu.cn/authserver" not in str(response.url) and "学生个人中心" in text
            if not alive:
                self._logged_in = False
            return alive
        except Exception:
            self._logged_in = False
            return False

    # ── 教务系统数据 ──────────────────────────────────────────────────────

    async def get_access_token(self) -> str | None:
        """取教务系统发放的 JWT（"互认互选" 外部对接）。

        实测结论：该 token 用于教务系统的外部互认互选，WEDS 到课平台不接受它。
        保留此方法用于诊断与后续对接。
        """
        await self.ensure_login()
        client = self._ensure_client()
        response = await client.get(JWC_ACCESS_TOKEN_URL, follow_redirects=False)
        location = response.headers.get("location", "")
        if "token=" in location:
            return location.split("token=", 1)[1].split("&")[0]
        return None


def _extract_error(html: str) -> str | None:
    """从登录失败页面里提取错误提示。"""
    if not html:
        return None
    if "验证码" in html:
        return "验证码错误"
    for pattern in (
        r'id="showErrorTip"[^>]*>(.*?)</',
        r'id="msg"[^>]*>(.*?)</',
        r'class="auth_error"[^>]*>(.*?)</',
    ):
        match = re.search(pattern, html, re.S)
        if match:
            text = re.sub(r"<[^>]+>", "", match.group(1))
            text = _html.unescape(text).strip()
            if text:
                return text
    return None
