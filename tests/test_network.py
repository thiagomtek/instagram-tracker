import unittest
from unittest.mock import patch

import requests

from app import app
from database import db
from models.user import User
from services import network


class NetworkStatusTest(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False, PROFILE_VALIDATION_PROXY="http://vpn:8888")
        self.ctx = app.app_context()
        self.ctx.push()
        db.session.remove()
        db.drop_all()
        db.create_all()
        network._last_vpn_check["at"] = None
        network._last_direct_check["at"] = None

    def tearDown(self):
        app.config["PROFILE_VALIDATION_PROXY"] = ""
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def lookup(self, direct, vpn):
        def fake(proxy=None):
            outcome = vpn if proxy else direct
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return fake

    def test_routes_through_vpn_when_it_answers_with_another_ip(self):
        with patch("services.network._lookup", self.lookup(("200.1.1.1", None), ("45.9.9.9", "Frankfurt, DE"))):
            status = network.refresh_network_status(force=True)

        self.assertTrue(status.vpn_up)
        self.assertEqual(status.route, "vpn")
        self.assertEqual(status.vpn_location, "Frankfurt, DE")
        self.assertEqual(network.current_route(), ("vpn", "http://vpn:8888"))

    def test_falls_back_to_direct_when_the_vpn_is_down(self):
        with patch("services.network._lookup", self.lookup(("200.1.1.1", None), requests.ConnectionError())):
            status = network.refresh_network_status(force=True)

        self.assertFalse(status.vpn_up)
        self.assertEqual(status.route, "direct")
        self.assertEqual(network.current_route(), ("direct", None))
        self.assertIn("VPN", status.detail)

    def test_user_can_forbid_the_direct_fallback(self):
        with patch("services.network._lookup", self.lookup(("200.1.1.1", None), requests.ConnectionError())):
            network.refresh_network_status(force=True)
            network.set_direct_fallback(False)

        self.assertEqual(network.current_route(), ("paused", None))
        self.assertTrue(network.web_checks_blocked_by_route())

    def test_proxy_that_returns_the_direct_ip_is_not_a_tunnel(self):
        with patch("services.network._lookup", self.lookup(("200.1.1.1", None), ("200.1.1.1", None))):
            status = network.refresh_network_status(force=True)

        self.assertFalse(status.vpn_up)
        self.assertEqual(status.route, "direct")

    def test_without_proxy_everything_is_direct(self):
        app.config["PROFILE_VALIDATION_PROXY"] = ""
        with patch("services.network._lookup", self.lookup(("200.1.1.1", None), None)):
            status = network.refresh_network_status(force=True)

        self.assertFalse(status.vpn_configured)
        self.assertEqual(network.current_route(), ("direct", None))

    def test_interface_shows_status_and_saves_the_fallback_choice(self):
        user = User(email="net@example.com")
        user.set_password("a-secure-test-password")
        db.session.add(user)
        db.session.commit()
        with patch("services.network._lookup", self.lookup(("200.1.1.1", None), ("45.9.9.9", "Frankfurt, DE"))):
            network.refresh_network_status(force=True)
        client = app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = user.id
            session["session_epoch"] = user.session_epoch
            session["csrf_token"] = "test-token"

        page = client.get("/processamentos")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Saindo pela VPN", page.get_data(as_text=True))
        self.assertIn("45.9.9.9", page.get_data(as_text=True))

        client.post("/network/fallback", data={"csrf_token": "test-token"})
        self.assertFalse(network.get_status().allow_direct_fallback)


class LookupFallbackTest(unittest.TestCase):
    def test_rate_limited_ip_service_does_not_mean_the_vpn_is_down(self):
        limited = requests.Response()
        limited.status_code = 429
        plain = requests.Response()
        plain.status_code = 200
        plain._content = b"185.222.213.21\n"

        def fake_get(url, **_kwargs):
            return limited if "ipinfo" in url else plain

        with patch("services.network.requests.get", side_effect=fake_get):
            ip, location = network._lookup("http://vpn:8888")

        self.assertEqual(ip, "185.222.213.21")
        self.assertIsNone(location)

    def test_raises_only_when_every_service_fails(self):
        with patch("services.network.requests.get", side_effect=requests.ConnectionError()):
            with self.assertRaises(requests.RequestException):
                network._lookup("http://vpn:8888")
