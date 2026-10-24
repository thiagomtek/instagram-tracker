from datetime import datetime, timedelta, timezone
import unittest

from flask import Flask

from services.google_drive import (
    _remove_processed_source,
    _has_complete_snapshot,
    _is_settled,
    _meta_export_folders,
    _relationship_files,
)


class Result:
    def __init__(self, payload=None):
        self.payload = payload or {}

    def execute(self):
        return self.payload


class FakeFiles:
    def __init__(self, tree):
        self.tree = tree
        self.listed = []
        self.deleted = []
        self.trashed = []

    def list(self, q, **_kwargs):
        folder_id = q.split("'")[1]
        self.listed.append(folder_id)
        return Result({"files": self.tree.get(folder_id, [])})

    def delete(self, fileId, **_kwargs):
        self.deleted.append(fileId)
        return Result()

    def update(self, fileId, body, **_kwargs):
        self.trashed.append((fileId, body))
        return Result()


class FakeService:
    def __init__(self, tree=None):
        self.api = FakeFiles(tree or {})

    def files(self):
        return self.api


def folder(item_id, name):
    return {"id": item_id, "name": name, "mimeType": "application/vnd.google-apps.folder"}


class GoogleDriveTest(unittest.TestCase):
    def test_only_standard_meta_folders_are_selected_from_my_drive(self):
        items = [
            folder("valid", "meta-2026-Oct-23-10-28-47"),
            folder("custom", "instagram-exports"),
            {"id": "zip", "name": "meta-export.zip", "mimeType": "application/zip"},
        ]

        self.assertEqual([item["id"] for item in _meta_export_folders(items)], ["valid"])

    def test_follows_meta_export_structure_without_scanning_other_content(self):
        tree = {
            "meta": [folder("instagram", "instagram-tthiago-2026-10-23-code")],
            "instagram": [folder("assets", "files"), folder("connections", "connections"), folder("media", "media")],
            "connections": [folder("relationships", "followers_and_following")],
            "relationships": [
                {"id": "followers", "name": "followers_1.html", "mimeType": "text/html"},
                {"id": "following", "name": "following.html", "mimeType": "text/html"},
            ],
        }
        service = FakeService(tree)
        files = _relationship_files(service, "meta", 100)
        self.assertEqual({item["id"] for item in files}, {"followers", "following"})
        self.assertEqual(service.api.listed, ["meta", "instagram", "connections", "relationships"])
        self.assertNotIn("assets", service.api.listed)
        self.assertNotIn("media", service.api.listed)

    def _remove(self, mode):
        flask_app = Flask(__name__)
        flask_app.config["GOOGLE_DRIVE_DELETE_MODE"] = mode
        service = FakeService()
        with flask_app.app_context():
            _remove_processed_source(service, "meta-folder-id")
        return service.api

    def test_processed_export_goes_to_trash_by_default(self):
        api = self._remove("trash")
        self.assertEqual(api.trashed, [("meta-folder-id", {"trashed": True})])
        self.assertEqual(api.deleted, [])

    def test_permanent_delete_only_when_configured(self):
        api = self._remove("delete")
        self.assertEqual(api.deleted, ["meta-folder-id"])
        self.assertEqual(api.trashed, [])

    def test_waits_for_complete_stable_export(self):
        recent = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
        incomplete = [{"name": "followers_1.html", "modifiedTime": old}]
        complete_recent = incomplete + [{"name": "following.html", "modifiedTime": recent}]
        complete_old = [
            {"name": "followers_1.html", "modifiedTime": old},
            {"name": "following.html", "modifiedTime": old},
        ]
        self.assertFalse(_has_complete_snapshot(incomplete))
        self.assertTrue(_has_complete_snapshot(complete_recent))
        self.assertFalse(_is_settled(complete_recent, 300))
        self.assertTrue(_is_settled(complete_old, 300))


if __name__ == "__main__":
    unittest.main()
