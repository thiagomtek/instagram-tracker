"""Turns database rows into the structures the templates and JSON API render."""

import plotly.graph_objects as go
from flask import current_app

from models.integration import RelationshipValidation
from services.clock import local_now, utc_now, utc_to_local
from services.profile_validation import ACTIVE_STATUSES

FINISHED = {"completed", "cancelled"}
ACTIVITY_SERIES = (
    ("gained", "Entradas", "#35cf84", "+{y} entradas"),
    ("reappeared", "Reapareceram", "#5aa7ff", "+{y} reapareceram"),
    ("followed_again", "Voltaram a seguir", "#a78bfa", "+{y} voltaram a seguir"),
    ("missing", "Ausentes", "#f7bd4f", "{y} aguardando validação"),
    ("lost", "Remoções confirmadas (−)", "#f36d7d", "-{y} confirmadas"),
)


def format_local_datetime(value):
    """Format a naive UTC datetime in the configured timezone."""
    if value is None:
        return ""
    return utc_to_local(value).strftime("%d/%m/%Y %H:%M")


def format_validation_attempt_time(dt):
    """Short local time for a naive UTC datetime ('14:05' today, '03/11 14:05' otherwise)."""
    if not dt:
        return ""
    local_dt = utc_to_local(dt)
    if local_dt.date() == local_now().date():
        return local_dt.strftime("%H:%M")
    return local_dt.strftime("%d/%m %H:%M")


def format_validation_item(item, max_attempts=3):
    kind_label = "Perfil" if item.purpose == "profile_scan" else ("Seguidor" if item.kind == "followers" else "Seguindo")
    status = item.status
    outcome = item.outcome
    attempts = item.attempts or 0
    attempt_limit = 2 if item.purpose == "profile_scan" else max_attempts
    next_time = format_validation_attempt_time(item.next_attempt_at) if item.next_attempt_at else ""

    badge_class = "badge-queued"
    status_label = "Na fila"
    sub_label = ""

    if status == "running":
        badge_class = "badge-running"
        status_label = "Averiguando agora"
        sub_label = f"Tentativa {attempts} de {attempt_limit}"
    elif status == "retry":
        badge_class = "badge-retry"
        if outcome in ("unavailable_pending_retest", "inconclusive_pending_retest"):
            status_label = "Reaveriguação agendada"
            sub_label = f"Reteste 1/1 previsto às {next_time}" if next_time else "Reteste 1/1 agendado"
        elif outcome == "blocked":
            status_label = "Instagram bloqueou a consulta"
            sub_label = f"Nova tentativa prevista às {next_time}" if next_time else "Nova tentativa agendada"
        else:
            status_label = "Nova tentativa agendada"
            upcoming = min(attempts + 1, attempt_limit)
            sub_label = f"Tentativa {upcoming} de {attempt_limit}" + (f" prevista às {next_time}" if next_time else "")
    elif status == "queued":
        prefix = "Existência e foto" if item.purpose == "profile_scan" else "Averiguação"
        sub_label = f"{prefix} prevista às {next_time}" if next_time else f"{prefix} aguardando o worker"
    elif status == "completed":
        badge_class = "badge-completed"
        if outcome == "profile_exists":
            status_label = "Perfil encontrado"
            sub_label = "Foto armazenada quando disponibilizada pelo Instagram"
        elif outcome == "profile_unavailable":
            status_label = "Perfil indisponível"
            sub_label = "Conta não acessível publicamente"
        elif outcome == "profile_inconclusive":
            status_label = "Consulta inconclusiva"
            sub_label = "Não foi possível confirmar o perfil"
        elif outcome == "relationship_removed":
            status_label = "Remoção confirmada"
            sub_label = "Não segue mais você" if item.kind == "followers" else "Você não segue mais"
        elif outcome == "untestable":
            status_label = "Indisponível"
            sub_label = "Conta excluída, desativada ou que bloqueou você"
        elif outcome == "exists_waiting_confirmation":
            status_label = "Existe no Instagram"
            sub_label = "Aguardando a próxima exportação para confirmar"
        else:
            status_label = "Concluído"
            sub_label = outcome or ""
    elif status == "cancelled":
        badge_class = "badge-cancelled"
        status_label = "Cancelado"
        sub_label = "Perfil voltou antes da averiguação"

    return {
        "id": item.id,
        "username": item.username,
        "kind": item.kind,
        "purpose": item.purpose,
        "kind_label": kind_label,
        "status": status,
        "status_label": status_label,
        "badge_class": badge_class,
        "sub_label": sub_label,
        "attempts": attempts,
        "outcome": outcome,
        "next_attempt_at": item.next_attempt_at.isoformat() if item.next_attempt_at else None,
        "next_attempt_formatted": next_time,
    }


def build_validation_progress(user_id):
    """Progress of the batch being validated, or of the latest finished batch."""
    active = RelationshipValidation.query.filter(
        RelationshipValidation.user_id == user_id,
        RelationshipValidation.status.in_(ACTIVE_STATUSES),
    ).all()
    if active:
        run_ids = {item.analysis_run_id for item in active if item.analysis_run_id}
        if run_ids:
            validations = RelationshipValidation.query.filter(
                RelationshipValidation.user_id == user_id,
                RelationshipValidation.analysis_run_id.in_(run_ids),
            ).all()
        else:
            validations = active
    else:
        latest = RelationshipValidation.query.filter_by(user_id=user_id).order_by(
            RelationshipValidation.created_at.desc(), RelationshipValidation.id.desc()
        ).first()
        if latest and latest.analysis_run_id:
            validations = RelationshipValidation.query.filter_by(
                user_id=user_id, analysis_run_id=latest.analysis_run_id
            ).all()
        else:
            validations = [latest] if latest else []

    total = len(validations)
    completed = sum(1 for item in validations if item.status in FINISHED)
    far_future = utc_now().replace(year=9999)
    items = sorted(
        validations,
        key=lambda item: (
            0 if item.status == "running" else (1 if item.status in ACTIVE_STATUSES else 2),
            item.next_attempt_at or far_future if item.status in ACTIVE_STATUSES else -(item.completed_at.timestamp() if item.completed_at else 0),
            item.id,
        ),
    )[:20]

    active_items = [item for item in validations if item.status in ACTIVE_STATUSES]
    queued_items = [item for item in active_items if item.status in {"queued", "retry"} and item.next_attempt_at]
    next_active = min(queued_items, key=lambda item: item.next_attempt_at, default=None)

    max_attempts = current_app.config["PROFILE_VALIDATION_MAX_ATTEMPTS"]
    formatted_items = [format_validation_item(item, max_attempts) for item in items]

    return {
        "active": bool(active),
        "total": total,
        "completed": completed,
        "percent": round((completed / total) * 100) if total else 0,
        "queued": sum(1 for item in validations if item.status in {"queued", "retry"}),
        "running": sum(1 for item in validations if item.status == "running"),
        "next_attempt_at": next_active.next_attempt_at.isoformat() if next_active else None,
        "next_attempt_formatted": format_validation_attempt_time(next_active.next_attempt_at) if next_active else None,
        "items": formatted_items,
    }


def generate_activity_graph(daily_counts, kind):
    """Render the change chart.

    ``daily_counts`` maps ``'YYYY-MM-DD'`` to ``{action: count}``. The plotly
    library itself is served by the app (see the ``plotly_js`` route).
    """
    if not daily_counts:
        return (
            "<div class='chart-empty'><strong>Nenhuma mudança no período</strong>"
            "<span>A primeira importação cria a base atual e não conta como ganho.</span></div>"
        )

    dates = sorted(daily_counts)
    subject = "seguidores" if kind == "followers" else "perfis seguidos"

    fig = go.Figure()
    for action, name, color, hover in ACTIVITY_SERIES:
        sign = -1 if action == "lost" else 1
        values = [sign * daily_counts[day].get(action, 0) for day in dates]
        fig.add_bar(
            name=name,
            x=dates,
            y=values,
            marker_color=color,
            customdata=[abs(value) for value in values],
            hovertemplate="%{x}<br>" + hover.replace("{y}", "%{customdata}") + "<extra></extra>",
        )

    fig.update_layout(
        title={"text": f"Mudanças em {subject}", "x": 0, "xanchor": "left"},
        barmode="group",
        font={"family": "Inter, sans-serif", "size": 12, "color": "#f7f7fa"},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        height=400,
        margin={"l": 40, "r": 16, "t": 52, "b": 100},
        legend={"orientation": "h", "yanchor": "top", "y": -0.14, "xanchor": "left", "x": 0},
        hoverlabel={"bgcolor": "#202029", "bordercolor": "#454551", "font_color": "#ffffff"},
    )
    fig.update_xaxes(showgrid=False, tickformat="%d/%m")
    fig.update_yaxes(
        title="Eventos",
        showgrid=True,
        gridcolor="rgba(255,255,255,.08)",
        zeroline=True,
        zerolinecolor="rgba(255,255,255,.3)",
    )

    return fig.to_html(
        full_html=False,
        include_plotlyjs=False,
        config={"displayModeBar": False, "responsive": True, "locale": "pt-BR"},
    )
