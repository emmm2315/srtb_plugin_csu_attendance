"""离线测试：不联网，用假宿主验证插件逻辑、绑定、渲染与日程同步。

运行：
    python tests/run_tests.py
"""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import sys
import types
import uuid
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0


def check(name: str, condition, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print("  [OK] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (name, detail))


# ── 伪造 shiritaibot.api ──────────────────────────────────────────────────


class FakeLogger:
    def __init__(self):
        self.lines: list[str] = []

    def _log(self, level, msg, *args, **kwargs):
        try:
            text = str(msg) % args if args else str(msg)
        except Exception:
            text = str(msg)
        self.lines.append("%s %s" % (level, text))

    def debug(self, msg, *args, **kwargs):
        self._log("debug", msg, *args, **kwargs)

    def log(self, msg, *args, **kwargs):
        self._log("log", msg, *args, **kwargs)

    def info(self, msg, *args, **kwargs):
        self._log("info", msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        self._log("warning", msg, *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        self._log("error", msg, *args, **kwargs)

    def exception(self, msg, *args, **kwargs):
        self._log("exception", msg, *args, **kwargs)


class FakeHook:
    def __init__(self):
        self.commands = {}

    def command(self, name, aliases=None, priority=0):
        def decorate(func):
            self.commands[name] = {"aliases": aliases or set(), "func": func}
            return func

        return decorate

    def command_group(self, name):
        class Group:
            def command(self, sub):
                def decorate(func):
                    return func

                return decorate

            def group(self, sub):
                return self

        return Group()

    def llm_tool(self, name=None, risk="safe", chat=False, **kwargs):
        def decorate(func):
            func._tool = {"name": name or func.__name__, "risk": risk, "chat": chat}
            return func

        return decorate

    def permission(self, *args, **kwargs):
        return lambda func: func

    def message_type(self, *args, **kwargs):
        return lambda func: func

    def platform(self, *args, **kwargs):
        return lambda func: func

    def regex(self, *args, **kwargs):
        return lambda func: func

    def on_llm_request(self):
        return lambda func: func

    def on_agent_begin(self):
        return lambda func: func

    def on_agent_done(self):
        return lambda func: func

    def on_decorating_result(self):
        return lambda func: func

    def after_message_sent(self):
        return lambda func: func

    def on_proactive_event(self):
        return lambda func: func

    def on_proactive_judged(self):
        return lambda func: func

    def on_proactive_done(self):
        return lambda func: func


class FakePlugin:
    def __init__(self, context, config=None):
        self.context = context
        self.config = config

    @property
    def logger(self):
        return self.context.logger

    @property
    def data_dir(self):
        return self.context.data_dir

    async def initialize(self):
        pass

    async def terminate(self):
        pass

    async def get_kv_data(self, key, default=None):
        return self.context.host.kv.get(key, default)

    async def put_kv_data(self, key, value):
        self.context.host.kv[key] = value

    async def delete_kv_data(self, key):
        self.context.host.kv.pop(key, None)


class FakeSettings(dict):
    async def save_config(self):
        self["saved"] = True


class PublicError(Exception):
    pass


class MessageResult:
    def __init__(self, text):
        self.text = text


class FakeEvent:
    def __init__(
        self,
        sender_id="123",
        platform="qq",
        name="小明",
        owner=False,
        segments=None,
        group_id="",
        adapter="test",
        raw=None,
    ):
        self.session = ("group" if group_id else "private") + ":%s:%s" % (platform, sender_id)
        self.text = ""
        self.sender_id = sender_id
        self.sender_name = name
        self.platform = platform
        self.adapter = adapter
        self.group_id = group_id
        self.message_id = ""
        self.is_owner = owner
        self.is_wake = True
        self.segments = segments or []
        self.raw = raw or {}

    def get_sender_name(self):
        return self.sender_name or self.sender_id

    def plain_result(self, text):
        return MessageResult(str(text))


def install_http_stubs() -> None:
    """插件目标环境有 httpx / cryptography；离线测试用最小桩代替。"""
    httpx = types.ModuleType("httpx")

    class AsyncClient:
        def __init__(self, *args, **kwargs):
            self.is_closed = False

        async def aclose(self):
            self.is_closed = True

        async def get(self, *args, **kwargs):
            raise RuntimeError("offline test")

        async def post(self, *args, **kwargs):
            raise RuntimeError("offline test")

    class HTTPError(Exception):
        pass

    httpx.AsyncClient = AsyncClient
    httpx.Timeout = lambda *args, **kwargs: None
    httpx.HTTPError = HTTPError
    sys.modules["httpx"] = httpx

    crypto = types.ModuleType("cryptography")
    hazmat = types.ModuleType("cryptography.hazmat")
    primitives = types.ModuleType("cryptography.hazmat.primitives")
    ciphers = types.ModuleType("cryptography.hazmat.primitives.ciphers")

    class Cipher:
        def __init__(self, *args, **kwargs):
            pass

        @classmethod
        def encryptor(cls, *args, **kwargs):
            return None

    ciphers.Cipher = Cipher
    ciphers.algorithms = types.SimpleNamespace(AES=object)
    ciphers.modes = types.SimpleNamespace(CBC=object)
    primitives.ciphers = ciphers
    hazmat.primitives = primitives
    crypto.hazmat = hazmat
    sys.modules["cryptography"] = crypto
    sys.modules["cryptography.hazmat"] = hazmat
    sys.modules["cryptography.hazmat.primitives"] = primitives
    sys.modules["cryptography.hazmat.primitives.ciphers"] = ciphers


def install_stub() -> FakeHook:
    install_http_stubs()
    hook = FakeHook()
    api = types.ModuleType("shiritaibot.api")
    api.Plugin = FakePlugin
    api.PluginSettings = FakeSettings
    api.Context = object
    api.MessageEvent = FakeEvent
    api.ToolContext = object
    api.PublicError = PublicError
    api.hook = hook
    api.Permission = types.SimpleNamespace(OWNER="owner", EVERYONE="everyone")
    api.MessageType = types.SimpleNamespace(PRIVATE="private", GROUP="group", ALL="all")
    api.Platform = types.SimpleNamespace(QQ="qq", WEB="web", ALL="all")
    api.Turn = object
    api.LLMRequest = object
    api.LLMResponse = object
    api.OutgoingMessage = object
    api.AgentResult = object
    api.ProactiveEvent = object
    api.Triage = object
    api.ProactiveOutcome = object
    api.TEXT = {"type": "string"}
    api.schema = lambda properties, required=None: {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }
    api.run_process = None
    api.Logger = object
    api.get_logger = lambda name: FakeLogger()
    api.brief = lambda value, limit=80: str(value)[:limit]

    shiritaibot = types.ModuleType("shiritaibot")
    shiritaibot.api = api
    sys.modules["shiritaibot"] = shiritaibot
    sys.modules["shiritaibot.api"] = api
    return hook


class FakeStore:
    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}

    async def put(self, kind, key, data, scope=""):
        body = {**data, "id": key, "scope": scope}
        self.rows[(kind, key)] = body
        return body

    async def get(self, kind, key):
        return self.rows.get((kind, key))

    async def delete(self, kind, key):
        self.rows.pop((kind, key), None)

    async def list(self, kind, scope=None, limit=500):
        return [row for (row_kind, _), row in self.rows.items() if row_kind == kind][:limit]


class FakeHost:
    def __init__(self):
        self.store = FakeStore()
        self.kv: dict = {}


class FakeContext:
    def __init__(self):
        self.id = "srtb_plugin_csu_attendance"
        self.data_dir = ROOT
        self.logger = FakeLogger()
        self.host = FakeHost()
        self.tools = []
        self.notified: list[str] = []
        self.sent: list[tuple[str, str]] = []
        self.secrets: dict[str, str] = {}

    async def resolve_secret(self, ref):
        return self.secrets.get(ref, "")

    async def notify_owner(self, text):
        self.notified.append(text)

    async def send_message(self, session, text):
        self.sent.append((session, text))

    async def emit(self, event):
        return "receipt"

    def register_tool(self, *args, **kwargs):
        self.tools.append((args, kwargs))


# ── 样本数据 ──────────────────────────────────────────────────────────────

TZ = timezone(timedelta(hours=8))


def sample_data() -> dict:
    today = datetime.now(TZ).date()
    # 接口 Week 数组按 周日→周六；以今天为基准造一周。
    sunday = today - timedelta(days=today.isoweekday() % 7)
    week = []
    for offset in range(7):
        day = sunday + timedelta(days=offset)
        week.append({"WeekName": "星期%d" % (offset if offset else 7), "Rq": day.isoformat()})
    monday = sunday + timedelta(days=1)
    tuesday = sunday + timedelta(days=2)
    return {
        "ClassNum": "10",
        "WeekNow": "11",
        "WeekNum": "21",
        "Week": week,
        "Rank": [
            {
                "CourseName": "高等数学A",
                "TeachName": "张三",
                "ClassLxBz": "正常",
                "Class": 1,
                "ClassEnd": 2,
                "ClassRoom": "A座101",
                "Rq": monday.isoformat(),
                "ClassSj": monday.isoformat() + " 07:59:33",
            },
            {
                "CourseName": "大学物理B（二）",
                "TeachName": "李四",
                "ClassLxBz": "未到",
                "Class": 3,
                "ClassEnd": 4,
                "ClassRoom": "B座214",
                "Rq": tuesday.isoformat(),
            },
            {
                "CourseName": "调课课程",
                "ClassLxBz": "调课",
                "Class": 7,
                "ClassEnd": 8,
                "ClassRoom": "C座305",
                "Rq": sunday.isoformat(),
            },
        ],
    }


# ── 测试 ──────────────────────────────────────────────────────────────────


async def collect(agen):
    return [item.text async for item in agen]


async def test_renderer():
    print("\n[1] 渲染器")
    from renderer import normalize, render_day_text, render_week_rank, time_range, weekday_from_text

    data = sample_data()
    info = normalize(data)
    check("共 3 条记录", info["stats"]["total"] == 3, str(info["stats"]))
    check("识别未到 1 条", info["stats"].get("未到") == 1, str(info["stats"]))
    check("第 3~4 节 10:00~11:40", time_range(3, 4) == ("10:00", "11:40"), str(time_range(3, 4)))
    check("周三解析为 4", weekday_from_text("周三") == 4, str(weekday_from_text("周三")))
    check("星期3 解析为 4", weekday_from_text("星期3") == 4)

    monday = (datetime.now(TZ).date() - timedelta(days=datetime.now(TZ).date().isoweekday() - 1))
    weekday = monday.isoweekday() % 7 + 1
    text = render_day_text(data, weekday, "今日课表", "小明", user_fallback="2024000001")
    check("单天含课程名", "高等数学A" in text, text)
    check("单天含打卡时间", "打卡时间07:59:33" in text, text)
    check("单天不含教师姓名", "张三" not in text)
    check("单天无 emoji", "📅" not in text)

    week = render_week_rank(data, user_serial="小明")
    check("整周含第 11 周", "第11周" in week, week)
    check("整周含正常/未到统计", "正常 1" in week and "未到 1" in week, week)


async def test_plugin():
    print("\n[2] 插件指令 / 绑定")
    hook = sys.modules["shiritaibot.api"].hook
    from main import CsuAttendance, VERSION

    ctx = FakeContext()
    settings = FakeSettings(
        token_check_enable=False,
        weds_token="TEST_TOKEN",
        weds_base="https://example.invalid",
        orga_id="10001",
        timezone="Asia/Shanghai",
        schedule_sync=False,
        class_reminder_minutes=15,
        render_mode="text",
    )
    plugin = CsuAttendance(ctx, settings)
    await plugin.initialize()

    async def fake_week_rank(token, user_serial, orga_id="10001", filter_week=0):
        return sample_data()

    plugin._weds.week_rank = fake_week_rank
    check("注册 /class 指令", "class" in hook.commands, str(hook.commands))
    check("注册 /bind 指令", "bind" in hook.commands)
    check("工具 csu_attendance 已注册", hasattr(plugin.csu_attendance_tool, "_tool"))
    check("工具 csu_sync_schedule 已注册", hasattr(plugin.csu_sync_schedule_tool, "_tool"))

    event = FakeEvent(sender_id="123", owner=False)
    bound = await collect(plugin.bind_cmd(event, "2024000001"))
    check("绑定成功", "绑定成功" in bound[0], bound)
    check("绑定键按平台+用户", "qq:123" in plugin._bindings, str(plugin._bindings))

    today_text = await collect(plugin.class_cmd(event, ""))
    check("今日查询有输出", bool(today_text), str(today_text))
    check("今日查询不暴露学号", "2024000001" not in today_text[0], today_text[0])

    # @ 别人首次绑定
    event2 = FakeEvent(sender_id="456", name="小红")
    event2.segments = [{"type": "at", "data": {"qq": "789"}}]
    bound2 = await collect(plugin.bind_cmd(event2, "@789 2024000002"))
    check("帮别人绑定成功", "已帮" in bound2[0], bound2)
    check("被 @ 用户绑定键", "qq:789" in plugin._bindings, str(plugin._bindings))

    # 回归：群里 @Bot 唤醒时，@Bot 不能当成 /bind 目标
    bot_id = "2737612185"
    wake_only = FakeEvent(
        sender_id="111",
        name="群主",
        group_id="226",
        segments=[{"type": "at", "data": {"qq": bot_id}}],
        raw={"self_id": bot_id},
    )
    wake_bound = await collect(plugin.bind_cmd(wake_only, "8209250511"))
    check("@Bot 唤醒时绑定的是发送者自己", "绑定成功" in wake_bound[0], wake_bound)
    check("@Bot 唤醒时不写入 bot 的绑定", "qq:" + bot_id not in plugin._bindings, str(plugin._bindings))

    # 回归：@Bot + @目标 时，应跳过 @Bot，取真正的目标
    target_event = FakeEvent(
        sender_id="111",
        name="群主",
        group_id="226",
        segments=[
            {"type": "at", "data": {"qq": bot_id}},
            {"type": "at", "data": {"qq": "789"}},
        ],
        raw={"self_id": bot_id},
    )
    target_bound = await collect(plugin.bind_cmd(target_event, "@条翻 8209250508"))
    check("@Bot + @目标 时取真实目标", "qq:789" in plugin._bindings, str(plugin._bindings))
    check("改绑提示不再出现 bot QQ", bot_id not in target_bound[0], target_bound[0])
    check("改绑提示不再出现原始 QQ 号", "789" not in target_bound[0], target_bound[0])

    unknown = await collect(plugin.class_cmd(event, "火星"))
    check("未知参数有提示", "未知参数" in unknown[0], unknown)

    # 主人权限门控
    owner = FakeEvent(sender_id="999", owner=True)
    denied = await collect(plugin.class_cmd(FakeEvent(sender_id="123"), "token abc"))
    check("非主人不能改 token", "主人专属" in denied[0], denied)
    status = await collect(plugin.class_cmd(owner, "status"))
    check("主人状态含版本", VERSION in status[0], status[0])

    await plugin.terminate()


async def test_schedule():
    print("\n[3] 日程同步")
    from main import CsuAttendance, VERSION

    ctx = FakeContext()
    settings = FakeSettings(
        token_check_enable=False,
        weds_token="TEST_TOKEN",
        weds_base="https://example.invalid",
        orga_id="10001",
        timezone="Asia/Shanghai",
        schedule_sync=False,
        class_reminder_minutes=15,
    )
    plugin = CsuAttendance(ctx, settings)
    await plugin.initialize()

    async def fake_week_rank(token, user_serial, orga_id="10001", filter_week=0):
        return sample_data()

    plugin._weds.week_rank = fake_week_rank
    owner = FakeEvent(sender_id="999", owner=True)
    plugin._bindings["qq:999"] = {"user_serial": "2024000001", "bound_at": 0}
    plugin._owner_key = "qq:999"

    result = await plugin._sync_schedule("2024000001")
    check("写入课程 3 节", result["courses"] == 3, str(result))
    check("至少 1 条未来提醒", result["reminders"] >= 1, str(result))

    routine = [row for (kind, _), row in ctx.host.store.rows.items() if kind == "routine"]
    courses = [row for row in routine if row["kind"] == "course"]
    reminders = [row for row in routine if row["kind"] == "reminder"]
    check("课程记录 kind=course", len(courses) == 3, str(len(courses)))
    check("课程标题含教室", any("A座101" in row["title"] for row in courses))
    check("课程带时区", all("+08:00" in row["due"] for row in courses), str(courses[0]["due"]))
    check("提醒 next_fire 是数字", all(isinstance(row["next_fire"], (int, float)) for row in reminders))
    check("提醒属于 csu_attendance", all(row["source"] == "csu_attendance" for row in routine))
    check("提醒标题含上课提醒", any("上课提醒" in row["title"] for row in reminders), str(reminders))

    # 再同步一次：不应重复创建
    before = len(routine)
    await plugin._sync_schedule("2024000001")
    routine2 = [row for (kind, _), row in ctx.host.store.rows.items() if kind == "routine"]
    check("重复同步不重复写入", len(routine2) == before, "%d -> %d" % (before, len(routine2)))

    # 关闭
    await plugin._sync_schedule("2024000001", remove=True)
    left = [row for (kind, _), row in ctx.host.store.rows.items() if kind == "routine"]
    check("关闭后移除所有托管项", left == [], str(left))

    # 重新同步后跨周清理：旧 key 应被删
    await plugin._sync_schedule("2024000001")
    old_keys = list(plugin._schedule_keys)
    plugin._schedule_keys = old_keys + ["csu:stale:key"]
    await ctx.host.store.put("routine", "csu:stale:key", {"kind": "course", "title": "旧课"})
    await plugin._sync_schedule("2024000001")
    check("清理旧日程项", ("routine", "csu:stale:key") not in ctx.host.store.rows)

    await plugin.terminate()


async def test_tools():
    print("\n[4] LLM 工具")
    from main import CsuAttendance, VERSION

    ctx = FakeContext()
    settings = FakeSettings(
        token_check_enable=False,
        weds_token="TEST_TOKEN",
        weds_base="https://example.invalid",
        orga_id="10001",
        timezone="Asia/Shanghai",
        schedule_sync=False,
        class_reminder_minutes=15,
    )
    plugin = CsuAttendance(ctx, settings)
    await plugin.initialize()

    async def fake_week_rank(token, user_serial, orga_id="10001", filter_week=0):
        return sample_data()

    plugin._weds.week_rank = fake_week_rank
    plugin._bindings["qq:123"] = {"user_serial": "2024000001", "bound_at": 0}
    plugin._owner_key = "qq:123"

    class Ctx:
        actor = "123"
        owner = True
        group = False
        session = "private:qq:123"
        mode = "agent"
        background = False

    out = await plugin.csu_attendance_tool(Ctx(), "week")
    check("工具返回整周文本", "打卡数据" in out and "第11周" in out, out[:100])

    result = await plugin.csu_sync_schedule_tool(Ctx())
    check("工具同步课程", result["courses"] == 3, str(result))

    class GroupCtx(Ctx):
        group = True

    try:
        await plugin.csu_sync_schedule_tool(GroupCtx())
        check("群聊拒绝写日程", False)
    except PublicError as exc:
        check("群聊拒绝写日程", "私聊" in str(exc), str(exc))

    await plugin.terminate()


async def test_update():
    print("\n[5] 自动更新")
    from main import CsuAttendance, VERSION
    from shiritaibot.api import PublicError

    check("版本号解析", CsuAttendance._version_tuple("v1.2.3") == (1, 2, 3))
    check(
        "GitHub 仓库解析",
        CsuAttendance._parse_update_repo(
            "https://github.com/emmm2315/srtb_plugin_csu_attendance"
        )
        == ("emmm2315", "srtb_plugin_csu_attendance"),
    )
    try:
        CsuAttendance._parse_update_repo("https://example.com/a/b")
        check("拒绝非 GitHub 更新源", False)
    except PublicError:
        check("拒绝非 GitHub 更新源", True)

    ctx = FakeContext()
    settings = FakeSettings(
        token_check_enable=False,
        weds_token="TEST_TOKEN",
        weds_base="https://example.invalid",
        orga_id="10001",
        timezone="Asia/Shanghai",
        schedule_sync=False,
        auto_update=False,
        update_repo="https://github.com/emmm2315/srtb_plugin_csu_attendance",
        update_interval_hours=24,
        class_reminder_minutes=15,
    )
    plugin = CsuAttendance(ctx, settings)
    await plugin.initialize()

    root = Path(r"D:\astrbot\_test_update_%s" % uuid.uuid4().hex[:8])
    code_dir = root / "code"
    data_dir = root / "data"
    code_dir.mkdir(parents=True)
    data_dir.mkdir()
    (code_dir / "main.py").write_text('VERSION = "1.1.0"\n', encoding="utf-8")
    (code_dir / "metadata.yaml").write_text(
        "name: srtb_plugin_csu_attendance\nversion: 1.1.0\n", encoding="utf-8"
    )
    (code_dir / "_conf_schema.json").write_text("{}", encoding="utf-8")
    plugin._code_dir_override = code_dir
    ctx.data_dir = data_dir

    def make_zip(version: str = "9.9.9", unsafe: bool = False) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "repo-main/metadata.yaml",
                "name: srtb_plugin_csu_attendance\nversion: %s\n" % version,
            )
            archive.writestr("repo-main/main.py", 'VERSION = "%s"\n' % version)
            archive.writestr("repo-main/_conf_schema.json", "{}")
            if unsafe:
                archive.writestr("repo-main/../evil.py", "print('evil')")
        return buffer.getvalue()

    try:
        result = plugin._install_archive_sync(make_zip(), "9.9.9")
        check("更新包安装成功", result["version"] == "9.9.9", str(result))
        check(
            "代码文件已替换",
            'VERSION = "9.9.9"' in (code_dir / "main.py").read_text(encoding="utf-8"),
        )
        backup = Path(result["backup"])
        check("更新前已备份", backup.is_dir(), str(backup))
        check(
            "备份保留旧版本",
            'VERSION = "1.1.0"' in (backup / "main.py").read_text(encoding="utf-8"),
        )

        try:
            plugin._install_archive_sync(make_zip(unsafe=True), "9.9.9")
            check("拒绝路径穿越", False)
        except PublicError as exc:
            check("拒绝路径穿越", "不安全" in str(exc), str(exc))

        try:
            plugin._install_archive_sync(make_zip(), "8.8.8")
            check("拒绝版本不一致", False)
        except PublicError as exc:
            check("拒绝版本不一致", "不一致" in str(exc), str(exc))

        check("更新包仍可重复安装", plugin._install_archive_sync(make_zip(), "9.9.9")["version"] == "9.9.9")

        owner = FakeEvent(sender_id="999", owner=True)
        state = await collect(plugin._cmd_update(owner, "auto"))
        check("自动更新状态可查询", "关闭" in state[0], state[0])
        enabled = await collect(plugin._cmd_update(owner, "auto on"))
        check("自动更新可开启", "已开启" in enabled[0], enabled[0])
        check("自动更新任务已启动", plugin._update_task is not None)
        disabled = await collect(plugin._cmd_update(owner, "auto off"))
        check("自动更新可关闭", "已关闭" in disabled[0], disabled[0])
        check("自动更新任务已停止", plugin._update_task is None)
    finally:
        shutil.rmtree(root, ignore_errors=True)
        await plugin.terminate()


def test_manifest():
    print("\n[6] 清单 / 配置")
    import re

    meta_text = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    meta = {}
    for line in meta_text.splitlines():
        if not line.strip() or line.lstrip().startswith("-"):
            continue
        match = re.match(r"^([A-Za-z_]+):\s*(.*)$", line)
        if match:
            meta[match.group(1)] = match.group(2).strip().strip('"')
    platforms = re.findall(r"^\s*-\s*(\w+)\s*$", meta_text, re.M)
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    check("metadata name 正确", meta.get("name") == "srtb_plugin_csu_attendance", str(meta))
    check("只声明 qq/web", set(platforms) <= {"qq", "web"}, str(platforms))
    check("版本范围满足 0.2.x", meta.get("shiritaibot_version") == ">=0.2,<1")
    check("weds_token 是 secret", schema["weds_token"].get("secret") is True)
    check("cas_password 是 secret", schema["cas_password"].get("secret") is True)
    check("自动更新默认关闭", schema["auto_update"].get("default") is False)
    check("更新源是 GitHub", "github.com" in schema["update_repo"].get("default", ""))
    allowed = {"string", "text", "int", "float", "bool", "object", "list", "dict", "template_list"}
    check("所有配置类型合法", all(spec.get("type") in allowed for spec in schema.values()))
    check("配置都有 description", all(spec.get("description") for spec in schema.values()))

    import tomllib

    legacy = tomllib.loads((ROOT / "plugin.toml").read_text(encoding="utf-8"))
    check("兼容 plugin.toml 为 api=1", legacy.get("api") == 1, str(legacy))
    check("兼容 settings_schema 是 object", legacy.get("settings_schema", {}).get("type") == "object")


async def main():
    install_stub()
    await test_renderer()
    await test_plugin()
    await test_schedule()
    await test_tools()
    await test_update()
    test_manifest()
    print("\n结果：%d 通过，%d 失败" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))


