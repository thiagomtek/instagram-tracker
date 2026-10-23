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
