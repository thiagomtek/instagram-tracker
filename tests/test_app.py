import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="instagram-tracker-tests-"))
os.environ.setdefault("SECRET_KEY", "tests-only-secret-key")
os.environ.setdefault("SESSION_COOKIE_SECURE", "false")

from app import app
from database import db
from models.integration import AnalysisRun, DriveConnection
from models.follower import Follower
from models.user import User


class AppTest(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        self.context = app.app_context()
        self.context.push()
        db.session.remove()
        db.drop_all()
        db.create_all()
        user = User(email="app@example.com")
        user.set_password("a-secure-test-password")
        db.session.add(user)
        db.session.commit()
        self.user_id = user.id
        self.client = app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def login(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
            session["csrf_token"] = "test-token"
            session.permanent = True

    def test_manifest_has_unique_identity_and_scope(self):
        response = self.client.get("/manifest.webmanifest")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["id"], "/instagram-tracker-pwa")
        self.assertEqual(response.json["scope"], "/")

    def test_google_drive_connect_keeps_pkce_without_incremental_scopes(self):
        self.login()
        app.config.update(
            GOOGLE_CLIENT_ID="client-id",
            GOOGLE_CLIENT_SECRET="client-secret",
            GOOGLE_REDIRECT_URI="https://example.test/integrations/google/callback",
        )
        flow = Mock()
        flow.code_verifier = "pkce-verifier"
        flow.authorization_url.return_value = ("https://accounts.google.com/o/oauth2/auth", "oauth-state")

        with patch("app.Flow.from_client_config", return_value=flow):
            response = self.client.get("/integrations/google/connect")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "https://accounts.google.com/o/oauth2/auth")
        flow.authorization_url.assert_called_once_with(access_type="offline", prompt="consent")
        with self.client.session_transaction() as session:
            self.assertEqual(session["google_oauth_state"], "oauth-state")
            self.assertEqual(session["google_oauth_code_verifier"], "pkce-verifier")

    def test_authenticated_home_is_mobile_ready(self):
        self.login()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'manifest.webmanifest', response.data)
        self.assertIn(b'mobile-tab-bar', response.data)

    def test_drive_panel_has_fixed_hourly_schedule_without_folder_picker(self):
        app.config.update(GOOGLE_CLIENT_ID="client-id", GOOGLE_CLIENT_SECRET="client-secret")
        db.session.add(
            DriveConnection(
                user_id=self.user_id,
                google_email="review@example.test",
                encrypted_refresh_token="encrypted-placeholder",
                folder_id="old-custom-folder",
                folder_name="Old custom folder",
            )
        )
        db.session.commit()
        self.login()

        response = self.client.get("/")

        self.assertEqual(app.config["GOOGLE_DRIVE_POLL_SECONDS"], 3600)
        self.assertIn("Automação a cada 1 hora".encode(), response.data)
        self.assertIn("automaticamente novas exportações".encode(), response.data)
        self.assertNotIn("Local das exportações do Instagram".encode(), response.data)
        self.assertNotIn("Pasta atual".encode(), response.data)

    def test_manual_drive_check_does_not_require_folder_configuration(self):
        db.session.add(
            DriveConnection(
                user_id=self.user_id,
                encrypted_refresh_token="encrypted-placeholder",
                folder_id=None,
                folder_name=None,
            )
        )
        db.session.commit()
        self.login()

        with patch("app.sync_connection_safely", return_value=[]) as sync:
            response = self.client.post(
                "/integrations/google/sync",
                data={"csrf_token": "test-token"},
            )

        self.assertEqual(response.status_code, 302)
        sync.assert_called_once()

    def test_dashboard_explains_current_snapshot_and_clear_relationships(self):
        db.session.add_all(
            [
                Follower(user_id=self.user_id, username="ana", kind="followers"),
                Follower(user_id=self.user_id, username="ana", kind="following"),
                Follower(user_id=self.user_id, username="bia", kind="following"),
            ]
        )
        db.session.commit()
        self.login()

        response = self.client.get("/dashboard")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Situação atual".encode(), response.data)
        self.assertIn("estado atual, não todo o seu histórico".encode(), response.data)
        self.assertIn("Você segue, mas não seguem você".encode(), response.data)
        self.assertIn(b"bia", response.data)

    def test_not_following_back_is_ordered_by_most_recent_follow_date(self):
        now = datetime.now()
        db.session.add_all(
            [
                Follower(user_id=self.user_id, username="mais_antigo", kind="following", date_added=now - timedelta(days=30)),
                Follower(user_id=self.user_id, username="mais_recente", kind="following", date_added=now - timedelta(days=1)),
                Follower(user_id=self.user_id, username="intermediario", kind="following", date_added=now - timedelta(days=10)),
            ]
        )
        db.session.commit()
        self.login()

        response = self.client.get("/dashboard")
        page = response.data.decode()

        self.assertLess(page.index("mais_recente"), page.index("intermediario"))
        self.assertLess(page.index("intermediario"), page.index("mais_antigo"))
        self.assertIn("Do seguido mais recentemente para o mais antigo.", page)

    def test_json_api_requires_csrf_header(self):
        self.login()
        response = self.client.post("/api/push/subscribe", json={})
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            "/api/push/subscribe",
            json={},
            headers={"X-CSRF-Token": "test-token"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["error"], "Assinatura inválida.")

    def test_processing_history_shows_follower_changes(self):
        db.session.add(
            AnalysisRun(
                user_id=self.user_id,
                source="google_drive",
                source_name="meta-2026-Oct-23-10-28-47",
                status="success",
                followers_found=120,
                followers_gained=3,
                followers_lost=1,
                gained_usernames='["ana", "bia", "caio"]',
                lost_usernames='["duda"]',
            )
        )
        db.session.commit()
        self.login()
        response = self.client.get("/processamentos")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"meta-2026-Oct-23-10-28-47", response.data)
        self.assertIn(b"+3", response.data)
        self.assertIn(b"-1", response.data)


if __name__ == "__main__":
    unittest.main()
