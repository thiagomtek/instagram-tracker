from functools import wraps

import hmac

import json

from pathlib import Path

import re

import secrets

from tempfile import TemporaryDirectory



from flask import Flask, Response, flash, jsonify, render_template, request, redirect, session, url_for

from google_auth_oauthlib.flow import Flow

import requests

from werkzeug.middleware.proxy_fix import ProxyFix

from werkzeug.utils import secure_filename

from config import Config

from database import db

from models.follower import Follower

from models.history import History

from models.integration import AnalysisRun, DriveConnection, DriveImport, PushSubscription, RelationshipValidation

from models.user import User

from services.storage import order_usernames_newest_first

from services.compare import cross_reference

from services.google_drive import (

    drive_service,

    google_drive_is_configured,

    sync_connection_safely,

)

from services.importer import process_import_paths

from services.analysis_runs import complete_analysis_run, fail_analysis_run, start_analysis_run
from services.notifications import send_analysis_notification

from services.token_crypto import decrypt_token, encrypt_token

import os

import plotly.graph_objects as go

from datetime import datetime, timedelta, timezone

from zoneinfo import ZoneInfo



app = Flask(__name__)

app.config.from_object(Config)

if app.config["TRUST_PROXY_HEADERS"]:

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

os.makedirs(os.environ.get("DATA_DIR", os.path.dirname(__file__)), exist_ok=True)



db.init_app(app)



def ensure_schema_columns():
    """Small SQLite migrations for installations created by earlier releases."""
    inspector = db.inspect(db.engine)
    for table_name in ("follower", "history"):
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        if "user_id" not in columns:
            try:
                db.session.execute(db.text(f"ALTER TABLE {table_name} ADD COLUMN user_id INTEGER"))
                db.session.commit()
            except Exception:
                db.session.rollback()
        try:
            db.session.execute(db.text(f"CREATE INDEX IF NOT EXISTS ix_{table_name}_user_id ON {table_name} (user_id)"))
            db.session.commit()
        except Exception:
            db.session.rollback()

    follower_columns = {column["name"] for column in inspector.get_columns("follower")}
    follower_cols = [
        ("is_present", "BOOLEAN NOT NULL DEFAULT 1"),
        ("missing_since", "DATETIME"),
        ("state", "VARCHAR(32) NOT NULL DEFAULT 'present'"),
        ("missing_count", "INTEGER NOT NULL DEFAULT 0"),
        ("validation_status", "VARCHAR(32) NOT NULL DEFAULT 'idle'"),
        ("last_validation_result", "VARCHAR(32)"),
        ("last_validated_at", "DATETIME"),
    ]
    for col_name, col_type in follower_cols:
        if col_name not in follower_columns:
            try:
                db.session.execute(db.text(f"ALTER TABLE follower ADD COLUMN {col_name} {col_type}"))
                db.session.commit()
            except Exception:
                db.session.rollback()

    try:
        db.session.execute(db.text("UPDATE follower SET is_present = 1 WHERE is_present IS NULL"))
        db.session.execute(db.text("UPDATE follower SET state = 'present' WHERE state IS NULL"))
        db.session.execute(db.text("UPDATE follower SET missing_count = 0 WHERE missing_count IS NULL"))
        db.session.execute(db.text("UPDATE follower SET validation_status = 'idle' WHERE validation_status IS NULL"))
        db.session.execute(db.text("CREATE INDEX IF NOT EXISTS ix_follower_presence ON follower (user_id, kind, is_present)"))
        db.session.commit()
    except Exception:
        db.session.rollback()

    run_columns = {column["name"] for column in inspector.get_columns("analysis_run")}
    run_cols = [
        ("followers_reappeared", "INTEGER NOT NULL DEFAULT 0"),
        ("reappeared_usernames", "TEXT"),
        ("followers_missing", "INTEGER NOT NULL DEFAULT 0"),
        ("validation_total", "INTEGER NOT NULL DEFAULT 0"),
        ("validation_completed", "INTEGER NOT NULL DEFAULT 0"),
    ]
    for col_name, col_type in run_cols:
        if col_name not in run_columns:
            try:
                db.session.execute(db.text(f"ALTER TABLE analysis_run ADD COLUMN {col_name} {col_type}"))
                db.session.commit()
            except Exception:
                db.session.rollback()

    try:
        db.session.execute(db.text("UPDATE history SET action = 'missing' WHERE action = 'lost'"))
        db.session.commit()
    except Exception:
        db.session.rollback()


def assign_orphaned_data(user_id):

    """Claims legacy data only when the database receives its first account."""

    History.query.filter(History.user_id.is_(None)).update({History.user_id: user_id})

    Follower.query.filter(Follower.user_id.is_(None)).update({Follower.user_id: user_id})

    db.session.commit()



with app.app_context():

    db.create_all()

    ensure_schema_columns()

    if User.query.count() == 0 and app.config["INITIAL_ADMIN_EMAIL"] and app.config["INITIAL_ADMIN_PASSWORD"]:

        initial_user = User(email=app.config["INITIAL_ADMIN_EMAIL"].strip().lower())

        initial_user.set_password(app.config["INITIAL_ADMIN_PASSWORD"])

        db.session.add(initial_user)

        db.session.commit()

        assign_orphaned_data(initial_user.id)



def is_valid_email(email):

    return len(email) <= 255 and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) is not None



def validate_password(password, confirmation):

    if len(password) < 12:

        return "A senha deve ter pelo menos 12 caracteres."

    if password != confirmation:

        return "As senhas não coincidem."

    return None



@app.context_processor

def inject_csrf_token():

    if "csrf_token" not in session:

        session["csrf_token"] = secrets.token_urlsafe(32)

    return {

        "csrf_token": session["csrf_token"],

        "push_public_key": app.config["PUSH_VAPID_PUBLIC_KEY"],

    }



@app.before_request

def verify_csrf_token():

    if request.method == "POST":

        submitted_token = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")

        expected_token = session.get("csrf_token", "")

        if not expected_token or not hmac.compare_digest(submitted_token, expected_token):

            return "Solicitação inválida. Atualize a página e tente novamente.", 400



def login_required(view):

    @wraps(view)

    def wrapped_view(*args, **kwargs):

        user_id = session.get("user_id")

        if not user_id or db.session.get(User, user_id) is None:

            session.clear()

            return redirect(url_for("login"))

        session.permanent = True

        return view(*args, **kwargs)

    return wrapped_view



@app.template_filter('avatar_color')

def avatar_color(username):

    import hashlib

    if not username: return "rgb(50, 50, 50)"

    hash_obj = hashlib.md5(username.encode())

    hex_hash = hash_obj.hexdigest()

    r = int(hex_hash[0:2], 16) % 150

    g = int(hex_hash[2:4], 16) % 150

    b = int(hex_hash[4:6], 16) % 150

    return f"rgb({r}, {g}, {b})"



@app.template_filter('initials')

def initials_filter(username): return username[:2].upper() if username else "??"



@app.template_filter('format_date')

def format_date(value):

    if value is None: return ""

    return value.strftime('%d/%m/%Y %H:%M')



@app.template_filter("local_datetime")

def format_local_datetime(value):

    if value is None:

        return ""

    if value.tzinfo is None:

        value = value.replace(tzinfo=timezone.utc)

    return value.astimezone(ZoneInfo(app.config["APP_TIMEZONE"])).strftime("%d/%m/%Y %H:%M")



@app.template_filter("duration")

def format_duration(run):

    if not run.completed_at:

        return "em andamento"

    seconds = max(0, int((run.completed_at - run.started_at).total_seconds()))

    if seconds < 60:

        return f"{seconds}s"

    minutes, seconds = divmod(seconds, 60)

    return f"{minutes}min {seconds}s"



@app.route("/")

@login_required

def index():

    drive_connection = DriveConnection.query.filter_by(user_id=session["user_id"]).first()

    return render_template(

        "index.html",

        drive_connection=drive_connection,

        google_configured=google_drive_is_configured(),

        push_configured=bool(app.config["PUSH_VAPID_PUBLIC_KEY"]),

    )



@app.route("/manifest.webmanifest")

def web_manifest():

    script_root = request.script_root.rstrip("/")

    response = jsonify(

        {

            "id": app.config["PWA_ID"],

            "name": "Instagram Tracker",

            "short_name": "IG Tracker",

            "description": "Análises privadas das exportações oficiais do Instagram.",

            "lang": "pt-BR",

            "start_url": f"{script_root}/",

            "scope": f"{script_root}/",

            "display": "standalone",

            "background_color": "#0b0b10",

            "theme_color": "#c72c65",

            "icons": [

                {

                    "src": url_for("static", filename="icon.svg"),

                    "sizes": "any",

                    "type": "image/svg+xml",

                    "purpose": "any maskable",

                }

            ],

        }

    )

    response.headers["Content-Type"] = "application/manifest+json"

    response.headers["Cache-Control"] = "no-cache"

    return response



@app.route("/healthz")

def healthz():

    return jsonify({"status": "ok"})



@app.route("/service-worker.js")

def service_worker():

    service_worker_path = Path(app.static_folder) / "service-worker.js"

    response = Response(service_worker_path.read_text(encoding="utf-8"), mimetype="application/javascript")

    response.headers["Service-Worker-Allowed"] = f"{request.script_root.rstrip('/')}/"

    response.headers["Cache-Control"] = "no-cache"

    return response



@app.route("/offline")

def offline():

    return render_template("offline.html")



@app.errorhandler(413)

def upload_too_large(_error):

    flash("O upload excede o limite configurado no servidor.", "error")

    return redirect(url_for("index"))



@app.route("/login", methods=["GET", "POST"])

def login():

    if session.get("user_id"):

        return redirect(url_for("index"))



    if request.method == "POST":

        email = request.form.get("email", "").strip().lower()

        password = request.form.get("password", "")

        user = User.query.filter_by(email=email).first()



        if user and user.check_password(password):

            session.clear()

            session["user_id"] = user.id

            session.permanent = True

            return redirect(url_for("index"))



        flash("E-mail ou senha inválidos.", "error")



    return render_template("login.html")



@app.route("/register", methods=["GET", "POST"])

def register():

    if session.get("user_id"):

        return redirect(url_for("index"))

    if not app.config["ALLOW_REGISTRATION"]:

        flash("O cadastro de novas contas está desativado.", "error")

        return redirect(url_for("login"))



    if request.method == "POST":

        email = request.form.get("email", "").strip().lower()

        password = request.form.get("password", "")

        confirmation = request.form.get("password_confirmation", "")



        if not is_valid_email(email):

            flash("Informe um e-mail válido.", "error")

        elif User.query.filter_by(email=email).first():

            flash("Já existe uma conta com este e-mail.", "error")

        elif password_error := validate_password(password, confirmation):

            flash(password_error, "error")

        else:

            is_first_user = User.query.count() == 0

            user = User(email=email)

            user.set_password(password)

            db.session.add(user)

            db.session.commit()

            if is_first_user:

                assign_orphaned_data(user.id)

            session.clear()

            session["user_id"] = user.id

            session.permanent = True

            flash("Conta criada. Você já pode usar o painel.", "success")

            return redirect(url_for("index"))



    return render_template("register.html")



@app.route("/logout", methods=["POST"])

@login_required

def logout():

    session.clear()

    return redirect(url_for("login"))



@app.route("/upload", methods=["POST"])

@login_required

def upload():

    user_id = session["user_id"]

    files = request.files.getlist("files")

    if not files or files[0].filename == "":

        flash("Selecione ao menos um arquivo.", "error")

        return redirect(url_for("index"))



    source_name = ", ".join((file.filename or "arquivo") for file in files)

    analysis_run = start_analysis_run(user_id, "manual", source_name)

    try:

        with TemporaryDirectory(prefix="instagram-upload-") as temp_dir:

            paths = []

            for index, uploaded_file in enumerate(files):

                filename = secure_filename(uploaded_file.filename) or f"arquivo-{index}"

                target = Path(temp_dir) / f"{index}-{filename}"

                uploaded_file.save(target)

                paths.append(target)

            summary = process_import_paths(paths, user_id, analysis_run_id=analysis_run.id)

        complete_analysis_run(analysis_run, summary)

        send_analysis_notification(user_id, summary)

        flash(f"Análise concluída. {summary.message()}", "success")

        return redirect(url_for("dashboard"))

    except (ValueError, OSError, json.JSONDecodeError) as exc:

        fail_analysis_run(analysis_run.id, exc)

        flash(str(exc), "error")

        return redirect(url_for("index"))

    except Exception as exc:

        fail_analysis_run(analysis_run.id, exc)

        app.logger.exception("Falha inesperada na importação manual")

        flash("A análise falhou. Consulte o histórico de processamentos para ver o erro.", "error")

        return redirect(url_for("processing_history"))



def _google_client_config():

    return {

        "web": {

            "client_id": app.config["GOOGLE_CLIENT_ID"],

            "client_secret": app.config["GOOGLE_CLIENT_SECRET"],

            "auth_uri": "https://accounts.google.com/o/oauth2/auth",

            "token_uri": "https://oauth2.googleapis.com/token",

        }

    }



def _google_redirect_uri():

    return app.config["GOOGLE_REDIRECT_URI"] or url_for("google_drive_callback", _external=True)



@app.route("/integrations/google/connect")

@login_required

def google_drive_connect():

    if not google_drive_is_configured():

        flash("A integração com o Google Drive ainda não foi configurada no servidor.", "error")

        return redirect(url_for("index"))

    flow = Flow.from_client_config(_google_client_config(), scopes=app.config["GOOGLE_DRIVE_SCOPES"])

    flow.redirect_uri = _google_redirect_uri()

    authorization_url, state = flow.authorization_url(

        access_type="offline",

        prompt="consent",

    )

    session["google_oauth_state"] = state

    if getattr(flow, "code_verifier", None):

        session["google_oauth_code_verifier"] = flow.code_verifier

    return redirect(authorization_url)



@app.route("/integrations/google/callback")

@login_required

def google_drive_callback():

    expected_state = session.pop("google_oauth_state", None)

    code_verifier = session.pop("google_oauth_code_verifier", None)

    if not expected_state or request.args.get("state") != expected_state:

        flash("A resposta do Google não pôde ser validada. Tente vincular novamente.", "error")

        return redirect(url_for("index"))

    try:

        flow = Flow.from_client_config(

            _google_client_config(),

            scopes=app.config["GOOGLE_DRIVE_SCOPES"],

            state=expected_state,

        )

        flow.redirect_uri = _google_redirect_uri()

        if code_verifier:

            flow.code_verifier = code_verifier

            flow.fetch_token(authorization_response=request.url, code_verifier=code_verifier)

        else:

            flow.fetch_token(authorization_response=request.url)

        credentials = flow.credentials

        if not credentials.refresh_token:

            raise ValueError("O Google não retornou um token de acesso offline.")

        service = drive_service_from_credentials(credentials)

        about = service.about().get(fields="user(displayName,emailAddress)").execute().get("user", {})

        connection = DriveConnection.query.filter_by(user_id=session["user_id"]).first()

        if connection is None:

            connection = DriveConnection(

                user_id=session["user_id"],

                encrypted_refresh_token="",

                folder_id="root",

                folder_name="Meu Drive (pastas meta-*)",

            )

            db.session.add(connection)

        connection.encrypted_refresh_token = encrypt_token(credentials.refresh_token)

        connection.google_email = about.get("emailAddress") or about.get("displayName")

        connection.folder_id = "root"

        connection.folder_name = "Meu Drive (pastas meta-*)"

        connection.enabled = True

        connection.last_error = None

        db.session.commit()

        flash("Conta do Google Drive vinculada com sucesso.", "success")

    except Exception as exc:

        db.session.rollback()

        app.logger.exception("Falha no retorno do OAuth do Google")

        flash(f"Não foi possível vincular o Google Drive: {exc}", "error")

    return redirect(url_for("index"))



def drive_service_from_credentials(credentials):

    from googleapiclient.discovery import build



    return build("drive", "v3", credentials=credentials, cache_discovery=False)



@app.route("/integrations/google/sync", methods=["POST"])

@login_required

def google_drive_sync_now():

    connection = DriveConnection.query.filter_by(user_id=session["user_id"]).first()

    if not connection:

        flash("Vincule uma conta do Google Drive primeiro.", "error")

        return redirect(url_for("index"))

    imported = sync_connection_safely(connection)

    current = db.session.get(DriveConnection, connection.id)

    if current and current.last_error:

        flash(f"A sincronização falhou: {current.last_error}", "error")

    elif imported:

        flash(f"Sincronização concluída: {len(imported)} nova(s) exportação(ões) processada(s).", "success")

    else:

        flash("O Google Drive está em dia; nenhuma exportação nova foi encontrada.", "success")

    return redirect(url_for("index"))



@app.route("/integrations/google/disconnect", methods=["POST"])

@login_required

def google_drive_disconnect():

    connection = DriveConnection.query.filter_by(user_id=session["user_id"]).first()

    if connection:

        try:

            token = decrypt_token(connection.encrypted_refresh_token)

            requests.post("https://oauth2.googleapis.com/revoke", params={"token": token}, timeout=10)

        except Exception:

            app.logger.warning("Não foi possível revogar o token no Google; removendo a cópia local.")

        DriveImport.query.filter_by(connection_id=connection.id).delete()

        db.session.delete(connection)

        db.session.commit()

    flash("Conta do Google Drive desvinculada deste serviço.", "success")

    return redirect(url_for("index"))



@app.route("/api/push/subscribe", methods=["POST"])

@login_required

def push_subscribe():

    payload = request.get_json(silent=True) or {}

    keys = payload.get("keys") or {}

    endpoint = payload.get("endpoint")

    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):

        return jsonify({"error": "Assinatura inválida."}), 400

    subscription = PushSubscription.query.filter_by(endpoint=endpoint).first()

    if subscription is None:

        subscription = PushSubscription(endpoint=endpoint)

        db.session.add(subscription)

    subscription.user_id = session["user_id"]

    subscription.p256dh = keys["p256dh"]

    subscription.auth = keys["auth"]

    db.session.commit()

    return jsonify({"ok": True})



@app.route("/api/push/unsubscribe", methods=["POST"])

@login_required

def push_unsubscribe():

    payload = request.get_json(silent=True) or {}

    endpoint = payload.get("endpoint")

    if endpoint:

        PushSubscription.query.filter_by(user_id=session["user_id"], endpoint=endpoint).delete()

        db.session.commit()

    return jsonify({"ok": True})



def build_validation_progress(user_id):
    active = RelationshipValidation.query.filter(
        RelationshipValidation.user_id == user_id,
        RelationshipValidation.status.in_(("queued", "retry", "running")),
    ).order_by(RelationshipValidation.next_attempt_at.asc()).all()
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
    completed = sum(1 for item in validations if item.status in {"completed", "cancelled"})
    items = sorted(
        validations,
        key=lambda item: (item.status not in {"running", "queued", "retry"}, item.next_attempt_at, item.id),
    )[:20]
    return {
        "active": bool(active),
        "total": total,
        "completed": completed,
        "percent": round((completed / total) * 100) if total else 0,
        "queued": sum(1 for item in validations if item.status in {"queued", "retry"}),
        "running": sum(1 for item in validations if item.status == "running"),
        "items": items,
    }


@app.route("/api/validations/status")
@login_required
def validation_status():
    progress = build_validation_progress(session["user_id"])
    return jsonify({
        "active": progress["active"],
        "total": progress["total"],
        "completed": progress["completed"],
        "percent": progress["percent"],
        "queued": progress["queued"],
        "running": progress["running"],
        "items": [
            {
                "username": item.username,
                "kind": item.kind,
                "status": item.status,
                "outcome": item.outcome,
                "attempts": item.attempts,
                "next_attempt_at": item.next_attempt_at.isoformat() if item.next_attempt_at else None,
            }
            for item in progress["items"]
        ],
    })


@app.route("/dashboard")

@login_required

def dashboard():

    user_id = session["user_id"]

    start_date_str = request.args.get('start_date')

    end_date_str = request.args.get('end_date')

    username_filter = request.args.get('username', '').strip()

    action_filter = request.args.get('action', 'all')

    kind_filter = request.args.get('kind', 'followers')



    if action_filter not in {'all', 'gained', 'missing', 'reappeared', 'lost'}:

        action_filter = 'all'

    if kind_filter not in {'followers', 'following'}:

        kind_filter = 'followers'



    if not start_date_str:

        start_date_str = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d')

    try:

        start_date = datetime.strptime(start_date_str, '%Y-%m-%d')

    except ValueError:

        start_date_str = None

        start_date = datetime.now() - timedelta(days=90)



    if not end_date_str:

        end_date_str = datetime.now().strftime('%Y-%m-%d')

    try:

        end_date = datetime.strptime(end_date_str, '%Y-%m-%d') + timedelta(days=1)

    except ValueError:

        end_date_str = None

        end_date = datetime.now() + timedelta(days=1)



    query = History.query.filter(

        History.user_id == user_id,

        History.kind == kind_filter,

        History.date_added >= start_date,

        History.date_added < end_date,

    )



    if username_filter:

        query = query.filter(History.username.ilike(f"%{username_filter}%"))



    if action_filter != 'all':

        query = query.filter(History.action == action_filter)



    history_logs = query.order_by(History.date_added.desc(), History.id.desc()).all()



    follower_records = (

        Follower.query.filter_by(user_id=user_id, kind='followers', is_present=True)

        .order_by(Follower.date_added.desc(), Follower.username.asc())

        .all()

    )

    following_records = (

        Follower.query.filter_by(user_id=user_id, kind='following', is_present=True)

        .order_by(Follower.date_added.desc(), Follower.username.asc())

        .all()

    )

    uncertain_records = Follower.query.filter(
        Follower.user_id == user_id,
        Follower.is_present.is_(False),
        Follower.state.in_(("pending", "absent")),
    ).all()
    uncertain_keys = {item.username.casefold() for item in uncertain_records}
    current_followers = [
        item.username for item in follower_records if item.username.casefold() not in uncertain_keys
    ]

    current_following = [
        item.username for item in following_records if item.username.casefold() not in uncertain_keys
    ]

    not_following_back, fans, mutuals = cross_reference(current_followers, current_following)



    follower_dates = {item.username.casefold(): item.date_added for item in follower_records}

    following_dates = {item.username.casefold(): item.date_added for item in following_records}

    not_following_back_set = set(not_following_back)

    not_following_back = [

        item.username for item in following_records

        if item.username in not_following_back_set

    ]

    not_following_back_dates = {

        item.username.casefold(): item.date_added for item in following_records

        if item.username in not_following_back_set

    }



    fans_dates = {

        username.casefold(): follower_dates.get(username.casefold())

        for username in fans

    }

    mutuals_dates = {

        username.casefold(): max(

            filter(

                None,

                (

                    follower_dates.get(username.casefold()),

                    following_dates.get(username.casefold()),

                ),

            ),

            default=None,

        )

        for username in mutuals

    }

    not_following_back = order_usernames_newest_first(not_following_back, not_following_back_dates)

    fans = order_usernames_newest_first(fans, fans_dates)

    mutuals = order_usernames_newest_first(mutuals, mutuals_dates)

    absent_records = (
        Follower.query.filter(
            Follower.user_id == user_id,
            Follower.kind == 'followers',
            Follower.is_present.is_(False),
            Follower.state.in_(("pending", "absent")),
        )
        .order_by(Follower.missing_since.desc(), Follower.username.asc())
        .all()
    )
    absent_followers = [item.username for item in absent_records]
    absent_dates = {item.username.casefold(): item.missing_since for item in absent_records}
    absent_following_records = (
        Follower.query.filter(
            Follower.user_id == user_id,
            Follower.kind == 'following',
            Follower.is_present.is_(False),
            Follower.state.in_(("pending", "absent")),
        )
        .order_by(Follower.missing_since.desc(), Follower.username.asc())
        .all()
    )
    absent_following = [item.username for item in absent_following_records]
    absent_following_dates = {
        item.username.casefold(): item.missing_since for item in absent_following_records
    }



    graph_html = generate_activity_graph(history_logs, kind_filter)



    latest_run = (

        AnalysisRun.query.filter_by(user_id=user_id)

        .filter(AnalysisRun.status.in_(("success", "cleanup_failed")))

        .order_by(AnalysisRun.completed_at.desc(), AnalysisRun.started_at.desc(), AnalysisRun.id.desc())

        .first()

    )



    gained_filtered = sum(1 for item in history_logs if item.action == 'gained')

    missing_filtered = sum(1 for item in history_logs if item.action == 'missing')

    lost_filtered = sum(1 for item in history_logs if item.action == 'lost')

    reappeared_filtered = sum(1 for item in history_logs if item.action == 'reappeared')



    kpis = {

        "followers_total": len(current_followers),

        "following_total": len(current_following),

        "mutuals_total": len(mutuals),

        "not_following_back_total": len(not_following_back),

        "absent_total": len({item.casefold() for item in absent_followers + absent_following}),

        "gained_filtered": gained_filtered,

        "missing_filtered": missing_filtered,

        "lost_filtered": lost_filtered,

        "reappeared_filtered": reappeared_filtered,

        "net_filtered": gained_filtered + reappeared_filtered - lost_filtered,

    }



    return render_template("dashboard.html",

                           history_logs=history_logs,

                           not_following_back=not_following_back,

                           not_following_back_dates=not_following_back_dates,

                           fans=fans,



                           fans_dates=fans_dates,

                           mutuals=mutuals,



                           mutuals_dates=mutuals_dates,

                           absent_followers=absent_followers,

                           absent_dates=absent_dates,
                           absent_following=absent_following,
                           absent_following_dates=absent_following_dates,
                           validation_progress=build_validation_progress(user_id),

                           graph_html=graph_html,

                           kpis=kpis,



                           latest_run=latest_run,

                           filters={

                               'start': start_date_str,

                               'end': end_date_str,

                               'username': username_filter,

                               'action': action_filter,



                               'kind': kind_filter

                           })



@app.route("/processamentos")

@login_required

def processing_history():

    page = request.args.get("page", 1, type=int)

    pagination = (

        AnalysisRun.query.filter_by(user_id=session["user_id"])

        .order_by(AnalysisRun.started_at.desc(), AnalysisRun.id.desc())

        .paginate(page=page, per_page=30, error_out=False)

    )

    for run in pagination.items:

        try:

            run.gained_list = json.loads(run.gained_usernames or "[]")

            run.lost_list = json.loads(run.lost_usernames or "[]")

            run.reappeared_list = json.loads(run.reappeared_usernames or "[]")

            run.confirmed_list = [
                item.username
                for item in RelationshipValidation.query.filter_by(
                    analysis_run_id=run.id, outcome="relationship_removed"
                ).order_by(RelationshipValidation.completed_at.desc()).all()
            ]

            run.validation_percent = round((run.validation_completed / run.validation_total) * 100) if run.validation_total else 0

        except json.JSONDecodeError:

            run.gained_list = []

            run.lost_list = []

            run.reappeared_list = []

            run.confirmed_list = []

            run.validation_percent = 0

    return render_template("processing_history.html", pagination=pagination)



def generate_activity_graph(history_logs, kind):

    """Render a compact change chart for the selected relationship."""

    if not history_logs:

        return (

            "<div class='chart-empty'><strong>Nenhuma mudança no período</strong>"

            "<span>A primeira importação cria a base atual e não conta como ganho.</span></div>"

        )



    grouped = {}

    for log in history_logs:

        day = log.date_added.strftime("%Y-%m-%d")

        grouped.setdefault(day, {"gained": 0, "missing": 0, "lost": 0, "reappeared": 0})

        action = log.action

        if action in grouped[day]:

            grouped[day][action] += 1



    dates = sorted(grouped)

    gained = [grouped[day]["gained"] for day in dates]

    missing = [grouped[day]["missing"] for day in dates]

    lost = [-grouped[day]["lost"] for day in dates]

    reappeared = [grouped[day]["reappeared"] for day in dates]

    subject = "seguidores" if kind == "followers" else "perfis seguidos"



    fig = go.Figure()

    fig.add_bar(

        name="Entradas",

        x=dates,

        y=gained,

        marker_color="#35cf84",

        hovertemplate="%{x}<br>+%{y} entradas<extra></extra>",

    )

    fig.add_bar(

        name="Reapareceram",

        x=dates,

        y=reappeared,

        marker_color="#5aa7ff",

        hovertemplate="%{x}<br>+%{y} reapareceram<extra></extra>",

    )

    fig.add_bar(

        name="Ausentes",

        x=dates,

        y=missing,

        marker_color="#f7bd4f",

        hovertemplate="%{x}<br>%{y} aguardando validação<extra></extra>",

    )

    fig.add_bar(

        name="Remoções confirmadas",

        x=dates,

        y=lost,

        marker_color="#f36d7d",

        customdata=[abs(value) for value in lost],

        hovertemplate="%{x}<br>-%{customdata} confirmadas<extra></extra>",

    )

    fig.update_layout(

        title={"text": f"Mudanças em {subject}", "x": 0, "xanchor": "left"},

        barmode="group",

        font={"family": "Inter, sans-serif", "size": 12, "color": "#f7f7fa"},

        paper_bgcolor="rgba(0,0,0,0)",

        plot_bgcolor="rgba(0,0,0,0)",

        height=350,

        margin={"l": 48, "r": 20, "t": 64, "b": 46},

        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "right", "x": 1},

        hoverlabel={"bgcolor": "#202029", "bordercolor": "#454551", "font_color": "#ffffff"},

    )

    fig.update_xaxes(showgrid=False, tickformat="%d/%m")

    fig.update_yaxes(

        title="Eventos / remoções confirmadas (-)",

        showgrid=True,

        gridcolor="rgba(255,255,255,.08)",

        zeroline=True,

        zerolinecolor="rgba(255,255,255,.3)",

        rangemode="tozero",

    )

    return fig.to_html(

        full_html=False,

        include_plotlyjs="cdn",

        config={"displayModeBar": False, "responsive": True, "locale": "pt-BR"},

    )



@app.route("/reset", methods=["POST"])

@login_required

def reset_db():

    try:

        # Reset analysis data but preserve user accounts and their passwords.

        user_id = session["user_id"]

        History.query.filter_by(user_id=user_id).delete()

        Follower.query.filter_by(user_id=user_id).delete()

        db.session.commit()

    except Exception as e: print(e)

    return redirect(url_for('index'))



if __name__ == "__main__":

    app.run(debug=True)

