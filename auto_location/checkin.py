from __future__ import annotations

import math
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from playwright.sync_api import Page, Request, Response, Route

URL = "https://jzsz.uestc.edu.cn/daily-checkin-h5/index.html?appId=294#/pages/student/report-today/home/index"
TODAY = "/daily-checkin/api/student-checkin-record/today"
SUBMIT = "/daily-checkin/api/student-checkin-record/checkin"
REVERSE = "/daily-checkin/api/map/reverse"
SHANGHAI = ZoneInfo("Asia/Shanghai")


class CheckinError(RuntimeError):
    pass


@dataclass(frozen=True)
class Location:
    latitude: float
    longitude: float
    accuracy: float = 30
    coordinate_system: str = "WGS84"
    captured_at: str | None = None

    def __post_init__(self):
        for name, low, high in (("latitude", -90, 90), ("longitude", -180, 180),
                                ("accuracy", 0, float("inf"))):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"Invalid {name}: {value}")
        if self.coordinate_system != "WGS84":
            raise ValueError("浏览器定位需要 WGS84 坐标；不能直接使用腾讯地图的 GCJ02 坐标。")

    def to_dict(self):
        return asdict(self)


def location_preferences(location: Location) -> dict:
    """Give Firefox a local location provider; avoid invoking Windows Location."""
    provider = json.dumps({"location": {"lat": location.latitude, "lng": location.longitude},
                           "accuracy": location.accuracy}, separators=(",", ":"))
    return {"geo.provider.testing": True, "geo.provider.ms-windows-location": False,
            "geo.provider.network.url": "data:application/json," + provider,
            "geo.wifi.scan": False}


def checked_record(record: dict | None, business_date: str) -> bool:
    if not record or record.get("recordStatusCode") != "CHECKED_IN":
        return False
    return (str(record.get("businessDate", ""))[:10] == business_date
            and bool(record.get("checkinTime")))


class CheckinPage:
    """Drive the site's UI; observe responses without a parallel API submission."""

    def __init__(self, page: Page, url: str = URL, timeout: float = 60,
                 simulate_miniprogram: bool = False):
        self.page, self.url, self.timeout = page, url, timeout
        self.origin = urlsplit(url).netloc
        self.record: dict | None = None
        self.record_version = 0
        self.submit_result: dict | None = None
        self.observation_error: str | None = None
        self.location_ready = False
        self.areas = None
        self.area_error = None
        self.location_resolution = None
        self.platform_simulation = ({"platform": "mp-weixin", "requests": 0,
                                     "original_platform": None} if simulate_miniprogram else None)
        page.set_default_timeout(timeout * 1000)
        page.on("response", self._observe)
        page.on("requestfinished", self._request_finished)
        page.on("dialog", lambda dialog: dialog.dismiss())
        if simulate_miniprogram:
            origin = urlsplit(url)
            target = f"{origin.scheme}://{origin.netloc}{SUBMIT}"
            page.route(re.compile(r"^" + re.escape(target) + r"(?:\?.*)?$"),
                       self._simulate_submission_platform)

    def _simulate_submission_platform(self, route: Route):
        request = route.request
        if request.method != "POST":
            route.fallback()
            return
        # Change one client-supplied marker on the page's own submission. Keep
        # authentication, coordinates, request body and response untouched.
        headers = request.all_headers()
        assert self.platform_simulation is not None
        self.platform_simulation["original_platform"] = headers.get("platform")
        self.platform_simulation["requests"] += 1
        route.fallback(headers={**headers, "platform": "mp-weixin"})

    def _request_finished(self, request: Request):
        parsed = urlsplit(request.url)
        if parsed.netloc == self.origin and parsed.path == REVERSE:
            self.location_ready = True

    def _observe(self, response: Response):
        parsed = urlsplit(response.url)
        from .areas import AVAILABLE, parse_areas
        if parsed.netloc == self.origin and parsed.path == AVAILABLE:
            self.areas = None
            try:
                if not response.ok:
                    raise CheckinError("允许打卡区域接口 HTTP 失败。")
                self.areas = parse_areas(response.json())
                self.area_error = None
            except Exception as exc:
                self.area_error = str(exc)
            return
        if parsed.netloc != self.origin or parsed.path not in (TODAY, SUBMIT):
            return
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("响应不是 JSON 对象")
            if parsed.path == TODAY:
                self.record_version += 1
                self.record = None
                if response.ok and payload.get("code") == 0 and isinstance(payload.get("data"), dict):
                    self.record = {key: payload["data"].get(key) for key in
                                   ("recordStatusCode", "businessDate", "checkinTime")}
                    self.observation_error = None
                else:
                    self.observation_error = "当天打卡状态接口未成功返回，请检查登录状态。"
            else:
                self.submit_result = {"http_status": response.status, "code": payload.get("code"),
                                      "message": str(payload.get("msg") or "")[:500]}
        except Exception as exc:
            self.observation_error = f"无法读取打卡状态：{type(exc).__name__}"

    def _wait(self, predicate, message):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            self.page.wait_for_timeout(200)
        raise CheckinError(message)

    def open(self):
        self.record = None
        self.location_ready = False
        same_document = self.page.url.split("#", 1)[0] == self.url.split("#", 1)[0]
        self.page.goto(self.url, wait_until="domcontentloaded")
        if same_document:
            # The portal may have already opened this SPA and fetched today's
            # record before this listener existed. A same-document goto does not
            # repeat that request; reload explicitly to obtain fresh evidence.
            self.page.reload(wait_until="domcontentloaded")
        self._wait(lambda: self.record is not None,
                   "未取得当天打卡状态。请先运行 login 完成登录；也可能是网站响应失败。")
        self._wait(lambda: self.page.locator(".check-in-circle, .location-pill.success, .status-free").count() > 0,
                   "状态已返回但页面没有完成渲染。")

    def capture_location(self) -> Location:
        key = "data-auto-location-" + uuid.uuid4().hex
        # A script runs in the page's main world, where native location callbacks
        # work. Poll its DOM result from Playwright's isolated world.
        script = self.page.add_script_tag(content="""(() => {
            const key = KEY;
            const finish = value => document.documentElement.setAttribute(key, JSON.stringify(value));
            if (!navigator.geolocation) { finish({error: '浏览器不支持定位'}); return; }
            navigator.geolocation.getCurrentPosition(p => finish({
                latitude: p.coords.latitude, longitude: p.coords.longitude,
                accuracy: p.coords.accuracy
            }), e => finish({error: `定位失败 (${e.code}): ${e.message}`}),
            {enableHighAccuracy: true, timeout: 30000, maximumAge: 0});
        })();""".replace("KEY", json.dumps(key)))
        result = None
        deadline = time.monotonic() + 31
        try:
            while time.monotonic() < deadline:
                raw = self.page.locator("html").get_attribute(key)
                if raw is not None:
                    result = json.loads(raw)
                    break
                self.page.wait_for_timeout(200)
        finally:
            # Clean up in the main world as well.
            cleanup = self.page.add_script_tag(content=f"document.documentElement.removeAttribute({json.dumps(key)});")
            cleanup.dispose()
            script.dispose()
        if result is None:
            raise CheckinError("定位请求超时；如弹出 Windows 位置设置，请检查系统定位是否可用。")
        if "error" in result:
            raise CheckinError(result["error"])
        return Location(**result, captured_at=datetime.now(SHANGHAI).isoformat())

    def discover_areas(self):
        self._wait(lambda: self.areas is not None or self.area_error is not None,
                   "未收到允许打卡区域响应，请检查登录或网站接口。")
        if self.area_error:
            raise CheckinError(self.area_error)
        return {"source": "account_allowed_areas", "device_location": False,
                "coordinate_system": "GCJ02", "areas": self.areas,
                "discovered_at": datetime.now(SHANGHAI).isoformat()}

    def use_area(self, index=None):
        from .areas import area_url, select_area, random_point
        evidence = self.discover_areas()
        selected = select_area(evidence["areas"], index)
        point = random_point(selected)
        self.location_resolution = {**evidence, "selected_area": selected, "selected_point": point,
                                    "method": "site_gcj02_route", "page_in_range": False,
                                    "range_validation": "pending"}
        self.url = area_url(self.url, point)
        self.open()
        self._wait(lambda: self.location_ready or self.inspect()["in_range"] or self.inspect()["location_failed"],
                   "页面未完成随机位置加载，停止操作。")
        self.page.wait_for_timeout(200)
        state = self.inspect()
        if state["location_failed"] or state["outside_range"]:
            raise CheckinError("随机位置未被页面认可。")
        # Outside the check-in window the site hides the range label entirely.
        # Do not claim a UI range confirmation based solely on reverse geocoding.
        if not state["in_range"]:
            text = self.page.locator("body").inner_text()
            if not any(label in text for label in ("未到", "已截止", "有效打卡（", "无需打卡", "免打卡")):
                raise CheckinError("页面未显示范围或可识别的非打卡状态。")
        self.location_resolution.update(page_in_range=state["in_range"],
                range_validation="confirmed" if state["in_range"] else "not_visible_in_current_state")
        return self.location_resolution

    def inspect(self) -> dict:
        text = self.page.locator("body").inner_text()
        return {"record": self.record, "in_range": "已进入打卡范围：" in text,
                "outside_range": "您当前未在宿舍打卡区域" in text,
                "location_failed": "获取定位失败" in text,
                "valid_checkin_visible": "有效打卡（" in text,
                "observation_error": self.observation_error}

    def verify(self, business_date: str | None = None) -> dict:
        expected = business_date or datetime.now(SHANGHAI).date().isoformat()
        before = self.record_version
        self.record = None
        self.page.reload(wait_until="domcontentloaded")
        self._wait(lambda: self.record_version > before and self.record is not None,
                   "刷新后无法获取当天状态，打卡结果未知。不要盲目重复提交。")
        if not checked_record(self.record, expected):
            raise CheckinError(f"未确认 {expected} 有效打卡：{self.record}")
        date_pattern = re.compile(r"有效打卡（\s*" + re.escape(expected))
        self.page.get_by_text(date_pattern).wait_for(state="visible")
        return {"status": "verified", "record": self.record, "verified_at": datetime.now(SHANGHAI).isoformat()}

    def checkin(self) -> dict:
        if self.record is None:
            raise CheckinError("必须先取得当天状态。")
        business_date = str(self.record.get("businessDate", ""))[:10]
        if business_date != datetime.now(SHANGHAI).date().isoformat():
            raise CheckinError("网站业务日期不是今天，停止提交。")
        if checked_record(self.record, business_date):
            return {**self.verify(business_date), "already_checked_in": True}
        state = self.record.get("recordStatusCode")
        if state != "PENDING":
            raise CheckinError(f"当前状态 {state} 不允许普通打卡（填报、免打卡等状态不能作为成功）。")
        self._wait(lambda: self.location_ready
                   or self.page.get_by_text("已进入打卡范围：", exact=False).count() > 0
                   or "获取定位失败" in self.page.locator("body").inner_text(),
                   "定位尚未完成，未提交打卡。")
        # Allow the app's location Promise continuation to update its circle.
        self.page.wait_for_timeout(200)
        self._wait(lambda: self.page.locator(".check-in-circle").count() > 0,
                   "未出现打卡控件，定位或页面加载失败。")
        circle = self.page.locator(".check-in-circle")
        self._wait(lambda: (self.page.get_by_text("已进入打卡范围：", exact=False).count() > 0
                           or "disabled" in (circle.first.get_attribute("class") or "")
                           or "请联系辅导员处理" in self.page.locator("body").inner_text()),
                   "定位尚未完成或页面状态无法识别。")
        text = self.page.locator("body").inner_text()
        if "请联系辅导员处理" in text:
            raise CheckinError("账户状态异常，页面要求联系辅导员。")
        if "您当前未在宿舍打卡区域" in text:
            raise CheckinError("当前定位不在宿舍打卡区域。")
        if "已截止" in text or "未到打卡时间段" in text or "未到\n时间" in text:
            raise CheckinError("当前不在网站允许的打卡时间内。")
        if circle.count() != 1 or "disabled" in (circle.get_attribute("class") or ""):
            raise CheckinError("打卡按钮不可用。")
        if "已进入打卡范围：" not in text or "请打卡" not in circle.inner_text():
            raise CheckinError("页面没有确认定位进入打卡范围。")
        self.submit_result = None
        previous_version = self.record_version
        circle.click()
        box = self.page.locator(".wd-message-box__container").filter(has_text="是否确认打卡？")
        box.wait_for(state="visible")
        confirm = box.locator(".wd-message-box__actions-btn").filter(
            has_text=re.compile(r"^\s*(确定|确认)\s*$"))
        if not confirm.count():
            confirm = box.get_by_role("button", name=re.compile(r"^(确定|确认)$"))
        confirm.click()
        try:
            self._wait(lambda: self.submit_result is not None, "提交响应超时。")
            self._wait(lambda: self.record_version > previous_version
                       or (self.submit_result or {}).get("code") != 0,
                       "等待页面刷新状态超时。")
        except CheckinError:
            pass
        try:
            result = self.verify(business_date)
        except CheckinError as exc:
            raise CheckinError(f"{exc}；提交响应：{self.submit_result}") from None
        result = {**result, "submission": self.submit_result, "already_checked_in": False}
        if self.platform_simulation is not None:
            result["platform_simulation"] = dict(self.platform_simulation)
        return result
