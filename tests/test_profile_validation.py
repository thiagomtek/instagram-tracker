import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from app import app
from database import db
from models.follower import Follower
from models.history import History
from models.integration import AnalysisRun, RelationshipValidation
from models.user import User
from services.profile_validation import (
    ProfileCheck,
    cancel_relationship_validations,
    enqueue_relationship_validation,
    process_next_validation,
)


class ProfileValidationTest(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config["TESTING"] = True
        self.app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        self.app.config["PROFILE_VALIDATION_MAX_ATTEMPTS"] = 2
        self.app.config["PROFILE_VALIDATION_INITIAL_MIN_SECONDS"] = 60
        self.app.config["PROFILE_VALIDATION_INITIAL_MAX_SECONDS"] = 120
        self.app.config["PROFILE_VALIDATION_RETRY_MIN_SECONDS"] = 30
        self.app.config["PROFILE_VALIDATION_RETRY_MAX_SECONDS"] = 60
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        user = User(email="test@example.com")
        user.set_password("secret123")
        db.session.add(user)
        db.session.commit()
        self.user_id = user.id

    def tearDown(self):
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
        mock_inspect.return_value = ProfileCheck("exists", "Perfil ativo e acessível")

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
            missing_since=datetime.now() - timedelta(days=5),
        )
        db.session.add(follower)
        db.session.commit()

        val, _ = enqueue_relationship_validation(follower, analysis_run_id=run.id)
        val.next_attempt_at = datetime.now() - timedelta(minutes=1)
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
    def test_waits_confirmation_on_first_missing_export(self, mock_inspect, mock_notify):
        mock_inspect.return_value = ProfileCheck("exists", "Perfil ativo e acessível")

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
            missing_since=datetime.now(),
        )
        db.session.add(follower)
        db.session.commit()

        val, _ = enqueue_relationship_validation(follower, analysis_run_id=run.id)
        val.next_attempt_at = datetime.now() - timedelta(minutes=1)
        db.session.commit()

        processed = process_next_validation()
        self.assertTrue(processed)

        db.session.refresh(follower)
        db.session.refresh(val)
        self.assertEqual(follower.state, "pending")
        self.assertEqual(val.status, "completed")
        self.assertEqual(val.outcome, "exists_waiting_confirmation")
        self.assertEqual(History.query.filter_by(user_id=self.user_id, action="lost").count(), 0)
        mock_notify.assert_called_once()

    @patch("services.profile_validation.send_profile_validation_notification")
    @patch("services.profile_validation.inspect_instagram_profile")
    def test_unavailable_retests_once_then_marks_untestable(self, mock_inspect, mock_notify):
        mock_inspect.return_value = ProfileCheck("unavailable", "Perfil não encontrado (404)")

        follower = Follower(
            user_id=self.user_id,
            username="sumido",
            kind="followers",
            is_present=False,
            state="pending",
            missing_count=1,
            retest_count=0,
            missing_since=datetime.now(),
        )
        db.session.add(follower)
        db.session.commit()

        val, _ = enqueue_relationship_validation(follower)
        val.next_attempt_at = datetime.now() - timedelta(minutes=1)
        db.session.commit()

        # Attempt 1: marks retry and retest_count = 1
        processed = process_next_validation()
        self.assertTrue(processed)
        db.session.refresh(follower)
        db.session.refresh(val)
        self.assertEqual(val.status, "retry")
        self.assertEqual(val.outcome, "unavailable_pending_retest")
        self.assertEqual(follower.retest_count, 1)

        # Attempt 2 (the 1 allowed retest): profile still unavailable -> becomes untestable
        val.next_attempt_at = datetime.now() - timedelta(minutes=1)
        db.session.commit()
        processed = process_next_validation()
        self.assertTrue(processed)
        db.session.refresh(follower)
        db.session.refresh(val)
        self.assertEqual(val.status, "completed")
        self.assertEqual(val.outcome, "untestable")
        self.assertEqual(follower.state, "untestable")
