import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from flask import current_app
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from database import db
from models.integration import AnalysisRun, DriveConnection, DriveImport
from services.analysis_runs import complete_analysis_run, fail_analysis_run, start_analysis_run
from services.clock import utc_now
from services.importer import ImportRejected, ImportSummary, process_import_paths
from services.notifications import send_analysis_notification
from services.token_crypto import decrypt_token


FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
DOWNLOADABLE_SUFFIXES = {".html", ".json", ".zip"}
META_EXPORT_PATTERN = re.compile(r"^meta-\d{4}-[A-Za-z]{3}-\d{1,2}-\d{2}-\d{2}-\d{2}", re.IGNORECASE)
MONITORED_FOLDER_ID = "root"
MONITORED_FOLDER_NAME = "Meu Drive (pastas meta-*)"


def google_drive_is_configured():
    return bool(
        current_app.config.get("GOOGLE_CLIENT_ID")
        and current_app.config.get("GOOGLE_CLIENT_SECRET")
    )


def credentials_for(connection):
    return Credentials(
        token=None,
        refresh_token=decrypt_token(connection.encrypted_refresh_token),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=current_app.config["GOOGLE_CLIENT_ID"],
        client_secret=current_app.config["GOOGLE_CLIENT_SECRET"],
        scopes=current_app.config["GOOGLE_DRIVE_SCOPES"],
    )


def drive_service(connection):
    return build("drive", "v3", credentials=credentials_for(connection), cache_discovery=False)


def list_children(service, folder_id):
    files = []
    page_token = None
    while True:
        response = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken, files(id,name,mimeType,modifiedTime,md5Checksum,size)",
            pageSize=1000,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            return files


def _meta_export_folders(items):
    """Select only Instagram's standard top-level export folders."""
    return [
        item
        for item in items
        if item.get("mimeType") == FOLDER_MIME_TYPE
        and META_EXPORT_PATTERN.match(item.get("name", ""))
    ]


def _folder_files(service, folder_id, item_limit):
    collected = []
    pending = [folder_id]
    visited = set()
    inspected = 0
    while pending:
        current_id = pending.pop()
        if current_id in visited:
            continue
        visited.add(current_id)
        for item in list_children(service, current_id):
            inspected += 1
            if inspected > item_limit:
                raise ValueError("A pasta excede o limite de itens configurado.")
            if item["mimeType"] == FOLDER_MIME_TYPE:
                pending.append(item["id"])
            else:
                collected.append(item)
    return collected


def _version_for(items):
    digest = hashlib.sha256()
    for item in sorted(items, key=lambda current: current["id"]):
        marker = item.get("md5Checksum") or item.get("modifiedTime") or ""
        digest.update(f"{item['id']}:{marker}:{item.get('size', '')}".encode("utf-8"))
    return digest.hexdigest()


def _download(service, item, destination):
    safe_name = Path(item["name"]).name
    target = Path(destination) / f"{item['id']}-{safe_name}"
    request = service.files().get_media(fileId=item["id"], supportsAllDrives=True)
    with target.open("wb") as file_handle:
        downloader = MediaIoBaseDownload(file_handle, request, chunksize=1024 * 1024)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return target


def _remove_processed_source(service, source_id):
    """Trash (default) or permanently delete the processed export folder."""
    if current_app.config["GOOGLE_DRIVE_DELETE_MODE"] == "delete":
        service.files().delete(fileId=source_id, supportsAllDrives=True).execute()
    else:
        service.files().update(fileId=source_id, body={"trashed": True}, supportsAllDrives=True).execute()


def _candidate_files(items):
    return [item for item in items if Path(item["name"]).suffix.lower() in DOWNLOADABLE_SUFFIXES]


def _has_complete_snapshot(items):
    names = [item["name"].lower() for item in items]
    if any(name.endswith(".zip") for name in names):
        return True
    # The manifest is enough to classify a bounded daily export. Such a batch
    # may legitimately have no relationship files when no event occurred.
    if any(name == "start_here.html" for name in names):
        return True
    return any("followers" in name for name in names) and any("following" in name for name in names)


def _is_settled(items, settle_seconds):
    modified_values = [item.get("modifiedTime") for item in items if item.get("modifiedTime")]
    if not modified_values:
        return True
    latest = max(datetime.fromisoformat(value.replace("Z", "+00:00")) for value in modified_values)
    return (datetime.now(timezone.utc) - latest).total_seconds() >= settle_seconds


def _relationship_files(service, export_folder_id, item_limit):
    """Follow Meta's export tree directly to connections/followers_and_following."""
    pending = [export_folder_id]
    visited = set()
    inspected = 0
    manifests = []
    while pending:
        folder_id = pending.pop(0)
        if folder_id in visited:
            continue
        visited.add(folder_id)
        children = list_children(service, folder_id)
        inspected += len(children)
        if inspected > item_limit:
            raise ValueError("A exportação excede o limite de itens configurado.")

        manifests.extend(
            item for item in children
            if item["mimeType"] != FOLDER_MIME_TYPE and item["name"].casefold() == "start_here.html"
        )
        folders = [item for item in children if item["mimeType"] == FOLDER_MIME_TYPE]
        relationship_folder = next(
            (item for item in folders if item["name"].lower() == "followers_and_following"),
            None,
        )
        if relationship_folder:
            relationship_files = _candidate_files(
                _folder_files(service, relationship_folder["id"], item_limit)
            )
            # The manifest carries the requested date range. Without it, a
            # bounded event export can be mistaken for a current snapshot.
            return manifests + relationship_files

        # Meta currently creates meta-*/instagram-*/connections/followers_and_following.
        # Following the known branch prevents scans through photos and other exports.
        connections = [item for item in folders if item["name"].lower() == "connections"]
        instagram_roots = [item for item in folders if item["name"].lower().startswith("instagram-")]
        preferred = connections or instagram_roots
        if preferred:
            pending.extend(item["id"] for item in preferred)
        else:
            pending.extend(item["id"] for item in folders if item["name"].lower() != "files")

    # Fallback supports a legacy meta-* layout with relationship files at its root.
    return _candidate_files(_folder_files(service, export_folder_id, item_limit))


def sync_connection(connection):
    if not connection.enabled:
        return []

    now = utc_now()
    # Instagram delivers exports into meta-* folders in My Drive. Persist the
    # fixed source as well so older custom-folder connections migrate on their
    # next automatic or manual check.
    connection.folder_id = MONITORED_FOLDER_ID
    connection.folder_name = MONITORED_FOLDER_NAME
    connection.last_checked_at = now
    connection.last_error = None
    db.session.commit()

    service = drive_service(connection)
    item_limit = current_app.config["GOOGLE_DRIVE_MAX_ITEMS"]
    top_level = list_children(service, MONITORED_FOLDER_ID)
    batches = []

    for item in _meta_export_folders(top_level):
        descendants = _relationship_files(service, item["id"], item_limit)
        if descendants:
            modified = max((child.get("modifiedTime", "") for child in descendants), default="")
            batches.append((modified, item["id"], item["name"], descendants))

    imported = []
    for _, source_id, source_name, items in sorted(batches, key=lambda batch: batch[0]):
        if not _has_complete_snapshot(items):
            current_app.logger.info("Exportação %s ainda não contém followers e following", source_name)
            continue
        if not _is_settled(items, current_app.config["GOOGLE_DRIVE_SETTLE_SECONDS"]):
            current_app.logger.info("Exportação %s ainda está no período de estabilização", source_name)
            continue
        source_version = _version_for(items)
        already_processed = DriveImport.query.filter_by(
            connection_id=connection.id,
            source_id=source_id,
            source_version=source_version,
        ).first()
        if already_processed:
            if not already_processed.deleted_at:
                try:
                    _remove_processed_source(service, source_id)
                    already_processed.deleted_at = utc_now()
                    already_processed.deletion_error = None
                    if already_processed.analysis_run_id:
                        previous_run = db.session.get(AnalysisRun, already_processed.analysis_run_id)
                        if previous_run:
                            previous_run.status = "success"
                            previous_run.cleanup_error = None
                    db.session.commit()
                except Exception as exc:
                    db.session.rollback()
                    current_import = db.session.get(DriveImport, already_processed.id)
                    if current_import:
                        current_import.deletion_error = str(exc)[:2000]
                        db.session.commit()
            continue

        analysis_run = start_analysis_run(connection.user_id, "google_drive", source_name)
        try:
            with TemporaryDirectory(prefix="instagram-drive-") as temp_dir:
                local_paths = [_download(service, item, temp_dir) for item in items]
                summary = process_import_paths(local_paths, connection.user_id, analysis_run_id=analysis_run.id)
        except ImportRejected as exc:
            reason = (
                f"{exc} (Exportação do Drive: {source_name}.) Importe manualmente com a confirmação "
                "e depois remova a pasta do Drive para liberar as próximas verificações."
            ) if exc.needs_confirmation else str(exc)
            fail_analysis_run(analysis_run.id, reason)
            raise ValueError(reason) from exc
        except Exception as exc:
            fail_analysis_run(analysis_run.id, exc)
            raise

        drive_import = DriveImport(
            connection_id=connection.id,
            source_id=source_id,
            source_version=source_version,
            source_name=source_name,
            analysis_run_id=analysis_run.id,
            summary=summary.to_json(),
        )
        db.session.add(drive_import)
        connection.last_success_at = utc_now()
        connection.last_import_summary = summary.to_json()
        db.session.commit()

        cleanup_error = None
        try:
            _remove_processed_source(service, source_id)
            drive_import.deleted_at = utc_now()
        except Exception as exc:
            cleanup_error = str(exc)[:2000]
            drive_import.deletion_error = cleanup_error
            current_app.logger.exception("Análise concluída, mas a origem %s não pôde ser excluída", source_name)
        db.session.commit()
        complete_analysis_run(analysis_run, summary, cleanup_error=cleanup_error)
        imported.append(summary)

    if imported:
        totals = ImportSummary()
        for summary in imported:
            totals.files_read += summary.files_read
            totals.followers_found = summary.followers_found or totals.followers_found
            totals.following_found = summary.following_found or totals.following_found
            for name in (
                "gained", "reappeared", "followed_again", "followers_gained", "followers_lost",
                "followers_missing", "followers_reappeared", "followers_followed_again",
                "recent_unfollows", "validation_total", "validation_completed",
            ):
                setattr(totals, name, getattr(totals, name) + getattr(summary, name))
            totals.gained_usernames.extend(summary.gained_usernames)
            totals.missing_usernames.extend(summary.missing_usernames)
            totals.reappeared_usernames.extend(summary.reappeared_usernames)
            totals.followed_again_usernames.extend(summary.followed_again_usernames)
        send_analysis_notification(connection.user_id, totals)
    return imported


def sync_connection_safely(connection):
    try:
        return sync_connection(connection)
    except Exception as exc:
        db.session.rollback()
        current = db.session.get(DriveConnection, connection.id)
        if current:
            current.last_checked_at = utc_now()
            current.last_error = str(exc)[:2000]
            db.session.commit()
        current_app.logger.exception("Falha ao sincronizar a conexão %s", connection.id)
        return []


def sync_all_connections(only_requested=False):
    """Sync every enabled connection, or only those the user asked to check now."""
    query = DriveConnection.query.filter_by(enabled=True)
    if only_requested:
        query = query.filter_by(sync_requested=True)
    for connection_id in [row.id for row in query.all()]:
        connection = db.session.get(DriveConnection, connection_id)
        if connection:
            connection.sync_requested = False
            db.session.commit()
            sync_connection_safely(connection)
