import hashlib
import json
import os
import stat
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZipFile

from bs4 import BeautifulSoup
from flask import current_app

from database import db
from models.follower import Follower
from models.history import History
from models.system import ImportObservation
from services.clock import local_now, utc_now
from services.parser import detect_kind, parse_instagram_file
from services.profile_validation import enqueue_profile_scan, enqueue_relationship_validation
from services.storage import merge_follower_delta, save_followers


MAX_ARCHIVE_FILES = int(os.environ.get("MAX_ARCHIVE_FILES", "10000"))
MAX_ARCHIVE_UNCOMPRESSED_BYTES = int(os.environ.get("MAX_ARCHIVE_UNCOMPRESSED_BYTES", str(1024 * 1024 * 1024)))
EXTRACTED_SUFFIXES = {".html", ".json"}
RELATIONSHIP_KINDS = ("followers", "following")
CONCLUSIVE_PROFILE_STATUSES = ("exists", "unavailable")


def _is_metadata_path(path):
    parts = Path(path).parts
    return "__MACOSX" in parts or any(part.startswith("._") for part in parts)


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
    mode: str = "snapshot"
    note: str = ""
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
                "Esta exportação é idêntica à última importada; "
                "nada foi alterado."
            )
        prefix = "Atualização diária" if self.mode == "delta" else "Retrato completo"
        parts = [
            f"{prefix}: {self.files_read} arquivo(s) analisado(s), "
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
        if self.note:
            parts.append(self.note)
        return ". ".join(parts) + "."


def _safe_extract(archive_path, destination):
    """Extract only the HTML/JSON members, enforcing limits on real bytes written."""
    destination = Path(destination).resolve()
    try:
        with ZipFile(archive_path) as archive:
            members = [
                member for member in archive.infolist()
                if not _is_metadata_path(member.filename)
                and (member.is_dir() or Path(member.filename).suffix.lower() in EXTRACTED_SUFFIXES)
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
                    if item.is_file()
                    and item.suffix.lower() in EXTRACTED_SUFFIXES
                    and not _is_metadata_path(item)
                )
            elif suffix in EXTRACTED_SUFFIXES and not _is_metadata_path(candidate):
                data_files.append(candidate)
    return data_files


def _inspect_export_manifests(files):
    """Classify Meta exports as complete snapshots or bounded deltas.

    ``start_here.html`` is the only authoritative metadata bundled by Meta.
    A bounded date range is an event slice: its entries can be merged, but
    omissions can never be interpreted as relationship removals.
    """
    def is_manifest(path):
        return path.name.casefold().endswith("start_here.html")

    available_kinds = {
        kind
        for path in files
        if not is_manifest(path)
        for kind in (detect_kind(str(path), path.name),)
        if kind
    }
    mode = "snapshot"
    manifests = [path for path in files if is_manifest(path)]
    for manifest in manifests:
        with manifest.open("r", encoding="utf-8") as handle:
            soup = BeautifulSoup(handle.read(), "html.parser")
        text = " ".join(soup.get_text(" ", strip=True).casefold().split())
        bounded_range = (
            ("contém os dados de " in text and " que você solicitou" in text)
            or ("contem os dados de " in text and " que voce solicitou" in text)
            or ("contains data from " in text and " that you requested" in text)
        )
        if bounded_range:
            mode = "delta"
            continue

        referenced_kinds = {
            kind
            for link in soup.find_all("a", href=True)
            for kind in (detect_kind("", Path(link["href"]).name),)
            if kind in RELATIONSHIP_KINDS
        }
        missing = referenced_kinds - available_kinds
        if missing:
            labels = {"followers": "seguidores", "following": "seguindo"}
            names = " e ".join(labels[kind] for kind in RELATIONSHIP_KINDS if kind in missing)
            raise ImportRejected(
                f"A exportação está incompleta: o índice aponta para o arquivo de {names}, "
                "mas ele não está no ZIP. Nada foi alterado."
            )
    return mode


def _source_fingerprint(files):
    """Identify the logical export while ignoring ZIP/root-folder renames."""
    entries = []
    for path in files:
        content_digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                content_digest.update(chunk)
        if path.name.casefold().endswith("start_here.html"):
            logical_kind = "manifest"
        else:
            logical_kind = detect_kind(str(path), path.name) or path.suffix.casefold()
        entries.append((logical_kind, content_digest.hexdigest()))

    digest = hashlib.sha256()
    for logical_kind, content_hash in sorted(entries):
        digest.update(logical_kind.encode("utf-8", errors="replace"))
        digest.update(b"\0")
        digest.update(content_hash.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


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


def _is_duplicate(user_id, kind, fingerprint, source_fingerprint):
    window = timedelta(hours=current_app.config["IMPORT_DUPLICATE_WINDOW_HOURS"])
    observation = ImportObservation.query.filter_by(user_id=user_id, kind=kind).first()
    return bool(
        observation
        and observation.fingerprint == fingerprint
        and (
            (source_fingerprint and observation.source_fingerprint == source_fingerprint)
            or (not source_fingerprint and utc_now() - observation.observed_at < window)
        )
    )


def _remember_observation(user_id, kind, fingerprint, source_fingerprint):
    observation = ImportObservation.query.filter_by(user_id=user_id, kind=kind).first()
    if observation is None:
        observation = ImportObservation(user_id=user_id, kind=kind)
        db.session.add(observation)
    observation.fingerprint = fingerprint
    observation.source_fingerprint = source_fingerprint
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


def _queue_profile_validations(kind, known_existing, user_id, analysis_run_id, observed_utc, summary, newly_missing):
    """Queue a conclusion only for profiles that just left the list.

    A full snapshot repeats everything already known. Profiles that were
    already settled in an earlier import (``absent`` after their retest) are
    not looked up again; they return to ``present`` by themselves if a later
    export lists them. Profiles still ``pending`` without any validation
    (for example when web validation was disabled) are picked up as well.
    """
    newly_missing = {value.casefold() for value in newly_missing}
    candidates = Follower.query.filter(
        Follower.user_id == user_id,
        Follower.kind == kind,
        Follower.is_present.is_(False),
        Follower.state.in_(("pending", "absent")),
        Follower.validation_status.in_(("idle", "completed")),
    ).all()
    candidates = [
        follower for follower in candidates
        if follower.username.casefold() in newly_missing
        or (follower.state == "pending" and follower.validation_status == "idle")
    ]

    for follower in candidates:
        if follower.username.casefold() in known_existing:
            _, created = enqueue_relationship_validation(
                follower,
                analysis_run_id=analysis_run_id,
                observed_at=observed_utc,
                evidence="opposite_list",
            )
        elif current_app.config["PROFILE_VALIDATION_ENABLED"]:
            _, created = enqueue_relationship_validation(
                follower,
                analysis_run_id=analysis_run_id,
                observed_at=observed_utc,
                evidence="web",
            )
        else:
            created = False
        if created:
            summary.validation_total += 1


def _queue_profile_scans(user_id, analysis_run_id, observed_utc, summary, usernames=None):
    """Queue one asynchronous existence/avatar scan per profile not yet conclusively scanned.

    Profiles whose scan already concluded (exists/unavailable) keep their photo
    and status; re-importing a full snapshot must not fetch the whole list again.
    Profiles left half-scanned (cancelled, blocked or inconclusive) are scanned.
    """
    if not current_app.config["PROFILE_VALIDATION_ENABLED"] or not current_app.config["PROFILE_SCAN_ALL_ENABLED"]:
        return
    requested = {value.casefold() for value in usernames} if usernames is not None else None
    rows = Follower.query.filter_by(user_id=user_id).order_by(Follower.id.asc()).all()
    already_checked = {row.username.casefold() for row in rows if row.profile_status in CONCLUSIVE_PROFILE_STATUSES}
    seen = set(already_checked)
    for follower in rows:
        key = follower.username.casefold()
        if not follower.is_present or key in seen or (requested is not None and key not in requested):
            continue
        seen.add(key)
        _, created = enqueue_profile_scan(
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
        source_fingerprint = _source_fingerprint(files)
        mode = _inspect_export_manifests(files)
        grouped = {}
        recognized_files = 0
        for path in files:
            kind = detect_kind(str(path), path.name)
            if not kind:
                continue
            recognized_files += 1
            grouped.setdefault(kind, []).extend(parse_instagram_file(str(path)))

    if not grouped and mode != "delta":
        raise ValueError("Nenhum arquivo de relacionamentos do Instagram foi reconhecido.")

    deduplicated = {kind: _deduplicate(items) for kind, items in grouped.items()}
    summary = ImportSummary(mode=mode, files_read=recognized_files)
    if mode == "delta" and not any(kind in deduplicated for kind in RELATIONSHIP_KINDS):
        summary.note = (
            "Atualização diária analisada: nenhum evento de seguidores ou perfis seguidos "
            "foi incluído neste período. O retrato atual não foi alterado."
        )
        return summary

    incoming_relationship_kinds = {kind for kind in RELATIONSHIP_KINDS if kind in deduplicated}
    missing_baselines = {
        kind
        for kind in incoming_relationship_kinds
        if mode == "delta" and ImportObservation.query.filter_by(user_id=user_id, kind=kind).first() is None
    }
    if missing_baselines == incoming_relationship_kinds and missing_baselines:
        summary.note = (
            "Atualização diária reconhecida, mas ignorada porque ainda não existe uma base completa. "
            "Importe uma exportação manual com o período 'Desde o início'; nenhum dado foi alterado."
        )
        return summary
    if missing_baselines:
        labels = {"followers": "seguidores", "following": "seguindo"}
        summary.note = "Sem base completa, foram ignorados: " + ", ".join(
            labels[kind] for kind in RELATIONSHIP_KINDS if kind in missing_baselines
        )

    # Read-only checks first, so a rejected import changes nothing.
    fingerprints = {}
    for kind in RELATIONSHIP_KINDS:
        if kind not in deduplicated:
            continue
        if kind in missing_baselines:
            continue
        observation_kind = f"{kind}_delta" if mode == "delta" else kind
        fingerprints[kind] = _fingerprint(deduplicated[kind])
        if _is_duplicate(user_id, observation_kind, fingerprints[kind], source_fingerprint):
            summary.skipped_duplicates.append(kind)
            continue
        if mode == "snapshot" and not allow_large_drop:
            _check_large_drop(user_id, kind, deduplicated[kind])

    observed_local = local_now()
    observed_utc = utc_now()
    for kind in RELATIONSHIP_KINDS:
        if kind not in deduplicated or kind in summary.skipped_duplicates or kind in missing_baselines:
            continue
        items = deduplicated[kind]
        if mode == "delta":
            gained, missing, reappeared, followed_again = merge_follower_delta(
                kind, items, user_id, commit=False, observed_at=observed_local
            )
            observation_kind = f"{kind}_delta"
        else:
            gained, missing, reappeared, followed_again = save_followers(
                kind, items, user_id, commit=False, observed_at=observed_local
            )
            observation_kind = kind
        _record_history(user_id, kind, "gained", gained, observed_local)
        _record_history(user_id, kind, "missing", missing, observed_local)
        _record_history(user_id, kind, "reappeared", reappeared, observed_local)
        _record_history(user_id, kind, "followed_again", followed_again, observed_local)
        _remember_observation(user_id, observation_kind, fingerprints[kind], source_fingerprint)

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
        if mode == "snapshot":
            db.session.flush()
            _queue_profile_validations(
                kind, known_existing, user_id, analysis_run_id, observed_utc, summary, missing
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

    db.session.flush()
    scan_usernames = None
    if mode == "delta":
        scan_usernames = {
            item["username"]
            for kind in RELATIONSHIP_KINDS
            for item in deduplicated.get(kind, [])
            if kind not in missing_baselines
        }
    if not summary.is_duplicate:
        _queue_profile_scans(
            user_id,
            analysis_run_id,
            observed_utc,
            summary,
            usernames=scan_usernames,
        )
    db.session.commit()
    return summary
