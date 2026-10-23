import json
import os
import stat
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZipFile

from database import db
from models.history import History
from models.follower import Follower
from services.parser import detect_kind, parse_instagram_file
from services.profile_validation import enqueue_relationship_validation
from services.storage import save_followers


MAX_ARCHIVE_FILES = int(os.environ.get("MAX_ARCHIVE_FILES", "10000"))
MAX_ARCHIVE_UNCOMPRESSED_BYTES = int(os.environ.get("MAX_ARCHIVE_UNCOMPRESSED_BYTES", str(1024 * 1024 * 1024)))


@dataclass
class ImportSummary:
    files_read: int = 0
    followers_found: int = 0
    following_found: int = 0
    gained: int = 0
    lost: int = 0
    reappeared: int = 0
    followers_gained: int = 0
    followers_lost: int = 0
    followers_missing: int = 0
    followers_reappeared: int = 0
    validation_total: int = 0
    gained_usernames: list = field(default_factory=list)
    lost_usernames: list = field(default_factory=list)
    reappeared_usernames: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)

    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False)

    def message(self):
        return (
            f"{self.files_read} arquivo(s) analisado(s), "
            f"{self.followers_found} seguidores e {self.following_found} perfis seguidos. "
            f"Seguidores: {self.followers_gained} novo(s), "
            f"{self.followers_missing} aguardando validação, "
            f"{self.followers_lost} remoção(ões) confirmada(s), "
            f"{self.followers_reappeared} reaparecido(s)."
        )


def _safe_extract(archive_path, destination):
    destination = Path(destination).resolve()
    try:
        with ZipFile(archive_path) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_FILES:
                raise ValueError("O arquivo ZIP contém itens demais.")
            if sum(item.file_size for item in members) > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise ValueError("O conteúdo descompactado excede o limite permitido.")
            for member in members:
                if stat.S_ISLNK(member.external_attr >> 16):
                    raise ValueError("O arquivo ZIP contém um link simbólico não permitido.")
                target = (destination / member.filename).resolve()
                if destination != target and destination not in target.parents:
                    raise ValueError("O arquivo ZIP contém um caminho inválido.")
            archive.extractall(destination)
    except BadZipFile as exc:
        raise ValueError("O arquivo ZIP está corrompido ou não é válido.") from exc


def _discover_data_files(paths, extraction_root):
    data_files = []
    for raw_path in paths:
        path = Path(raw_path)
        if path.is_dir():
            candidates = list(path.rglob("*"))
        else:
            candidates = [path]
        for candidate in candidates:
            if not candidate.is_file():
                continue
            suffix = candidate.suffix.lower()
            if suffix == ".zip":
                zip_destination = Path(extraction_root) / f"archive-{len(data_files)}-{candidate.stem}"
                zip_destination.mkdir(parents=True, exist_ok=True)
                _safe_extract(candidate, zip_destination)
                data_files.extend(
                    item for item in zip_destination.rglob("*")
                    if item.is_file() and item.suffix.lower() in {".html", ".json"}
                )
            elif suffix in {".html", ".json"}:
                data_files.append(candidate)
    return data_files


def _deduplicate(items):
    by_username = {}
    for item in items:
        username = item["username"]
        previous = by_username.get(username)
        if previous is None or item["date"] < previous["date"]:
            by_username[username] = item
    return list(by_username.values())


def process_import_paths(paths, user_id, analysis_run_id=None):
    """Process an upload batch without retaining any source file on the server."""
    with TemporaryDirectory(prefix="instagram-analysis-") as extraction_root:
        files = _discover_data_files(paths, extraction_root)
        grouped = {}
        for path in files:
            kind = detect_kind(str(path), path.name)
            if not kind:
                continue
            parsed = parse_instagram_file(str(path))
            grouped.setdefault(kind, []).extend(parsed)

        if not grouped:
            raise ValueError("Nenhum arquivo de relacionamentos do Instagram foi reconhecido.")

        summary = ImportSummary(files_read=len(files))
        observed_at = datetime.now()
        for kind in ("followers", "following"):
            if kind not in grouped:
                continue
            items = _deduplicate(grouped.get(kind, []))
            gained_names, missing_names, reappeared_names = save_followers(
                kind,
                items,
                user_id,
                commit=False,
                observed_at=observed_at,
            )
            for username in gained_names:
                db.session.add(
                    History(user_id=user_id, username=username, action="gained", kind=kind, date_added=observed_at)
                )
            for username in missing_names:
                db.session.add(
                    History(user_id=user_id, username=username, action="missing", kind=kind, date_added=observed_at)
                )
            for username in reappeared_names:
                db.session.add(
                    History(user_id=user_id, username=username, action="reappeared", kind=kind, date_added=observed_at)
                )
            validation_candidates = Follower.query.filter(
                Follower.user_id == user_id,
                Follower.kind == kind,
                Follower.is_present.is_(False),
                Follower.state == "pending",
                Follower.validation_status.in_(("idle", "completed")),
            ).all()
            for follower in validation_candidates:
                _, created = enqueue_relationship_validation(
                    follower,
                    analysis_run_id=analysis_run_id,
                    observed_at=observed_at,
                )
                if created:
                    summary.validation_total += 1
            summary.gained += len(gained_names)
            summary.reappeared += len(reappeared_names)
            if kind == "followers":
                summary.followers_found = len(items)
                summary.followers_gained = len(gained_names)
                summary.followers_missing = len(missing_names)
                summary.followers_reappeared = len(reappeared_names)
                summary.gained_usernames = gained_names
                summary.lost_usernames = missing_names
                summary.reappeared_usernames = reappeared_names
            else:
                summary.following_found = len(items)

        for item in _deduplicate(grouped.get("recently_unfollowed", [])):
            exists = History.query.filter_by(
                user_id=user_id,
                username=item["username"],
                kind="following",
            ).filter(History.action.in_(("missing", "lost"))).first()
            if not exists:
                db.session.add(
                    History(
                        user_id=user_id,
                        username=item["username"],
                        action="missing",
                        kind="following",
                        date_added=item["date"],
                    )
                )
                summary.lost += 1

        db.session.commit()
        return summary
