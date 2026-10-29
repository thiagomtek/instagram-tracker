import random
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlparse

import requests
from flask import current_app
from sqlalchemy import case, func

from database import db
from models.follower import Follower
from models.history import History
from models.integration import AnalysisRun, RelationshipValidation
from services.clock import local_now, utc_now
from services.network import (
    avoid_vpn,
    get_status,
    can_leave_vpn_when_blocked,
    current_route,
    return_to_vpn,
    save_block_state,
    web_checks_blocked_by_route,
)
from services.notifications import send_profile_validation_notification


ACTIVE_STATUSES = ("queued", "retry", "running")
FINISHED_STATUSES = ("completed", "cancelled")

@dataclass
class ProfileCheck:
    outcome: str
    details: str
    avatar_data: bytes | None = None
    avatar_mime: str | None = None
    # Route used for the request: "vpn" or "direct".
    route: str | None = None


def enqueue_relationship_validation(follower, analysis_run_id=None, observed_at=None, evidence="web"):
    """Queue a web check. ``observed_at`` is naive UTC."""
    active = RelationshipValidation.query.filter(
        RelationshipValidation.follower_id == follower.id,
        RelationshipValidation.purpose == "relationship",
        RelationshipValidation.status.in_(ACTIVE_STATUSES),
    ).first()
    if active:
        active.missing_count_snapshot = follower.missing_count
        if evidence == "opposite_list":
            active.evidence = evidence
        return active, False

    now = observed_at or utc_now()
    validation = RelationshipValidation(
        user_id=follower.user_id,
        analysis_run_id=analysis_run_id,
        follower_id=follower.id,
        username=follower.username,
        kind=follower.kind,
        purpose="relationship",
        evidence=evidence,
        status="queued",
        missing_count_snapshot=follower.missing_count,
        next_attempt_at=now,
    )
    follower.validation_status = "queued"
    db.session.add(validation)
    return validation, True


def enqueue_profile_scan(follower, analysis_run_id=None, observed_at=None):
    """Queue one existence/avatar scan per unique username."""
    active = RelationshipValidation.query.filter(
        RelationshipValidation.user_id == follower.user_id,
        RelationshipValidation.purpose == "profile_scan",
        func.lower(RelationshipValidation.username) == follower.username.casefold(),
        RelationshipValidation.status.in_(ACTIVE_STATUSES),
    ).first()
    if active:
        return active, False

    now = observed_at or utc_now()
    validation = RelationshipValidation(
        user_id=follower.user_id,
        analysis_run_id=analysis_run_id,
        follower_id=follower.id,
        username=follower.username,
        kind=follower.kind,
        purpose="profile_scan",
        evidence="web",
        status="queued",
        missing_count_snapshot=0,
        next_attempt_at=now,
    )
    db.session.add(validation)
    return validation, True


def enqueue_existing_profile_scans():
    """Queue a two-pass scan for profiles imported before this feature existed."""
    if not current_app.config["PROFILE_VALIDATION_ENABLED"] or not current_app.config["PROFILE_SCAN_ALL_ENABLED"]:
        return 0
    rows = Follower.query.filter_by(is_present=True).filter(
        Follower.profile_checked_at.is_(None)
    ).order_by(Follower.user_id.asc(), Follower.id.asc()).all()
    by_user = {}
    for follower in rows:
        by_user.setdefault(follower.user_id, []).append(follower)

    total_created = 0
    now = utc_now()
    for user_id, user_rows in by_user.items():
        active_usernames = {
            username.casefold()
            for (username,) in db.session.query(RelationshipValidation.username).filter(
                RelationshipValidation.user_id == user_id,
                RelationshipValidation.purpose == "profile_scan",
                RelationshipValidation.status.in_(ACTIVE_STATUSES),
            ).all()
        }
        candidates = []
        seen = set()
        for follower in user_rows:
            key = follower.username.casefold()
            if key in seen or key in active_usernames:
                continue
            seen.add(key)
            candidates.append(follower)
        if not candidates:
            continue

        run = AnalysisRun(
            user_id=user_id,
            source="profile_scan",
            source_name="Varredura de perfis já importados",
            status="success",
            completed_at=now,
        )
        db.session.add(run)
        db.session.flush()
        created = 0
        for follower in candidates:
            _, was_created = enqueue_profile_scan(
                follower,
                analysis_run_id=run.id,
                observed_at=now,
            )
            created += int(was_created)
        run.validation_total = created
        total_created += created
    if total_created:
        db.session.commit()
    return total_created


def cancel_relationship_validations(follower):
    now = utc_now()
    validations = RelationshipValidation.query.filter(
        RelationshipValidation.follower_id == follower.id,
        RelationshipValidation.purpose == "relationship",
        RelationshipValidation.status.in_(ACTIVE_STATUSES),
    ).all()
    for validation in validations:
        validation.status = "cancelled"
        validation.outcome = "reappeared"
        validation.completed_at = now
        validation.details = "O perfil reapareceu na exportação antes da confirmação."
        _increment_completed(validation.analysis_run_id)
    return len(validations)


def record_confirmed_loss(follower, observed_at):
    """Add the 'lost' history entry once per absence. ``observed_at`` is local time."""
    existing = History.query.filter_by(
        user_id=follower.user_id,
        username=follower.username,
        kind=follower.kind,
        action="lost",
    )
    if follower.missing_since:
        existing = existing.filter(History.date_added >= follower.missing_since)
    if existing.first():
        return False
    db.session.add(
        History(
            user_id=follower.user_id,
            username=follower.username,
            kind=follower.kind,
            action="lost",
            date_added=observed_at,
        )
    )
    return True


def resolve_existing_profile(follower, observed_at):
    """Apply the rule for a missing profile that is known to exist.

    Returns ``(outcome, newly_lost)``. ``observed_at`` is local time because it
    is written to the history.
    """
    # The export is a current snapshot. Once the profile's existence is
    # positively proved, its absence from that snapshot is enough to confirm
    # the relationship removal; a second export would only repeat the proof.
    follower.retest_count = 0
    follower.state = "removed"
    return "relationship_removed", record_confirmed_loss(follower, observed_at)


BROWSER_IDENTITIES = (
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36",
        "viewport": {"width": 1366, "height": 768},
        "locale": "pt-BR",
        "accept_language": "pt-BR,pt;q=0.9,en;q=0.8",
    },
    {
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/123.0.0.0 Safari/537.36",
        "viewport": {"width": 1440, "height": 900},
        "locale": "pt-BR",
        "accept_language": "pt-BR,pt;q=0.8,en-US;q=0.5,en;q=0.3",
    },
    {
        "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/122.0.0.0 Safari/537.36",
        "viewport": {"width": 1280, "height": 800},
        "locale": "en-US",
        "accept_language": "en-US,en;q=0.9,pt-BR;q=0.7",
    },
    {
        "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
                      "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
        "viewport": {"width": 390, "height": 844},
        "locale": "pt-BR",
        "accept_language": "pt-BR,pt;q=0.9",
        "is_mobile": True,
    },
    {
        "user_agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Mobile Safari/537.36",
        "viewport": {"width": 412, "height": 915},
        "locale": "pt-BR",
        "accept_language": "pt-BR,pt;q=0.9,en;q=0.7",
        "is_mobile": True,
    },
)

# Process-wide state of the (single) worker: which identity currently works,
# and when web checks may resume after Instagram blocked every identity.
_identity_state = {"index": 0, "blocked_until": None, "consecutive_blocks": 0}


def _next_identity_index(current):
    """Pick a different identity than ``current`` at random."""
    choices = [index for index in range(len(BROWSER_IDENTITIES)) if index != current]
    return random.choice(choices) if choices else current


def web_checks_paused_until():
    """UTC time before which no Instagram request should be made, or None."""
    blocked_until = _identity_state["blocked_until"]
    if blocked_until and blocked_until > utc_now():
        return blocked_until
    return None


def register_block():
    """Start (or extend) the cooldown after every browser identity was blocked."""
    config = current_app.config
    _identity_state["consecutive_blocks"] += 1
    delay = min(
        config["PROFILE_VALIDATION_BLOCK_COOLDOWN_SECONDS"] * 2 ** (_identity_state["consecutive_blocks"] - 1),
        config["PROFILE_VALIDATION_BLOCK_COOLDOWN_MAX_SECONDS"],
    )
    _identity_state["blocked_until"] = utc_now() + timedelta(seconds=delay)
    return _identity_state["blocked_until"]


# Web checks are spaced by a random pause so Instagram's rate limit is not tripped.
_pacing = {"next_at": 0.0}


def web_check_spacing_active():
    return time.monotonic() < _pacing["next_at"]


def _space_next_web_check():
    config = current_app.config
    low = config["PROFILE_VALIDATION_MIN_INTERVAL_SECONDS"]
    high = max(low, config["PROFILE_VALIDATION_MAX_INTERVAL_SECONDS"])
    _pacing["next_at"] = time.monotonic() + random.uniform(low, high)


def restore_block_state():
    """Resume a pause that was running when the worker restarted."""
    status = get_status()
    if status.blocked_until and status.blocked_until > utc_now():
        _identity_state["blocked_until"] = status.blocked_until
        _identity_state["consecutive_blocks"] = status.consecutive_blocks or 1
    elif status.blocked_until or status.consecutive_blocks:
        status.blocked_until = None
        db.session.commit()


def register_unblocked():
    """Clear the block state; returns True when there was one to clear."""
    had_block = bool(_identity_state["consecutive_blocks"] or _identity_state["blocked_until"])
    _identity_state["consecutive_blocks"] = 0
    _identity_state["blocked_until"] = None
    return had_block


def inspect_instagram_profile(username):
    """Check a profile, swapping the browser identity when Instagram blocks the request."""
    # While Instagram is already blocking, one probe per check is enough: more
    # identities on the same address only add requests to a rate limit.
    attempts = 1 if _identity_state["consecutive_blocks"] else max(
        1, current_app.config["PROFILE_VALIDATION_IDENTITY_ROTATIONS"]
    )
    route, proxy = current_route()
    for attempt in range(attempts):
        index = _identity_state["index"]
        check = _render_instagram_profile(username, BROWSER_IDENTITIES[index], proxy)
        check.route = route
        if check.outcome != "blocked":
            return check
        current_app.logger.warning(
            "Instagram bloqueou a consulta de @%s com a identidade %s; trocando de identidade (%s/%s).",
            username, index, attempt + 1, attempts,
        )
        _identity_state["index"] = _next_identity_index(index)
    return check


def _render_instagram_profile(username, identity, proxy=None):
    """Render a public profile and return only strong, user-visible signals."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    url = f"https://www.instagram.com/{username}/"
    unavailable_markers = (
        "profile isn't available",
        "perfil não está disponível",
        "sorry, this page isn't available",
        "the link you followed may be broken",
        "o link pode estar corrompido",
        "o perfil pode ter sido removido",
    )
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=["--disable-dev-shm-usage", "--no-sandbox"],
                proxy={"server": proxy} if proxy else None,
            )
            try:
                context = browser.new_context(
                    user_agent=identity["user_agent"],
                    viewport=identity["viewport"],
                    locale=identity["locale"],
                    is_mobile=identity.get("is_mobile", False),
                    has_touch=identity.get("is_mobile", False),
                    extra_http_headers={"Accept-Language": identity["accept_language"]},
                )
                # Existence is read from text; the photo comes from og:image and is
                # downloaded separately, so page images, media and fonts are dead weight.
                context.route(
                    "**/*",
                    lambda route: route.abort()
                    if route.request.resource_type in ("image", "media", "font")
                    else route.continue_(),
                )
                page = context.new_page()
                response = page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=current_app.config["PROFILE_VALIDATION_PAGE_TIMEOUT_MS"],
                )
                try:
                    page.wait_for_load_state(
                        "networkidle",
                        timeout=current_app.config["PROFILE_VALIDATION_PAGE_TIMEOUT_MS"],
                    )
                except PlaywrightTimeoutError:
                    pass
                try:
                    avatar_url = page.locator('meta[property="og:image"]').get_attribute(
                        "content", timeout=2_000
                    )
                except PlaywrightTimeoutError:
                    avatar_url = None
                body = page.locator("body").inner_text(timeout=10_000)
                status = response.status if response else None
                redirected_to_login = "/accounts/login" in page.url
            finally:
                browser.close()
    except PlaywrightTimeoutError:
        return ProfileCheck("inconclusive", "O Instagram não terminou de carregar a página.")
    except Exception as exc:
        current_app.logger.warning("Falha ao renderizar o perfil @%s: %s", username, exc)
        return ProfileCheck("inconclusive", "A página não pôde ser renderizada pelo validador.")

    normalized = " ".join(body.casefold().split())
    blocked = ProfileCheck("blocked", "O Instagram bloqueou ou limitou temporariamente a consulta.")
    if status in (403, 429) or any(marker in normalized for marker in ("try again later", "tente novamente mais tarde")):
        return blocked
    if any(marker in normalized for marker in unavailable_markers):
        return ProfileCheck("unavailable", "O Instagram informou que o perfil não está disponível.")

    exact_username = re.search(rf"(?<![\w.]){re.escape(username.casefold())}(?![\w.])", normalized)
    relationship_labels = ("seguidores", "seguindo", "followers", "following")
    if exact_username and any(label in normalized for label in relationship_labels):
        avatar_data, avatar_mime = _download_profile_image(avatar_url, proxy)
        return ProfileCheck(
            "exists",
            "O perfil foi renderizado com username e dados do cabeçalho.",
            avatar_data,
            avatar_mime,
        )

    if redirected_to_login:
        return blocked
    return ProfileCheck("inconclusive", "A resposta não comprovou existência nem indisponibilidade.")


def _download_profile_image(url, proxy=None):
    """Best-effort, size-limited download from Instagram's image CDNs."""
    if not url:
        return None, None

    def allowed(candidate):
        hostname = (urlparse(candidate).hostname or "").casefold()
        return any(
            hostname == suffix or hostname.endswith(f".{suffix}")
            for suffix in ("cdninstagram.com", "fbcdn.net")
        )

    if not allowed(url):
        return None, None
    try:
        with requests.get(
            url,
            stream=True,
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
            proxies={"http": proxy, "https": proxy} if proxy else None,
        ) as response:
            response.raise_for_status()
            if not allowed(response.url):
                return None, None
            mime = response.headers.get("Content-Type", "").split(";", 1)[0].casefold()
            if mime not in {"image/jpeg", "image/png", "image/webp"}:
                return None, None
            chunks = []
            size = 0
            for chunk in response.iter_content(64 * 1024):
                size += len(chunk)
                if size > 1024 * 1024:
                    return None, None
                chunks.append(chunk)
            return b"".join(chunks), mime
    except requests.RequestException as exc:
        current_app.logger.info("Foto de @%s não pôde ser armazenada: %s", urlparse(url).path, exc)
        return None, None


def _increment_completed(analysis_run_id):
    if not analysis_run_id:
        return
    run = db.session.get(AnalysisRun, analysis_run_id)
    if run:
        run.validation_completed = min(run.validation_total, run.validation_completed + 1)


def _complete(validation, follower, outcome, details, now):
    validation.status = "completed"
    validation.outcome = outcome
    validation.details = details
    validation.completed_at = now
    follower.validation_status = "completed"
    follower.last_validation_result = outcome
    follower.last_validated_at = now
    _increment_completed(validation.analysis_run_id)


def _schedule_retest(validation, follower, outcome, details, now):
    follower.state = "absent"
    follower.retest_count = 1
    follower.validation_status = "queued"
    follower.last_validation_result = outcome
    follower.last_validated_at = now
    validation.status = "retry"
    validation.outcome = outcome
    validation.details = details
    validation.next_attempt_at = now


def settle_confirmed_scans(now):
    """Complete scans whose first pass already proved the profile exists.

    Older versions always asked twice; the second request adds nothing once
    existence is proved and only costs rate limit.
    """
    pending = RelationshipValidation.query.filter(
        RelationshipValidation.purpose == "profile_scan",
        RelationshipValidation.status.in_(("queued", "retry")),
        RelationshipValidation.outcome == "profile_exists_first_pass",
    ).all()
    for validation in pending:
        for row in Follower.query.filter(
            Follower.user_id == validation.user_id,
            func.lower(Follower.username) == validation.username.casefold(),
        ).all():
            row.profile_status = "exists"
        validation.status = "completed"
        validation.outcome = "profile_exists"
        validation.details = "Perfil comprovado na primeira consulta."
        validation.completed_at = now
        _increment_completed(validation.analysis_run_id)
    if pending:
        db.session.commit()
    return len(pending)


def recover_stale_validations(now):
    """Re-queue checks left 'running' by a worker that died mid-check."""
    stale_after = timedelta(seconds=int(current_app.config["PROFILE_VALIDATION_STALE_SECONDS"]))
    stale = RelationshipValidation.query.filter(
        RelationshipValidation.status == "running",
        RelationshipValidation.last_attempt_at < now - stale_after,
    ).all()
    for validation in stale:
        validation.status = "retry"
        validation.outcome = validation.outcome or "inconclusive"
        validation.details = "A consulta anterior foi interrompida; nova tentativa agendada."
        validation.next_attempt_at = now
        follower = db.session.get(Follower, validation.follower_id)
        if validation.purpose == "relationship" and follower and follower.validation_status == "verifying":
            follower.validation_status = "queued"
    if stale:
        db.session.commit()
    return len(stale)


def process_next_validation():
    """Process at most one due validation so requests remain serialized."""
    now = utc_now()
    recover_stale_validations(now)
    settle_confirmed_scans(now)
    due_filters = [
        RelationshipValidation.status.in_(("queued", "retry")),
        RelationshipValidation.next_attempt_at <= now,
    ]
    if (
        not current_app.config["PROFILE_VALIDATION_ENABLED"]
        or web_checks_paused_until()
        or web_check_spacing_active()
        or web_checks_blocked_by_route()
    ):
        # Deterministic proofs from the opposite list remain asynchronous and
        # do not access Instagram, even when web validation is disabled or
        # paused after Instagram blocked every browser identity.
        due_filters.append(RelationshipValidation.evidence == "opposite_list")
    validation = (
        RelationshipValidation.query.filter(*due_filters)
        .order_by(
            # Removal checks first, then finish profiles already started (their
            # second pass) before opening new ones, then oldest first.
            case((RelationshipValidation.purpose == "relationship", 0), else_=1),
            case((RelationshipValidation.status == "retry", 0), else_=1),
            RelationshipValidation.next_attempt_at.asc(),
            RelationshipValidation.id.asc(),
        )
        .first()
    )
    if not validation:
        notify_finished_validation_batches()
        return 0

    validation.status = "running"
    validation.attempts += 1
    validation.last_attempt_at = now
    follower = db.session.get(Follower, validation.follower_id)
    if follower and validation.purpose == "relationship":
        follower.validation_status = "verifying"
    db.session.commit()

    if validation.evidence == "opposite_list":
        opposite_label = "seguindo" if validation.kind == "followers" else "seguidores"
        check = ProfileCheck(
            "exists",
            f"Existência comprovada pela presença na lista de {opposite_label} da mesma exportação.",
        )
    else:
        check = inspect_instagram_profile(validation.username)
        _space_next_web_check()
    db.session.expire_all()
    validation = db.session.get(RelationshipValidation, validation.id)
    follower = db.session.get(Follower, validation.follower_id)
    now = utc_now()

    if validation.evidence != "opposite_list":
        if check.outcome == "blocked" and check.route == "vpn" and can_leave_vpn_when_blocked():
            # The VPN address is the problem, not the profile: try the direct
            # network right away and come back to the VPN later.
            avoid_vpn(current_app.config["PROFILE_VALIDATION_VPN_AVOID_SECONDS"])
            if validation.status == "running":
                validation.attempts = max(0, validation.attempts - 1)
                validation.status = "retry"
                validation.outcome = "blocked"
                validation.details = "O Instagram bloqueou o IP da VPN; nova tentativa pela rede direta."
                validation.next_attempt_at = now
                if follower and validation.purpose == "relationship":
                    follower.validation_status = "queued"
                db.session.commit()
                return True
        elif check.outcome == "blocked":
            # Both routes are blocked: wait, and start over from the VPN afterwards.
            return_to_vpn()
            resume_at = register_block()
            save_block_state(resume_at, _identity_state["consecutive_blocks"])
            if validation.status == "running":
                # A block says nothing about the profile: it does not spend an attempt.
                validation.attempts = max(0, validation.attempts - 1)
                validation.status = "retry"
                validation.outcome = "blocked"
                validation.details = (
                    f"{check.details} Consultas web pausadas e nova tentativa agendada."
                )
                validation.next_attempt_at = resume_at
                if follower and validation.purpose == "relationship":
                    follower.validation_status = "queued"
                db.session.commit()
                return True
        elif register_unblocked():
            save_block_state(None, 0)

    if validation.purpose == "profile_scan":
        if validation.status == "cancelled" or not follower:
            if validation.status != "cancelled":
                validation.status = "cancelled"
                validation.outcome = "profile_missing_locally"
                validation.details = "O perfil não existe mais na base local."
                validation.completed_at = now
                _increment_completed(validation.analysis_run_id)
        else:
            related_rows = Follower.query.filter(
                Follower.user_id == validation.user_id,
                func.lower(Follower.username) == validation.username.casefold(),
            ).all()
            for row in related_rows:
                row.profile_checked_at = now
                if check.avatar_data:
                    row.avatar_data = check.avatar_data
                    row.avatar_mime = check.avatar_mime

            # A profile that exists needs no confirmation; anything else is checked twice.
            if validation.attempts < 2 and check.outcome != "exists":
                for row in related_rows:
                    row.profile_status = "checking"
                validation.status = "retry"
                validation.outcome = f"profile_{check.outcome}_first_pass"
                validation.details = f"Primeira de duas consultas: {check.details}"
                validation.next_attempt_at = now
            else:
                first_outcome = validation.outcome or ""
                if check.outcome == "exists" or first_outcome == "profile_exists_first_pass":
                    final_status = "exists"
                    final_outcome = "profile_exists"
                elif check.outcome == "unavailable" or first_outcome == "profile_unavailable_first_pass":
                    final_status = "unavailable"
                    final_outcome = "profile_unavailable"
                else:
                    final_status = "inconclusive"
                    final_outcome = "profile_inconclusive"
                for row in related_rows:
                    row.profile_status = final_status
                validation.status = "completed"
                validation.outcome = final_outcome
                validation.details = (
                    check.details if validation.attempts < 2 else f"Segunda de duas consultas: {check.details}"
                )
                validation.completed_at = now
                _increment_completed(validation.analysis_run_id)
        db.session.commit()
        notify_finished_validation_batches()
        return True

    if follower and check.avatar_data:
        for row in Follower.query.filter(
            Follower.user_id == validation.user_id,
            func.lower(Follower.username) == validation.username.casefold(),
        ).all():
            row.avatar_data = check.avatar_data
            row.avatar_mime = check.avatar_mime

    if validation.status == "cancelled" or not follower or follower.is_present:
        if validation.status != "cancelled":
            validation.status = "cancelled"
            validation.outcome = "reappeared"
            validation.details = "O perfil reapareceu antes da validação terminar."
            validation.completed_at = now
            _increment_completed(validation.analysis_run_id)
        db.session.commit()
        notify_finished_validation_batches()
        return True

    if check.outcome == "exists":
        outcome, newly_lost = resolve_existing_profile(follower, local_now())
        if newly_lost and validation.analysis_run_id and follower.kind == "followers":
            run = db.session.get(AnalysisRun, validation.analysis_run_id)
            if run:
                run.followers_lost += 1
        _complete(validation, follower, outcome, check.details, now)
    elif check.outcome == "unavailable":
        if follower.retest_count == 0:
            _schedule_retest(
                validation, follower, "unavailable_pending_retest",
                "Perfil indisponível no Instagram. 1 reteste agendado para verificar reativação.", now,
            )
        else:
            follower.state = "absent"
            _complete(
                validation, follower, "untestable",
                "Perfil permaneceu indisponível após 1 reteste e continuará em Ausentes. "
                "Pode ter sido excluído, desativado ou ter bloqueado você.",
                now,
            )
    elif validation.attempts < current_app.config["PROFILE_VALIDATION_MAX_ATTEMPTS"]:
        validation.status = "retry"
        validation.outcome = "inconclusive"
        validation.details = check.details
        validation.next_attempt_at = now
        follower.validation_status = "queued"
        follower.last_validation_result = "inconclusive"
        follower.last_validated_at = now
    elif follower.retest_count == 0:
        _schedule_retest(
            validation, follower, "inconclusive_pending_retest",
            f"Consultas inconclusivas ({check.details}). 1 reteste agendado.", now,
        )
    else:
        follower.state = "absent"
        _complete(
            validation, follower, "untestable",
            f"Consultas inconclusivas persistentes ({check.details}). Mantido em Ausentes.",
            now,
        )

    db.session.commit()
    notify_finished_validation_batches()
    return True


def notify_finished_validation_batches():
    run_ids = [
        value
        for (value,) in db.session.query(RelationshipValidation.analysis_run_id)
        .filter(
            RelationshipValidation.analysis_run_id.is_not(None),
            RelationshipValidation.notification_sent_at.is_(None),
            RelationshipValidation.status.in_(FINISHED_STATUSES),
        )
        .distinct()
        .all()
    ]
    now = utc_now()
    for run_id in run_ids:
        unfinished = RelationshipValidation.query.filter(
            RelationshipValidation.analysis_run_id == run_id,
            RelationshipValidation.status.in_(ACTIVE_STATUSES),
        ).count()
        if unfinished:
            continue
        validations = RelationshipValidation.query.filter_by(analysis_run_id=run_id).all()
        if not validations:
            continue
        send_profile_validation_notification(validations[0].user_id, validations)
        for validation in validations:
            validation.notification_sent_at = now
        db.session.commit()
