"""Build-time Linux browser check. Does not contact the school."""
from camoufox.sync_api import Camoufox

with Camoufox(headless=True, geoip=False, os="linux") as browser:
    page = browser.new_page()
    page.goto("data:text/html,<title>container-smoke</title><h1>ready</h1>")
    assert page.title() == "container-smoke"
    assert page.locator("h1").inner_text() == "ready"
    print("Camoufox container smoke check passed")
