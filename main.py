"""中南大学课表 / 到课查询插件（ShiritaiBot API v2）。

指令::

    /class                          今日课表
    /class 明天                     明日课表（也支持 昨天 / 后天）
    /class 周三                     指定星期
    /class 周                       整周课表
    /class 文字                     强制文字（ShiritaiBot 目前只支持文字）
    /class 日程                     把本周课表导入 ShiritaiBot 日程
    /class 日程 关闭                移除本插件导入的日程
    /bind <学号>                    绑定学号
    /bind @某人 <学号>              帮某人完成首次绑定
    /class unbind                   解绑
    /class status                   查看状态
    /class help                     帮助

仅主人可用::

    /class token <token>            保存 WEDS token
    /class token check              校验 token 是否可用
    /class setup                    抓包获取 token 的教程
    /class refresh [目标]           跳过缓存强制刷新
    /class raw                      查询并附带原始 JSON
    /class bind login <学号> <密码>  保存统一身份认证凭据
    /class login                    手动触发一次教务系统登录（诊断）
    /class jw                       取教务系统 accessToken.do 的 JWT（诊断）

关于 token：WEDS 的 token 与学号无绑定关系，任意有效 token 可查询任意学号，
因此建议由主人在插件配置里统一填一个 token，所有人群聊共用。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo

from shiritaibot.api import (
    Context,
    LLMRequest,
    MessageEvent,
    Plugin,
    PluginSettings,
    PublicError,
    ToolContext,
    Turn,
    hook,
)

try:  # 插件正常加载方式：main.py 作为包内模块运行
    from .cas import (
        CasError,
        CasSession,
        CaptchaRequired,
        decode_jwt_payload,
    )
    from .renderer import (
        DAY_NAMES,
        normalize,
        render_day_text,
        render_week_rank,
        weekday_from_text,
    )
    from .weds import (
        DEFAULT_ORGA_ID,
        TokenInvalid,
        WedsClient,
        WedsError,
        decode_token_payload,
    )
except ImportError:  # 兼容直接以脚本路径加载 / 单文件测试
    from cas import CasError, CasSession, CaptchaRequired, decode_jwt_payload
    from renderer import (
        DAY_NAMES,
        normalize,
        render_day_text,
        render_week_rank,
        weekday_from_text,
    )
    from weds import (
        DEFAULT_ORGA_ID,
        TokenInvalid,
        WedsClient,
        WedsError,
        decode_token_payload,
    )

PLUGIN_ID = "srtb_plugin_csu_attendance"
VERSION = "1.0.1"

WEEKDAY_LABELS = {i: DAY_NAMES[i] for i in range(1, 8)}

# KV 键
KV_BINDINGS = "bindings_v2"          # {"平台:用户ID": {"user_serial":..., "bound_at":...}}
KV_TOKEN = "weds_token"              # {"value": ..., "updated_at": ...}
KV_OWNER_KEY = "owner_key"           # 最近一次使用本插件的主人的绑定键
KV_SCHEDULE_KEYS = "schedule_keys"   # 本插件写入日程的 routine id 列表
KV_LEGACY_BINDINGS = "bindings"      # 旧版按会话存的绑定，仅用于清理

# 接口原始数据缓存时长（秒），避免频繁打学校接口
RAW_CACHE_TTL = 30 * 60

# token 体检状态
HEALTH_OK = "ok"
HEALTH_INVALID = "invalid"
HEALTH_UNSET = "unset"
HEALTH_NOUSER = "nouser"
HEALTH_ERROR = "error"

HEALTH_LABEL = {
    HEALTH_OK: "✅ 有效",
    HEALTH_INVALID: "❌ 已失效",
    HEALTH_UNSET: "➖ 未配置",
    HEALTH_NOUSER: "⚠️ 缺少学号，无法校验",
    HEALTH_ERROR: "⚠️ 网络/接口异常",
}


class CsuAttendance(Plugin):
    def __init__(self, context: Context, config: PluginSettings | None = None):
        super().__init__(context, config)
        self.config = config or {}
        self._bindings: dict[str, dict] = {}
        self._token_cache: str = ""
        self._config_token: str = ""
        self._config_password: str = ""
        self._token_updated_at: float = 0.0
        # 接口原始数据缓存: (user_serial, filter_week) -> (expire_at, data)
        self._raw_cache: dict[tuple[str, int], tuple[float, dict]] = {}
        # token 体检
        self._health_task: asyncio.Task | None = None
        self._health_status: str = HEALTH_UNSET
        self._health_checked_at: float = 0.0
        self._health_message: str = ""
        self._alerted_token: str = ""
        # 日程同步
        self._schedule_task: asyncio.Task | None = None
        self._schedule_keys: list[str] = []
        self._owner_key: str = ""
        self._tz_name: str = str(self._cfg("timezone") or "Asia/Shanghai")
        try:
            self._tz = ZoneInfo(self._tz_name)
        except Exception:
            # 极简 Python 环境可能没有 tzdata；中国没有夏令时，固定 +08:00 等价。
            self._tz = timezone(timedelta(hours=8))
            self._tz_name = "Asia/Shanghai"

        cfg = self._cfg
        self._cas = CasSession(
            username=cfg("cas_username") or "",
            password="",  # 在 initialize 里从 Vault 解析
            service=cfg("cas_service") or "",
            timeout=int(cfg("request_timeout") or 20),
            proxy=cfg("http_proxy") or "",
            use_ocr=bool(cfg("use_ocr")),
            logger=getattr(self, "logger", None),
        )
        self._weds = WedsClient(
            base=cfg("weds_base") or "",
            timeout=int(cfg("request_timeout") or 20),
            proxy=cfg("http_proxy") or "",
            logger=getattr(self, "logger", None),
        )

    # ── 配置读取 ──────────────────────────────────────────────────────────

    def _cfg(self, key: str):
        if isinstance(self.config, dict):
            return self.config.get(key)
        try:
            return self.config[key]
        except Exception:
            return None

    def _as_int(self, key: str, default: int) -> int:
        try:
            return int(float(self._cfg(key)))
        except (TypeError, ValueError):
            return default

    def _as_float(self, key: str, default: float) -> float:
        try:
            return float(self._cfg(key))
        except (TypeError, ValueError):
            return default

    def _as_bool(self, key: str, default: bool = False) -> bool:
        value = self._cfg(key)
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on", "是", "开"}

    def _orga_id(self) -> str:
        return str(self._cfg("orga_id") or DEFAULT_ORGA_ID)

    async def _resolve_secret(self, value) -> str:
        text = str(value or "")
        if not text:
            return ""
        if text.startswith("secret:"):
            try:
                return await self.context.resolve_secret(text)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("[CSU] 凭据解析失败（%s）" % type(exc).__name__)
                return ""
        return text

    # ── 生命周期 ──────────────────────────────────────────────────────────

    async def initialize(self):
        saved_bindings = await self.get_kv_data(KV_BINDINGS, {})
        if isinstance(saved_bindings, dict):
            self._bindings = saved_bindings
        saved_token = await self.get_kv_data(KV_TOKEN, {})
        if isinstance(saved_token, dict) and saved_token.get("value"):
            self._token_cache = saved_token["value"]
            self._token_updated_at = float(saved_token.get("updated_at") or 0)
        self._owner_key = await self.get_kv_data(KV_OWNER_KEY, "") or ""
        keys = await self.get_kv_data(KV_SCHEDULE_KEYS, [])
        self._schedule_keys = [str(key) for key in keys] if isinstance(keys, list) else []

        self._config_token = await self._resolve_secret(self._cfg("weds_token"))
        self._config_password = await self._resolve_secret(self._cfg("cas_password"))
        self._cas.password = self._config_password

        self.logger.log(
            "[CSU] 插件已加载：%d 个绑定，WEDS token %s，日程同步 %s"
            % (
                len(self._bindings),
                "已配置" if self._current_token() else "未配置",
                "开启" if self._as_bool("schedule_sync") else "关闭",
            )
        )

        await self._purge_legacy_bindings()

        if self._as_bool("token_check_enable", True):
            self._health_task = asyncio.create_task(self._health_loop())
        if self._as_bool("schedule_sync"):
            self._schedule_task = asyncio.create_task(self._schedule_loop())

    async def terminate(self):
        for task in (self._health_task, self._schedule_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._health_task = None
        self._schedule_task = None
        await self._cas.close()
        await self._weds.close()

    # ── token 自动体检 ────────────────────────────────────────────────────

    def _health_user_serial(self) -> str:
        """挑一个学号用于体检：优先主人的绑定，其次任意绑定，最后 CAS 账号。"""
        if self._owner_key and self._owner_key in self._bindings:
            serial = self._bindings[self._owner_key].get("user_serial")
            if serial:
                return str(serial)
        for binding in self._bindings.values():
            serial = binding.get("user_serial")
            if serial:
                return str(serial)
        return self._cas.username or ""

    async def _health_loop(self) -> None:
        """后台定时体检 token，失效时告警一次。"""
        interval_hours = self._as_float("token_check_interval_hours", 6.0)
        await asyncio.sleep(2)
        while True:
            try:
                await self._run_health_check()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.error("[CSU] token 体检任务异常", exc=None)

            if interval_hours <= 0:
                self.logger.log("[CSU] token 体检间隔为 0，仅启动时检查一次")
                return
            try:
                await asyncio.sleep(interval_hours * 3600)
            except asyncio.CancelledError:
                raise

    async def _run_health_check(self, notify: bool = True) -> str:
        """执行一次 token 体检。返回状态码。"""
        token = self._current_token()
        status = await self._weds.check_token(
            token=token,
            user_serial=self._health_user_serial(),
            orga_id=self._orga_id(),
        )
        self._health_status = status
        self._health_checked_at = time.time()
        self._health_message = HEALTH_LABEL.get(status, status)

        if status == HEALTH_OK:
            self.logger.log("[CSU] token 体检：有效")
            self._alerted_token = ""
            return status

        if status == HEALTH_INVALID:
            self.logger.warning("[CSU] token 体检：已失效")
            # 同一个 token 只告警一次
            if notify and token and token != self._alerted_token:
                self._alerted_token = token
                await self._notify_token_invalid()
            return status

        self.logger.log("[CSU] token 体检：%s" % HEALTH_LABEL.get(status, status))
        return status

    async def _notify_token_invalid(self) -> None:
        text = (
            "⚠️ 中南大学到课插件：WEDS token 已失效\n"
            "─────────────────────────\n"
            "后台体检在 %s 检测到 token 不可用，目前所有人都查不到到课数据。\n\n"
            "请重新抓包一次，然后发送：\n"
            "/class token <新 token>\n\n"
            "发送 /class setup 可查看手机抓包的详细步骤。"
            % time.strftime("%Y-%m-%d %H:%M", time.localtime(self._health_checked_at))
        )
        target = str(self._cfg("alert_session") or "").strip()
        try:
            if target:
                await self.context.send_message(target, text)
            else:
                await self.context.notify_owner(text)
            self.logger.log("[CSU] 失效告警已发送")
        except Exception:
            self.logger.warning("[CSU] 失效告警发送失败")

    async def _maybe_recheck_token(self) -> None:
        """绑定补上后，如果之前因缺学号没法体检，补一次。"""
        if self._current_token() and self._health_status in (HEALTH_NOUSER, HEALTH_UNSET):
            await self._run_health_check(notify=False)

    # ── token 与绑定管理 ──────────────────────────────────────────────────

    def _current_token(self) -> str:
        return (self._config_token or "").strip() or self._token_cache

    async def _save_token(self, token: str) -> None:
        self._token_cache = token
        self._config_token = token
        self._token_updated_at = time.time()
        await self.put_kv_data(
            KV_TOKEN, {"value": token, "updated_at": self._token_updated_at}
        )
        # 同步写回插件配置（secret 字段会由宿主加密），否则重载后配置里的旧
        # token 会重新盖过命令设置的新 token。
        self.config["weds_token"] = token
        saver = getattr(self.config, "save_config", None)
        if callable(saver):
            try:
                await saver()
            except Exception:
                self.logger.warning("[CSU] 新 token 写入插件配置失败，已保存在 KV 中")

    @staticmethod
    def _user_key(platform_id: str, user_id: str) -> str:
        return "%s:%s" % (platform_id or "?", str(user_id or "?"))

    def _event_key(self, event: MessageEvent) -> str:
        return self._user_key(event.platform, event.sender_id)

    def _binding_for_event(self, event: MessageEvent) -> dict | None:
        return self._bindings.get(self._event_key(event))

    def _binding_for_actor(self, actor: str) -> tuple[str, dict] | None:
        """按 actor（平台用户 ID）找绑定；ToolContext 没有 platform，只能按后缀匹配。"""
        actor = str(actor or "")
        if not actor:
            return None
        preferred = "qq:" + actor
        if preferred in self._bindings:
            return preferred, self._bindings[preferred]
        for key, binding in self._bindings.items():
            if key.rsplit(":", 1)[-1] == actor:
                return key, binding
        return None

    def _self_id(self, event: MessageEvent) -> str:
        """机器人自己的 QQ 号。

        群里唤醒 Bot 时，消息里会带一个 @Bot 的 at 段；它不能当成 /bind 的目标。
        OneBot 原始事件里的 self_id 最可靠，其次问协议端。
        """
        raw = event.raw or {}
        for key in ("self_id", "selfId", "bot_id"):
            value = raw.get(key)
            if value:
                return str(value)
        try:
            platform = self.context.get_platform(event.adapter)
        except Exception:
            platform = None
        return str(getattr(platform, "self_id", "") or "")

    def _at_target(self, event: MessageEvent) -> str:
        """取消息里被 @ 的用户 ID，忽略 @全体 和 @Bot 自己。

        没有有效的 @ 目标时返回空串，/bind 会按“绑定自己”处理。
        """
        self_id = self._self_id(event)
        targets: list[str] = []
        sources = list(event.segments or [])
        raw = event.raw or {}
        sources += list(raw.get("message") or [])
        for segment in sources:
            if not isinstance(segment, dict) or segment.get("type") != "at":
                continue
            data = segment.get("data") or {}
            target = str(data.get("qq") or "")
            if target and target != "all" and target != self_id and target not in targets:
                targets.append(target)
        return targets[0] if targets else ""

    async def _group_member_name(self, event: MessageEvent, target: str) -> str:
        """尽量取群名片/昵称做展示，取不到就返回空串，避免把 QQ 号当名字。"""
        if not event.group_id or not event.adapter:
            return ""
        try:
            platform = self.context.get_platform(event.adapter)
        except Exception:
            return ""
        if platform is None:
            return ""
        try:
            info = await asyncio.wait_for(
                platform.call(
                    "get_group_member_info",
                    {
                        "group_id": int(event.group_id),
                        "user_id": int(target),
                        "no_cache": False,
                    },
                ),
                2.5,
            )
        except Exception:
            return ""
        if isinstance(info, dict):
            return str(info.get("card") or info.get("nickname") or "").strip()
        return ""

    async def _target_label(self, event: MessageEvent, target: str) -> str:
        if target == event.sender_id:
            return "你"
        name = await self._group_member_name(event, target)
        return "@" + name if name else "TA"

    async def _save_bindings(self) -> None:
        await self.put_kv_data(KV_BINDINGS, self._bindings)

    async def _purge_legacy_bindings(self) -> None:
        """清掉旧版按会话存的绑定（它们会导致同群串号）。"""
        try:
            legacy = await self.get_kv_data(KV_LEGACY_BINDINGS, None)
        except Exception:
            return
        if legacy:
            count = len(legacy) if isinstance(legacy, (dict, list)) else 0
            await self.put_kv_data(KV_LEGACY_BINDINGS, {})
            if count:
                self.logger.log("[CSU] 已清理 %d 条旧版按会话的绑定" % count)

    async def _remember_owner(self, event: MessageEvent) -> None:
        if not event.is_owner:
            return
        key = self._event_key(event)
        if key and key != self._owner_key:
            self._owner_key = key
            await self.put_kv_data(KV_OWNER_KEY, key)

    # ── 教务系统登录 ──────────────────────────────────────────────────────

    async def _ensure_cas(self) -> str | None:
        """尝试登录教务系统。返回 None 表示成功，否则返回错误说明。"""
        if not self._cas.username or not self._cas.password:
            return (
                "未配置统一身份认证账号或密码（可在插件配置中填写，"
                "或使用 /class bind login <学号> <密码>）"
            )
        try:
            await self._cas.ensure_login()
            return None
        except CaptchaRequired as exc:
            return str(exc)
        except CasError as exc:
            return "教务系统登录失败：%s" % exc
        except Exception as exc:  # noqa: BLE001 - 网络层异常统一兜底
            self.logger.error("[CSU] CAS 登录异常", exc=exc)
            return "教务系统登录异常：%s" % exc

    # ── 查询主流程 ────────────────────────────────────────────────────────

    # 目标关键字 -> 相对今天的天数
    _REL_DAYS = {
        "": 0, "今天": 0, "今日": 0, "today": 0, "本日": 0,
        "明天": 1, "明日": 1, "tomorrow": 1,
        "昨天": -1, "昨日": -1, "yesterday": -1,
        "后天": 2,
    }
    # 整周视图关键字
    _WEEK_WORDS = {"周", "整周", "本周", "week", "全部", "all", "一览", "整周课表"}

    def _today(self) -> date:
        return datetime.now(self._tz).date()

    def _resolve_target(self, text: str) -> tuple[str, int, str] | None:
        """把用户参数解析成 (视图类型, 星期编号, 标题)。无法识别返回 None。

        返回的星期编号是接口口径（1=周日 … 7=周六）。
        """
        raw = (text or "").strip().lower()

        if raw in self._WEEK_WORDS:
            return ("week", 0, "本周课表")

        if raw in self._REL_DAYS:
            delta = self._REL_DAYS[raw]
            target = self._today() + timedelta(days=delta)
            weekday = target.isoweekday() % 7 + 1
            if delta == 0:
                title = "今日课表"
            elif delta == 1:
                title = "明日课表"
            elif delta == -1:
                title = "昨日课表"
            elif delta == 2:
                title = "后日课表"
            else:
                title = "课表"
            return ("day", weekday, title)

        weekday = weekday_from_text(raw)
        if weekday:
            return ("day", weekday, "%s课表" % WEEKDAY_LABELS[weekday])

        return None

    async def _fetch(self, user_serial: str, force: bool = False) -> dict:
        cache_key = (str(user_serial), 0)
        cached = self._raw_cache.get(cache_key)
        if not force and cached and cached[0] > time.time():
            self.logger.debug("[CSU] 命中缓存: %s" % user_serial)
            return cached[1]

        token = self._current_token()
        if not token:
            raise PublicError(
                "⚠️ 尚未配置 WEDS token，查询不可用。\n"
                "请联系主人按 /class setup 的指引配置。"
            )
        try:
            data = await self._weds.week_rank(
                token=token,
                user_serial=user_serial,
                orga_id=self._orga_id(),
                filter_week=0,
            )
        except TokenInvalid:
            raise PublicError(
                "⚠️ WEDS token 已失效，查询暂不可用。\n请联系主人更新 token。"
            ) from None
        except WedsError as exc:
            raise PublicError("❌ 查询失败：%s" % exc) from None
        self._raw_cache[cache_key] = (time.time() + RAW_CACHE_TTL, data)
        return data

    async def _query_text(
        self,
        event: MessageEvent,
        target: str = "",
        force: bool = False,
        raw: bool = False,
    ) -> str:
        binding = self._binding_for_event(event) or {}
        user_serial = str(binding.get("user_serial") or "").strip()
        if not user_serial:
            # 未绑定则退回到 CAS 账号（学号即 userSerial）
            user_serial = (self._cas.username or "").strip()
        if not user_serial:
            return (
                "⚠️ 你还没有绑定学号。\n"
                "私聊或群里发送：/bind <你的学号>\n"
                "例如：/bind 2024000001\n"
                "也可以让别人帮你首次绑定：/bind @你 <你的学号>"
            )

        plan = self._resolve_target(target)
        if plan is None:
            return (
                "⚠️ 看不懂参数：%s\n"
                "可用：/class、/class 明天、/class 周三、/class 周" % (target or "")
            )
        kind, weekday, title = plan
        data = await self._fetch(user_serial, force=force)
        label = self._sender_label(event)

        if raw:
            return render_week_rank(
                data, user_serial=user_serial, title=title, raw=True
            )
        if kind == "week":
            return render_week_rank(data, user_serial=label or user_serial, title=title)
        return render_day_text(
            data, weekday, title, label, user_fallback=user_serial
        )

    @staticmethod
    def _sender_label(event: MessageEvent) -> str:
        """用发送者昵称做抬头，避免把学号暴露在群聊里。"""
        try:
            name = event.get_sender_name()
            return str(name).strip() if name else ""
        except Exception:
            return ""

    # ── /class 指令 ───────────────────────────────────────────────────────

    # 仅主人可用的子命令（会改动全群共用凭据或属于诊断）
    ADMIN_ONLY_SUBS = {
        "token", "setup", "配置", "教程", "guide",
        "login", "jw", "raw", "refresh", "刷新", "强制", "日程", "schedule",
    }

    async def _guard(self, event: MessageEvent, source, label: str):
        """驱动子生成器，保证至少产出一条结果，且异常不逃逸。"""
        produced = False
        try:
            async for result in source:
                produced = True
                yield result
        except PublicError as exc:
            yield event.plain_result(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - 任何异常都要转成可见回复
            self.logger.error("[CSU] %s 处理异常" % label, exc=exc)
            yield event.plain_result("❌ %s 处理出错：%s" % (label, exc))
            return

        if not produced:
            yield event.plain_result(
                "⚠️ 这次没有产生任何输出。\n发送 /class help 查看可用用法。"
            )

    @hook.command("class", aliases={"课表"})
    async def class_cmd(self, event: MessageEvent, arg: str = ""):
        """查询课表 / 到课情况：/class [今天|明天|周三|周|文字|日程]"""
        async for result in self._guard(event, self._dispatch_class(event, arg), "/class"):
            yield result

    async def _dispatch_class(self, event: MessageEvent, arg: str):
        await self._remember_owner(event)

        is_owner = bool(event.is_owner)
        raw = (arg or "").strip()
        parts = raw.split(maxsplit=1)
        sub = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub in ("help", "h", "帮助"):
            yield event.plain_result(self._help_text(is_owner))
            return

        if sub == "bind":
            first = rest.split()[0].lower() if rest.split() else ""
            if first == "login":
                if not is_owner:
                    yield event.plain_result(
                        "⛔ `bind login` 会修改全群共用的登录凭据，仅主人可用。\n"
                        "绑定自己的学号请直接用：/bind <你的学号>"
                    )
                    return
                async for r in self._cmd_bind_login(event, rest):
                    yield r
                return
            async for r in self._bind_core(event, rest):
                yield r
            return

        if sub == "unbind":
            async for r in self._cmd_unbind(event):
                yield r
            return

        if sub == "status":
            yield event.plain_result(await self._status_text(event, is_owner))
            return

        if sub in self.ADMIN_ONLY_SUBS and not is_owner:
            yield event.plain_result(
                "⛔ `/class %s` 是主人专属命令。\n"
                "发送 /class help 查看你能用的命令。" % sub
            )
            return

        if sub in ("setup", "配置", "教程", "guide"):
            yield event.plain_result(self._setup_text())
            return

        if sub == "token":
            async for r in self._cmd_token(event, rest):
                yield r
            return

        if sub == "login":
            async for r in self._cmd_login(event):
                yield r
            return

        if sub == "jw":
            async for r in self._cmd_jw_token(event):
                yield r
            return

        if sub == "raw":
            yield event.plain_result(
                await self._query_text(event, target="", force=True, raw=True)
            )
            return

        if sub in ("refresh", "刷新", "强制"):
            yield event.plain_result(await self._query_text(event, target=rest, force=True))
            return

        if sub in ("日程", "schedule"):
            async for r in self._cmd_schedule(event, rest):
                yield r
            return

        # 文字 / 图片 强制切换（ShiritaiBot 只支持文本）
        if sub in ("文字", "text", "txt"):
            yield event.plain_result(await self._query_text(event, target=rest))
            return

        if sub in ("图", "图片", "image", "pic", "img"):
            text = await self._query_text(event, target=rest)
            yield event.plain_result(
                "ℹ️ ShiritaiBot 目前只支持文本回复，已为你输出文字版。\n\n" + text
            )
            return

        # 其余参数统一交给目标解析（今天/明天/周三/周…）
        known = self._resolve_target(sub) is not None
        if known:
            yield event.plain_result(await self._query_text(event, target=sub))
            return

        # /class 明天 这种两段式参数
        combined = ("%s %s" % (sub, rest)).strip() if rest else sub
        if rest and self._resolve_target(combined) is not None:
            yield event.plain_result(await self._query_text(event, target=combined))
            return

        if sub:
            yield event.plain_result(
                "⚠️ 未知参数: %s\n\n%s" % (sub, self._help_text(is_owner))
            )
            return

        yield event.plain_result(await self._query_text(event))

    # ── /bind：绑定学号（支持帮别人首次绑定）─────────────────────────────

    @hook.command("bind", aliases={"绑定"})
    async def bind_cmd(self, event: MessageEvent, arg: str = ""):
        """绑定学号。用法: /bind <学号>  或  /bind @某人 <学号>"""
        async for result in self._guard(event, self._bind_core(event, arg), "/bind"):
            yield result

    async def _bind_core(self, event: MessageEvent, body: str):
        """绑定逻辑（/bind 与 /class bind 共用）。

        body 为去掉命令名后的参数文本，例如 "2024000001" 或 "@某人 2024000001"。
        对别人绑定时：本人可自助改绑，他人仅能帮忙完成首次绑定（主人不受限）。
        """
        await self._remember_owner(event)
        is_owner = bool(event.is_owner)
        at_target = self._at_target(event)
        student_id = self._first_student_id(body)

        if not student_id:
            yield event.plain_result(
                "⚠️ 用法：\n"
                "  /bind <学号>              绑定自己\n"
                "  /bind @某人 <学号>        帮某人绑定\n"
                "例如：/bind 2024000001"
            )
            return

        if not at_target or at_target == event.sender_id:
            self._bindings[self._event_key(event)] = {
                "user_serial": student_id,
                "bound_at": time.time(),
            }
            await self._save_bindings()
            await self._maybe_recheck_token()
            yield event.plain_result(
                "✅ 绑定成功，学号：%s\n发送 /class 查看今日课表。" % student_id
            )
            return

        target_key = self._user_key(event.platform, at_target)
        existing = self._bindings.get(target_key)
        label = await self._target_label(event, at_target)
        if existing and not is_owner:
            yield event.plain_result(
                "⚠️ %s 已经绑定过学号了（%s）。\n"
                "改绑需要主人操作，或让 TA 本人发送 /bind <新学号>。"
                % (label, existing.get("user_serial") or "?")
            )
            return

        self._bindings[target_key] = {
            "user_serial": student_id,
            "bound_at": time.time(),
            "bound_by": event.sender_id,
        }
        await self._save_bindings()
        await self._maybe_recheck_token()

        if existing:
            yield event.plain_result(
                "✅ 已把 %s 的学号由 %s 改为 %s。"
                % (label, existing.get("user_serial") or "?", student_id)
            )
        else:
            yield event.plain_result(
                "✅ 已帮 %s 绑定学号：%s\nTA 现在可以直接 /class 查询了。"
                % (label, student_id)
            )

    @staticmethod
    def _first_student_id(text: str) -> str:
        """从文本里挑出第一个像学号的纯数字串（>=6 位）。"""
        for match in re.finditer(r"\d{6,}", text or ""):
            return match.group(0)
        return ""

    async def _cmd_unbind(self, event: MessageEvent):
        key = self._event_key(event)
        if key in self._bindings:
            del self._bindings[key]
            await self._save_bindings()
            yield event.plain_result("✅ 已解绑。")
        else:
            yield event.plain_result("ℹ️ 当前会话没有绑定记录。")

    async def _cmd_bind_login(self, event: MessageEvent, arg: str):
        """/class bind login <学号> <密码>（仅主人）；普通绑定请用 /bind"""
        tokens = arg.split()
        if len(tokens) < 3:
            yield event.plain_result("⚠️ 用法: /class bind login <学号> <密码>")
            return
        user_serial, password = tokens[1], tokens[2]
        if not user_serial.isdigit() or len(user_serial) < 6:
            yield event.plain_result("⚠️ 学号看起来不对：%s（应为纯数字）" % user_serial)
            return

        self._cas.username = user_serial
        self._cas.password = password
        self.config["cas_username"] = user_serial
        self.config["cas_password"] = password
        saver = getattr(self.config, "save_config", None)
        if callable(saver):
            try:
                await saver()
            except Exception:
                self.logger.warning("[CSU] 插件配置保存失败")
        error = await self._ensure_cas()
        if error:
            yield event.plain_result("⚠️ 凭据已保存，但登录验证未通过：\n%s" % error)
        else:
            yield event.plain_result("✅ 统一身份认证登录成功，凭据已加密保存。")

    # ── 子命令实现 ────────────────────────────────────────────────────────

    async def _cmd_token(self, event: MessageEvent, arg: str):
        arg = (arg or "").strip()
        if not arg:
            token = self._current_token()
            if not token:
                yield event.plain_result("ℹ️ 当前未配置 WEDS token。")
                return
            yield event.plain_result(
                "ℹ️ 当前 token: %s…（共 %d 字符，%s）\n体检状态: %s"
                % (
                    token[:12],
                    len(token),
                    time.strftime(
                        "%Y-%m-%d %H:%M", time.localtime(self._token_updated_at)
                    )
                    if self._token_updated_at
                    else "来源: 插件配置",
                    HEALTH_LABEL.get(self._health_status, "未检查"),
                )
            )
            return

        if arg.lower() == "check":
            status = await self._run_health_check(notify=False)
            user_serial = self._health_user_serial()
            if status == HEALTH_NOUSER:
                yield event.plain_result(
                    "⚠️ 缺少可用学号，无法校验。请先 /bind <学号>。"
                )
                return
            yield event.plain_result(
                ("%s " % HEALTH_LABEL.get(status, status))
                + ("（学号 %s）" % user_serial if user_serial else "")
            )
            return

        await self._save_token(arg)
        uid = self._event_key(event)
        if uid and event.is_owner:
            self._owner_key = uid
            await self.put_kv_data(KV_OWNER_KEY, uid)
        # 新 token 立即体检，顺便清掉失效告警状态
        self._alerted_token = ""
        status = await self._run_health_check(notify=False)
        if status == HEALTH_OK:
            yield event.plain_result(
                "✅ WEDS token 已保存并验证通过（%d 字符）。发送 /class 查看。" % len(arg)
            )
        elif status == HEALTH_NOUSER:
            yield event.plain_result(
                "✅ WEDS token 已保存（%d 字符），但还没有绑定学号，无法验证。\n"
                "请先发送：/bind <你的学号>" % len(arg)
            )
        elif status == HEALTH_ERROR:
            yield event.plain_result(
                "⚠️ WEDS token 已保存（%d 字符），但验证时网络/接口异常，稍后会自动复检。"
                % len(arg)
            )
        else:
            yield event.plain_result(
                "❌ WEDS token 已保存，但验证未通过（token 被拒绝）。\n"
                "请确认复制的是 Authorization 头里 Token 后面的完整字符串。"
            )

    async def _cmd_login(self, event: MessageEvent):
        yield event.plain_result("🔐 正在登录统一身份认证…")
        error = await self._ensure_cas()
        if error:
            yield event.plain_result("❌ %s" % error)
            return
        yield event.plain_result("✅ 教务系统会话已建立。")

    async def _cmd_jw_token(self, event: MessageEvent):
        """诊断：取教务系统 accessToken.do 的 JWT。"""
        error = await self._ensure_cas()
        if error:
            yield event.plain_result("❌ %s" % error)
            return
        try:
            token = await self._cas.get_access_token()
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result("❌ 获取失败：%s" % exc)
            return
        if not token:
            yield event.plain_result("⚠️ 教务系统未返回 accessToken。")
            return
        payload = decode_jwt_payload(token)
        exp = payload.get("exp")
        exp_text = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp)) if exp else "未知"
        )
        yield event.plain_result(
            "ℹ️ 教务系统 accessToken（互认互选专用）：\n"
            "%s\n\n"
            "payload: %s\n"
            "过期时间: %s\n\n"
            "⚠️ 实测该 token 无法用于 WEDS 到课平台（返回 401 token失效），"
            "仅作诊断用途。" % (token, payload, exp_text)
        )

    async def _status_text(self, event: MessageEvent, is_owner: bool = False) -> str:
        binding = self._binding_for_event(event) or {}
        bound_at = binding.get("bound_at")

        if not is_owner:
            lines = [
                "📊 你的到课查询状态",
                "─────────────────────────",
                "绑定学号: %s" % (binding.get("user_serial") or "未绑定"),
                "绑定时间: %s"
                % (
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(bound_at))
                    if bound_at
                    else "-"
                ),
                "查询服务: %s"
                % ("可用" if self._current_token() else "暂不可用（请联系主人）"),
            ]
            if not binding.get("user_serial"):
                lines.append("")
                lines.append("发送 /bind <你的学号> 完成绑定。")
            return "\n".join(lines)

        token = self._current_token()
        cas_ready = bool(self._cas.username and self._cas.password)
        lines = [
            "📊 中南大学到课插件状态（主人视图）",
            "─────────────────────────",
            "版本: %s" % VERSION,
            "绑定学号: %s" % (binding.get("user_serial") or "未绑定"),
            "绑定时间: %s"
            % (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(bound_at))
                if bound_at
                else "-"
            ),
            "统一身份认证: %s" % ("已配置" if cas_ready else "未配置"),
            "教务会话: %s" % ("已建立" if self._cas._logged_in else "未建立"),
            "WEDS token: %s" % ("已配置（%d 字符）" % len(token) if token else "未配置"),
            "token 体检: %s%s"
            % (
                HEALTH_LABEL.get(self._health_status, "未检查"),
                (
                    "（%s）"
                    % time.strftime(
                        "%m-%d %H:%M", time.localtime(self._health_checked_at)
                    )
                    if self._health_checked_at
                    else ""
                ),
            ),
            "WEDS 地址: %s" % self._weds.base,
            "机构 ID: %s" % self._orga_id(),
            "已绑定会话数: %d" % len(self._bindings),
            "已导入日程项: %d" % len(self._schedule_keys),
            "日程自动同步: %s" % ("开启" if self._as_bool("schedule_sync") else "关闭"),
        ]
        if token:
            lines.append("")
            expires = self._token_expiry(token)
            if expires:
                lines.append(expires)
            lines.append("提示: 发送 /class token check 可立即校验 token。")
            lines.append("提示: 发送 /class setup 可查看抓包配置教程。")
        else:
            lines.append("")
            lines.append("⚠️ 还没有配置 token，查询不可用。发送 /class setup 看教程。")
        if self._health_status == HEALTH_INVALID:
            lines.append("")
            lines.append("❌ token 已失效，请发送 /class token <新 token> 更新。")
        return "\n".join(lines)

    @staticmethod
    def _setup_text() -> str:
        return (
            "🔑 配置 WEDS token（只做一次，之后全群共用）\n"
            "─────────────────────────\n"
            "💡 token 与学号无关：任意有效 token 可查任意学号，\n"
            "   所以只需要主人抓一次，不需要每个同学各自抓。\n"
            "\n"
            "【手机抓包步骤】\n"
            "1. 装一个抓包 App（任一即可）：\n"
            "   · iOS：Stream、Thor\n"
            "   · Android：Reqable、HttpCanary（小黄鸟）\n"
            "2. 按 App 引导安装它的 CA 证书\n"
            "   iOS：设置 → 通用 → VPN与设备管理 装描述文件，\n"
            "        再去 设置 → 通用 → 关于本机 → 证书信任设置 里打开信任\n"
            "   Android：设置 → 安全 → 加密与凭据 → 安装证书\n"
            "3. 开启抓包，然后打开那个「到课 / 课表」小程序，\n"
            "   随便翻一下页面，让它发出请求\n"
            "4. 在抓到的请求里找域名 wedsk12.weds.com.cn\n"
            "5. 打开该请求的请求头，复制 Authorization 这一行里\n"
            "   「Token 」后面的那串字符（不含 Token 和空格）\n"
            "\n"
            "【提交】\n"
            "把复制到的内容发给机器人：\n"
            "/class token <粘贴的内容>\n"
            "（建议私聊发，避免 token 暴露在群里）\n"
            "\n"
            "提交后插件会立即验证，通过才算配置成功。\n"
            "之后插件会每 6 小时自动体检一次，失效时主动私聊主人告警。"
        )

    @staticmethod
    def _token_expiry(token: str) -> str:
        """从 token 里解出过期时间，返回一句给用户看的说明。"""
        payload = decode_token_payload(token)
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)) or exp <= 0:
            return ""
        left = (exp - time.time()) / 86400.0
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(exp))
        if left <= 0:
            return "⏰ token 已于 %s 过期" % when
        return "⏰ token 有效期至 %s（剩余 %.1f 天）" % (when, left)

    @staticmethod
    def _help_text(is_owner: bool = False) -> str:
        """普通用户只看得到日常命令；主人额外看到运维命令。"""
        lines = [
            "📚 中南大学课表与到课查询",
            "─────────────────────────",
            "/class                  今日课表",
            "/class 明天             明日课表（也支持 昨天 / 后天）",
            "/class 周三             指定星期（周一~周日）",
            "/class 周               整周课表",
            "/bind <学号>            绑定你的学号",
            "/bind @某人 <学号>      帮某人绑定（TA 未绑定过时）",
            "/class unbind           解绑",
            "/class status           查看你的绑定状态",
            "/class help             显示本帮助",
        ]
        if is_owner:
            lines += [
                "",
                "──── 主人命令 ────",
                "/class token <token>    设置 WEDS token（保存后自动验证）",
                "/class token check      校验 token 是否有效",
                "/class setup            抓包获取 token 的教程",
                "/class 日程             把本周课表导入 ShiritaiBot 日程",
                "/class 日程 关闭        移除本插件导入的日程",
                "/class refresh [目标]   跳过缓存强制刷新",
                "/class raw              附带接口原始 JSON",
                "/class bind login <学号> <密码>",
                "                       保存统一身份认证凭据（加密）",
                "/class login            手动登录教务系统（诊断）",
                "/class jw               查看教务系统 accessToken（诊断）",
            ]
        return "\n".join(lines)

    # ── 日程（Routine）集成 ───────────────────────────────────────────────

    def _routine_store(self):
        """返回宿主 Store；当前 ShiritaiBot 的 v2 Context 通过 host 暴露它。"""
        host = getattr(self.context, "host", None)
        store = getattr(host, "store", None)
        if store is None:
            raise PublicError("当前 ShiritaiBot 版本不支持日程同步（找不到日程存储）")
        return store

    def _schedule_owner_serial(self) -> str:
        if self._owner_key and self._owner_key in self._bindings:
            serial = self._bindings[self._owner_key].get("user_serial")
            if serial:
                return str(serial)
        return ""

    def _course_datetimes(self, course: dict) -> tuple[datetime, datetime] | None:
        """把一条课程记录转成带时区的起止时间；数据不完整时返回 None。"""
        day_text = str(course.get("date_full") or "").strip()
        begin_text = str(course.get("time_begin") or "").strip()
        if not day_text or not begin_text:
            return None
        try:
            day = date.fromisoformat(day_text[:10])
            start_clock = dt_time.fromisoformat(begin_text)
        except ValueError:
            return None
        start = datetime.combine(day, start_clock, tzinfo=self._tz)
        end_text = str(course.get("time_end") or "").strip()
        end_clock = None
        if end_text:
            try:
                end_clock = dt_time.fromisoformat(end_text)
            except ValueError:
                end_clock = None
        if end_clock is None:
            end = start + timedelta(minutes=45)
        else:
            end = datetime.combine(day, end_clock, tzinfo=self._tz)
            if end <= start:
                end = start + timedelta(minutes=45)
        return start, end

    def _routine_key(self, student: str, course: dict, prefix: str = "course") -> str:
        raw = "%s|%s|%s|%s|%s" % (
            student,
            course.get("date_full") or "",
            course.get("class_start") or 0,
            course.get("name") or "",
            course.get("room") or "",
        )
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
        day = str(course.get("date_full") or "").replace("-", "")[:8] or "00000000"
        return "csu:%s:%s:%02d:%s" % (student, day, int(course.get("class_start") or 0), digest)

    async def _sync_schedule(self, student: str, remove: bool = False) -> dict:
        """把一位学生的本周课表写入 ShiritaiBot 日程。

        - 课程：kind=course，只用于查询/展示，不触发提醒；
        - 提醒：kind=reminder，按 class_reminder_minutes 提前排程，由宿主的日程调度器
          触发并经过主动性引擎，属于主人设定的提醒，不会被分诊丢弃。
        """
        store = self._routine_store()
        if remove:
            removed = 0
            for key in self._schedule_keys:
                await store.delete("routine", key)
                removed += 1
            self._schedule_keys = []
            await self.put_kv_data(KV_SCHEDULE_KEYS, [])
            return {"removed": removed, "courses": 0, "reminders": 0}

        data = await self._fetch(student, force=True)
        info = normalize(data)
        lead = max(0, self._as_int("class_reminder_minutes", 15))
        now = datetime.now(self._tz)

        new_keys: list[str] = []
        courses_written = 0
        reminders_written = 0
        skipped = 0

        for course in info["courses"]:
            times = self._course_datetimes(course)
            if times is None:
                skipped += 1
                continue
            start, end = times
            title = str(course.get("name") or "课程")
            room = str(course.get("room") or "").strip()
            if room:
                title = "%s · %s" % (title, room)

            key = self._routine_key(student, course)
            new_keys.append(key)
            existing = await store.get("routine", key) or {}
            status = existing.get("status") or "open"
            if status in {"completed", "cancelled"}:
                # 用户已经手动结束这条课程，不再复活。
                continue
            await store.put(
                "routine",
                key,
                {
                    "kind": "course",
                    "title": title,
                    "due": start.isoformat(),
                    "end": end.isoformat(),
                    "cron": "",
                    "timezone": self._tz_name,
                    "status": "open",
                    "next_fire": None,
                    "source": "csu_attendance",
                    "managed_by": PLUGIN_ID,
                    "student": student,
                    "course_key": key,
                    "room": room,
                    "period": course.get("period") or "",
                },
                "owner",
            )
            courses_written += 1

            if lead <= 0:
                continue
            remind_at = start - timedelta(minutes=lead)
            rem_key = key + ":rem"
            rem_existing = await store.get("routine", rem_key) or {}
            rem_status = rem_existing.get("status") or "scheduled"
            if rem_status in {"fired", "cancelled", "completed"}:
                # 同一个提醒已经触发过，保留记录，不重发也不删除。
                new_keys.append(rem_key)
                continue
            if remind_at <= now and not rem_existing:
                # 这条课已经开始了，不补建提醒。
                continue
            new_keys.append(rem_key)
            await store.put(
                "routine",
                rem_key,
                {
                    "kind": "reminder",
                    "title": "上课提醒：%s（%s 开始）"
                    % (title, start.strftime("%H:%M")),
                    "due": remind_at.isoformat(),
                    "end": None,
                    "cron": "",
                    "timezone": self._tz_name,
                    "status": "scheduled",
                    "next_fire": remind_at.timestamp(),
                    "source": "csu_attendance",
                    "managed_by": PLUGIN_ID,
                    "student": student,
                    "course_key": key,
                    "room": room,
                },
                "owner",
            )
            reminders_written += 1

        # 清掉上一次同步写入、这一次不再存在的项（跨周、退课、换教室等）。
        stale = set(self._schedule_keys) - set(new_keys)
        for key in stale:
            await store.delete("routine", key)

        self._schedule_keys = new_keys
        await self.put_kv_data(KV_SCHEDULE_KEYS, new_keys)
        self.logger.log(
            "[CSU] 日程同步完成：%d 节课，%d 条提醒，清理 %d 条旧项"
            % (courses_written, reminders_written, len(stale))
        )
        return {
            "courses": courses_written,
            "reminders": reminders_written,
            "removed": len(stale),
            "skipped": skipped,
            "week_now": info.get("week_now") or "",
            "week_num": info.get("week_num") or "",
        }

    async def _cmd_schedule(self, event: MessageEvent, arg: str):
        if (arg or "").strip().lower() in {"关闭", "off", "清除", "remove", "stop"}:
            result = await self._sync_schedule("", remove=True)
            yield event.plain_result(
                "✅ 已移除本插件导入的日程（%d 项）。\n"
                "手动创建的其他日程不受影响。" % result["removed"]
            )
            return
        binding = self._bindings.get(self._event_key(event)) or {}
        student = str(binding.get("user_serial") or "").strip()
        if not student:
            student = self._schedule_owner_serial()
        if not student:
            student = (self._cas.username or "").strip()
        if not student:
            yield event.plain_result(
                "⚠️ 还没有绑定自己的学号，无法导入日程。\n请先发送：/bind <你的学号>"
            )
            return
        try:
            result = await self._sync_schedule(student)
        except PublicError as exc:
            yield event.plain_result(str(exc))
            return
        if student and event.is_owner:
            self._owner_key = self._event_key(event)
            await self.put_kv_data(KV_OWNER_KEY, self._owner_key)
        yield event.plain_result(
            "✅ 课表已导入 ShiritaiBot 日程\n"
            "─────────────────────────\n"
            "本周：第 %s 周 / 共 %s 周\n"
            "写入课程：%d 节\n"
            "创建上课提醒：%d 条%s\n"
            "清理旧日程：%d 条\n\n"
            "现在可以用「日程」页面查看，也可以直接问我「今天有什么课」。\n"
            "提醒会由 ShiritaiBot 的日程系统发出；关闭提醒请把配置里的"
            "「上课提醒提前量」设为 0。"
            % (
                result.get("week_now") or "?",
                result.get("week_num") or "?",
                result["courses"],
                result["reminders"],
                "（提前 %d 分钟）" % self._as_int("class_reminder_minutes", 15)
                if result["reminders"]
                else "",
                result["removed"],
            )
        )

    async def _schedule_loop(self) -> None:
        """主人开启自动同步后，定期刷新课表，跨周自动替换旧课程。"""
        interval = max(1.0, self._as_float("schedule_sync_hours", 6.0))
        await asyncio.sleep(20)
        while True:
            try:
                student = self._schedule_owner_serial()
                if student:
                    await self._sync_schedule(student)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("[CSU] 日程自动同步失败：%s" % exc)
            try:
                await asyncio.sleep(interval * 3600)
            except asyncio.CancelledError:
                raise

    # ── LLM 工具 ──────────────────────────────────────────────────────────

    def _tool_binding(self, ctx: ToolContext) -> dict:
        found = self._binding_for_actor(ctx.actor)
        if found:
            return found[1]
        if ctx.owner and self._owner_key:
            return self._bindings.get(self._owner_key) or {}
        if ctx.owner:
            # 主人可能只绑过 CAS 账号
            serial = (self._cas.username or "").strip()
            if serial:
                return {"user_serial": serial}
        raise PublicError("还没有绑定学号，请先发送 /bind <学号>")

    @hook.llm_tool("csu_attendance", risk="safe", chat=True)
    async def csu_attendance_tool(self, ctx: ToolContext, day: str = ""):
        """查询中南大学课表与到课 / 打卡情况。

        Args:
            day(string): today、tomorrow、yesterday、week，或 周一…周日；留空表示今天
        """
        binding = self._tool_binding(ctx)
        user_serial = str(binding.get("user_serial") or "").strip()
        if not user_serial:
            raise PublicError("绑定记录缺少学号，请重新发送 /bind <学号>")

        target = (day or "").strip().lower()
        aliases = {
            "today": "今天",
            "tomorrow": "明天",
            "yesterday": "昨天",
            "week": "周",
            "this week": "周",
        }
        target = aliases.get(target, target)
        plan = self._resolve_target(target)
        if plan is None:
            raise PublicError("无法识别 day 参数，可用 today / tomorrow / week / 周一…周日")

        try:
            data = await self._fetch(user_serial)
        except PublicError as exc:
            raise PublicError(str(exc)) from None

        kind, weekday, title = plan
        if kind == "week":
            return render_week_rank(data, user_serial="你", title=title)
        return render_day_text(data, weekday, title, "你", user_fallback=user_serial)

    @hook.llm_tool("csu_sync_schedule", risk="normal")
    async def csu_sync_schedule_tool(self, ctx: ToolContext):
        """把主人绑定的本周课表写入 ShiritaiBot 日程，并创建上课提醒。

        仅在主人私聊 / WebUI 中可用；写入后可用 routine_list 查看。
        """
        if not ctx.owner or ctx.group:
            raise PublicError("这个工具只能由主人在私聊 / WebUI 中使用")
        binding = self._tool_binding(ctx)
        student = str(binding.get("user_serial") or "").strip()
        if not student:
            raise PublicError("绑定记录缺少学号，请重新发送 /bind <学号>")
        result = await self._sync_schedule(student)
        return {
            "ok": True,
            "student": student,
            **{key: result.get(key) for key in ("courses", "reminders", "removed", "week_now", "week_num")},
            "note": "课程已写入日程，可用 routine_list 查看；提醒由日程系统触发。",
        }

    @hook.on_llm_request()
    async def add_csu_hint(self, turn: Turn, request: LLMRequest):
        if request.step == 0 and turn.owner and not turn.background:
            request.add_context(
                "<csu_attendance>主人问课表、到课、打卡或上课时间时，可调用 "
                "csu_attendance；要刷新日程里的课程可调用 csu_sync_schedule，"
                "已导入的课程也能用 routine_list 查看。</csu_attendance>"
            )

