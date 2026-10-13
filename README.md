# 晚安成电自动打卡

## 使用指南

- **Windows 单人打卡：** [打开图文使用说明](Windows使用说明.html)，也可查看 [Markdown 版](docs/windows-single-user.md)。说明涵盖首次登录、二次验证、前台观察、后台运行和 Windows 定时任务。
- **Docker 自动打卡：** [打开 Docker 部署与使用说明](docs/docker.md)。说明涵盖容器部署、多用户设置、网页操作和定时随机打卡。

Windows 用户首次使用“单人登录初始化.cmd”完成加密凭据配置、登录验证和设备信任；之后可选择前台观察、后台打卡或 Windows 定时运行。Docker 用户通过网页配置账号，并可设置每日随机启动时间段。

通过 Camoufox 操作实际网页：采集初始定位、提供定位、点击打卡和确认按钮、刷新后核验当天结果。入口为 `main.py`，不直接调用打卡 POST 接口。

## 多账号一键打卡

每个账号使用独立的 profile 保存浏览器加密凭据、Cookie 和 Camoufox 指纹。指纹首次生成后保存在该 profile 的 `.auto-location-identity.json`，后续复用；不是每次登录重新随机更换身份。账号清单 `.state/accounts.json` 仅保存本地别名、profile 路径、是否启用及可选区域序号，不保存学号或密码。不同浏览器指纹不等于不同网络 IP。

```powershell
# 将当前已登录、已保存密码的 profile 注册到批量清单
uv run python main.py account-add --account-id account-a --profile .state/camoufox
# 新账号自动分配独立 profile；重复 ID/profile 和嵌套 profile 会被拒绝
uv run python main.py account-add --account-id account-b
# 在新账号自己的浏览器密码管理器中保存凭据，不在命令行输入密码
uv run python main.py credentials --account-id account-b
# 每个账号首次初始化后，可以单独验证登录
uv run python main.py login --auto-login --account-id account-b --keep-open

# 查看注册列表和每个 profile 的已保存凭据数量
uv run python main.py account-list
# 一次完成所有启用账号的自动登录
uv run python main.py batch-login
# 随机顺序逐个登录并检查各自的允许区域和随机位置，不提交
uv run python main.py batch-inspect
# 随机顺序逐个打卡，默认读取每个账号自己的允许区域并随机取点
uv run python main.py batch-checkin --simulate-miniprogram
```

完成首次账号初始化后，双击项目根目录的 **`一键打卡.cmd`** 即可运行 `batch-checkin --simulate-miniprogram`，结束后显示退出码并保留终端。默认逐个显示可见 Camoufox 窗口，完成一个账号后关闭其浏览器，再处理下一个。可以使用 `--headless`；批量命令不使用 `--keep-open`，以免等待关闭一个窗口阻塞后续账号。

每次批量任务使用系统随机源打乱所有 `enabled=true` 的账号；随机排列可能恰好与上次相同。每个账号单独启动浏览器进程、自动登录、发现自己的区域并随机取点、提交一次并刷新核验。已有效打卡的账号只核验。登录、定位、时间限制或提交失败会记录该账号失败，继续处理后续账号；失败不自动重试。批量模式和 `--account-id` 不使用全局 `UESTC_USERNAME/UESTC_PASSWORD`，避免账号串用。验证码或主密码仍需在相应 profile 人工完成。

多个允许区域需要在注册时添加 `--area-index 0`，或修改清单中该账号的 `area_index`。单一区域无需配置。清单中 `enabled=false` 可暂时排除账号；单账号命令仍可用其别名初始化凭据。配置相对路径按清单文件所在目录解析。

批量结果实时写入 `.state/batch-result.json`，包含实际执行顺序、逐账号结果、成功/失败数及未处理账号。每次任务另存独立结果到 `.state/batch-runs/<运行编号>/<账号别名>.json`。只有所有账号都达到对应命令的成功条件才返回退出码 0。Ctrl+C 停止后续账号，当前操作无法确认时标记为未知，不盲目重试。批量锁防止同一清单重复运行；异常退出遗留 `.batch.lock` 时，确认任务已停止后再移除。不要同时用其他任务打开这些 profile。

## 单账号使用

```powershell
uv sync
# 若本机尚未安装 Camoufox 浏览器
uv run python -m camoufox fetch

# 首次：在可见的浏览器密码管理器中保存账号密码，再在终端按回车
uv run python main.py credentials

# 自动填充、登录，并从平台打开“晚安成电”；完成后保持 GUI 供检查
uv run python main.py login --auto-login --keep-open

# 在需要采集的位置获取浏览器真实返回的 WGS84 坐标
uv run python main.py capture

# 提供保存的坐标，先查看资格，再执行一次打卡
uv run python main.py inspect --location .state/location.json
uv run python main.py checkin --location .state/location.json

# 独立刷新核验
uv run python main.py verify --location .state/location.json
```

所有命令默认使用 `.state/camoufox` 保存登录会话和浏览器凭据。`capture` 保存 `.state/location.json`，`credentials` 保存不含凭据的 `.state/credentials-result.json`，其他命令保存 `.state/result.json`。失败退出码为 1；成功为 0。采集失败写入 `location.error.json`，保留原有定位文件。浏览器运行目录、会话、凭据、位置和运行结果已加入 Git 忽略规则。运行期间不要用多个进程同时打开同一 profile。

`capture` 不提供模拟坐标，不启用 IP 定位。在 Windows 上使用系统定位；系统定位关闭时可能弹出 Windows 位置设置，需要用户自行开启相关功能。系统定位不可用时会报错，不能把截图地图中心或 IP 推测位置当作真实采集结果。`checkin --location ...` 使用 Playwright 的浏览器定位覆盖和 Camoufox 定位配置，并关闭 Windows 定位提供器，因此无需调用系统定位，也不会为获取位置打开 Windows 设置。采集后的坐标不会自动修正或选择宿舍坐标；使用前请核对位置及精度。

也可以通过 `--location` 指定自行提供的 JSON，字段为 `latitude`、`longitude`、`accuracy`（米，默认 30）、`coordinate_system`（必须是 `WGS84`），可选 `captured_at`。腾讯地图链接和本网站地图中心使用 GCJ02，不能直接作为浏览器 WGS84 坐标使用。网站自己的转换流程会把浏览器坐标转换为地图坐标。

## 自动识别允许打卡区域

```powershell
# 读取当前账号在页面中收到的允许区域列表，不提交打卡
uv run python main.py discover --auto-login --keep-open
# 单一区域自动随机选择内部坐标，并检验页面是否确认进入范围
uv run python main.py inspect --auto-login --auto-location --keep-open
# 多个区域时，使用 discover 返回的 index 显式选择
uv run python main.py inspect --auto-login --auto-location --area-index 0
# 检验后按需执行打卡，仍遵守当天状态、时间及刷新核验条件
uv run python main.py checkin --auto-login --auto-location --simulate-miniprogram
```

工具观察页面的 `/daily-checkin/api/checkin-location/available-locations` 响应，读取账号允许的圆形区域中心、半径和名称；不会使用校园围栏替代宿舍范围。`discover` 默认保存 `.state/allowed-areas.json`。无区域、接口失败、无效数据或多个区域未选择时停止；多个区域按列表序号选择，配置变化后应重新查看列表。

每次调用 `--auto-location` 时，在所选区域半径的 1%–90% 范围内按面积均匀随机取点，排除中心，并保留至少 10% 半径的边界余量。采用与页面一致的地球半径计算球面坐标和距离，生成后再次检查坐标确实位于区域内。一个运行内的页面刷新、提交和核验沿用同一点，不反复随机。结果的 `selected_point` 记录实际坐标、距中心距离和边界余量；`selected_area` 保留原配置中心。

非打卡时段网站隐藏范围标签；此时可发现并加载随机目标位置，但 `page_in_range=false`、`range_validation=not_visible_in_current_state`，不宣称页面已确认范围。执行打卡仍要求页面明确显示进入允许范围。

2026-10-12 真实账号实测返回单一区域“本科8组团”，GCJ02 中心为纬度 `30.755685`、经度 `103.932944`，半径 `140` 米。自动选择并加载区域中心后没有定位失败；当时为下午非打卡时段，页面隐藏范围标签，因此尚未确认打卡时段的页面范围识别，也没有提交打卡。结果保存在 `.state/auto-area-inspect.json`。

`--auto-location` 使用网站已有的路由坐标入口，直接提供地图 GCJ02 区域内随机坐标，不将它当作浏览器 WGS84 定位，也不进行重复转换。此模式禁用浏览器系统定位，不调用 Windows 位置设置；不能与 `--location` 或 `--connect` 同用。结果的 `location_resolution` 包含来源、区域列表、所选区域、实际随机位置及页面范围验证。**这是从账户配置范围生成的模拟目标位置，不是设备实时定位，也不证明人在宿舍。** `capture` 仍用于获取真实设备位置。区域发现和 `inspect` 不会提交打卡。

## 自动登录流程

`login` 默认先打开 `https://jzsz.uestc.edu.cn/`，由平台跳转到学校统一身份认证；打卡页面本身不会自动跳回登录页。手动登录后，从平台打开“晚安成电”，工具会识别新标签页。

首次运行 `credentials` 会打开 Camoufox 原生 `about:logins`，预填官方认证域名 `https://idas.uestc.edu.cn`。请直接在浏览器内输入账号密码，点击 Save/保存，再在终端按回车。后续自动登录由浏览器填充，Python 只检查输入框是否非空及已保存的凭据数量，不读取或解密密码。请勿把密码发送到聊天、写进源码或放进命令行参数。已有凭据时，该命令打开原生管理页，供本地修改。

```powershell
uv run python main.py login --auto-login
# 以可见 GUI 运行，操作完成后继续保持窗口打开，供监控和检查
uv run python main.py login --auto-login --keep-open
# 清除本 profile 的学校 Cookie，验证账号密码自动填充和完整登录
uv run python main.py login --auto-login --force-login --keep-open
uv run python main.py checkin --auto-login --location .state/location.json
```

自动流程先访问平台入口；已登录时复用会话，否则等待浏览器在官方 `idas.uestc.edu.cn/authserver/login` 页面填充账号密码，点击网站登录按钮，由网站完成密码加密和认证，然后从平台打开“晚安成电”。结果中的 `authentication.source` 区分 `session`、`browser_passwords`、`environment`，`attempts` 表示本次提交登录表单的次数。失败只尝试一次，错误和结果文件不记录凭据。出现验证码、二次认证或未解锁的主密码时需要人工处理，不会绕过解锁。手动登录仍可使用 `uv run python main.py login`。尚未保存浏览器凭据时，本地环境变量 `UESTC_USERNAME`、`UESTC_PASSWORD` 可作为兼容来源；它们不等同于已在浏览器中加密保存。

学校表单未触发浏览器自动填充时，工具在同一官方认证页面临时创建标准登录字段，让原生密码管理器填充，再在页面内部将值转入原输入框，随后移除临时字段。此过程只向 Python 返回是否完成填充的布尔状态，保留原输入框的事件监听和学校的加密处理。兼容流程要求该 profile 只保存一个学校账号，填充后会再次检查验证码。

Camoufox 随包策略默认关闭密码管理器；工具首次启动时在 `.state/browser-runtime/<版本>/` 建立项目专用浏览器副本，仅启用原生密码管理器策略及相关启动偏好，不修改系统安装的 Camoufox。账号密码由 Firefox 自己加密存储在 profile 的 `logins.db`（旧版本为 `logins.json`），密钥由 `key4.db` 管理。无需额外保存明文配置文件。默认未设置主密码，支持无人值守登录；持有整个 profile 的人可能恢复凭据，因此应限制 profile 访问。可在浏览器中设置主密码，但每次启动需人工解锁。参见 [Mozilla 主密码说明](https://support.mozilla.org/en-US/kb/use-primary-password-protect-stored-logins)。

`--profile` 已提供隔离入口：不同目录分别保存账号密码、密钥和 Cookie。每个 profile 建议只保存一个学校账号；保存和登录时使用同一个路径。例如：

```powershell
uv run python main.py credentials --profile .state/profiles/account-a
uv run python main.py login --auto-login --profile .state/profiles/account-a --keep-open
```

2026-10-11 已用真实账号验证浏览器凭据登录：清除学校 Cookie 后，自动填充、提交一次登录表单并打开“晚安成电”，结果为 `authentication.source=browser_passwords`、`attempts=1`，页面显示当天有效打卡，记录时间为此前的 `22:11:36`。此轮只登录和查看状态，证据保存在 `.state/browser-login-result.json`。

## 小程序平台模拟实验

```powershell
uv run python main.py checkin --auto-login --simulate-miniprogram --keep-open --output .state/simulated-checkin-result.json
# 已保存定位时，可同时添加 --location .state/location.json
```

`--simulate-miniprogram` 仅用于 `checkin`，把页面产生的打卡 POST 请求头 `platform` 从 H5 标记改为 `mp-weixin`。登录和查询请求、认证信息、定位数据及请求正文沿用原流程，成功仍要求刷新后的当天记录和有效打卡界面。默认运行不启用模拟。

实验结果包含 `platform_simulation`，记录目标平台、拦截次数和原平台标记。失败结果保存当天记录及提交响应，先写入结果文件，再等待用户关闭 GUI，因此可以在窗口保持打开时检查证据。请求次数为 0 或尚未登录时，不能据此判断服务端是否接受平台模拟。此次真实账户验证成功，后续运行仍逐次核验；具体证据见 [微信小程序依赖分析](docs/wechat-analysis.md)。

2026-10-11 实测使用的定位来自上一晚浏览器缓存中腾讯 `type=1` 坐标转换请求的原始 WGS84 输入，已保存为 `.state/recovered-wgs84-location.json`，来源记录为 `.state/recovered-location-source.json`。这是历史定位，不是当晚新采集；原始传感器精度未恢复，定位覆盖使用工具默认的 30 米。使用该文件时可添加 `--location .state/recovered-wgs84-location.json`，浏览器无需调用 Windows 系统定位。

## 打卡和验证条件

- 从页面收到的 `/daily-checkin/api/student-checkin-record/today` 响应读取状态和业务日期。
- 只有今天、`PENDING`、按钮可用且页面明确显示“已进入打卡范围”才提交；时间是否允许由页面判断（当前界面为每天 21:00–23:30）。
- 自动点击“请打卡”，在“是否确认打卡？”弹窗中点击“确定/确认”。提交只执行一次。
- 刷新后同时确认接口的 `CHECKED_IN`、当天业务日期、非空打卡时间，以及页面的“有效打卡（当天日期）”。单独的“打卡成功”提示或 HTTP 200 不算完成。
- 已打卡时只验证，不再提交。区域外、免打卡、已填报、异常账户、登录失效、日期不符均不报告打卡成功。提交响应超时后也会刷新核验；无法确认时返回失败，不自动重复提交。

## 现有 Camoufox 实例

截图中的进程使用 `-juggler-pipe` 启动，不能通过普通调试端口接管，工具也不会复制其活动 profile 或关闭它。可用 `login` 建立自己的持久会话。若实例原本通过 Playwright server 启动并提供 WebSocket 地址，可使用：

```powershell
uv run python main.py inspect --connect ws://实际的Playwright地址
```

`--connect` 要求实例只有一个 browser context，只支持实例当前的定位，不支持 `--location`；它不是 Chromium CDP 连接。连接结束只断开驱动，不关闭远端浏览器。

其他选项：`--profile` 更改 profile 目录，`--output` 更改结果路径，`--timeout` 更改网页等待秒数，`--login-url` 更改手动登录入口，`--headless` 用于 inspect/checkin/verify 或自动 login。credentials、手动 login 和 capture 使用可见浏览器。`--force-login` 需配合 `--auto-login`，只清除所选本地 profile 的学校 Cookie。

## 验证

```powershell
uv run python -m unittest discover -s tests -v
```

测试在真实 Camoufox 中运行本地拦截页面，覆盖坐标传入、正常提交、刷新验证、重复运行、区域外、服务端拒绝、免打卡、错误日期，以及自动登录、会话复用、新标签页、缺少凭据、认证拒绝、验证码和非官方认证地址。密码测试使用虚构凭据，在原生管理器中保存、关闭并重启浏览器，确认加密存储、无环境变量时自动填充和提交、特殊表单兼容填充、不同 profile 隔离，以及失败不重试、不泄露凭据。认证表单复现学校禁用可见密码框、提交独立加密字段的行为；填充后出现验证码时确认零次登录提交并移除临时字段。平台模拟另用本地 HTTP 服务检查实际收到的请求，确认仅修改打卡 POST 的平台头，保留正文、Cookie 和认证头，并在失败时及时保存证据。测试不会向学校提交打卡或登录凭据。实际网页的选择器与接口路径已根据 2026-10-10 部署的公开前端资源核对；真实账户端到端提交仍需要可用登录会话及位置。

参考：[Camoufox 定位配置](https://camoufox.com/fingerprint/geolocation/)、[Playwright 定位模拟](https://playwright.dev/python/docs/emulation#geolocation)、[Camoufox 持久会话](https://camoufox.com/python/usage/)。

### 网页监视和加密账号配置

容器网页提供实时浏览器画面及账号配置。学校账号密码由服务端 AES-256-GCM 加密，密钥保存在独立只读卷。网页表单只允许写入或替换，不回显、不导出已有凭据，原密码管理器入口已关闭。开启监视后，“测试自动登录”显示 GUI，并提供发送/输入验证码、确认验证和信任设备的受限控件；登录测试保留设备信任信息，成功或失败后保留画面两分钟供调试。常规批量任务仍在后台认证后显示打卡画面，监视在服务器端强制只读。首次部署需运行 `docker compose run --rm vault-init`，详见 [容器部署说明](docs/docker.md)。

容器支持每日定时守护：在网页设置北京时间的开始、结束时间并启用，每天随机选择一次后台批量启动时间。计划持久化，重启不重复执行；忙碌等待、错过窗口和失败均记录在网页。首次默认关闭，详见 [每日定时守护](docs/docker.md#每日定时守护)。
