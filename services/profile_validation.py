import random
import re
from dataclasses import dataclass
from datetime import timedelta

from flask import current_app

from database import db
from models.follower import Follower
from models.history import History
from models.integration import AnalysisRun, RelationshipValidation
from services.clock import local_now, utc_now
from services.notifications import send_profile_validation_notification


ACTIVE_STATUSES = ("queued", "retry", "running")
FINISHED_STATUSES = ("completed", "cancelled")

# A removal is confirmed when the profile is still missing from this many
# consecutive exports and the profile itself is proven to exist.
REMOVAL_CONFIRMATION_EXPORTS = 2


@dataclass
class ProfileCheck:
    outcome: str
    details: str


def _random_delay(minimum_key, maximum_key):
    minimum = int(current_app.config[minimum_key])
    maximum = int(current_app.config[maximum_key])
    return timedelta(seconds=random.randint(minimum, maximum))


def enqueue_relationship_validation(follower, analysis_run_id=None, observed_at=None):
    """Queue a web check. ``observed_at`` is naive UTC."""
    active = RelationshipValidation.query.filter(
        RelationshipValidation.follower_id == follower.id,
        RelationshipValidation.status.in_(ACTIVE_STATUSES),
    ).first()
    if active:
        active.missing_count_snapshot = follower.missing_count
        return active, False

    now = observed_at or utc_now()
    validation = RelationshipValidation(
        user_id=follower.user_id,
        analysis_run_id=analysis_run_id,
        follower_id=follower.id,
        username=follower.username,
        kind=follower.kind,
        status="queued",
        missing_count_snapshot=follower.missing_count,
        next_attempt_at=now + _random_delay(
            "PROFILE_VALIDATION_INITIAL_MIN_SECONDS",
            "PROFILE_VALIDATION_INITIAL_MAX_SECONDS",
        ),
    )
    follower.validation_status = "queued"
    db.session.add(validation)
    return validation, True


def cancel_relationship_validations(follower):
    now = utc_now()
    validations = RelationshipValidation.query.filter(
        RelationshipValidation.follower_id == follower.id,
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
    follower.retest_count = 0
    if follower.missing_count >= REMOVAL_CONFIRMATION_EXPORTS:
        follower.state = "removed"
        return "relationship_removed", record_confirmed_loss(follower, observed_at)
    follower.state = "pending"
    return "exists_waiting_confirmation", False


def inspect_instagram_profile(username):
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
            )
            try:
                context = browser.new_context(locale="pt-BR")
                page = context.new_page()
                response = page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=current_app.config["PROFILE_VALIDATION_PAGE_TIMEOUT_MS"],
                )
                page.wait_for_timeout(
                    random.randint(
                        current_app.config["PROFILE_VALIDATION_RENDER_MIN_MS"],
                        current_app.config["PROFILE_VALIDATION_RENDER_MAX_MS"],
                    )
                )
                body = page.locator("body").inner_text(timeout=10_000)
                status = response.status if response else None
            finally:
                browser.close()
    except PlaywrightTimeoutError:
        return ProfileCheck("inconclusive", "O Instagram não terminou de carregar a página.")
    except Exception as exc:
        current_app.logger.warning("Falha ao renderizar o perfil @%s: %s", username, exc)
        return ProfileCheck("inconclusive", "A página não pôde ser renderizada pelo validador.")

    normalized = " ".join(body.casefold().split())
    if status == 429 or any(marker in normalized for marker in ("try again later", "tente novamente mais tarde")):
        return ProfileCheck("inconclusive", "O Instagram limitou temporariamente as consultas.")
    if any(marker in normalized for marker in unavailable_markers):
        return ProfileCheck("unavailable", "O Instagram informou que o perfil não está disponível.")

    exact_username = re.search(rf"(?<![\w.]){re.escape(username.casefold())}(?![\w.])", normalized)
    relationship_labels = ("seguidores", "seguindo", "followers", "following")
    if exact_username and any(label in normalized for label in relationship_labels):
        return ProfileCheck("exists", "O perfil foi renderizado com username e dados do cabeçalho.")

    return ProfileCheck("inconclusive", "A resposta não comprovou existência nem indisponibilidade.")


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
    validation.next_attempt_at = now + _random_delay(
        "PROFILE_VALIDATION_RETRY_MIN_SECONDS",
        "PROFILE_VALIDATION_RETRY_MAX_SECONDS",
    )


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
        if follower and follower.validation_status == "verifying":
            follower.validation_status = "queued"
    if stale:
        db.session.commit()
    return len(stale)


def process_next_validation():
    """Process at most one due validation so requests remain serialized and slow."""
    now = utc_now()
    recover_stale_validations(now)
    validation = (
        RelationshipValidation.query.filter(
            RelationshipValidation.status.in_(("queued", "retry")),
            RelationshipValidation.next_attempt_at <= now,
        )
        .order_by(RelationshipValidation.next_attempt_at.asc(), RelationshipValidation.id.asc())
        .first()
    )
    if not validation:
        notify_finished_validation_batches()
        return False

    validation.status = "running"
    validation.attempts += 1
    validation.last_attempt_at = now
    follower = db.session.get(Follower, validation.follower_id)
    if follower:
        follower.validation_status = "verifying"
    db.session.commit()

    check = inspect_instagram_profile(validation.username)
    db.session.expire_all()
    validation = db.session.get(RelationshipValidation, validation.id)
    follower = db.session.get(Follower, validation.follower_id)
    now = utc_now()

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
            follower.state = "untestable"
            _complete(
                validation, follower, "untestable",
                "Perfil permaneceu indisponível após 1 reteste. Pode ter sido excluído, desativado ou ter bloqueado você.",
                now,
            )
    elif validation.attempts < current_app.config["PROFILE_VALIDATION_MAX_ATTEMPTS"]:
        validation.status = "retry"
        validation.outcome = "inconclusive"
        validation.details = check.details
        validation.next_attempt_at = now + _random_delay(
            "PROFILE_VALIDATION_RETRY_MIN_SECONDS",
            "PROFILE_VALIDATION_RETRY_MAX_SECONDS",
        )
        follower.validation_status = "queued"
        follower.last_validation_result = "inconclusive"
        follower.last_validated_at = now
    elif follower.retest_count == 0:
        _schedule_retest(
            validation, follower, "inconclusive_pending_retest",
            f"Consultas inconclusivas ({check.details}). 1 reteste agendado.", now,
        )
    else:
        follower.state = "untestable"
        _complete(
            validation, follower, "untestable",
            f"Consultas inconclusivas persistentes ({check.details}). Definido como não retestável.",
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


def seconds_until_next_validation():
    return random.randint(
        current_app.config["PROFILE_VALIDATION_GAP_MIN_SECONDS"],
        current_app.config["PROFILE_VALIDATION_GAP_MAX_SECONDS"],
    )
