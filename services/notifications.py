import json

from flask import current_app
from pywebpush import WebPushException, webpush

from database import db
from models.integration import PushSubscription


def send_analysis_notification(user_id, summary):
    private_key = current_app.config.get("PUSH_VAPID_PRIVATE_KEY")
    subject = current_app.config.get("PUSH_VAPID_SUBJECT")
    if not private_key or not subject:
        return

    payload = json.dumps(
        {
            "title": "Análise do Instagram concluída",
            "body": summary.message(),
            "url": "dashboard",
            "tag": "instagram-analysis-complete",
        },
        ensure_ascii=False,
    )
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
    if changed:
        db.session.commit()


def send_profile_validation_notification(user_id, validations):
    outcomes = {}
    for validation in validations:
        outcomes[validation.outcome] = outcomes.get(validation.outcome, 0) + 1

    parts = []
    if outcomes.get("relationship_removed"):
        parts.append(f"{outcomes['relationship_removed']} relação(ões) removida(s) confirmada(s)")
    confirmed_active = outcomes.get("exists_confirmed_via_following", 0) + outcomes.get("exists_confirmed_via_followers", 0)
    if confirmed_active:
        parts.append(f"{confirmed_active} perfil(is) ativo(s) confirmado(s)")
    if outcomes.get("unavailable"):
        parts.append(f"{outcomes['unavailable']} perfil(is) indisponível(is)")
    if outcomes.get("untestable"):
        parts.append(f"{outcomes['untestable']} perfil(is) não retestável(is)")
    if outcomes.get("exists_waiting_confirmation"):
        parts.append(f"{outcomes['exists_waiting_confirmation']} aguardando nova exportação")
    if outcomes.get("inconclusive"):
        parts.append(f"{outcomes['inconclusive']} verificação(ões) inconclusiva(s)")
    if outcomes.get("reappeared"):
        parts.append(f"{outcomes['reappeared']} perfil(is) reapareceu(ram)")
    if outcomes.get("followed_again"):
        parts.append(f"{outcomes['followed_again']} perfil(is) voltou(aram) a seguir")

    _send_push(
        user_id,
        {
            "title": "Validação de perfis concluída",
            "body": "; ".join(parts) or "As validações pendentes foram concluídas.",
            "url": "dashboard",
            "tag": f"instagram-profile-validation-{validations[0].analysis_run_id or validations[0].id}",
        },
    )


def _send_push(user_id, data):
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
    if changed:
        db.session.commit()
