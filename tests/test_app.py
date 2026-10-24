import unittest
from datetime import datetime, timedelta
from io import BytesIO
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from sqlalchemy.exc import OperationalError

import app as app_module
from app import app
from database import db
from models.integration import AnalysisRun, DriveConnection, DriveImport, PushSubscription, RelationshipValidation
from models.follower import Follower
from models.history import History
from models.system import ImportObservation, LoginThrottle, SchemaMigration
from models.user import User
from services.clock import utc_now
from services.presentation import format_validation_attempt_time
from services.schema import run_schema_upgrades


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

        response = self.client.post(
            "/integrations/google/sync",
            data={"csrf_token": "test-token"},
        )

        # The web request only flags the connection; the worker runs the sync.
        self.assertEqual(response.status_code, 302)
        self.assertTrue(DriveConnection.query.filter_by(user_id=self.user_id).first().sync_requested)

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

        self.assertIn("Situação atual".encode(), response.data)
        self.assertIn("As listas separam perfis presentes e ausentes na exportação atual".encode(), response.data)
        self.assertIn("Você segue, mas não seguem você".encode(), response.data)
        self.assertIn(b"bia", response.data)

    def test_dashboard_renders_with_validation_progress(self):
        follower = Follower(user_id=self.user_id, username="pedro", kind="followers", is_present=False, state="pending", missing_count=1)
        db.session.add(follower)
        db.session.commit()
        val = RelationshipValidation(
            user_id=self.user_id,
            follower_id=follower.id,
            username="pedro",
            kind="followers",
            status="queued",
            next_attempt_at=datetime.now() + timedelta(minutes=10),
        )
        db.session.add(val)
        db.session.commit()
        self.login()

        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Validação assíncrona".encode(), response.data)
        self.assertIn(b"@pedro", response.data)

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

    def test_every_dashboard_list_is_ordered_from_newest_to_oldest(self):
        now = datetime.now()
        db.session.add_all(
            [
                Follower(user_id=self.user_id, username="fan_antigo", kind="followers", date_added=now - timedelta(days=40)),
                Follower(user_id=self.user_id, username="fan_recente", kind="followers", date_added=now - timedelta(days=2)),
                Follower(user_id=self.user_id, username="mutuo_antigo", kind="followers", date_added=now - timedelta(days=30)),
                Follower(user_id=self.user_id, username="mutuo_antigo", kind="following", date_added=now - timedelta(days=35)),
                Follower(user_id=self.user_id, username="mutuo_recente", kind="followers", date_added=now - timedelta(days=1)),
                Follower(user_id=self.user_id, username="mutuo_recente", kind="following", date_added=now - timedelta(days=5)),
                History(user_id=self.user_id, username="atividade_antiga", action="gained", kind="followers", date_added=now - timedelta(days=3)),
                History(user_id=self.user_id, username="atividade_recente", action="lost", kind="followers", date_added=now - timedelta(hours=1)),
            ]
        )
        db.session.commit()
        self.login()

        page = self.client.get("/dashboard").data.decode()

        self.assertLess(page.index("fan_recente"), page.index("fan_antigo"))
        self.assertLess(page.index("mutuo_recente"), page.index("mutuo_antigo"))
        self.assertLess(page.index("atividade_recente"), page.index("atividade_antiga"))
        self.assertIn("Do relacionamento mais recente para o mais antigo.", page)
        self.assertIn("Do seguidor mais recente para o mais antigo.", page)

    def test_processing_history_is_ordered_from_newest_to_oldest(self):
        now = datetime.now()
        db.session.add_all(
            [
                AnalysisRun(
                    user_id=self.user_id,
                    source="manual",
                    source_name="processamento-antigo.zip",
                    status="success",
                    started_at=now - timedelta(days=2),
                ),
                AnalysisRun(
                    user_id=self.user_id,
                    source="manual",
                    source_name="processamento-recente.zip",
                    status="success",
                    started_at=now - timedelta(minutes=10),
                ),
            ]
        )
        db.session.commit()
        self.login()

        page = self.client.get("/processamentos").data.decode()

        self.assertLess(page.index("processamento-recente.zip"), page.index("processamento-antigo.zip"))

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

    def test_validation_status_api_and_dashboard_render_with_scheduled_retest(self):
        f = Follower(user_id=self.user_id, username="sumido_teste", kind="followers", state="pending", is_present=False)
        db.session.add(f)
        db.session.commit()
        val = RelationshipValidation(
            user_id=self.user_id,
            follower_id=f.id,
            username="sumido_teste",
            kind="followers",
            status="retry",
            outcome="unavailable_pending_retest",
            attempts=1,
            next_attempt_at=datetime.utcnow() + timedelta(minutes=30),
        )
        db.session.add(val)
        db.session.commit()

        self.login()
        # API status test
        res = self.client.get("/api/validations/status")
        self.assertEqual(res.status_code, 200)
        data = res.json
        self.assertTrue(data["active"])
        self.assertEqual(data["queued"], 1)
        self.assertIsNotNone(data["next_attempt_formatted"])
        self.assertEqual(data["items"][0]["status_label"], "Reaveriguação agendada")
        self.assertIn("Reteste 1/1 previsto", data["items"][0]["sub_label"])

        # Dashboard rendering test
        dash_res = self.client.get("/dashboard")
        self.assertEqual(dash_res.status_code, 200)
        self.assertIn(b"Pr\xc3\xb3xima averigua\xc3\xa7\xc3\xa3o prevista", dash_res.data)
        self.assertIn(b"@sumido_teste", dash_res.data)
        self.assertIn(b"Reaverigua\xc3\xa7\xc3\xa3o agendada", dash_res.data)

    def test_dashboard_renders_unified_absent_both_card(self):
        # absent in followers
        db.session.add(Follower(
            user_id=self.user_id, username="sumido_ambos", kind="followers", state="pending", is_present=False, missing_since=datetime.utcnow()
        ))
        # absent in following
        db.session.add(Follower(
            user_id=self.user_id, username="sumido_ambos", kind="following", state="pending", is_present=False, missing_since=datetime.utcnow()
        ))
        # absent only in followers
        db.session.add(Follower(
            user_id=self.user_id, username="apenas_seguidor", kind="followers", state="pending", is_present=False, missing_since=datetime.utcnow()
        ))
        db.session.commit()

        self.login()
        res = self.client.get("/dashboard")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Ausentes em ambas as listas", res.data)
        self.assertIn(b"sumido_ambos", res.data)
        self.assertIn(b"apenas_seguidor", res.data)

    # ----- data safety regressions

    def test_schema_upgrade_never_rewrites_confirmed_removals_on_restart(self):
        db.session.add(History(user_id=self.user_id, username="ex", action="lost", kind="followers"))
        db.session.commit()

        run_schema_upgrades()
        run_schema_upgrades()

        self.assertEqual(History.query.filter_by(action="lost").count(), 1)
        self.assertEqual(History.query.filter_by(action="missing").count(), 0)

    def test_legacy_lost_rows_are_converted_exactly_once(self):
        # Simulate a database from before profile validation existed.
        db.session.execute(db.text("DROP TABLE schema_migration"))
        db.session.execute(db.text("DROP TABLE relationship_validation"))
        db.session.execute(db.text("DROP TABLE follower"))
        db.session.execute(db.text(
            "CREATE TABLE follower (id INTEGER PRIMARY KEY, user_id INTEGER, username VARCHAR NOT NULL, "
            "kind VARCHAR NOT NULL, date_added DATETIME)"
        ))
        db.session.commit()
        db.create_all()
        db.session.add(History(user_id=self.user_id, username="antigo", action="lost", kind="followers"))
        db.session.commit()

        run_schema_upgrades()
        self.assertEqual(History.query.filter_by(username="antigo").first().action, "missing")

        db.session.add(History(user_id=self.user_id, username="novo", action="lost", kind="followers"))
        db.session.commit()
        run_schema_upgrades()
        self.assertEqual(History.query.filter_by(username="novo").first().action, "lost")
        self.assertIsNotNone(db.session.get(SchemaMigration, "legacy_lost_to_missing"))

    def test_schema_upgrade_removes_duplicate_followers_before_indexing(self):
        db.session.execute(db.text("DROP INDEX IF EXISTS uq_follower_user_kind_username"))
        db.session.execute(db.text("DELETE FROM schema_migration"))
        db.session.commit()
        # The table-level constraint of the model forbids duplicates; rebuild without it.
        db.session.execute(db.text("DROP TABLE relationship_validation"))
        db.session.execute(db.text("DROP TABLE follower"))
        db.session.execute(db.text(
            "CREATE TABLE follower (id INTEGER PRIMARY KEY, user_id INTEGER, username VARCHAR NOT NULL, "
            "kind VARCHAR NOT NULL, date_added DATETIME, is_present BOOLEAN NOT NULL DEFAULT 1, missing_since DATETIME, "
            "state VARCHAR(32) NOT NULL DEFAULT 'present', missing_count INTEGER NOT NULL DEFAULT 0, "
            "validation_status VARCHAR(32) NOT NULL DEFAULT 'idle', last_validation_result VARCHAR(64), "
            "last_validated_at DATETIME, retest_count INTEGER NOT NULL DEFAULT 0)"
        ))
        db.session.execute(db.text(
            "INSERT INTO follower (user_id, username, kind) VALUES (1, 'ana', 'followers'), (1, 'ana', 'followers')"
        ))
        db.session.commit()
        db.create_all()

        run_schema_upgrades()

        self.assertEqual(Follower.query.filter_by(username="ana").count(), 1)

    def test_database_initialization_survives_a_concurrent_start(self):
        race = OperationalError("CREATE TABLE schema_migration", {}, Exception("table schema_migration already exists"))
        with patch.object(app_module, "run_schema_upgrades", side_effect=[race, race, None]) as upgrades,                 patch.object(app_module.time, "sleep"):
            app_module.initialize_database()
        self.assertEqual(upgrades.call_count, 3)

    def test_database_initialization_gives_up_after_the_last_attempt(self):
        race = OperationalError("CREATE TABLE x", {}, Exception("locked"))
        with patch.object(app_module, "run_schema_upgrades", side_effect=race), patch.object(app_module.time, "sleep"):
            with self.assertRaises(OperationalError):
                app_module.initialize_database(attempts=2)

    def test_reset_erases_everything_derived_from_imports_even_with_validations(self):
        follower = Follower(user_id=self.user_id, username="p", kind="followers", is_present=False, state="pending")
        run = AnalysisRun(user_id=self.user_id, source="manual", source_name="x")
        db.session.add_all([follower, run, History(user_id=self.user_id, username="p", action="missing", kind="followers"),
                            ImportObservation(user_id=self.user_id, kind="followers", fingerprint="abc")])
        db.session.commit()
        db.session.add(RelationshipValidation(
            user_id=self.user_id, follower_id=follower.id, analysis_run_id=run.id, username="p",
            kind="followers", next_attempt_at=utc_now(),
        ))
        connection = DriveConnection(user_id=self.user_id, encrypted_refresh_token="x")
        db.session.add(connection)
        db.session.commit()
        db.session.add(DriveImport(connection_id=connection.id, source_id="s", source_version="v", source_name="n", analysis_run_id=run.id))
        db.session.commit()
        self.login()

        response = self.client.post("/reset", data={"csrf_token": "test-token"}, follow_redirects=True)

        self.assertIn("Histórico e listas apagados".encode(), response.data)
        for model in (Follower, History, AnalysisRun, RelationshipValidation, ImportObservation):
            self.assertEqual(model.query.count(), 0, model.__name__)
        # The Drive link and its import ledger are kept.
        self.assertEqual(DriveConnection.query.count(), 1)
        self.assertIsNone(DriveImport.query.first().analysis_run_id)

    # ----- time zones

    def test_validation_eta_is_shown_in_local_time(self):
        target = datetime(2030, 6, 1, 18, 30)  # naive UTC
        expected = target.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("America/Sao_Paulo"))
        self.assertEqual(format_validation_attempt_time(target), expected.strftime("%d/%m %H:%M"))
        self.assertEqual(expected.strftime("%H:%M"), "15:30")

    # ----- accounts and security

    def post(self, path, **data):
        return self.client.post(path, data={"csrf_token": "test-token", **data})

    def test_login_is_blocked_after_repeated_failures(self):
        self.login()
        with self.client.session_transaction() as session:
            session.pop("user_id")
        app.config["LOGIN_MAX_FAILURES"] = 3
        try:
            for _ in range(3):
                response = self.post("/login", email="app@example.com", password="wrong-password")
                self.assertEqual(response.status_code, 200)
            blocked = self.post("/login", email="app@example.com", password="a-secure-test-password")
        finally:
            app.config["LOGIN_MAX_FAILURES"] = 5

        self.assertEqual(blocked.status_code, 429)
        self.assertIn("Muitas tentativas".encode(), blocked.data)
        self.assertEqual(LoginThrottle.query.count(), 1)

    def test_successful_login_clears_failures(self):
        self.login()
        with self.client.session_transaction() as session:
            session.pop("user_id")
        self.post("/login", email="app@example.com", password="wrong-password")
        self.assertEqual(LoginThrottle.query.count(), 1)

        response = self.post("/login", email="app@example.com", password="a-secure-test-password")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(LoginThrottle.query.count(), 0)

    def test_registration_is_closed_by_default_once_an_account_exists(self):
        app.config["ALLOW_REGISTRATION"] = False
        response = self.client.get("/register")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/login"))

        db.session.delete(db.session.get(User, self.user_id))
        db.session.commit()
        self.assertEqual(self.client.get("/register").status_code, 200)

    def test_changing_password_signs_out_other_devices(self):
        self.login()
        other_device = app.test_client()
        with other_device.session_transaction() as session:
            session["user_id"] = self.user_id
            session["epoch"] = 0
        self.assertEqual(other_device.get("/").status_code, 200)

        response = self.post(
            "/account/password",
            current_password="a-secure-test-password",
            new_password="a-brand-new-password",
            new_password_confirmation="a-brand-new-password",
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(other_device.get("/").status_code, 302)
        self.assertTrue(db.session.get(User, self.user_id).check_password("a-brand-new-password"))

    def test_changing_password_requires_current_password(self):
        self.login()
        self.post(
            "/account/password",
            current_password="not-the-password",
            new_password="a-brand-new-password",
            new_password_confirmation="a-brand-new-password",
        )
        self.assertTrue(db.session.get(User, self.user_id).check_password("a-secure-test-password"))

    def test_deleting_account_removes_user_and_data(self):
        db.session.add_all([
            Follower(user_id=self.user_id, username="ana", kind="followers"),
            History(user_id=self.user_id, username="ana", action="gained", kind="followers"),
            PushSubscription(user_id=self.user_id, endpoint="https://push.example/1", p256dh="k", auth="a"),
        ])
        db.session.commit()
        self.login()

        refused = self.post("/account/delete", password="a-secure-test-password", confirmation="talvez")
        self.assertEqual(User.query.count(), 1)
        self.assertEqual(refused.status_code, 302)

        self.post("/account/delete", password="a-secure-test-password", confirmation="excluir")

        self.assertEqual(User.query.count(), 0)
        for model in (Follower, History, PushSubscription):
            self.assertEqual(model.query.count(), 0, model.__name__)

    def test_security_headers_and_no_store_on_private_pages(self):
        self.login()
        response = self.client.get("/dashboard")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_push_subscription_must_use_https(self):
        self.login()
        response = self.client.post(
            "/api/push/subscribe",
            json={"endpoint": "http://insecure.example/x", "keys": {"p256dh": "k", "auth": "a"}},
            headers={"X-CSRF-Token": "test-token"},
        )
        self.assertEqual(response.status_code, 400)

    def test_health_check_and_local_plotly_bundle(self):
        self.assertEqual(self.client.get("/healthz").json, {"status": "ok"})
        bundle = self.client.get("/vendor/plotly.min.js")
        self.assertEqual(bundle.status_code, 200)
        self.assertIn("immutable", bundle.headers["Cache-Control"])

    def test_pages_do_not_depend_on_font_awesome_or_the_plotly_cdn(self):
        self.login()
        for path in ("/", "/dashboard", "/processamentos", "/account"):
            page = self.client.get(path).data.decode()
            self.assertNotIn("fa-solid", page, path)
            self.assertNotIn("cdn.plot.ly", page, path)

    # ----- dashboard behavior

    def test_unavailable_profiles_stay_visible_after_failed_retest(self):
        db.session.add(Follower(
            user_id=self.user_id, username="conta_sumida", kind="followers", is_present=False,
            state="untestable", missing_since=datetime.now(),
        ))
        db.session.commit()
        self.login()

        page = self.client.get("/dashboard").data.decode()

        self.assertIn("Perfis indisponíveis", page)
        self.assertIn("conta_sumida", page)

    def test_clearing_the_start_date_removes_the_period_limit(self):
        db.session.add(History(
            user_id=self.user_id, username="muito_antigo", action="gained", kind="followers",
            date_added=datetime.now() - timedelta(days=400),
        ))
        db.session.commit()
        self.login()

        default_page = self.client.get("/dashboard").data.decode()
        all_time_page = self.client.get("/dashboard?start_date=&end_date=").data.decode()

        self.assertNotIn("muito_antigo", default_page)
        self.assertIn("muito_antigo", all_time_page)

    def test_activity_list_is_capped_but_counts_are_complete(self):
        now = datetime.now()
        db.session.add_all([
            History(user_id=self.user_id, username=f"perfil{index}", action="gained", kind="followers", date_added=now)
            for index in range(7)
        ])
        db.session.commit()
        self.login()

        with patch.object(app_module, "ACTIVITY_LIST_LIMIT", 3):
            page = self.client.get("/dashboard").data.decode()

        self.assertIn("Mostrando os 3 mais recentes de 7", page)
        self.assertIn("+7", page)

    def test_username_filter_treats_wildcards_literally(self):
        now = datetime.now()
        db.session.add_all([
            History(user_id=self.user_id, username="a_b", action="gained", kind="followers", date_added=now),
            History(user_id=self.user_id, username="axb", action="gained", kind="followers", date_added=now),
        ])
        db.session.commit()
        self.login()

        page = self.client.get("/dashboard?username=a_b").data.decode()

        self.assertIn(">a_b<", page)
        self.assertNotIn(">axb<", page)

    def test_processing_history_reports_validation_progress_from_run_counters(self):
        db.session.add(AnalysisRun(
            user_id=self.user_id, source="manual", source_name="x.zip", status="success",
            validation_total=4, validation_completed=4,
        ))
        db.session.commit()
        self.login()

        page = self.client.get("/processamentos").data.decode()

        self.assertIn("4 de 4 concluídas · 100%", page)
        self.assertIn("Validação finalizada", page)

    # ----- upload flow

    def upload(self, *files):
        return self.client.post(
            "/upload",
            data={"csrf_token": "test-token", "files": [(BytesIO(content.encode()), name) for name, content in files]},
            content_type="multipart/form-data",
        )

    @staticmethod
    def html(usernames):
        return "<html><body>" + "".join(
            f'<div class="_a6-p"><div><a href="https://www.instagram.com/{name}/">{name}</a></div>'
            '<div>jan 01, 2026 1:00 da tarde</div></div>' for name in usernames
        ) + "</body></html>"

    def test_upload_guards_ask_for_confirmation_and_ignore_duplicates(self):
        self.login()
        names = [f"p{index}" for index in range(40)]
        self.assertEqual(self.upload(("followers_1.html", self.html(names))).status_code, 302)

        # Half the list: refused, and the form is told to show the confirmation box.
        refused = self.upload(("followers_1.html", self.html(names[:20])))
        self.assertIn("confirm_drop=1", refused.location)
        self.assertEqual(Follower.query.filter_by(is_present=True).count(), 40)
        self.assertIn("confirm_large_drop".encode(), self.client.get("/?confirm_drop=1").data)
        self.assertNotIn("confirm_large_drop".encode(), self.client.get("/").data)
        self.assertEqual(AnalysisRun.query.filter_by(status="failed").count(), 1)

        # Same refused file with the confirmation goes through...
        confirmed = self.client.post(
            "/upload",
            data={"csrf_token": "test-token", "confirm_large_drop": "1",
                  "files": [(BytesIO(self.html(names[:20]).encode()), "followers_1.html")]},
            content_type="multipart/form-data",
        )
        self.assertTrue(confirmed.location.endswith("/dashboard"))
        self.assertEqual(Follower.query.filter_by(is_present=True).count(), 20)

        # ...and sending it again is recognised as the same export.
        again = self.upload(("followers_1.html", self.html(names[:20])))
        page = self.client.get(again.location).data.decode()
        self.assertIn("idêntica à última importada", page)
        self.assertEqual(Follower.query.filter_by(kind="followers", username="p25").first().missing_count, 1)

    def test_upload_error_does_not_leak_internal_details(self):
        self.login()
        with patch("app.process_import_paths", side_effect=OSError("/srv/secret/path failed")):
            response = self.upload(("followers_1.html", self.html(["a"])))
        page = self.client.get(response.location).data.decode()
        self.assertNotIn("/srv/secret/path", page)

    def test_oauth_error_does_not_leak_internal_details(self):
        self.login()
        with self.client.session_transaction() as session:
            session["google_oauth_state"] = "state"
        with patch("app.Flow.from_client_config", side_effect=RuntimeError("client_secret=abc123")):
            response = self.client.get("/integrations/google/callback?state=state", follow_redirects=True)
        self.assertNotIn(b"abc123", response.data)


if __name__ == "__main__":
    unittest.main()
