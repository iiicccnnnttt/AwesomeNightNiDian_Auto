import json
import unittest
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from camoufox.sync_api import Camoufox
from camoufox.pkgman import launch_path

from auto_location.identities import profile_identity
from auto_location.checkin import CheckinError


class IdentityTests(unittest.TestCase):
    def test_distinct_profiles_and_restart_keep_browser_identity_and_cookies(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "first", root / "second"
            a = profile_identity(first, Path(launch_path()))
            b = profile_identity(second, Path(launch_path()))
            self.assertNotEqual(a["id"], b["id"])
            self.assertNotEqual(a["config"], b["config"])
            self.assertEqual(a, profile_identity(first, Path(launch_path())))

            def probe(profile, identity, add_cookie=False):
                with Camoufox(headless=True, persistent_context=True, user_data_dir=str(profile),
                              config=dict(identity["config"]), geoip=False,
                              i_know_what_im_doing=True) as context:
                    context.route("https://example.test/**", lambda route: route.fulfill(body="<html>fixture</html>", content_type="text/html"))
                    page = context.new_page()
                    page.goto("https://example.test/")
                    if add_cookie:
                        context.add_cookies([{"name": "fixture", "value": "profile-a", "url": "https://example.test/",
                                              "expires": int(time.time()) + 86400}])
                    values = page.evaluate("""() => ({ua: navigator.userAgent, platform: navigator.platform,
                        cores: navigator.hardwareConcurrency, width: screen.width, height: screen.height,
                        depth: screen.colorDepth})""")
                    return values, context.cookies("https://example.test")
            values_a, cookies_a = probe(first, a, True)
            values_b, cookies_b = probe(second, b)
            restarted, cookies_restart = probe(first, a)
            self.assertEqual(values_a, restarted)
            self.assertTrue(cookies_a)
            self.assertFalse(cookies_b)
            self.assertEqual(cookies_a, cookies_restart)
            self.assertEqual(values_a["ua"], a["config"]["navigator.userAgent"])
            # Different full generated configurations need not have different UA.
            self.assertEqual(values_b["ua"], b["config"]["navigator.userAgent"])
            self.assertNotIn("env", a)
            self.assertFalse(any(key.startswith("geolocation:") for key in a["config"]))

    def test_corrupt_identity_is_not_silently_replaced(self):
        with TemporaryDirectory() as directory:
            profile = Path(directory)
            path = profile / ".auto-location-identity.json"
            path.write_text(json.dumps({"version": 99}), encoding="utf-8")
            with self.assertRaises(CheckinError):
                profile_identity(profile, Path(launch_path()))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 99)
