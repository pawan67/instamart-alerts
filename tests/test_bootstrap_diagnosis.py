"""Telling WAF failure modes apart.

"No aws-waf-token" is the same symptom for a CAPTCHA, an outright block, a slow
challenge and a dead network — but only one of those is worth changing browser
flags over. These are the pages each one actually returns.
"""

from __future__ import annotations

import pytest

from instamart_alerts.session import _diagnose

CAPTCHA_PAGE = """
<html><head><script src="https://de5282c3ca0c.captcha.awswaf.com/captcha.js"></script>
</head><body><div id="captcha-container"></div></body></html>
"""

CHALLENGE_PAGE = """
<html><head><script src="https://5b1a2c.token.awswaf.com/challenge.js"></script>
</head><body>Just a moment…</body></html>
"""

BLOCK_PAGE = "<html><body><h1>Access Denied</h1></body></html>"


def test_a_captcha_points_at_the_ip_not_the_browser():
    why = _diagnose(CAPTCHA_PAGE, {"deviceId": "x"})
    assert "CAPTCHA" in why
    assert "PROXY_URL" in why


def test_an_outright_block_points_at_the_ip():
    assert "blocked rather than challenged" in _diagnose(BLOCK_PAGE, {})


def test_an_unfinished_challenge_points_at_the_timeout():
    why = _diagnose(CHALLENGE_PAGE, {"deviceId": "x"})
    assert "never finished" in why
    assert "longer" in why


def test_no_cookies_at_all_points_at_the_network():
    assert "never reached" in _diagnose("<html></html>", {})


def test_an_unrecognised_page_still_names_the_cookies_it_did_get():
    why = _diagnose("<html>hello</html>", {"deviceId": "x", "_sessionid": "y"})
    assert "_sessionid" in why and "deviceId" in why


def test_a_captcha_wins_over_the_challenge_marker():
    """A CAPTCHA page also loads challenge.js; the CAPTCHA is the real news."""
    assert "CAPTCHA" in _diagnose(CAPTCHA_PAGE + CHALLENGE_PAGE, {})


@pytest.mark.parametrize("page", [CAPTCHA_PAGE.upper(), CAPTCHA_PAGE.lower()])
def test_detection_is_case_insensitive(page):
    assert "CAPTCHA" in _diagnose(page, {})
