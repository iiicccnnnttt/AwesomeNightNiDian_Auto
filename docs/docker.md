# Docker 使用说明

适合已熟悉 Docker Compose、SSH 和卷备份的用户。服务提供多账号配置、只读浏览器监视、首次登录二次验证及每天随机时间启动的定时守护。以下地址、目录和端口为通用示例，不包含实际服务器或账号信息。

## 部署

在完整项目根目录执行，需要 Docker Engine 和 Compose v2：

```sh
docker compose build checkin
docker compose run --rm vault-init
docker compose up -d checkin
docker compose ps
```

打开 [本机控制台](http://localhost:8080)。Compose 仅将 HTTP 绑定到宿主回环地址；远程访问使用 SSH 隧道，终端保持开启：

```sh
ssh -N -L 8080:127.0.0.1:8080 user@server.example
```

`user@server.example` 替换成自己的 SSH 目标。右侧端口是服务器发布端口，按配置调整；本机浏览器访问 `http://localhost:8080`。本机 8080 已占用时，左侧改为 18080，并访问本机 18080。

可在项目 `.env` 配置：

```dotenv
CHECKIN_IMAGE=auto-location:local
CHECKIN_HTTP_PORT=8080
CHECKIN_SETUP_PORT=6080
```

`CHECKIN_SETUP_PORT` 仅用于可选 `setup` profile；日常网页监视通过同源 `/monitor/`，无需单独映射 VNC 端口。服务默认启动 GUI 监视组件，定时批量任务使用无 GUI 模式。

## Nginx 子路径反代

支持在 `/auto/` 或其他以 `/` 结尾的子目录访问。网页按当前访问目录生成 API、监视资源及 WebSocket 地址，不需要额外配置服务端前缀，也不需要 `sub_filter` 或 Cookie 路径重写。

将以下内容放进现有 `server {}`（完整示例见 `docker/nginx-auto.conf`）：

```nginx
location = /auto {
    return 308 /auto/;
}

location /auto/ {
    proxy_pass http://127.0.0.1:18080/;
    proxy_http_version 1.1;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    proxy_buffering off;
}
```

将上游端口替换为实际发布端口；Nginx 若也运行在容器中，`127.0.0.1` 指向 Nginx 容器，应改用同一 Docker 网络上的服务名（例如 `http://checkin:8080/`）。使用其他目录时同步修改两个 `location` 和跳转目标。保留 `proxy_pass` 末尾 `/`，将子路径剥离后转发给应用；外部地址须以 `/` 结尾，避免相对 URL 指向上一层。

`Host $http_host` 保留访问端口，让 WebSocket 的同源校验正确；仅配置 `proxy_pass` 会导致监视连接失败。协议升级请求头须显式转发，参见 [Nginx WebSocket 官方说明](https://nginx.org/en/docs/http/websocket.html)。HTTPS 入口自动使用 `wss`。Nginx 的 HTTPS 与访问认证配置沿用现有站点；此段配置本身不提供用户认证。

升级应用镜像并刷新网页后，执行 `nginx -t`，通过后由管理员 reload，再访问 `https://你的域名/auto/`。部署旧版时创建的 `/monitor/` Cookie 不适用于新子路径，重新打开首页即可获得新的 Cookie。API 会发往 `/auto/api/…`，监视资源及 WebSocket 发往 `/auto/monitor/…`；不要额外将它们暴露为站点根路由。

## 初始化用户与手动操作

1. 在“用户配置”新增账号别名，点击“设置登录凭据”加密保存学校账号密码。表单只写入、不回显。
2. 勾选该用户的“启用”和“自动登录”，保存设置。单个允许区域可留空区域序号；多个区域须先确认正确序号，从 0 开始。
3. 开启“监视本次浏览器操作”，点击该用户的“测试自动登录”。首次验证在画面旁的控件发送、填写验证码，完成确认及设备信任。浏览器画面只读，不能直接在画面内点击或输入。
4. 登录前图形验证码和登录后二次验证，均按网页识别出的验证控件操作；填写后点击“填写验证码并继续登录”。若页面提示没有唯一提交按钮，按提示选择已识别按钮。未识别的滑块等验证需管理员另行处理，不会自动绕过。
5. 登录测试不打卡，最多等待约 10 分钟，结束后保留画面约两分钟；可“结束登录调试”提前关闭。确认登录可用后，再在学校允许时段执行“一键打卡”。

关闭监视时登录测试在后台执行，需要验证会失败退出。批量任务随机排列启用账号，各账号使用独立 profile、Cookie 和持久化指纹；失败后继续其他账号，不自动重试。已完成账号只核验，成功以刷新后的当天有效记录为准。

“删除用户”移除配置、加密凭据和服务管理的浏览器数据；外部导入的 profile 与历史报告保留。重新添加须重新配置。账号增删、凭据修改和浏览器任务互斥。

## 每日定时守护

网页“每日定时打卡”设置开始、结束时间，勾选“启用无人值守”并保存。**默认关闭**；示例可设 21:00–23:00，并为账号数量和学校截止时间预留运行余量。

- 固定北京时间 `Asia/Shanghai`，每天在 `[开始, 结束)` 内随机选一个秒级启动时间，不支持跨午夜。
- 时间段限制的是批量任务开始时间；任务可能在结束时间后继续运行。实际启动以守护线程轮询和任务空闲为准。
- 计划及执行记录保存到 `.state/schedule.json`。重启保留计划；仍在窗口内可以补启动，超过结束时间则记为 `missed`。
- 到点忙碌则在窗口内等待；失败不自动重试。当日已领取或执行的定时任务不会因改配置、关闭再开启或重启而重复触发。
- 重启前运行中的任务记为 `interrupted`，结果未知，须检查报告及学校记录后人工核验。关闭定时不终止正在运行的任务，修改时间段会重新抽取未领取计划。
- 网页显示今日状态、下次随机时间及最近七条记录。定时任务无 GUI；需要新验证码时失败，先通过 GUI 登录测试修复。

服务进程自带守护线程，无需宿主 cron。`restart: unless-stopped` 支持异常退出及宿主重启后恢复；手动停止的服务需再次启动。仅运行一个 `checkin` 副本，Linux 文件锁防止同卷多守护并行；调度状态损坏不会自动重置，须恢复备份。

## 运维与结果

```sh
# 服务状态、日志
docker compose ps
docker compose logs --tail=100 checkin

# 只检查账号，不提交打卡
docker compose run --rm cli batch-inspect --headless

# 手动后台批量打卡（与网页任务、定时任务错开）
docker compose run --rm cli batch-checkin --headless --simulate-miniprogram

# 升级：先确认没有任务运行，保留同一 Compose 项目名和卷
docker compose build checkin
docker compose up -d --no-build checkin
```

主要持久化文件均位于数据卷：`accounts.json`（账号配置）、`schedule.json`（定时状态）、`batch-result.json`（最新批量结果）、`batch-runs/`（逐次历史）、`service-job.log`（最近网页任务日志）。`/healthz` 仅说明 HTTP 服务可用；不能代替逐账号的打卡核验。分享结果、日志前需遮盖姓名、学号、宿舍和坐标。

## 加密、备份与访问边界

`checkin-state` 卷保存账号、profile、会话和 AES-256-GCM 密文；`vault-key` 卷独立保存 `/run/secrets/vault.key`，仅以只读方式挂载服务。`vault-init` 只初始化不存在的密钥，不覆盖已有密钥。旧浏览器凭据须通过网页重新配置；成功保存后清理原生密码数据库，不自动解密迁移。

**同时备份数据卷和密钥卷，并限制备份读取权限。** 恢复时使用匹配的两份备份及相同卷配置。密钥丢失后无法恢复已有凭据，不可生成新密钥代替。`docker compose down` 保留卷，`down -v` 删除数据和密钥。

网页是可信管理员控制台，不提供逐用户权限隔离。不要将 HTTP 直接暴露公网；外部发布需 HTTPS 和访问认证。密文不能防止宿主 root、Docker 管理员或运行进程调试者接触解密后的内存。位置来自允许区域内模拟坐标，不是实际设备定位。

## 下载代理

镜像拉取使用 Docker **守护进程**代理，由管理员配置；不能只给 CLI 设置 `HTTP_PROXY`。Linux systemd 环境可使用项目脚本：

```sh
sudo sh docker/configure-daemon-proxy.sh
```

脚本使用示例代理 `http://127.0.0.1:7890`，执行前确认符合宿主配置；会重启 Docker，应在任务与构建空闲时执行。已有 `daemon.json` 代理配置可能优先于 systemd 环境变量，参见 [Docker 官方代理说明](https://docs.docker.com/engine/daemon/proxy/)。

构建中下载 Camoufox 可设置 `CAMOUFOX_DOWNLOAD_PROXY`；地址必须能从构建容器访问，容器内 `127.0.0.1` 不是宿主代理。`CHECKIN_IMAGE` 若指向预构建镜像，应自行确保镜像包含当前服务代码。
