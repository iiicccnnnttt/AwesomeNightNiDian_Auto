import unittest
import random
from urllib.parse import parse_qs, urlsplit

from auto_location.areas import parse_areas, select_area, area_url, random_point, distance_meter
from auto_location.checkin import CheckinError, URL


class AreaTests(unittest.TestCase):
    def test_random_points_stay_inside_with_clearance_and_are_not_center(self):
        rng = random.Random(123)
        for latitude, longitude, radius in ((30.755685, 103.932944, 140),
                                             (89.99, 179.999, 10000), (-89.99, -179.999, 10000),
                                             (30.7, 104.1, 0.01)):
            area = {"latitude": latitude, "longitude": longitude, "radius_meter": radius}
            points = [random_point(area, rng) for _ in range(100)]
            for point in points:
                d = distance_meter(area, point)
                self.assertGreater(d, 0)
                self.assertLess(d, radius)
                self.assertLessEqual(d, radius * .9 + 1e-7)
                self.assertAlmostEqual(point["boundary_clearance_meter"], radius - d)
                self.assertEqual(point["coordinate_system"], "GCJ02")
                self.assertTrue(-180 <= point["longitude"] <= 180)
            self.assertGreater(len({(p["latitude"], p["longitude"]) for p in points}), 90)

    def test_area_sampling_is_not_uniform_radius(self):
        rng = random.Random(456)
        area = {"latitude": 30.7, "longitude": 104.1, "radius_meter": 140}
        normalized_squared = [(random_point(area, rng)["distance_from_center_meter"] / 140) ** 2
                              for _ in range(2000)]
        self.assertAlmostEqual(sum(normalized_squared) / len(normalized_squared),
                               (.01 ** 2 + .9 ** 2) / 2, delta=.025)

    def payload(self, **changes):
        return {"code": 0, "data": [{"latitude": 30.7, "longitude": 104.1,
                "radiusMeter": 150, "groupName": "测试宿舍", **changes}]}

    def test_preserve_gcj02_and_route_query(self):
        area = select_area(parse_areas(self.payload()))
        self.assertEqual(area["coordinate_system"], "GCJ02")
        result = area_url(URL + "?latitude=1&longitude=2&test=keep", area)
        parsed = urlsplit(result)
        self.assertEqual(parsed.query, "appId=294")
        query = parse_qs(parsed.fragment.split("?", 1)[1])
        self.assertEqual(query, {"latitude": ["30.7"], "longitude": ["104.1"], "test": ["keep"]})

    def test_multiple_and_empty_do_not_guess(self):
        areas = parse_areas(self.payload())
        with self.assertRaises(CheckinError):
            select_area([])
        with self.assertRaises(CheckinError):
            select_area(areas * 2)
        self.assertEqual(select_area(areas * 2, 1), areas[0])
        for index in (-1, 2):
            with self.assertRaises(CheckinError):
                select_area(areas * 2, index)

    def test_invalid_api_data(self):
        for payload in ({"code": 1, "data": []}, {"code": 0, "data": None},
                        self.payload(latitude=True), self.payload(longitude=181),
                        self.payload(radiusMeter=0), self.payload(radiusMeter=float("nan")),
                        {"code": 0, "data": [None]}):
            with self.subTest(payload=payload), self.assertRaises(CheckinError):
                parse_areas(payload)
