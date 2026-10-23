import json
import os
import stat
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZipFile

from database import db
from models.follower import Follower
from models.history import History
from models.integration import AnalysisRun, RelationshipValidation
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
    followed_again: int = 0
    followers_gained: int = 0
    followers_lost: int = 0
    followers_missing: int = 0
    followers_reappeared: int = 0
    followers_followed_again: int = 0
    validation_total: int = 0
    gained_usernames: list = field(default_factory=list)
    lost_usernames: list = field(default_factory=list)
    reappeared_usernames: list = field(default_factory=list)
    followed_again_usernames: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)

    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False)

    def message(self):
        parts = [
            f"{self.files_read} arquivo(s) analisado(s), "
            f"{self.followers_found} seguidores e {self.following_found} perfis seguidos.",
            f"Seguidores: {self.followers_gained} novo(s)",
            f"{self.followers_missing} aguardando validação",
            f"{self.followers_lost} remoção(ões) confirmada(s)",
            f"{self.followers_reappeared} reaparecido(s)",
        ]
        if self.followers_followed_again:
            parts.append(f"{self.followers_followed_again} voltou(aram) a seguir")
        return ". ".join(parts) + "."


def _safe_extract(archive_path, destination):
    destination = Path(destination).resolve()
    try:
        with ZipFile(archive_path) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_FILES:
                raise ValueError("O arquivo ZIP excede o limite máximo de arquivos permitidos.")
            total_uncompressed = sum(member.file_size for member in members)
            if total_uncompressed > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise ValueError("O conteúdo descompactado do ZIP excede o tamanho máximo permitido.")
            for member in members:
                target_path = (destination / member.filename).resolve()
                if not str(target_path).startswith(str(destination)):
                    raise ValueError("O arquivo ZIP contém um caminho inválido.")
                if member.is_dir():
                    target_path.mkdir(parents=True, exist_ok=True)
                    continue
                target_path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, open(target_path, "wb") as target:
                    while chunk := source.read(64 * 1024):
                        target.write(chunk)
                os.chmod(target_path, stat.S_IREAD | stat.S_IWRITE)
    except BadZipFile as exc:
        raise ValueError("O arquivo enviado não é um ZIP válido.") from exc


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
            gained_names, missing_names, reappeared_names, followed_again_names = save_followers(
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
            for username in followed_again_names:
                db.session.add(
                    History(user_id=user_id, username=username, action="followed_again", kind=kind, date_added=observed_at)
                )

            # Prova inteligente de existência da conta através da lista oposta (seguidores vs seguindo)
            opposite_kind = "following" if kind == "followers" else "followers"
            opposite_incoming = {it["username"].casefold() for it in grouped.get(opposite_kind, [])}
            opposite_db = {
                f.username.casefold()
                for f in Follower.query.filter_by(user_id=user_id, kind=opposite_kind, is_present=True).all()
            }
            known_existing = opposite_incoming | opposite_db

            validation_candidates = Follower.query.filter(
                Follower.user_id == user_id,
                Follower.kind == kind,
                Follower.is_present.is_(False),
                Follower.state.in_(("pending", "absent")),
                Follower.validation_status.in_(("idle", "completed")),
            ).all()

            for follower in validation_candidates:
                if follower.username.casefold() in known_existing:
                    # Conta comprovadamente ativa na lista oposta; dispensa consulta web
                    if follower.missing_count >= 2:
                        follower.state = "removed"
                        outcome = "relationship_removed"
                        details = (
                            f"Existência comprovada pela presença em "
                            f"{'seguindo' if kind == 'followers' else 'seguidores'}. "
                            f"Remoção confirmada após 2 exportações completas."
                        )
                        lost_query = History.query.filter_by(
                            user_id=follower.user_id,
                            username=follower.username,
                            kind=follower.kind,
                            action="lost",
                        )
                        if follower.missing_since:
                            lost_query = lost_query.filter(History.date_added >= follower.missing_since)
                        exists_lost = lost_query.first()
                        if not exists_lost:
                            db.session.add(
                                History(
                                    user_id=follower.user_id,
                                    username=follower.username,
                                    kind=follower.kind,
                                    action="lost",
                                    date_added=observed_at,
                                )
                            )
                            if follower.kind == "followers":
                                summary.followers_lost += 1
                                if analysis_run_id:
                                    run = db.session.get(AnalysisRun, analysis_run_id)
                                    if run:
                                        run.followers_lost += 1
                    else:
                        follower.state = "pending"
                        outcome = "exists_waiting_confirmation"
                        details = (
                            f"Conta ativa em "
                            f"{'seguindo' if kind == 'followers' else 'seguidores'}. "
                            f"Comprovada sem consulta web. Aguardando 2ª exportação."
                        )

                    follower.validation_status = "completed"
                    follower.last_validation_result = outcome
                    follower.last_validated_at = observed_at

                    val = RelationshipValidation(
                        user_id=follower.user_id,
                        analysis_run_id=analysis_run_id,
                        follower_id=follower.id,
                        username=follower.username,
                        kind=follower.kind,
                        status="completed",
                        outcome=outcome,
                        attempts=0,
                        missing_count_snapshot=follower.missing_count,
                        next_attempt_at=observed_at,
                        last_attempt_at=observed_at,
                        completed_at=observed_at,
                        notification_sent_at=observed_at,
                        details=details,
                        created_at=observed_at,
                    )
                    db.session.add(val)
                    summary.validation_total += 1
                    if analysis_run_id:
                        run = db.session.get(AnalysisRun, analysis_run_id)
                        if run:
                            run.validation_total += 1
                            run.validation_completed += 1
                else:
                    _, created = enqueue_relationship_validation(
                        follower,
                        analysis_run_id=analysis_run_id,
                        observed_at=observed_at,
                    )
                    if created:
                        summary.validation_total += 1

            summary.gained += len(gained_names)
            summary.reappeared += len(reappeared_names)
            summary.followed_again += len(followed_again_names)
            if kind == "followers":
                summary.followers_found = len(items)
                summary.followers_gained = len(gained_names)
                summary.followers_missing = len(missing_names)
                summary.followers_reappeared = len(reappeared_names)
                summary.followers_followed_again = len(followed_again_names)
                summary.gained_usernames = gained_names
                summary.lost_usernames = missing_names
                summary.reappeared_usernames = reappeared_names
                summary.followed_again_usernames = followed_again_names
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
