"""Account-configured GCJ02 circles; these are not sensed device locations."""
import math
import random
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .checkin import CheckinError

AVAILABLE = "/daily-checkin/api/checkin-location/available-locations"
EARTH_RADIUS_METER = 6378137  # Same spherical distance model as the site's UI.


def distance_meter(a, b):
    lat1, lat2 = math.radians(a["latitude"]), math.radians(b["latitude"])
    dlat = lat2 - lat1
    dlon = math.radians(b["longitude"] - a["longitude"])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_METER * math.asin(math.sqrt(max(0, min(1, h))))


def random_point(area, rng=None):
    """Sample a spherical annulus: exclude the center and outer 10% radius."""
    radius = area["radius_meter"]
    if not math.isfinite(radius) or not 0 < radius < math.pi * EARTH_RADIUS_METER:
        raise CheckinError("区域半径不适合随机定位。")
    rng = rng if rng is not None else random.SystemRandom()
    inner, outer = radius * 0.01, radius * 0.9
    low = math.sin(inner / (2 * EARTH_RADIUS_METER)) ** 2
    high = math.sin(outer / (2 * EARTH_RADIUS_METER)) ** 2
    lat, lon = math.radians(area["latitude"]), math.radians(area["longitude"])
    for _ in range(16):
        # Uniform area distribution, rather than uniform distance from center.
        arc = 2 * math.asin(math.sqrt(low + rng.random() * (high - low)))
        bearing = rng.random() * 2 * math.pi
        new_lat = math.asin(max(-1, min(1, math.sin(lat) * math.cos(arc)
                           + math.cos(lat) * math.sin(arc) * math.cos(bearing))))
        new_lon = lon + math.atan2(math.sin(bearing) * math.sin(arc) * math.cos(lat),
                                   math.cos(arc) - math.sin(lat) * math.sin(new_lat))
        point = {"latitude": math.degrees(new_lat),
                 "longitude": (math.degrees(new_lon) + 180) % 360 - 180,
                 "coordinate_system": "GCJ02"}
        distance = distance_meter(area, point)
        # Validate the final coordinates, including floating point effects.
        if 0 < distance < radius and distance <= outer + 1e-7:
            return {**point, "distance_from_center_meter": distance,
                    "boundary_clearance_meter": radius - distance,
                    "sampling": "uniform_area_annulus", "inner_radius_meter": inner,
                    "outer_radius_meter": outer}
    raise CheckinError("未生成可验证的非中心区域内坐标，停止操作。")


def parse_areas(payload):
    if not isinstance(payload, dict) or payload.get("code") != 0 or not isinstance(payload.get("data"), list):
        raise CheckinError("允许打卡区域接口未成功返回。")
    areas = []
    for item in payload["data"]:
        if not isinstance(item, dict):
            raise CheckinError("允许打卡区域格式无效。")
        for key, low, high in (("latitude", -90, 90), ("longitude", -180, 180), ("radiusMeter", 0, math.inf)):
            value = item.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise CheckinError("允许打卡区域包含无效坐标或半径。")
        if item["radiusMeter"] <= 0 or not item["latitude"] or not item["longitude"]:
            raise CheckinError("允许打卡区域不可用。")
        areas.append({"index": len(areas), "name": str(item.get("groupName") or "未命名区域"),
                      "latitude": item["latitude"], "longitude": item["longitude"],
                      "radius_meter": item["radiusMeter"], "coordinate_system": "GCJ02"})
    return areas


def select_area(areas, index=None):
    if not areas:
        raise CheckinError("账户没有配置允许打卡区域，请检查宿舍配置。")
    if index is None:
        if len(areas) != 1:
            raise CheckinError("账户有多个允许区域，请先运行 discover，再用 --area-index 选择。")
        index = 0
    if index < 0 or index >= len(areas):
        raise CheckinError("area-index 超出允许区域列表。")
    return areas[index]


def area_url(url, area):
    # Uni-app's route query consumes GCJ02 directly, avoiding double conversion.
    parsed = urlsplit(url)
    path, _, query = parsed.fragment.partition("?")
    pairs = [(k, v) for k, v in parse_qsl(query) if k not in ("latitude", "longitude")]
    pairs += [("latitude", str(area["latitude"])), ("longitude", str(area["longitude"]))]
    return urlunsplit(parsed._replace(fragment=path + "?" + urlencode(pairs)))
