# 微信小程序依赖与模拟可行性分析

分析及实测日期：2026-10-11（Asia/Shanghai）。依据是 2026-10-10 的真实打卡响应、2026-10-11 读取的学校公开 H5 资源，以及 uni-app 官方文档。后续在用户授权下进行了一次平台模拟提交，结果见末尾实测记录。

**实测结论：2026-10-11 将打卡 POST 的平台头改为 `mp-weixin` 后，服务端接受了提交，刷新确认当天有效打卡。** 默认 H5 提交曾被拒绝，但该账户本次成功流程没有使用真实微信宿主。以下保留原先分析中的未知条件，并区分此次实测能够证实的范围。

## 已确认的操作边界

| 操作 | 当前 Camoufox 能力 | 小程序依赖 |
| --- | --- | --- |
| 学校统一认证、进入晚安成电 | 可完成，支持复用会话和本地环境变量登录 | 当前 H5 登录链路未要求微信登录 |
| 读取打卡时间、宿舍范围、当天记录 | H5 提供相应查询接口 | 实测未因普通浏览器而拒绝这些查询 |
| 提供经纬度、显示地图、判断进入范围 | 可完成；页面同时支持定位 API 与路由传入的坐标 | 获取手机定位和手机授权状态时，需要对应宿主能力 |
| 跳转小程序的原生定位页面 | 普通浏览器不能执行宿主页面跳转 | 需要真实的小程序桥接和宿主页面 |
| 点击打卡、确认弹窗、发起提交 | 页面操作可以完成，平台模拟实测成功 | 默认 H5 曾被拒绝；本次更改平台头后被接受，无需真实微信宿主 |
| 刷新并验证成功 | 可读取当天状态并核对日期、时间和页面 | 无需把验证程序迁入小程序 |

## 真实拒绝证据

保存结果：`../.state/real-checkin-result.json`。业务日期为 **2026-10-10**，不是分析当天。

- 打卡接口 HTTP 状态为 200，业务错误码为 `4006`。
- 消息为“请在微信小程序内打开打卡页面后再打卡”。
- 刷新后为 `PENDING`，`checkinTime` 为 null，没有完成打卡。

因此，拒绝来自服务端业务响应。更改页面文字、移除弹窗或模拟一个成功提示，都不能改变已保存的打卡记录。

## 公开 H5 中的关键线索

1. 主包请求拦截器设置 `platform: "h5"`；用户信息中有 token 时，再添加 Bearer Authorization。这个平台标记来自编译后的 H5 代码，修改 User-Agent 不会自动修改它。
2. 小程序分支条件编译为 `"h5".startsWith("mp-")`，在当前包中为 false。伪造 JavaScript 环境标志不能把这个包变成小程序编译产物。
3. 启动时查询 `/api/auth/user-info`，支持 URL 中的 `sessionId`。已检查的 H5 登录和提交链路没有出现微信登录 code、openid 或 session_key。小程序宿主是否另外生成凭据，仍未知。
4. 今日页面初始化时读取路由的 `latitude` 和 `longitude`。两者已有值时，执行逆地理编码并跳过本页定位采集。这里使用的坐标语义为 GCJ02；浏览器 Geolocation 的输入仍应为 WGS84。
5. 页面通过 `window.webUni.webView.redirectTo` 跳转 `/pages/getLocation/index`，并传入 H5 项目与返回页面。桥接库在微信环境中转交给 `window.wx.miniProgram`。这说明页面设计中存在小程序宿主获取定位的路径，但公开 H5 不包含该宿主页面的实现。
6. 提交 `/api/student-checkin-record/checkin` 时包含经纬度、逆地理编码、客户端时间、User-Agent 和定位诊断信息。诊断字段包括坐标系、精度、定位耗时、网络类型、定位开关、授权和精度授权状态。不能据此判断服务端对每个字段的校验方式。

主包 SHA-256：`af20b93221c6a5f0b0c24bc20037bb7edaa8fbe857e3d0d47115a2c2cd54a2b3`。公开资源快照保存于 `../.state/analysis-source/`。

## 能模拟什么，是否能绕过

| 方案 | 可以做到的事情 | 对服务端成功的判断 |
| --- | --- | --- |
| 修改 User-Agent、环境标志 | 模拟微信环境判断的输入 | 不会自动更改 H5 平台请求头，未证明有效 |
| 提供 `wx` / `webUni` 桩对象 | 模拟桥接回调、页面跳转与定位结果，用于测试 | 不提供真实微信宿主或微信登录凭据 |
| 修改客户端提交的平台标记 | 可改变请求中的平台字段 | `mp-weixin` 在此次真实账户提交中有效；不能据此断言服务端没有任何其他检查 |
| 获取真实微信登录凭据后接入 | 如果业务确实要求微信认证，可沿真实微信登录链路取得凭据 | 需要确认该站实际使用的认证协议；普通 H5 不支持 `uni.login` 的微信小程序登录 |
| 在真实手机微信内操作小程序 | 使用真实宿主、定位与授权链路 | 符合报错提示要求的运行环境，仍需按当天服务端记录核验成功 |

如果后续平台模拟再次失败，可在用户自己的正常小程序会话中对比一条真实打卡请求与 H5 请求，关注平台字段、认证会话类型和提交字段。诊断记录只保留字段名、平台值和非敏感状态，不保存密码、Cookie 或 token 内容。仅比较只读查询请求不能证明打卡 POST 的检查条件。

单次实测已经证实本次账户和部署接受了平台模拟。它不能覆盖其他账户、其他时间或后续服务端变更，因此工具继续刷新核验每次实际打卡记录。

## 2026-10-11 平台模拟实验

工具已新增 `checkin --simulate-miniprogram`：仅拦截打卡 POST，将 `platform` 设为 `mp-weixin`，不修改原请求正文或服务端响应。这个平台值已在本次实测中被接受。原有刷新核验和避免重复提交的条件继续生效。

19 项本地测试已通过。本地 HTTP 服务实测收到普通模式的 `h5` 与实验模式的 `mp-weixin`，请求正文、Cookie 和 Bearer 认证保持完整，查询及无关 POST 的平台值保持原样。

最初的准备过程遇到会话过期和 Windows 系统定位关闭，均未产生打卡 POST。用户重新登录后，从旧浏览器缓存中恢复腾讯 `type=1` 坐标转换请求的原始 WGS84 输入，并作为定位覆盖传给 Camoufox。没有反算地图中心，也没有把 GCJ02 直接作为浏览器坐标。来源时间为 2026-10-10 22:58:46；这不是当天新采集的定位。原始传感器精度未知，覆盖精度使用工具默认的 30 米。

随后执行：

```powershell
uv run python main.py checkin --auto-login --simulate-miniprogram --location .state/recovered-wgs84-location.json --keep-open --timeout 60 --output .state/simulated-checkin-result.json
```

- 原平台标记：`h5`；实际提交标记：`mp-weixin`；实验提交次数：1。
- 服务端 HTTP 状态：200；业务码：0；消息：“操作成功”。
- 刷新后状态：`CHECKED_IN`；业务日期：`2026-10-11`。
- 打卡时间：`2026-10-11 22:11:36`；核验时间：`2026-10-11T22:11:37.142308+08:00`。
- 核验同时要求页面显示当天“有效打卡”，没有只依赖提交提示。

实测结果保存在 `../.state/simulated-checkin-result.json`。恢复的定位和来源记录分别为 `../.state/recovered-wgs84-location.json`、`../.state/recovered-location-source.json`。

## 参考资源

- [学校主包](https://jzsz.uestc.edu.cn/daily-checkin-h5/assets/index-C6QmvmXo.js)
- [学校今日打卡页面](https://jzsz.uestc.edu.cn/daily-checkin-h5/assets/pages-student-report-today-home-index.CNkaaYrP.js)
- [学校学生接口模块](https://jzsz.uestc.edu.cn/daily-checkin-h5/assets/student.B-7kz2Ar.js)
- [学校 WebView 桥接库](https://jzsz.uestc.edu.cn/daily-checkin-h5/static/js/uni-webview-js-1.5.5.js)
- [uni-app web-view 文档](https://uniapp.dcloud.net.cn/component/web-view)
- [uni-app 定位文档](https://uniapp.dcloud.net.cn/api/location/location)
- [uni-app 登录文档](https://uniapp.dcloud.net.cn/api/plugins/login)

uni-app 官方文档区分了普通 H5 和小程序运行环境；微信小程序登录返回的 code 需要由开发者后端交换身份信息。微信开发者官方文档本次无法访问，因此以上平台能力说明引用 uni-app 官方文档，不把未读取的微信文档作为证据。
