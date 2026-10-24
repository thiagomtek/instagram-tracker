import json

from flask import current_app
from pywebpush import WebPushException, webpush

from database import db
from models.integration import PushSubscription


def _send_push(user_id, data):
    """Send one Web Push message to every device of the user.

    A failing push service must never break an import or the worker loop, so
    every error is logged and swallowed; expired subscriptions are removed.
    """
    private_key = current_app.config.get("PUSH_VAPID_PRIVATE_KEY")
    subject = current_app.config.get("PUSH_VAPID_SUBJECT")
    if not private_key or not subject:
        return

    payload = json.dumps(data, ensure_ascii=False)
    changed = False
    for subscription in PushSubscription.query.filter_by(user_id=user_id).all():
        try:
            webpush(
                subscription_info={
                    "endpoint": subscription.endpoint,
                    "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
                },
                data=payload,
                vapid_private_key=private_key,
                vapid_claims={"sub": subject},
            )
        except WebPushException as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code in (404, 410):
                db.session.delete(subscription)
                changed = True
            else:
                current_app.logger.warning("Falha ao enviar Web Push: %s", exc)
        except Exception as exc:
            current_app.logger.warning("Falha inesperada ao enviar Web Push: %s", exc)
    if changed:
        db.session.commit()


def send_analysis_notification(user_id, summary):
    _send_push(
        user_id,
        {
            "title": "Análise do Instagram concluída",
            "body": summary.message(),
            "url": "dashboard",
            "tag": "instagram-analysis-complete",
        },
    )


def send_profile_validation_notification(user_id, validations):
    outcomes = {}
    for validation in validations:
        outcomes[validation.outcome] = outcomes.get(validation.outcome, 0) + 1

    labels = (
        ("relationship_removed", "{} relação(ões) removida(s) confirmada(s)"),
        ("untestable", "{} perfil(is) indisponível(is) após reteste"),
        ("exists_waiting_confirmation", "{} aguardando nova exportação"),
        ("inconclusive", "{} verificação(ões) inconclusiva(s)"),
        ("reappeared", "{} perfil(is) reapareceu(ram)"),
        ("followed_again", "{} perfil(is) voltou(aram) a seguir"),
    )
    parts = [text.format(outcomes[key]) for key, text in labels if outcomes.get(key)]

    _send_push(
        user_id,
        {
            "title": "Validação de perfis concluída",
            "body": "; ".join(parts) or "As validações pendentes foram concluídas.",
            "url": "dashboard",
            "tag": f"instagram-profile-validation-{validations[0].analysis_run_id or validations[0].id}",
        },
    )
