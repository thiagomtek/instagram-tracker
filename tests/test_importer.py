import json
import os
import tempfile
import unittest
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

        self.assertEqual(summary.lost, 1)
        self.assertEqual(
            [item.username for item in Follower.query.filter_by(user_id=self.user_id, kind="followers")],
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

        self.assertEqual(summary.followers_lost, 1)
        self.assertEqual(
            {item.username for item in Follower.query.filter_by(user_id=self.user_id, kind="followers")},
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


if __name__ == "__main__":
    unittest.main()
