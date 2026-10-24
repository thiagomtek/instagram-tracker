import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zipfile import ZipFile

from app import app
from database import db
from models.follower import Follower
from models.history import History
from models.integration import AnalysisRun, RelationshipValidation
from models.system import ImportObservation
from models.user import User
from services.analysis_runs import complete_analysis_run
from services.clock import local_now
from services.importer import ImportRejected, process_import_paths
from services.parser import parse_instagram_date
from services.storage import load_followers, save_followers


def relationship_html(usernames):
    rows = []
    for username in usernames:
        rows.append(
            '<div class="_a6-p"><div>'
            f'<a href="https://www.instagram.com/{username}/">{username}</a>'
            '</div><div>jan 01, 2026 1:00 da tarde</div></div>'
        )
    return f"<html><body>{''.join(rows)}</body></html>"


class ImporterTest(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        self.context = app.app_context()
        self.context.push()
        db.session.remove()
        db.drop_all()
        db.create_all()
        user = User(email="test@example.com")
        user.set_password("a-secure-test-password")
        db.session.add(user)
        db.session.commit()
        self.user_id = user.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def write(self, root, name, usernames):
        path = Path(root) / name
        path.write_text(relationship_html(usernames), encoding="utf-8")
        return path

    def test_combines_split_files_before_comparing_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first = self.write(temp_dir, "followers_1.html", ["ana"])
            second = self.write(temp_dir, "followers_2.html", ["bia"])
            following = self.write(temp_dir, "following.html", ["ana", "caio"])

            summary = process_import_paths([first, second, following], self.user_id)

        self.assertEqual(summary.followers_found, 2)
        self.assertEqual(
            {item.username for item in Follower.query.filter_by(user_id=self.user_id, kind="followers")},
            {"ana", "bia"},
        )
        self.assertEqual(History.query.filter_by(user_id=self.user_id, action="lost").count(), 0)

    def test_reads_complete_zip_and_removes_missing_follower(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            initial = self.write(temp_dir, "followers_1.html", ["ana", "bia"])
            process_import_paths([initial], self.user_id)

            archive = Path(temp_dir) / "instagram-export.zip"
            with ZipFile(archive, "w") as zip_file:
                zip_file.writestr(
                    "connections/followers_and_following/followers_1.html",
                    relationship_html(["ana"]),
                )
            summary = process_import_paths([archive], self.user_id)

        self.assertEqual(summary.followers_missing, 1)
        self.assertEqual(
            [item.username for item in Follower.query.filter_by(user_id=self.user_id, kind="followers", is_present=True)],
            ["ana"],
        )

    def test_all_time_export_replaces_snapshot_even_with_old_relationship_dates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            initial = self.write(temp_dir, "followers_1.html", ["ana", "perfil_antigo"])
            process_import_paths([initial], self.user_id)

            # Both exports carry an old relationship date. The missing username
            # must still be removed because the file is a current snapshot.
            current = self.write(temp_dir, "followers_current.html", ["ana"])
            summary = process_import_paths([current], self.user_id)

        self.assertEqual(summary.followers_missing, 1)
        self.assertEqual(
            {item.username for item in Follower.query.filter_by(user_id=self.user_id, kind="followers", is_present=True)},
            {"ana"},
        )

    def test_first_snapshot_is_a_baseline_not_a_gain_event(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.write(temp_dir, "followers_1.html", ["ana", "bia"])
            summary = process_import_paths([path], self.user_id)

        self.assertEqual(summary.followers_found, 2)
        self.assertEqual(summary.followers_gained, 0)
        self.assertEqual(History.query.filter_by(user_id=self.user_id).count(), 0)

    def test_reads_instagram_json_structure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "following.json"
            path.write_text(
                json.dumps(
                    {
                        "relationships_following": [
                            {"string_list_data": [{"value": "duda", "timestamp": 1760000000}]}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            summary = process_import_paths([path], self.user_id)

        self.assertEqual(summary.following_found, 1)
        self.assertEqual(Follower.query.filter_by(user_id=self.user_id, username="duda").count(), 1)

    def test_rejects_zip_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "bad.zip"
            with ZipFile(archive, "w") as zip_file:
                zip_file.writestr("../followers_1.html", relationship_html(["ana"]))
            with self.assertRaisesRegex(ValueError, "caminho inválido"):
                process_import_paths([archive], self.user_id)

    def test_relationship_storage_returns_newest_items_first(self):
        now = local_now()
        save_followers(
            "followers",
            [
                {"username": "base_antiga", "date": now - timedelta(days=40)},
                {"username": "base_recente", "date": now - timedelta(days=2)},
            ],
            self.user_id,
        )

        self.assertEqual(load_followers("followers", self.user_id), ["base_recente", "base_antiga"])

        gained, missing, reappeared, followed_again = save_followers(
            "followers",
            [
                {"username": "base_antiga", "date": now - timedelta(days=40)},
                {"username": "nova_antiga", "date": now - timedelta(days=10)},
                {"username": "nova_recente", "date": now - timedelta(hours=1)},
            ],
            self.user_id,
        )

        self.assertEqual(gained, ["nova_recente", "nova_antiga"])
        self.assertEqual(missing, ["base_recente"])
        self.assertEqual(reappeared, [])
        self.assertEqual(followed_again, [])

    def test_followed_again_distinction_from_reappeared(self):
        now = local_now()
        # Was pending (temporary absence), returns -> reappeared
        f1 = Follower(user_id=self.user_id, kind="followers", username="user_pending", state="pending", is_present=False)
        # Was removed (confirmed), returns -> followed_again
        f2 = Follower(user_id=self.user_id, kind="followers", username="user_removed", state="removed", is_present=False)
        db.session.add_all([f1, f2])
        db.session.commit()

        gained, missing, reappeared, followed_again = save_followers(
            "followers",
            [
                {"username": "user_pending", "date": now},
                {"username": "user_removed", "date": now},
            ],
            self.user_id,
        )
        self.assertIn("user_pending", reappeared)
        self.assertIn("user_removed", followed_again)

    def test_cross_list_detection_resolves_without_scraping(self):
        db.session.add(Follower(
            user_id=self.user_id, kind="following", username="active_friend", is_present=True, state="present"
        ))
        db.session.add(Follower(
            user_id=self.user_id, kind="followers", username="active_friend", is_present=False, state="pending",
            missing_count=1, missing_since=local_now() - timedelta(days=2),
        ))
        db.session.commit()

        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "followers_export.zip"
            with ZipFile(archive, "w") as zf:
                zf.writestr("connections/followers_and_following/followers_1.html", relationship_html(["other_user"]))

            summary = process_import_paths([archive], self.user_id)

        follower = Follower.query.filter_by(user_id=self.user_id, kind="followers", username="active_friend").first()
        self.assertEqual(follower.state, "removed")
        self.assertEqual(follower.last_validation_result, "relationship_removed")
        self.assertEqual(summary.followers_lost, 1)
        self.assertIsNotNone(History.query.filter_by(user_id=self.user_id, username="active_friend", action="lost").first())

    # ----- safeguards against destroying the snapshot

    def test_file_without_profiles_never_wipes_the_list(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            full = self.write(temp_dir, "followers_1.html", ["a", "b", "c"])
            process_import_paths([full], self.user_id)
            full.write_text("<html><body><div class='novo-layout'></div></body></html>", encoding="utf-8")

            with self.assertRaises(ImportRejected) as raised:
                process_import_paths([full], self.user_id)

        self.assertTrue(raised.exception.needs_confirmation)
        self.assertEqual(Follower.query.filter_by(is_present=True).count(), 3)
        self.assertEqual(History.query.count(), 0)

    def test_large_drop_needs_explicit_confirmation(self):
        everyone = [f"perfil_{index}" for index in range(40)]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.write(temp_dir, "followers_1.html", everyone)
            process_import_paths([path], self.user_id)
            # Same export missing its second half, e.g. followers_2.html was not sent.
            path = self.write(temp_dir, "followers_1.html", everyone[:20])

            with self.assertRaisesRegex(ImportRejected, "perderia 20 de 40"):
                process_import_paths([path], self.user_id)
            self.assertEqual(Follower.query.filter_by(is_present=True).count(), 40)

            summary = process_import_paths([path], self.user_id, allow_large_drop=True)

        self.assertEqual(summary.followers_missing, 20)
        self.assertEqual(Follower.query.filter_by(is_present=True).count(), 20)

    def test_identical_reupload_is_ignored_and_does_not_confirm_removal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            followers = self.write(temp_dir, "followers_1.html", ["a", "b", "c"])
            following = self.write(temp_dir, "following.html", ["a", "b", "c"])
            process_import_paths([followers, following], self.user_id)
            self.write(temp_dir, "followers_1.html", ["a", "b"])
            first = process_import_paths([followers, following], self.user_id)
            second = process_import_paths([followers, following], self.user_id)

        self.assertEqual(first.followers_missing, 1)
        self.assertTrue(second.is_duplicate)
        profile = Follower.query.filter_by(username="c", kind="followers").first()
        self.assertEqual(profile.missing_count, 1)
        self.assertNotEqual(profile.state, "removed")
        self.assertEqual(History.query.filter_by(action="lost").count(), 0)

    def test_second_distinct_export_after_the_window_confirms_removal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            followers = self.write(temp_dir, "followers_1.html", ["a", "b", "c"])
            following = self.write(temp_dir, "following.html", ["a", "b", "c"])
            process_import_paths([followers, following], self.user_id)
            self.write(temp_dir, "followers_1.html", ["a", "b"])
            process_import_paths([followers, following], self.user_id)
            for observation in ImportObservation.query.all():
                observation.observed_at -= timedelta(days=2)
            db.session.commit()
            summary = process_import_paths([followers, following], self.user_id)

        self.assertEqual(summary.followers_lost, 1)
        self.assertEqual(Follower.query.filter_by(username="c", kind="followers").first().state, "removed")

    def test_opposite_list_from_this_upload_wins_over_the_stale_database_copy(self):
        # "c" is still followed in the database, but this upload drops "c" from
        # both lists, so nothing proves the profile exists without a web check.
        with tempfile.TemporaryDirectory() as temp_dir:
            followers = self.write(temp_dir, "followers_1.html", ["a", "c"])
            following = self.write(temp_dir, "following.html", ["a", "c"])
            process_import_paths([followers, following], self.user_id)
            self.write(temp_dir, "followers_1.html", ["a"])
            self.write(temp_dir, "following.html", ["a"])
            summary = process_import_paths([followers, following], self.user_id)

        self.assertEqual(summary.followers_missing, 1)
        validation = RelationshipValidation.query.filter_by(username="c", kind="followers").first()
        self.assertEqual(validation.status, "queued")

    def test_web_validation_can_be_disabled(self):
        app.config["PROFILE_VALIDATION_ENABLED"] = False
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                path = self.write(temp_dir, "followers_1.html", ["a", "c"])
                process_import_paths([path], self.user_id)
                self.write(temp_dir, "followers_1.html", ["a"])
                summary = process_import_paths([path], self.user_id)
        finally:
            app.config["PROFILE_VALIDATION_ENABLED"] = True

        self.assertEqual(summary.followers_missing, 1)
        self.assertEqual(RelationshipValidation.query.count(), 0)

    def test_counters_of_validations_resolved_during_import_survive_completion(self):
        run = AnalysisRun(user_id=self.user_id, source="manual", source_name="x")
        db.session.add(run)
        db.session.commit()
        with tempfile.TemporaryDirectory() as temp_dir:
            followers = self.write(temp_dir, "followers_1.html", ["a", "b", "c"])
            following = self.write(temp_dir, "following.html", ["a", "b", "c"])
            process_import_paths([followers, following], self.user_id, analysis_run_id=run.id)
            self.write(temp_dir, "followers_1.html", ["a", "b"])
            summary = process_import_paths([followers, following], self.user_id, analysis_run_id=run.id)
        complete_analysis_run(run, summary)

        self.assertEqual((run.validation_total, run.validation_completed), (1, 1))

    # ----- parsing and archives

    def test_dates_in_portuguese_and_english_exports(self):
        self.assertEqual(parse_instagram_date("jan 06, 2019 6:21 da tarde"), datetime(2019, 1, 6, 18, 21))
        self.assertEqual(parse_instagram_date("dez 03, 2025 12:05 da manhã"), datetime(2025, 12, 3, 0, 5))
        self.assertEqual(parse_instagram_date("Oct 09, 2024 3:30 pm"), datetime(2024, 10, 9, 15, 30))
        self.assertEqual(parse_instagram_date("Mar 01, 2024 12:10 am"), datetime(2024, 3, 1, 0, 10))
        self.assertIsNone(parse_instagram_date("não é uma data"))
        self.assertIsNone(parse_instagram_date("xyz 09, 2024 3:30 pm"))

    def test_unparseable_date_keeps_the_stored_date(self):
        stored = datetime(2020, 5, 5, 10, 0)
        save_followers("followers", [{"username": "ana", "date": stored}], self.user_id)
        save_followers("followers", [{"username": "ana", "date": None}], self.user_id)
        self.assertEqual(Follower.query.filter_by(username="ana").first().date_added, stored)

    def test_rejects_zip_member_that_escapes_through_a_sibling_prefix(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "bad.zip"
            with ZipFile(archive, "w") as zip_file:
                # Resolves next to the extraction folder and shares its name as a prefix.
                zip_file.writestr("../archive-1-bad2/followers_1.html", relationship_html(["ana"]))
            with self.assertRaisesRegex(ValueError, "caminho inválido"):
                process_import_paths([archive], self.user_id)

    def test_zip_media_is_never_extracted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "export.zip"
            with ZipFile(archive, "w") as zip_file:
                zip_file.writestr("connections/followers_and_following/followers_1.html", relationship_html(["ana"]))
                zip_file.writestr("media/photo.jpg", b"x" * 1024)
            summary = process_import_paths([archive], self.user_id)
        self.assertEqual(summary.files_read, 1)


if __name__ == "__main__":
    unittest.main()
