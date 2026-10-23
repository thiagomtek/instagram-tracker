from datetime import datetime, timedelta, timezone
import unittest

from services.google_drive import (
    _delete_processed_source,
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

    def list(self, q, **_kwargs):
        folder_id = q.split("'")[1]
        self.listed.append(folder_id)
        return Result({"files": self.tree.get(folder_id, [])})

    def delete(self, fileId, **_kwargs):
        self.deleted.append(fileId)
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

    def test_deletes_whole_export_folder_after_processing(self):
        service = FakeService()
        _delete_processed_source(service, "meta-folder-id", [])
        self.assertEqual(service.api.deleted, ["meta-folder-id"])

    def test_deletes_loose_files_but_never_the_monitored_root(self):
        service = FakeService()
        items = [{"id": "one"}, {"id": "two"}]
        _delete_processed_source(service, "root:root", items)
        self.assertEqual(service.api.deleted, ["one", "two"])

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
