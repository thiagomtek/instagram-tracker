import unittest
from datetime import timedelta
from unittest.mock import patch

from app import app
from database import db
from models.follower import Follower
from models.history import History
from models.integration import AnalysisRun, RelationshipValidation
from models.user import User
from services import network
from services.clock import local_now, utc_now
from services.profile_validation import (
    BROWSER_IDENTITIES,
    ProfileCheck,
    _identity_state,
    inspect_instagram_profile,
    web_checks_paused_until,
    cancel_relationship_validations,
    enqueue_profile_scan,
    enqueue_existing_profile_scans,
    enqueue_relationship_validation,
    process_next_validation,
    recover_stale_validations,
    resolve_existing_profile,
)


class ProfileValidationTest(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config["TESTING"] = True
        self.app.config["PROFILE_VALIDATION_MAX_ATTEMPTS"] = 2
        self.app.config.update(PROFILE_VALIDATION_MIN_INTERVAL_SECONDS=0, PROFILE_VALIDATION_MAX_INTERVAL_SECONDS=0)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        user = User(email="test@example.com")
        user.set_password("secret123")
        db.session.add(user)
        db.session.commit()
        self.user_id = user.id

    def tearDown(self):
        _identity_state.update(index=0, blocked_until=None, consecutive_blocks=0)
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_enqueue_creates_pending_validation_and_deduplicates(self):
        follower = Follower(
            user_id=self.user_id,
            username="maria",
            kind="followers",
            is_present=False,
            state="pending",
            missing_count=1,
        )
        db.session.add(follower)
        db.session.commit()

        val1, created1 = enqueue_relationship_validation(follower)
        self.assertTrue(created1)
        self.assertEqual(val1.status, "queued")
        self.assertEqual(follower.validation_status, "queued")

        val2, created2 = enqueue_relationship_validation(follower)
        self.assertFalse(created2)
        self.assertEqual(val1.id, val2.id)

    @patch("services.profile_validation.send_profile_validation_notification")
    @patch("services.profile_validation.inspect_instagram_profile")
    def test_profile_scan_updates_all_matching_rows_and_avatar_without_changing_relationship(self, mock_inspect, _mock_notify):
        mock_inspect.return_value = ProfileCheck("exists", "Perfil ativo", b"avatar", "image/jpeg")
        rows = [
            Follower(user_id=self.user_id, username="ana", kind="followers", is_present=True, state="present"),
            Follower(user_id=self.user_id, username="ana", kind="following", is_present=True, state="present"),
        ]
        db.session.add_all(rows)
        db.session.commit()
        validation, _ = enqueue_profile_scan(rows[0])
        validation.next_attempt_at = utc_now() - timedelta(seconds=1)
        db.session.commit()

        self.assertTrue(process_next_validation())

        # A profile that exists is settled by a single request.
        db.session.refresh(validation)
        self.assertEqual(validation.status, "completed")
        self.assertEqual(validation.attempts, 1)
        for row in rows:
            db.session.refresh(row)
            self.assertTrue(row.is_present)
            self.assertEqual(row.state, "present")
            self.assertEqual(row.profile_status, "exists")
            self.assertEqual(row.avatar_data, b"avatar")
        self.assertEqual(validation.outcome, "profile_exists")

    @patch("services.profile_validation.send_profile_validation_notification")
    @patch("services.profile_validation.inspect_instagram_profile")
    def test_profile_scan_unavailable_never_marks_present_relationship_absent(self, mock_inspect, _mock_notify):
        mock_inspect.return_value = ProfileCheck("unavailable", "Perfil indisponível")
        follower = Follower(
            user_id=self.user_id, username="ana", kind="followers", is_present=True, state="present"
        )
        db.session.add(follower)
        db.session.commit()
        validation, _ = enqueue_profile_scan(follower)
        validation.next_attempt_at = utc_now() - timedelta(seconds=1)
        db.session.commit()

        self.assertTrue(process_next_validation())
        db.session.refresh(validation)
        self.assertEqual(validation.status, "retry")
        validation.next_attempt_at = utc_now() - timedelta(seconds=1)
        db.session.commit()
        self.assertTrue(process_next_validation())
        db.session.refresh(follower)

        self.assertTrue(follower.is_present)
        self.assertEqual(follower.state, "present")
        self.assertEqual(follower.profile_status, "unavailable")
        self.assertEqual(validation.outcome, "profile_unavailable")

    @patch("services.profile_validation._render_instagram_profile")
    def test_blocked_request_is_repeated_with_another_browser_identity(self, mock_render):
        mock_render.side_effect = [
            ProfileCheck("blocked", "bloqueado"),
            ProfileCheck("exists", "ok"),
        ]
        start = _identity_state["index"]

        check = inspect_instagram_profile("ana")

        self.assertEqual(check.outcome, "exists")
        identities = [call.args[1] for call in mock_render.call_args_list]
        self.assertNotEqual(identities[0]["user_agent"], identities[1]["user_agent"])
        self.assertEqual(identities[0], BROWSER_IDENTITIES[start])
        self.assertNotEqual(_identity_state["index"], start)

    @patch("services.profile_validation.send_profile_validation_notification")
    @patch("services.profile_validation.inspect_instagram_profile")
    def test_block_pauses_web_checks_without_spending_attempts_or_concluding(self, mock_inspect, _mock_notify):
        mock_inspect.return_value = ProfileCheck("blocked", "bloqueado")
        follower = Follower(
            user_id=self.user_id, username="ana", kind="followers", is_present=False,
            state="pending", missing_count=1,
        )
        db.session.add(follower)
        db.session.commit()
        validation, _ = enqueue_relationship_validation(follower)
        validation.next_attempt_at = utc_now() - timedelta(seconds=1)
        db.session.commit()

        self.assertTrue(process_next_validation())

        db.session.refresh(validation)
        db.session.refresh(follower)
        self.assertEqual(validation.status, "retry")
        self.assertEqual(validation.outcome, "blocked")
        self.assertEqual(validation.attempts, 0)
        self.assertGreater(validation.next_attempt_at, utc_now())
        self.assertEqual(follower.state, "pending")
        self.assertEqual(follower.validation_status, "queued")
        self.assertIsNotNone(web_checks_paused_until())

        # While paused, no further web check is attempted.
        other = Follower(
            user_id=self.user_id, username="bia", kind="followers", is_present=False,
            state="pending", missing_count=1,
        )
        db.session.add(other)
        db.session.commit()
        queued, _ = enqueue_relationship_validation(other)
        queued.next_attempt_at = utc_now() - timedelta(seconds=1)
        db.session.commit()
        mock_inspect.reset_mock()
        self.assertEqual(process_next_validation(), 0)
        mock_inspect.assert_not_called()

    @patch("services.profile_validation.send_profile_validation_notification")
    @patch("services.profile_validation.inspect_instagram_profile")
    def test_removal_checks_and_second_passes_go_before_new_scans(self, mock_inspect, _mock_notify):
        mock_inspect.return_value = ProfileCheck("exists", "ok")
        long_ago = utc_now() - timedelta(hours=2)
        rows = [
            Follower(user_id=self.user_id, username=name, kind="followers", is_present=True, state="present")
            for name in ("novo", "iniciado")
        ]
        gone = Follower(
            user_id=self.user_id, username="sumiu", kind="followers", is_present=False,
            state="pending", missing_count=1,
        )
        db.session.add_all(rows + [gone])
        db.session.commit()
        new_scan, _ = enqueue_profile_scan(rows[0])
        started_scan, _ = enqueue_profile_scan(rows[1])
        removal, _ = enqueue_relationship_validation(gone)
        new_scan.next_attempt_at = long_ago
        started_scan.status = "retry"
        started_scan.attempts = 1
        started_scan.next_attempt_at = utc_now() - timedelta(minutes=1)
        removal.next_attempt_at = utc_now() - timedelta(minutes=1)
        db.session.commit()

        order = []
        for _ in range(3):
            mock_inspect.reset_mock()
            process_next_validation()
            order.append(mock_inspect.call_args.args[0])

        self.assertEqual(order, ["sumiu", "iniciado", "novo"])

    @patch("services.profile_validation.inspect_instagram_profile")
    def test_scans_first_pass_confirmed_by_older_versions_are_settled_without_a_request(self, mock_inspect):
        follower = Follower(user_id=self.user_id, username="ana", kind="followers", is_present=True, state="present")
        db.session.add(follower)
        db.session.commit()
        validation, _ = enqueue_profile_scan(follower)
        validation.status = "retry"
        validation.outcome = "profile_exists_first_pass"
        validation.attempts = 1
        follower.profile_status = "checking"
        db.session.commit()

        process_next_validation()

        db.session.refresh(validation)
        db.session.refresh(follower)
        self.assertEqual(validation.status, "completed")
        self.assertEqual(follower.profile_status, "exists")
        mock_inspect.assert_not_called()

    @patch("services.profile_validation.inspect_instagram_profile")
    def test_web_checks_are_spaced_but_removal_proofs_from_the_other_list_are_not(self, mock_inspect):
        from services import profile_validation

        mock_inspect.return_value = ProfileCheck("exists", "ok")
        rows = [Follower(user_id=self.user_id, username=n, kind="followers", is_present=True, state="present")
                for n in ("ana", "bia")]
        db.session.add_all(rows)
        db.session.commit()
        for row in rows:
            scan, _ = enqueue_profile_scan(row)
            scan.next_attempt_at = utc_now() - timedelta(seconds=1)
        db.session.commit()
        self.app.config.update(PROFILE_VALIDATION_MIN_INTERVAL_SECONDS=60, PROFILE_VALIDATION_MAX_INTERVAL_SECONDS=60)

        self.assertTrue(process_next_validation())
        self.assertEqual(process_next_validation(), 0)
        self.assertEqual(mock_inspect.call_count, 1)
        profile_validation._pacing["next_at"] = 0.0

    def test_pause_in_progress_survives_a_worker_restart(self):
        from services.profile_validation import restore_block_state

        network.save_block_state(utc_now() + timedelta(minutes=20), 3)
        restore_block_state()

        self.assertIsNotNone(web_checks_paused_until())
        self.assertEqual(_identity_state["consecutive_blocks"], 3)

    def test_existing_imported_profiles_are_bootstrapped_once(self):
        rows = [
            Follower(user_id=self.user_id, username="ana", kind="followers"),
            Follower(user_id=self.user_id, username="ana", kind="following"),
            Follower(user_id=self.user_id, username="bia", kind="followers"),
        ]
        db.session.add_all(rows)
        db.session.commit()

        created = enqueue_existing_profile_scans()
        created_again = enqueue_existing_profile_scans()

        self.assertEqual(created, 2)
        self.assertEqual(created_again, 0)
        run = AnalysisRun.query.filter_by(source="profile_scan").one()
        self.assertEqual(run.validation_total, 2)
        self.assertEqual(RelationshipValidation.query.filter_by(purpose="profile_scan").count(), 2)

    def test_cancels_validation_when_profile_reappears(self):
        follower = Follower(
            user_id=self.user_id,
            username="maria",
            kind="followers",
            is_present=False,
            state="pending",
            missing_count=1,
        )
        db.session.add(follower)
        db.session.commit()
        enqueue_relationship_validation(follower)
        db.session.commit()

        cancelled_count = cancel_relationship_validations(follower)
        self.assertEqual(cancelled_count, 1)

        val = RelationshipValidation.query.filter_by(follower_id=follower.id).first()
        self.assertEqual(val.status, "cancelled")
        self.assertEqual(val.outcome, "reappeared")

    @patch("services.profile_validation.send_profile_validation_notification")
    @patch("services.profile_validation.inspect_instagram_profile")
    def test_confirms_removal_on_second_missing_export(self, mock_inspect, mock_notify):
        mock_inspect.return_value = ProfileCheck(
            "exists", "Perfil ativo e acessível", b"fake-jpeg", "image/jpeg"
        )

        run = AnalysisRun(
            user_id=self.user_id,
            source="manual",
            source_name="export.zip",
            validation_total=1,
            validation_completed=0,
        )
        db.session.add(run)
        db.session.commit()

        follower = Follower(
            user_id=self.user_id,
            username="maria",
            kind="followers",
            is_present=False,
            state="pending",
            missing_count=2,
            missing_since=local_now() - timedelta(days=5),
        )
        db.session.add(follower)
        db.session.commit()

        val, _ = enqueue_relationship_validation(follower, analysis_run_id=run.id)
        val.next_attempt_at = utc_now() - timedelta(minutes=1)
        db.session.commit()

        processed = process_next_validation()
        self.assertTrue(processed)

        db.session.refresh(follower)
        db.session.refresh(val)
        self.assertEqual(follower.state, "removed")
        self.assertEqual(val.status, "completed")
        self.assertEqual(val.outcome, "relationship_removed")

        # Lost history record was created
        lost_history = History.query.filter_by(
            user_id=self.user_id, username="maria", action="lost"
        ).first()
        self.assertIsNotNone(lost_history)
        mock_notify.assert_called_once()

    @patch("services.profile_validation.send_profile_validation_notification")
    @patch("services.profile_validation.inspect_instagram_profile")
    def test_confirms_removal_on_first_missing_export_when_profile_exists(self, mock_inspect, mock_notify):
        mock_inspect.return_value = ProfileCheck(
            "exists", "Perfil ativo e acessível", b"fake-jpeg", "image/jpeg"
        )

        run = AnalysisRun(
            user_id=self.user_id,
            source="manual",
            source_name="export.zip",
            validation_total=1,
            validation_completed=0,
        )
        db.session.add(run)
        db.session.commit()

        follower = Follower(
            user_id=self.user_id,
            username="lucas",
            kind="followers",
            is_present=False,
            state="pending",
            missing_count=1,
            missing_since=local_now(),
        )
        db.session.add(follower)
        db.session.commit()

        val, _ = enqueue_relationship_validation(follower, analysis_run_id=run.id)
        val.next_attempt_at = utc_now() - timedelta(minutes=1)
        db.session.commit()

        processed = process_next_validation()
        self.assertTrue(processed)

        db.session.refresh(follower)
        db.session.refresh(val)
        self.assertEqual(follower.state, "removed")
        self.assertEqual(val.status, "completed")
        self.assertEqual(val.outcome, "relationship_removed")
        self.assertEqual(History.query.filter_by(user_id=self.user_id, action="lost").count(), 1)
        self.assertEqual(follower.avatar_data, b"fake-jpeg")
        self.assertEqual(follower.avatar_mime, "image/jpeg")
        mock_notify.assert_called_once()

    @patch("services.profile_validation.send_profile_validation_notification")
    @patch("services.profile_validation.inspect_instagram_profile")
    def test_unavailable_retests_once_then_keeps_profile_absent(self, mock_inspect, mock_notify):
        mock_inspect.return_value = ProfileCheck("unavailable", "Perfil não encontrado (404)")

        follower = Follower(
            user_id=self.user_id,
            username="sumido",
            kind="followers",
            is_present=False,
            state="pending",
            missing_count=1,
            retest_count=0,
            missing_since=local_now(),
        )
        db.session.add(follower)
        db.session.commit()

        val, _ = enqueue_relationship_validation(follower)
        val.next_attempt_at = utc_now() - timedelta(minutes=1)
        db.session.commit()

        # Attempt 1: marks retry and retest_count = 1
        processed = process_next_validation()
        self.assertTrue(processed)
        db.session.refresh(follower)
        db.session.refresh(val)
        self.assertEqual(val.status, "retry")
        self.assertEqual(val.outcome, "unavailable_pending_retest")
        self.assertEqual(follower.retest_count, 1)

        # Attempt 2: the profile remains unavailable and stays in Ausentes.
        val.next_attempt_at = utc_now() - timedelta(minutes=1)
        db.session.commit()
        processed = process_next_validation()
        self.assertTrue(processed)
        db.session.refresh(follower)
        db.session.refresh(val)
        self.assertEqual(val.status, "completed")
        self.assertEqual(val.outcome, "untestable")
        self.assertEqual(follower.state, "absent")

    def test_stale_running_validation_is_requeued_after_worker_crash(self):
        follower = Follower(
            user_id=self.user_id, username="travado", kind="followers",
            is_present=False, state="pending", missing_count=1, missing_since=local_now(),
        )
        db.session.add(follower)
        db.session.commit()
        val, _ = enqueue_relationship_validation(follower)
        val.status = "running"
        val.attempts = 1
        val.last_attempt_at = utc_now() - timedelta(hours=2)
        follower.validation_status = "verifying"
        db.session.commit()

        recovered = recover_stale_validations(utc_now())

        db.session.refresh(val)
        db.session.refresh(follower)
        self.assertEqual(recovered, 1)
        self.assertEqual(val.status, "retry")
        self.assertLessEqual(val.next_attempt_at, utc_now())
        self.assertEqual(follower.validation_status, "queued")

    def test_recent_running_validation_is_left_alone(self):
        follower = Follower(user_id=self.user_id, username="ativo", kind="followers", is_present=False, state="pending")
        db.session.add(follower)
        db.session.commit()
        val, _ = enqueue_relationship_validation(follower)
        val.status = "running"
        val.last_attempt_at = utc_now() - timedelta(minutes=1)
        db.session.commit()

        self.assertEqual(recover_stale_validations(utc_now()), 0)

    def test_resolve_existing_profile_confirms_once_after_existence_proof(self):
        follower = Follower(
            user_id=self.user_id, username="ana", kind="followers", is_present=False,
            state="pending", missing_count=1, missing_since=local_now() - timedelta(days=3),
        )
        db.session.add(follower)
        db.session.commit()

        outcome, newly_lost = resolve_existing_profile(follower, local_now())
        self.assertEqual((outcome, newly_lost, follower.state), ("relationship_removed", True, "removed"))
        db.session.commit()

        outcome, newly_lost = resolve_existing_profile(follower, local_now())
        self.assertEqual((outcome, newly_lost), ("relationship_removed", False))
        self.assertEqual(History.query.filter_by(user_id=self.user_id, action="lost").count(), 1)


class VpnBlockSwitchTest(unittest.TestCase):
    def setUp(self):
        app.config.update(
            TESTING=True,
            PROFILE_VALIDATION_PROXY="http://vpn:8888",
            PROFILE_VALIDATION_VPN_AVOID_SECONDS=1800,
            PROFILE_VALIDATION_MIN_INTERVAL_SECONDS=0,
            PROFILE_VALIDATION_MAX_INTERVAL_SECONDS=0,
        )
        self.ctx = app.app_context()
        self.ctx.push()
        db.create_all()
        user = User(email="vpn@example.com")
        user.set_password("secret123")
        db.session.add(user)
        db.session.commit()
        status = network.get_status()
        status.vpn_configured = True
        status.vpn_up = True
        status.route = "vpn"
        follower = Follower(
            user_id=user.id, username="ana", kind="followers", is_present=False,
            state="pending", missing_count=1,
        )
        db.session.add(follower)
        db.session.commit()
        self.validation, _ = enqueue_relationship_validation(follower)
        self.validation.next_attempt_at = utc_now() - timedelta(seconds=1)
        db.session.commit()

    def tearDown(self):
        app.config["PROFILE_VALIDATION_PROXY"] = ""
        _identity_state.update(index=0, blocked_until=None, consecutive_blocks=0)
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    @patch("services.profile_validation.send_profile_validation_notification")
    @patch("services.profile_validation._render_instagram_profile")
    def test_blocked_vpn_switches_to_direct_then_returns_to_vpn(self, mock_render, _notify):
        mock_render.return_value = ProfileCheck("blocked", "bloqueado")
        self.assertTrue(process_next_validation())

        db.session.refresh(self.validation)
        self.assertEqual(self.validation.status, "retry")
        self.assertEqual(self.validation.attempts, 0)
        self.assertLessEqual(self.validation.next_attempt_at, utc_now())
        self.assertIsNone(web_checks_paused_until())
        self.assertEqual(network.current_route(), ("direct", None))

        # The direct network works: the check concludes, still without a pause.
        mock_render.return_value = ProfileCheck("exists", "ok")
        self.assertTrue(process_next_validation())
        self.assertEqual(mock_render.call_args.args[2], None)

        network.get_status().vpn_avoid_until = utc_now() - timedelta(seconds=1)
        db.session.commit()
        self.assertEqual(network.current_route(), ("vpn", "http://vpn:8888"))

    @patch("services.profile_validation.send_profile_validation_notification")
    @patch("services.profile_validation._render_instagram_profile")
    def test_blocked_on_both_routes_pauses_and_restarts_from_vpn(self, mock_render, _notify):
        mock_render.return_value = ProfileCheck("blocked", "bloqueado")
        process_next_validation()
        db.session.refresh(self.validation)
        self.validation.next_attempt_at = utc_now() - timedelta(seconds=1)
        db.session.commit()

        process_next_validation()

        self.assertIsNotNone(web_checks_paused_until())
        self.assertEqual(network.current_route()[0], "vpn")

    @patch("services.profile_validation._render_instagram_profile")
    def test_does_not_leave_the_vpn_when_direct_fallback_is_forbidden(self, mock_render):
        network.set_direct_fallback(False)
        mock_render.return_value = ProfileCheck("blocked", "bloqueado")

        process_next_validation()

        self.assertIsNotNone(web_checks_paused_until())
        self.assertIsNone(network.get_status().vpn_avoid_until)
