# srtb_plugin_csu_attendance

中南大学**课表 / 到课 / 打卡查询**插件，从 AstrBot 版 `astrbot_plugin_csu_attendance` 迁移到
ShiritaiBot（API v2）。除了原有的课表查询、学号绑定、WEDS token 体检外，新增了
**把本周课表每周自动导入 ShiritaiBot 日程**、**上课前提醒**、给模型使用的查询工具，以及
**可选的 GitHub 自动更新**。

---

## 一、功能

| 功能 | 说明 |
| --- | --- |
| 课表 / 到课查询 | 今日、明天、昨天、后天、指定星期、整周 |
| 学号绑定 | 按「平台 + 用户」保存，群里不会串号；支持帮别人首次绑定 |
| WEDS token | 配置一次全群共用；支持聊天命令更新、立即校验、定时体检、失效告警 |
| 统一身份认证（CAS） | 可选，用于诊断和教务会话；WEDS 查询不依赖它 |
| 日程同步 | 每周自动把主人课表写成 `course` 和上课提醒 `reminder`，交给 ShiritaiBot 日程系统触发 |
| 自动更新 | 可选，从 GitHub 检查 `metadata.yaml` 版本；校验、备份后自动重载 |
| LLM 工具 | `csu_attendance` 查询课表；`csu_sync_schedule` 刷新日程（仅主人私聊） |

> ShiritaiBot 的插件指令目前只支持**文本回复**，所以原 AstrBot 版的 HTML / 图片课表没有迁移；
> 所有输出都是文字。原版的图片渲染配置项（`render_mode`、`t2i_endpoint`）已移除。

## 二、安装

1. 在 ShiritaiBot 管理后台「插件 → 安装插件」上传 ZIP，或把本目录复制到
   `<数据目录>/plugins/srtb_plugin_csu_attendance/`。
2. 点击「重载插件」，在插件列表中找到「中南大学到课与课表」。
3. 填写配置并启用。

插件 ID 是目录名 `srtb_plugin_csu_attendance`。宿主不会自动安装依赖；本插件只使用
ShiritaiBot 自带的 `httpx`、`cryptography`，**不需要额外安装**。
需要验证码 OCR 时再安装 `ddddocr`（可选）。

> **关于两个清单文件**：ShiritaiBot 0.2.x 的 WebUI「安装插件」在解压 ZIP 时仍只识别
> `plugin.toml`（并要求 `api = 1`），而插件扫描在发现 `metadata.yaml` 时会优先按 v2 加载。
> 因此本包同时带了一个**最小兼容用** `plugin.toml`（只为通过 ZIP 安装校验）和真正的
> v2 清单 `metadata.yaml` + `_conf_schema.json`。运行时以 v2 为准，`plugin.toml` 里的
> 空 `settings_schema` 不会覆盖 v2 配置。若以后 ShiritaiBot 的 ZIP 安装支持 v2，
> 可以删掉 `plugin.toml`。

## 三、配置

| 配置项 | 必填 | 说明 |
| --- | --- | --- |
| `weds_token` | 是 | WEDS 平台 token。加密保存；留空则回退到 `/class token` 缓存的 token |
| `cas_username` / `cas_password` | 否 | 统一身份认证账号密码，仅用于教务系统诊断；密码加密保存 |
| `cas_service` | 否 | CAS 的 service 参数，默认教务系统 sso.jsp |
| `weds_base` | 否 | 默认 `https://wedsk12.weds.com.cn:8081` |
| `orga_id` | 否 | 默认 `10001` |
| `http_proxy` | 否 | 如 `http://127.0.0.1:7890` |
| `request_timeout` | 否 | 网络超时，默认 20 秒 |
| `use_ocr` | 否 | 需要验证码时用 ddddocr（需自行安装） |
| `token_check_enable` | 否 | 后台体检 token，默认开 |
| `token_check_interval_hours` | 否 | 体检间隔，默认 6 小时；0 表示只查一次 |
| `alert_session` | 否 | 失效告警会话；留空私聊主人 |
| `timezone` | 否 | 默认 `Asia/Shanghai` |
| `weekly_schedule` | 否 | 每周自动导入课表到日程，默认**开启** |
| `schedule_weekday` | 否 | 每周导入日，默认 `0`（周一） |
| `schedule_time` | 否 | 每周导入时间，默认 `06:10` |
| `class_reminder_minutes` | 否 | 上课提醒提前量，默认 15 分钟；0 表示只导入课程、不建提醒 |
| `auto_update` | 否 | 自动从 GitHub 更新插件，默认关 |
| `update_repo` | 否 | 更新源，默认本插件仓库 `https://github.com/emmm2315/srtb_plugin_csu_attendance` |
| `update_interval_hours` | 否 | 自动检查间隔，默认 24 小时 |

### 获取 WEDS token（约 1 分钟，只做一次）

1. 电脑微信打开「到课 / 课表」类小程序。
2. 用 Fiddler / Reqable / Charles / mitmproxy 抓包，找到发往 `wedsk12.weds.com.cn` 的请求。
3. 复制请求头 `Authorization: Token xxxxx` 中 `Token ` 后面的字符串。
4. 填进插件配置的 `weds_token`，重载并启用。

也可以由主人在聊天里发送 `/class token <token>`，插件会立即校验再保存。

> token 与学号无关：任意有效 token 可查询任意学号。所以只需主人抓一次，全群共用；
> 不要把 token 发到公开群聊。

## 四、指令

别名：`/课表` 等价于 `/class`，`/绑定` 等价于 `/bind`。

### 所有人可用

| 指令 | 作用 |
| --- | --- |
| `/class` | 今日课表（文字） |
| `/class 明天` | 明日课表（也支持 `昨天` / `后天`） |
| `/class 周三` | 指定星期（`周一`~`周日`，也接受 `周3` / `星期3`） |
| `/class 周` | 整周课表 |
| `/bind <学号>` | 绑定你的学号 |
| `/bind @某人 <学号>` | 帮某人完成首次绑定 |
| `/class unbind` | 解绑 |
| `/class status` | 查看自己的绑定状态 |
| `/class help` | 显示帮助 |

### 仅主人可用

| 指令 | 作用 |
| --- | --- |
| `/class token <token>` | 设置 WEDS token（保存后立即验证） |
| `/class token check` | 立即体检 token |
| `/class setup` | 抓包 + 配置 token 的分步教程 |
| `/class 日程` | 把本周课表导入 ShiritaiBot 日程，并创建上课提醒 |
| `/class 日程 自动 on\|off` | 开启 / 关闭每周自动导入 |
| `/class 日程 关闭` | 移除本插件导入的日程（自动导入下周仍会继续） |
| `/class update` | 立即检查并更新插件 |
| `/class update check` | 只检查是否有新版本 |
| `/class update auto on\|off` | 开启 / 关闭自动更新 |
| `/class refresh [目标]` | 跳过缓存强制刷新 |
| `/class raw` | 查询并附带原始 JSON |
| `/class bind login <学号> <密码>` | 保存统一身份认证凭据（加密） |
| `/class login` | 手动登录教务系统（诊断） |
| `/class jw` | 查看教务系统 accessToken（诊断） |

主人判定使用 ShiritaiBot 的 `event.is_owner` / `ToolContext.owner`，不再使用 AstrBot 的
`is_admin()`。

> `/bind` 在群里会忽略用于唤醒 Bot 的 `@Bot`，只把真正的 `@目标` 当作绑定对象；
> 如果消息里只有 `@Bot`，就按“绑定自己”处理。提示里的名字优先取群名片/昵称，
> 取不到时显示 `TA`，不会再把 QQ 号当成用户名显示。

## 五、和 ShiritaiBot 日程结合

ShiritaiBot 的「日程」有五种类型：`todo` / `event` / `course` / `reminder` / `job`。
本插件写入其中两种：

- **`course`**：本周每节课一条，标题形如 `高等数学A · A座101`，带开始 / 结束时间和时区；
  只用于查看，不触发提醒。可以在「日程」页面看到，也可以用 `routine_list` 查询。
- **`reminder`**：按 `class_reminder_minutes` 提前创建，例如 08:00 的课提前 15 分钟在 07:45 提醒。
  它由 ShiritaiBot 自己的日程调度器触发，属于「主人设定的提醒」，会经过主动性引擎并强制通知。

使用方式：

1. 主人先 `/bind <学号>`。`weekly_schedule` 默认开启，绑定后如果本周还没导入，
   插件会立即补一次；之后每周在 `schedule_weekday` / `schedule_time`（默认周一 06:10）
   自动导入本周课表。
2. 也可以随时手动执行 `/class 日程` 立即导入。
3. 用 `/class 日程 自动 on|off` 开关每周自动导入；或在配置里改
   `weekly_schedule`、`schedule_weekday`、`schedule_time`。
4. 不想要提醒，把 `class_reminder_minutes` 设为 `0`；只导入课程。
5. `/class 日程 关闭` 只删除本插件写入的项；自动导入仍会保持开启，下周会重新导入，
   要彻底停止请再执行 `/class 日程 自动 off`。

每周导入按 ISO 周去重：同一周只自动导入一次；手动 `/class 日程` 也会标记本周已导入。
机器人周一 06:10 离线时，下次启动发现本周还没导入会立即补上。跨周导入会先清理上周
由本插件写入的课程 / 提醒，再写入新一周的项。如果 WEDS 返回的 `Week` 日期还不包含今天
（接口尚未换周），本周导入会失败并标记为待重试，约 6 小时后再试。

> 每周自动导入只针对**主人**：使用主人最近一次 `/bind` 的学号；如果主人没有绑定，
> 则回退到配置里的 `cas_username`。群成员的绑定不会被写进全局日程。

实现说明：插件通过 v2 `Context.host.store` 写入 `routine` 记录，记录上带
`source=csu_attendance` / `managed_by` / `student` 标记，避免误删别人的日程。这是当前
ShiritaiBot 0.2.x 可用的集成方式；如果宿主以后提供正式的日程 API，只需替换
`_sync_schedule()` 里的读写部分。

## 六、自动更新

插件可以从 GitHub 检查并更新自己，默认**关闭**。开启方式：

```text
/class update auto on
```

或在插件配置里把 `auto_update` 设为开。开启后按 `update_interval_hours`（默认 24 小时）
检查一次；也可以随时手动执行：

```text
/class update check     # 只检查
/class update           # 立即检查并更新
/class update auto off  # 关闭自动更新
```

### 工作方式

1. 读取 `update_repo` 指向仓库默认分支的 `metadata.yaml`，比较 `version`；
2. 只有远端版本**高于**当前版本才继续；
3. 下载该分支的 ZIP；
4. 校验：路径穿越 / 符号链接、文件数与大小、`metadata.yaml` 版本、`main.py` 可编译、
   `main.py` 的 `VERSION` 与 `metadata.yaml` 一致、`_conf_schema.json` 是合法 JSON；
5. 把当前代码备份到 `plugin_data/srtb_plugin_csu_attendance/update_backups/`；
6. 只替换代码文件，不碰插件配置、Vault 和 `plugin_data` 里的数据；
7. 约 2 秒后调用宿主的 `PluginManager.reload()` 自动重载；
8. 如果重载失败，自动恢复备份并再次重载旧代码，同时私聊主人告警。

### 发布新版本时

自动更新依赖版本号，必须同时修改：

- `metadata.yaml` 的 `version`
- `main.py` 的 `VERSION`
- `plugin.toml` 的 `version`（兼容清单）

然后推送到仓库默认分支 `main`。只改代码不升版本，自动更新不会触发。

### 安全说明

- 只接受 `https://github.com/作者/仓库` 形式的公开仓库；
- 如果插件目录本身是 git 工作区（含 `.git`），自动更新会拒绝执行，请改用 `git pull`；
- 自动更新执行的是从网络下载的代码，**只应把 `update_repo` 指向你信任的仓库**；
- 默认关闭；开启后任何能修改该仓库的人都能向 Bot 推送代码，请保护仓库权限；
- 更新失败会尽量回滚，但强烈建议在升级前保留 `plugin_data` 备份。

## 七、权限与隐私

- **token、CAS 密码**使用 `_conf_schema.json` 的 `secret` 字段，保存进 ShiritaiBot 的 Vault，
  不会回显、不会写入日志、不会返回给模型。
- 绑定按 `平台:用户ID` 保存，与群会话解耦；A 绑了学号不会让 B 查到 A 的课表。
- 文字输出默认用**昵称**做抬头，不显示教师姓名；`/class raw` 仅主人可用。
- `csu_attendance` 工具是只读 `safe` 工具，在 Chat / Agent 都可用，但只会查询
  调用者自己的绑定；`csu_sync_schedule` 是 `normal` 工具，仅主人私聊可用。
- 课表内容会发往学校 WEDS 接口；图片渲染已移除，不再有第三方 t2i 外发。

## 八、和 AstrBot 版的差异

| 项目 | AstrBot 版 | ShiritaiBot 版 |
| --- | --- | --- |
| 插件 API | `Star` + `@filter.command` | `Plugin` + `@hook.command` |
| 清单 | `metadata.yaml` | `metadata.yaml` + `_conf_schema.json`（另带兼容用 `plugin.toml`） |
| 管理判断 | `event.is_admin()` | `event.is_owner` / `ctx.owner` |
| 图片课表 | `html_render` + 模板 | 移除，只输出文字 |
| 管理员告警 | 最近管理员会话 | `notify_owner()` / `alert_session` |
| 日程 | 无 | 写入 `course` + `reminder`，接入 ShiritaiBot 日程调度 |
| 模型工具 | 无 | `csu_attendance` / `csu_sync_schedule` |
| HTTP | `aiohttp` | `httpx`（宿主自带） |
| AES | `pycryptodome` | `cryptography`（宿主自带） |
| HTML 解析 | `beautifulsoup4` | 标准库 `html.parser` |

> AstrBot 和 ShiritaiBot 的数据目录、KV 与配置是分开的，迁移后需要重新 `/bind` 学号、
> 重新填一次 `weds_token`；旧 AstrBot 数据不会自动带过来。

## 九、开发与测试

```bash
# 离线测试（不联网，用假宿主）
python tests/run_tests.py
```

测试覆盖：文本渲染、学号绑定、权限门控、日程写入 / 去重 / 清理、每周自动导入去重、
LLM 工具、自动更新安装 / 校验 / 备份、清单结构。
真实接口联调需要有效的 WEDS token，测试时不要把 token 写进仓库。

## 十、已知限制

- WEDS token 仍需人工抓包：token 由微信小程序下发，无法仅凭学号密码换取。
- 教务系统 `accessToken.do` 的 JWT 不被 WEDS 接受，`/class jw` 只作诊断。
- 课表接口按周返回；自动同步间隔决定跨周切换的及时性。
- 日程写入依赖宿主当前的 `routine` 存储结构；ShiritaiBot 升级后如改动该结构，需要同步调整。
- 自动更新只支持公开 GitHub 仓库，且必须通过提升版本号触发；更新后依赖宿主的
  `PluginManager.reload()`，如果宿主改为异步重载机制，需要相应调整。
- 自动更新通过 GitHub 公开 API 读取 `metadata.yaml` / 下载 zipball；未认证请求有每小时
  60 次的限额，默认 24 小时检查一次足够，频繁手动检查可能遇到 403。

