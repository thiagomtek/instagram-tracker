import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zipfile import ZipFile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="instagram-tracker-tests-"))
os.environ.setdefault("SECRET_KEY", "tests-only-secret-key")
os.environ.setdefault("SESSION_COOKIE_SECURE", "false")

from app import app
from database import db
from models.follower import Follower
from models.history import History
from models.user import User
from services.importer import process_import_paths
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

    def test_combines_split_files_before_comparing_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "followers_1.html"
            second = root / "followers_2.html"
            following = root / "following.html"
            first.write_text(relationship_html(["ana"]), encoding="utf-8")
            second.write_text(relationship_html(["bia"]), encoding="utf-8")
            following.write_text(relationship_html(["ana", "caio"]), encoding="utf-8")

            summary = process_import_paths([first, second, following], self.user_id)

        self.assertEqual(summary.followers_found, 2)
        self.assertEqual(
            {item.username for item in Follower.query.filter_by(user_id=self.user_id, kind="followers")},
            {"ana", "bia"},
        )
        self.assertEqual(History.query.filter_by(user_id=self.user_id, action="lost").count(), 0)

    def test_reads_complete_zip_and_removes_missing_follower(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            initial = root / "followers_1.html"
            initial.write_text(relationship_html(["ana", "bia"]), encoding="utf-8")
            process_import_paths([initial], self.user_id)

            archive = root / "instagram-export.zip"
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
            root = Path(temp_dir)
            initial = root / "followers_1.html"
            initial.write_text(relationship_html(["ana", "perfil_antigo"]), encoding="utf-8")
            process_import_paths([initial], self.user_id)

            # Both exports carry an old relationship date. The missing username
            # must still be removed because the file is a current snapshot.
            current = root / "followers_current.html"
            current.write_text(relationship_html(["ana"]), encoding="utf-8")
            summary = process_import_paths([current], self.user_id)

        self.assertEqual(summary.followers_missing, 1)
        self.assertEqual(
            {item.username for item in Follower.query.filter_by(user_id=self.user_id, kind="followers", is_present=True)},
            {"ana"},
        )

    def test_first_snapshot_is_a_baseline_not_a_gain_event(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "followers_1.html"
            path.write_text(relationship_html(["ana", "bia"]), encoding="utf-8")
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
        now = datetime.now()
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
        now = datetime.now()
        # Case 1: Was pending (temporary absent), returns -> action = reappeared
        f1 = Follower(user_id=self.user_id, kind="followers", username="user_pending", state="pending", is_present=False)
        # Case 2: Was removed (confirmed lost), returns -> action = followed_again
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
        # User is active in following
        db.session.add(Follower(
            user_id=self.user_id, kind="following", username="active_friend", is_present=True, state="present"
        ))
        # User goes missing in followers with missing_count = 1
        db.session.add(Follower(
            user_id=self.user_id, kind="followers", username="active_friend", is_present=False, state="pending", missing_count=1, missing_since=datetime.now() - timedelta(days=2)
        ))
        db.session.commit()

        # Import followers without active_friend (now missing_count reaches 2)
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "followers_export.zip"
            with ZipFile(archive, "w") as zf:
                zf.writestr("connections/followers_and_following/followers_1.html", relationship_html(["other_user"]))

            summary = process_import_paths([archive], self.user_id)

        # Because active_friend is present in following, existence is proven cross-list without scraping
        follower = Follower.query.filter_by(user_id=self.user_id, kind="followers", username="active_friend").first()
        self.assertEqual(follower.state, "removed")
        self.assertEqual(follower.last_validation_result, "relationship_removed")
        self.assertEqual(summary.followers_lost, 1)
        lost_history = History.query.filter_by(user_id=self.user_id, username="active_friend", action="lost").first()
        self.assertIsNotNone(lost_history)


if __name__ == "__main__":
    unittest.main()
