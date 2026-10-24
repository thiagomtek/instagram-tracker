import hashlib
import json
import os
import stat
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZipFile

from flask import current_app

from database import db
from models.follower import Follower
from models.history import History
from models.integration import RelationshipValidation
from models.system import ImportObservation
from services.clock import local_now, utc_now
from services.parser import detect_kind, parse_instagram_file
from services.profile_validation import enqueue_relationship_validation, resolve_existing_profile
from services.storage import save_followers


MAX_ARCHIVE_FILES = int(os.environ.get("MAX_ARCHIVE_FILES", "10000"))
MAX_ARCHIVE_UNCOMPRESSED_BYTES = int(os.environ.get("MAX_ARCHIVE_UNCOMPRESSED_BYTES", str(1024 * 1024 * 1024)))
EXTRACTED_SUFFIXES = {".html", ".json"}
RELATIONSHIP_KINDS = ("followers", "following")


class ImportRejected(ValueError):
    """The import was refused before touching the database.

    ``needs_confirmation`` tells the caller that the user may repeat the
    import after explicitly confirming a large drop in the list.
    """

    def __init__(self, message, needs_confirmation=False):
        super().__init__(message)
        self.needs_confirmation = needs_confirmation


@dataclass
class ImportSummary:
    files_read: int = 0
    followers_found: int = 0
    following_found: int = 0
    gained: int = 0
    reappeared: int = 0
    followed_again: int = 0
    followers_gained: int = 0
    followers_lost: int = 0
    followers_missing: int = 0
    followers_reappeared: int = 0
    followers_followed_again: int = 0
    recent_unfollows: int = 0
    validation_total: int = 0
    validation_completed: int = 0
    gained_usernames: list = field(default_factory=list)
    missing_usernames: list = field(default_factory=list)
    reappeared_usernames: list = field(default_factory=list)
    followed_again_usernames: list = field(default_factory=list)
    skipped_duplicates: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)

    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @property
    def is_duplicate(self):
        """True when every relationship list in the upload was already imported."""
        return bool(self.skipped_duplicates) and not (self.followers_found or self.following_found)

    def message(self):
        if self.is_duplicate:
            return (
                "Esta exportação é idêntica à última importada recentemente; "
                "nada foi alterado."
            )
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
        if self.skipped_duplicates:
            parts.append("Listas idênticas à importação anterior foram ignoradas")
        return ". ".join(parts) + "."


def _safe_extract(archive_path, destination):
    """Extract only the HTML/JSON members, enforcing limits on real bytes written."""
    destination = Path(destination).resolve()
    try:
        with ZipFile(archive_path) as archive:
            members = [
                member for member in archive.infolist()
                if member.is_dir() or Path(member.filename).suffix.lower() in EXTRACTED_SUFFIXES
            ]
            if len(members) > MAX_ARCHIVE_FILES:
                raise ValueError("O arquivo ZIP excede o limite máximo de arquivos permitidos.")
            if sum(member.file_size for member in members) > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise ValueError("O conteúdo descompactado do ZIP excede o tamanho máximo permitido.")

            written_total = 0
            for member in members:
                target_path = (destination / member.filename).resolve()
                if not target_path.is_relative_to(destination):
                    raise ValueError("O arquivo ZIP contém um caminho inválido.")
                if member.is_dir():
                    target_path.mkdir(parents=True, exist_ok=True)
                    continue
                target_path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, open(target_path, "wb") as target:
                    while chunk := source.read(64 * 1024):
                        written_total += len(chunk)
                        if written_total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                            raise ValueError("O conteúdo descompactado do ZIP excede o tamanho máximo permitido.")
                        target.write(chunk)
                os.chmod(target_path, stat.S_IREAD | stat.S_IWRITE)
    except BadZipFile as exc:
        raise ValueError("O arquivo enviado não é um ZIP válido.") from exc


def _discover_data_files(paths, extraction_root):
    data_files = []
    archives = 0
    for raw_path in paths:
        path = Path(raw_path)
        candidates = list(path.rglob("*")) if path.is_dir() else [path]
        for candidate in candidates:
            if not candidate.is_file():
                continue
            suffix = candidate.suffix.lower()
            if suffix == ".zip":
                archives += 1
                zip_destination = Path(extraction_root) / f"archive-{archives}-{candidate.stem}"
                zip_destination.mkdir(parents=True, exist_ok=True)
                _safe_extract(candidate, zip_destination)
                data_files.extend(
                    item for item in zip_destination.rglob("*")
                    if item.is_file() and item.suffix.lower() in EXTRACTED_SUFFIXES
                )
            elif suffix in EXTRACTED_SUFFIXES:
                data_files.append(candidate)
    return data_files


def _deduplicate(items):
    """Keep one entry per username (the oldest known date wins)."""
    by_username = {}
    for item in items:
        key = item["username"].casefold()
        previous = by_username.get(key)
        if previous is None:
            by_username[key] = item
        elif item["date"] and (previous["date"] is None or item["date"] < previous["date"]):
            by_username[key] = item
    return list(by_username.values())


def _fingerprint(items):
    digest = hashlib.sha256()
    for username in sorted(item["username"].casefold() for item in items):
        digest.update(username.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _is_recent_duplicate(user_id, kind, fingerprint):
    window = timedelta(hours=current_app.config["IMPORT_DUPLICATE_WINDOW_HOURS"])
    observation = ImportObservation.query.filter_by(user_id=user_id, kind=kind).first()
    return bool(
        observation
        and observation.fingerprint == fingerprint
        and utc_now() - observation.observed_at < window
    )


def _remember_observation(user_id, kind, fingerprint):
    observation = ImportObservation.query.filter_by(user_id=user_id, kind=kind).first()
    if observation is None:
        observation = ImportObservation(user_id=user_id, kind=kind)
        db.session.add(observation)
    observation.fingerprint = fingerprint
    observation.observed_at = utc_now()


def _check_large_drop(user_id, kind, items):
    """Refuse snapshots that look empty or truncated instead of wiping the list."""
    present = {
        row.username.casefold()
        for row in Follower.query.filter_by(user_id=user_id, kind=kind, is_present=True).all()
    }
    if not present:
        return
    label = "seguidores" if kind == "followers" else "seguindo"
    incoming = {item["username"].casefold() for item in items}
    if not incoming:
        raise ImportRejected(
            f"O arquivo de {label} foi lido, mas não contém nenhum perfil. Nada foi alterado. "
            "Se a lista realmente ficou vazia, confirme a importação.",
            needs_confirmation=True,
        )
    dropped = len(present - incoming)
    ratio = dropped / len(present)
    config = current_app.config
    if dropped >= config["IMPORT_MASS_DROP_MIN"] and ratio >= config["IMPORT_MASS_DROP_RATIO"]:
        raise ImportRejected(
            f"A lista de {label} perderia {dropped} de {len(present)} perfis ({round(ratio * 100)}%). "
            "Isso costuma indicar uma exportação incompleta (por exemplo, faltando followers_2.html). "
            "Nada foi alterado. Se a queda é real, confirme a importação.",
            needs_confirmation=True,
        )


def _record_history(user_id, kind, action, usernames, observed_at):
    for username in usernames:
        db.session.add(History(user_id=user_id, username=username, action=action, kind=kind, date_added=observed_at))


def _resolve_known_profiles(kind, known_existing, user_id, analysis_run_id, observed_local, observed_utc, summary):
    """Settle missing profiles whose existence is proven by the opposite list;
    queue a web check for the rest."""
    opposite_label = "seguindo" if kind == "followers" else "seguidores"
    candidates = Follower.query.filter(
        Follower.user_id == user_id,
        Follower.kind == kind,
        Follower.is_present.is_(False),
        Follower.state.in_(("pending", "absent")),
        Follower.validation_status.in_(("idle", "completed")),
    ).all()

    for follower in candidates:
        if follower.username.casefold() in known_existing:
            outcome, newly_lost = resolve_existing_profile(follower, observed_local)
            if outcome == "relationship_removed":
                details = (
                    f"Existência comprovada pela presença em {opposite_label}. "
                    f"Remoção confirmada após {follower.missing_count} exportações completas."
                )
            else:
                details = (
                    f"Conta ativa em {opposite_label}. Comprovada sem consulta web. "
                    "Aguardando nova exportação."
                )
            if newly_lost and kind == "followers":
                summary.followers_lost += 1

            follower.validation_status = "completed"
            follower.last_validation_result = outcome
            follower.last_validated_at = observed_utc
            db.session.add(
                RelationshipValidation(
                    user_id=follower.user_id,
                    analysis_run_id=analysis_run_id,
                    follower_id=follower.id,
                    username=follower.username,
                    kind=follower.kind,
                    status="completed",
                    outcome=outcome,
                    attempts=0,
                    missing_count_snapshot=follower.missing_count,
                    next_attempt_at=observed_utc,
                    last_attempt_at=observed_utc,
                    completed_at=observed_utc,
                    notification_sent_at=observed_utc,
                    details=details,
                    created_at=observed_utc,
                )
            )
            summary.validation_total += 1
            summary.validation_completed += 1
        elif current_app.config["PROFILE_VALIDATION_ENABLED"]:
            _, created = enqueue_relationship_validation(
                follower,
                analysis_run_id=analysis_run_id,
                observed_at=observed_utc,
            )
            if created:
                summary.validation_total += 1


def process_import_paths(paths, user_id, analysis_run_id=None, allow_large_drop=False):
    """Process an upload batch without retaining any source file on the server."""
    with TemporaryDirectory(prefix="instagram-analysis-") as extraction_root:
        files = _discover_data_files(paths, extraction_root)
        grouped = {}
        for path in files:
            kind = detect_kind(str(path), path.name)
            if not kind:
                continue
            grouped.setdefault(kind, []).extend(parse_instagram_file(str(path)))

    if not grouped:
        raise ValueError("Nenhum arquivo de relacionamentos do Instagram foi reconhecido.")

    deduplicated = {kind: _deduplicate(items) for kind, items in grouped.items()}
    summary = ImportSummary(files_read=len(files))

    # Read-only checks first, so a rejected import changes nothing.
    fingerprints = {}
    for kind in RELATIONSHIP_KINDS:
        if kind not in deduplicated:
            continue
        fingerprints[kind] = _fingerprint(deduplicated[kind])
        if _is_recent_duplicate(user_id, kind, fingerprints[kind]):
            summary.skipped_duplicates.append(kind)
            continue
        if not allow_large_drop:
            _check_large_drop(user_id, kind, deduplicated[kind])

    observed_local = local_now()
    observed_utc = utc_now()
    for kind in RELATIONSHIP_KINDS:
        if kind not in deduplicated or kind in summary.skipped_duplicates:
            continue
        items = deduplicated[kind]
        gained, missing, reappeared, followed_again = save_followers(
            kind, items, user_id, commit=False, observed_at=observed_local
        )
        _record_history(user_id, kind, "gained", gained, observed_local)
        _record_history(user_id, kind, "missing", missing, observed_local)
        _record_history(user_id, kind, "reappeared", reappeared, observed_local)
        _record_history(user_id, kind, "followed_again", followed_again, observed_local)
        _remember_observation(user_id, kind, fingerprints[kind])

        # Existence proof from the opposite list. When both lists are in this
        # upload, the incoming one is authoritative; the database copy of the
        # opposite list may still hold profiles this very upload removed.
        opposite_kind = "following" if kind == "followers" else "followers"
        if opposite_kind in deduplicated:
            known_existing = {item["username"].casefold() for item in deduplicated[opposite_kind]}
        else:
            known_existing = {
                row.username.casefold()
                for row in Follower.query.filter_by(user_id=user_id, kind=opposite_kind, is_present=True).all()
            }
        db.session.flush()
        _resolve_known_profiles(
            kind, known_existing, user_id, analysis_run_id, observed_local, observed_utc, summary
        )

        summary.gained += len(gained)
        summary.reappeared += len(reappeared)
        summary.followed_again += len(followed_again)
        if kind == "followers":
            summary.followers_found = len(items)
            summary.followers_gained = len(gained)
            summary.followers_missing = len(missing)
            summary.followers_reappeared = len(reappeared)
            summary.followers_followed_again = len(followed_again)
            summary.gained_usernames = gained
            summary.missing_usernames = missing
            summary.reappeared_usernames = reappeared
            summary.followed_again_usernames = followed_again
        else:
            summary.following_found = len(items)

    for item in deduplicated.get("recently_unfollowed", []):
        already_recorded = History.query.filter_by(
            user_id=user_id,
            username=item["username"],
            kind="following",
        ).filter(History.action.in_(("missing", "lost"))).first()
        if not already_recorded:
            db.session.add(
                History(
                    user_id=user_id,
                    username=item["username"],
                    action="missing",
                    kind="following",
                    date_added=item["date"] or observed_local,
                )
            )
            summary.recent_unfollows += 1

    db.session.commit()
    return summary
