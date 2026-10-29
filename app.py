import hashlib
import hmac
import json
import os
import re
import secrets
import time
from collections import defaultdict
from functools import wraps
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import plotly
import requests
from flask import Flask, Response, abort, flash, g, jsonify, redirect, render_template, request, session, url_for
from google_auth_oauthlib.flow import Flow
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, OperationalError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from config import Config
from database import db
from models.follower import Follower
from models.history import History
from models.integration import AnalysisRun, DriveConnection, DriveImport, PushSubscription, RelationshipValidation
from models.system import ImportObservation
from models.user import User
from services.analysis_runs import complete_analysis_run, fail_analysis_run, start_analysis_run
from services.clock import local_now, utc_now
from services.compare import cross_reference
from services.google_drive import google_drive_is_configured
from services.importer import ImportRejected, process_import_paths
from services.network import get_status as get_network_status, set_direct_fallback
from services.notifications import send_analysis_notification
from services.presentation import (
    build_validation_progress,
    format_local_datetime,
    format_validation_attempt_time,
    generate_activity_graph,
)
from services.schema import run_schema_upgrades
from services.security import clear_failures, register_failure, seconds_locked
from services.storage import order_usernames_newest_first
from services.token_crypto import decrypt_token, encrypt_token

ACTIVITY_LIST_LIMIT = 500
MIN_PASSWORD_LENGTH = 12
DEFAULT_PERIOD_DAYS = 90
# Compared against when the e-mail does not exist, so response time does not reveal it.
_DUMMY_PASSWORD_HASH = generate_password_hash("not-a-real-password")
_plotly_bundle = {}

app = Flask(__name__)
app.config.from_object(Config)
if app.config["TRUST_PROXY_HEADERS"]:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
if app.config["SECRET_KEY_IS_EPHEMERAL"]:
    app.logger.warning("SECRET_KEY não definido: sessões serão invalidadas a cada reinício. Use apenas em desenvolvimento.")

os.makedirs(os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__))), exist_ok=True)
db.init_app(app)


def assign_orphaned_data(user_id):
    """Claims legacy data only when the database receives its first account."""
    History.query.filter(History.user_id.is_(None)).update({History.user_id: user_id})
    Follower.query.filter(Follower.user_id.is_(None)).update({Follower.user_id: user_id})
    db.session.commit()


def create_initial_admin():
    email = app.config["INITIAL_ADMIN_EMAIL"].strip().lower()
    password = app.config["INITIAL_ADMIN_PASSWORD"]
    if not (email and password) or User.query.count() > 0:
        return
    user = User(email=email)
    user.set_password(password)
    db.session.add(user)
    try:
        db.session.commit()
    except IntegrityError:
        # Another process (Gunicorn worker or the Drive worker) won the race.
        db.session.rollback()
        return
    assign_orphaned_data(user.id)


def initialize_database(attempts=6):
    """Create tables, upgrade the schema and seed the first account.

    The web processes and the worker import this module at the same time, so
    two of them can race on DDL ("table already exists", duplicate rows). Every
    step is idempotent: the loser rolls back, waits and runs the steps again.
    """
    for attempt in range(1, attempts + 1):
        try:
            with app.app_context():
                db.create_all()
                run_schema_upgrades()
                create_initial_admin()
            return
        except (OperationalError, IntegrityError):
            # The context used by the failed attempt has already closed.
            # Open a fresh one before touching Flask-SQLAlchemy's scoped session.
            with app.app_context():
                db.session.remove()
            if attempt == attempts:
                raise
            app.logger.warning("Inicialização concorrente do banco; tentativa %s/%s repetida.", attempt, attempts)
            time.sleep(0.5 * attempt)


initialize_database()


# --------------------------------------------------------------------------- helpers

def is_valid_email(email):
    return len(email) <= 255 and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) is not None


def validate_password(password, confirmation):
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"A senha deve ter pelo menos {MIN_PASSWORD_LENGTH} caracteres."
    if password != confirmation:
        return "As senhas não coincidem."
    return None


def registration_open():
    return app.config["ALLOW_REGISTRATION"] or User.query.count() == 0


def start_session(user, remember=True):
    session.clear()
    session["user_id"] = user.id
    session["epoch"] = user.session_epoch
    session["remember"] = remember
    session.permanent = remember


def json_list(raw):
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def erase_analysis_data(user_id):
    """Remove everything derived from imports; the account and Drive link stay."""
    RelationshipValidation.query.filter_by(user_id=user_id).delete()
    connection_ids = [
        row.id
        for row in DriveConnection.query.filter_by(user_id=user_id).with_entities(DriveConnection.id)
    ]
    if connection_ids:
        DriveImport.query.filter(DriveImport.connection_id.in_(connection_ids)).delete(
            synchronize_session=False
        )
        DriveConnection.query.filter(DriveConnection.id.in_(connection_ids)).update(
            {
                DriveConnection.last_success_at: None,
                DriveConnection.last_error: None,
                DriveConnection.last_import_summary: None,
                DriveConnection.sync_requested: False,
            },
            synchronize_session=False,
        )
    AnalysisRun.query.filter_by(user_id=user_id).delete()
    History.query.filter_by(user_id=user_id).delete()
    Follower.query.filter_by(user_id=user_id).delete()
    ImportObservation.query.filter_by(user_id=user_id).delete()


def revoke_and_remove_drive_connection(connection):
    try:
        token = decrypt_token(connection.encrypted_refresh_token)
        requests.post("https://oauth2.googleapis.com/revoke", params={"token": token}, timeout=10)
    except Exception:
        app.logger.warning("Não foi possível revogar o token no Google; removendo a cópia local.")
    DriveImport.query.filter_by(connection_id=connection.id).delete()
    db.session.delete(connection)


# --------------------------------------------------------------------------- request hooks

@app.context_processor
def inject_template_globals():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)

    def asset(filename):
        """Static URL versioned by the file's modification time (cache busting)."""
        try:
            version = int((Path(app.static_folder) / filename).stat().st_mtime)
        except OSError:
            version = 0
        return url_for("static", filename=filename, v=version)

    return {
        "csrf_token": session["csrf_token"],
        "push_public_key": app.config["PUSH_VAPID_PUBLIC_KEY"],
        "asset": asset,
        "registration_open": registration_open(),
    }


@app.before_request
def verify_csrf_token():
    if request.method == "POST":
        submitted_token = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
        expected_token = session.get("csrf_token", "")
        if not expected_token or not hmac.compare_digest(submitted_token, expected_token):
            return "Solicitação inválida. Atualize a página e tente novamente.", 400


@app.after_request
def set_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if request.endpoint not in ("static", "plotly_js", "service_worker", "web_manifest", "offline", "healthz"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        user_id = session.get("user_id")
        user = db.session.get(User, user_id) if user_id else None
        if user is None or session.get("epoch", 0) != user.session_epoch:
            session.clear()
            return redirect(url_for("login"))
        g.user = user
        session.permanent = session.get("remember", True)
        return view(*args, **kwargs)

    return wrapped_view


@app.template_filter("avatar_color")
def avatar_color(username):
    if not username:
        return "rgb(50, 50, 50)"
    digest = hashlib.md5(username.encode(), usedforsecurity=False).hexdigest()
    r, g_, b = (int(digest[index:index + 2], 16) % 150 for index in (0, 2, 4))
    return f"rgb({r}, {g_}, {b})"


@app.template_filter("initials")
def initials_filter(username):
    return username[:2].upper() if username else "??"


@app.template_filter("format_date")
def format_date(value):
    """Snapshot dates (History/Follower) are already naive local time."""
    return "" if value is None else value.strftime("%d/%m/%Y %H:%M")


app.add_template_filter(format_local_datetime, "local_datetime")


@app.template_filter("duration")
def format_duration(run):
    if not run.completed_at:
        return "em andamento"
    seconds = max(0, int((run.completed_at - run.started_at).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}min {seconds}s"


# --------------------------------------------------------------------------- public routes

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
                {"src": url_for("static", filename="icon-192.png"), "sizes": "192x192", "type": "image/png", "purpose": "any"},
                {"src": url_for("static", filename="icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "any"},
                {"src": url_for("static", filename="icon-maskable-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
                {"src": url_for("static", filename="icon.svg"), "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
            ],
        }
    )
    response.headers["Content-Type"] = "application/manifest+json"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/healthz")
def healthz():
    try:
        db.session.execute(db.text("SELECT 1"))
    except Exception:
        app.logger.exception("Health check do banco falhou")
        return jsonify({"status": "error"}), 503
    return jsonify({"status": "ok"})


@app.route("/service-worker.js")
def service_worker():
    service_worker_path = Path(app.static_folder) / "service-worker.js"
    response = Response(service_worker_path.read_text(encoding="utf-8"), mimetype="application/javascript")
    response.headers["Service-Worker-Allowed"] = f"{request.script_root.rstrip('/')}/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/vendor/plotly.min.js")
def plotly_js():
    """Serve plotly.js from the installed package so the dashboard needs no third-party CDN."""
    if "js" not in _plotly_bundle:
        _plotly_bundle["js"] = plotly.offline.get_plotlyjs()
    response = Response(_plotly_bundle["js"], mimetype="application/javascript")
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


@app.route("/offline")
def offline():
    return render_template("offline.html")


@app.errorhandler(413)
def upload_too_large(_error):
    flash("O upload excede o limite configurado no servidor.", "error")
    return redirect(url_for("index"))


# --------------------------------------------------------------------------- accounts

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id") and db.session.get(User, session["user_id"]):
        return redirect(url_for("index"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        client = request.remote_addr or "unknown"

        wait = seconds_locked(client, email)
        if wait:
            flash(f"Muitas tentativas. Tente novamente em {max(1, round(wait / 60))} minuto(s).", "error")
            return render_template("login.html"), 429

        user = User.query.filter_by(email=email).first()
        valid = check_password_hash(user.password_hash if user else _DUMMY_PASSWORD_HASH, password)
        if user and valid:
            clear_failures(client, email)
            start_session(user, remember=request.form.get("remember") == "on")
            return redirect(url_for("index"))

        register_failure(client, email)
        flash("E-mail ou senha inválidos.", "error")

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("user_id") and db.session.get(User, session["user_id"]):
        return redirect(url_for("index"))
    if not registration_open():
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
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("Já existe uma conta com este e-mail.", "error")
                return render_template("register.html")
            if is_first_user:
                assign_orphaned_data(user.id)
            start_session(user)
            flash("Conta criada. Você já pode usar o painel.", "success")
            return redirect(url_for("index"))

    return render_template("register.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/account")
@login_required
def account():
    return render_template("account.html", min_password_length=MIN_PASSWORD_LENGTH)


@app.route("/account/password", methods=["POST"])
@login_required
def change_password():
    user = g.user
    new_password = request.form.get("new_password", "")
    if not user.check_password(request.form.get("current_password", "")):
        flash("A senha atual está incorreta.", "error")
    elif password_error := validate_password(new_password, request.form.get("new_password_confirmation", "")):
        flash(password_error, "error")
    else:
        user.set_password(new_password)
        user.session_epoch += 1
        db.session.commit()
        # Every other device signed in with the old password is now logged out.
        session["epoch"] = user.session_epoch
        flash("Senha alterada. Os outros dispositivos foram desconectados.", "success")
    return redirect(url_for("account"))


@app.route("/account/delete", methods=["POST"])
@login_required
def delete_account():
    user = g.user
    if not user.check_password(request.form.get("password", "")) or request.form.get("confirmation", "").strip().upper() != "EXCLUIR":
        flash("Confirme sua senha e digite EXCLUIR para apagar a conta.", "error")
        return redirect(url_for("account"))

    user_id = user.id
    try:
        connection = DriveConnection.query.filter_by(user_id=user_id).first()
        if connection:
            revoke_and_remove_drive_connection(connection)
        erase_analysis_data(user_id)
        PushSubscription.query.filter_by(user_id=user_id).delete()
        db.session.delete(user)
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Falha ao excluir a conta %s", user_id)
        flash("Não foi possível excluir a conta. Tente novamente.", "error")
        return redirect(url_for("account"))

    session.clear()
    flash("Conta e dados excluídos.", "success")
    return redirect(url_for("login"))


# --------------------------------------------------------------------------- import

@app.route("/")
@login_required
def index():
    drive_connection = DriveConnection.query.filter_by(user_id=session["user_id"]).first()
    return render_template(
        "index.html",
        drive_connection=drive_connection,
        google_configured=google_drive_is_configured(),
        push_configured=bool(app.config["PUSH_VAPID_PUBLIC_KEY"]),
        needs_drop_confirmation=request.args.get("confirm_drop") == "1",
        drive_delete_mode=app.config["GOOGLE_DRIVE_DELETE_MODE"],
    )


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    user_id = session["user_id"]
    files = request.files.getlist("files")
    if not files or files[0].filename == "":
        flash("Selecione ao menos um arquivo.", "error")
        return redirect(url_for("index"))

    allow_large_drop = request.form.get("confirm_large_drop") == "1"
    source_name = ", ".join((file.filename or "arquivo") for file in files)
    analysis_run = start_analysis_run(user_id, "manual", source_name)

    try:
        with TemporaryDirectory(prefix="instagram-upload-") as temp_dir:
            paths = []
            for position, uploaded_file in enumerate(files):
                filename = secure_filename(uploaded_file.filename) or f"arquivo-{position}"
                target = Path(temp_dir) / f"{position}-{filename}"
                uploaded_file.save(target)
                paths.append(target)
            summary = process_import_paths(
                paths, user_id, analysis_run_id=analysis_run.id, allow_large_drop=allow_large_drop
            )

        complete_analysis_run(analysis_run, summary)
        if summary.is_duplicate:
            flash(summary.message(), "warning")
            return redirect(url_for("dashboard"))
        send_analysis_notification(user_id, summary)
        flash(f"Análise concluída. {summary.message()}", "success")
        return redirect(url_for("dashboard"))

    except ImportRejected as exc:
        fail_analysis_run(analysis_run.id, exc)
        flash(str(exc), "error")
        return redirect(url_for("index", confirm_drop=1 if exc.needs_confirmation else None))
    except UnicodeDecodeError as exc:
        fail_analysis_run(analysis_run.id, exc)
        flash("Um dos arquivos não está em UTF-8. Use a exportação original do Instagram.", "error")
        return redirect(url_for("index"))
    except (ValueError, json.JSONDecodeError) as exc:
        fail_analysis_run(analysis_run.id, exc)
        flash(str(exc), "error")
        return redirect(url_for("index"))
    except Exception:
        # The exception text may contain server paths; keep it in the log only.
        fail_analysis_run(analysis_run.id, "Erro interno ao processar os arquivos. Os detalhes estão no log do servidor.")
        app.logger.exception("Falha inesperada na importação manual")
        flash("A análise falhou. Consulte o histórico de processamentos para ver o erro.", "error")
        return redirect(url_for("processing_history"))


@app.route("/reset", methods=["POST"])
@login_required
def reset_db():
    try:
        erase_analysis_data(session["user_id"])
        db.session.commit()
        flash("Histórico e listas apagados. A próxima importação criará uma nova base.", "success")
    except Exception:
        db.session.rollback()
        app.logger.exception("Falha ao apagar o histórico do usuário %s", session["user_id"])
        flash("Não foi possível apagar o histórico. Tente novamente.", "error")
    return redirect(url_for("index"))


# --------------------------------------------------------------------------- Google Drive

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


def drive_service_from_credentials(credentials):
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=credentials, cache_discovery=False)


@app.route("/integrations/google/connect")
@login_required
def google_drive_connect():
    if not google_drive_is_configured():
        flash("A integração com o Google Drive ainda não foi configurada no servidor.", "error")
        return redirect(url_for("index"))

    flow = Flow.from_client_config(_google_client_config(), scopes=app.config["GOOGLE_DRIVE_SCOPES"])
    flow.redirect_uri = _google_redirect_uri()
    authorization_url, state = flow.authorization_url(access_type="offline", prompt="consent")
    session["google_oauth_state"] = state
    if getattr(flow, "code_verifier", None):
        session["google_oauth_code_verifier"] = flow.code_verifier
    return redirect(authorization_url)


@app.route("/integrations/google/callback")
@login_required
def google_drive_callback():
    expected_state = session.pop("google_oauth_state", None)
    code_verifier = session.pop("google_oauth_code_verifier", None)
    if not expected_state or not hmac.compare_digest(request.args.get("state", ""), expected_state):
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
            connection = DriveConnection(user_id=session["user_id"], encrypted_refresh_token="")
            db.session.add(connection)

        connection.encrypted_refresh_token = encrypt_token(credentials.refresh_token)
        connection.google_email = about.get("emailAddress") or about.get("displayName")
        connection.folder_id = "root"
        connection.folder_name = "Meu Drive (pastas meta-*)"
        connection.enabled = True
        connection.last_error = None
        db.session.commit()
        flash("Conta do Google Drive vinculada com sucesso.", "success")
    except Exception:
        db.session.rollback()
        app.logger.exception("Falha no retorno do OAuth do Google")
        flash("Não foi possível vincular o Google Drive. Tente novamente; os detalhes estão no log do servidor.", "error")

    return redirect(url_for("index"))


@app.route("/integrations/google/sync", methods=["POST"])
@login_required
def google_drive_sync_now():
    connection = DriveConnection.query.filter_by(user_id=session["user_id"]).first()
    if not connection:
        flash("Vincule uma conta do Google Drive primeiro.", "error")
        return redirect(url_for("index"))

    # The worker runs the sync: a large export can take longer than a web request may last.
    connection.sync_requested = True
    db.session.commit()
    flash("Verificação solicitada. O worker começa em instantes; o resultado aparece em Processamentos.", "success")
    return redirect(url_for("index"))


@app.route("/integrations/google/disconnect", methods=["POST"])
@login_required
def google_drive_disconnect():
    connection = DriveConnection.query.filter_by(user_id=session["user_id"]).first()
    if connection:
        revoke_and_remove_drive_connection(connection)
        db.session.commit()
    flash("Conta do Google Drive desvinculada deste serviço.", "success")
    return redirect(url_for("index"))


# --------------------------------------------------------------------------- push API

@app.route("/api/push/subscribe", methods=["POST"])
@login_required
def push_subscribe():
    payload = request.get_json(silent=True) or {}
    keys = payload.get("keys") or {}
    endpoint = payload.get("endpoint")
    if (
        not isinstance(endpoint, str)
        or not endpoint.startswith("https://")
        or len(endpoint) > 2048
        or not keys.get("p256dh")
        or not keys.get("auth")
    ):
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
    endpoint = (request.get_json(silent=True) or {}).get("endpoint")
    if endpoint:
        PushSubscription.query.filter_by(user_id=session["user_id"], endpoint=endpoint).delete()
        db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/validations/status")
@login_required
def validation_status():
    return jsonify(build_validation_progress(session["user_id"]))


@app.route("/profile-avatar/<int:follower_id>")
@login_required
def profile_avatar(follower_id):
    follower = db.session.get(Follower, follower_id)
    if not follower or follower.user_id != session["user_id"] or not follower.avatar_data:
        abort(404)
    response = Response(follower.avatar_data, mimetype=follower.avatar_mime or "image/jpeg")
    response.headers["Cache-Control"] = "private, max-age=86400"
    return response


# --------------------------------------------------------------------------- dashboard

def _date_argument(name, default):
    """Absent -> default; present but empty -> no limit (None); invalid -> default."""
    if name not in request.args:
        return default, default.strftime("%Y-%m-%d") if default else ""
    raw = request.args.get(name, "").strip()
    if not raw:
        return None, ""
    try:
        return datetime.strptime(raw, "%Y-%m-%d"), raw
    except ValueError:
        return default, default.strftime("%Y-%m-%d") if default else ""


def _relationship_snapshot(user_id):
    """Group the stored relationships into the lists the dashboard shows."""
    records = Follower.query.filter_by(user_id=user_id).all()
    present = {"followers": {}, "following": {}}
    absent = {"followers": {}, "following": {}}
    unavailable = {}
    avatar_ids = {}
    for item in records:
        key = item.username.casefold()
        if item.avatar_data:
            avatar_ids.setdefault(key, item.id)
        if item.is_present:
            present[item.kind][key] = item
        elif item.state in ("pending", "absent"):
            absent[item.kind][key] = item
        elif item.state == "untestable":
            unavailable.setdefault(key, item)

    # A profile that disappeared is never eligible for a relationship
    # conclusion until it either reappears in an export or its existence is
    # positively proved. This includes profiles that remained unavailable
    # after the web retest (deleted, deactivated or possibly blocking us).
    uncertain = set(absent["followers"]) | set(absent["following"]) | set(unavailable)
    followers = {key: item for key, item in present["followers"].items() if key not in uncertain}
    following = {key: item for key, item in present["following"].items() if key not in uncertain}
    not_following_back, fans, mutuals = cross_reference(
        [item.username for item in followers.values()],
        [item.username for item in following.values()],
    )

    follower_dates = {key: item.date_added for key, item in present["followers"].items()}
    following_dates = {key: item.date_added for key, item in present["following"].items()}
    not_following_back_dates = {name.casefold(): following_dates.get(name.casefold()) for name in not_following_back}
    fans_dates = {name.casefold(): follower_dates.get(name.casefold()) for name in fans}
    mutuals_dates = {
        name.casefold(): max(
            filter(None, (follower_dates.get(name.casefold()), following_dates.get(name.casefold()))),
            default=None,
        )
        for name in mutuals
    }

    absent_dates = {kind: {key: item.missing_since for key, item in absent[kind].items()} for kind in absent}
    both = set(absent["followers"]) & set(absent["following"])
    names = {key: item.username for kind in absent for key, item in absent[kind].items()}
    absent_both_dates = {
        key: max(filter(None, (absent_dates["followers"].get(key), absent_dates["following"].get(key))), default=None)
        for key in both
    }
    followers_only = {key: absent_dates["followers"][key] for key in absent["followers"] if key not in both}
    following_only = {key: absent_dates["following"][key] for key in absent["following"] if key not in both}
    unavailable_dates = {key: item.last_validated_at or item.missing_since for key, item in unavailable.items()}

    return {
        "current_followers": len(followers),
        "current_following": len(following),
        "not_following_back": order_usernames_newest_first(not_following_back, not_following_back_dates),
        "not_following_back_dates": not_following_back_dates,
        "fans": order_usernames_newest_first(fans, fans_dates),
        "fans_dates": fans_dates,
        "mutuals": order_usernames_newest_first(mutuals, mutuals_dates),
        "mutuals_dates": mutuals_dates,
        "absent_both": order_usernames_newest_first([names[key] for key in both], absent_both_dates),
        "absent_both_dates": absent_both_dates,
        "absent_followers_only": order_usernames_newest_first([names[key] for key in followers_only], followers_only),
        "absent_followers_only_dates": followers_only,
        "absent_following_only": order_usernames_newest_first([names[key] for key in following_only], following_only),
        "absent_following_only_dates": following_only,
        "absent_total": len(uncertain),
        "unavailable": order_usernames_newest_first([item.username for item in unavailable.values()], unavailable_dates),
        "unavailable_dates": unavailable_dates,
        "avatar_ids": avatar_ids,
    }


def _activity(user_id, kind, start_date, end_date, username_filter, action_filter):
    conditions = [History.user_id == user_id, History.kind == kind]
    if start_date:
        conditions.append(History.date_added >= start_date)
    if end_date:
        conditions.append(History.date_added < end_date + timedelta(days=1))
    if username_filter:
        escaped = username_filter.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(History.username.ilike(f"%{escaped}%", escape="\\"))
    if action_filter != "all":
        conditions.append(History.action == action_filter)

    counts = dict(
        db.session.query(History.action, func.count(History.id)).filter(*conditions).group_by(History.action).all()
    )
    rows = (
        History.query.filter(*conditions)
        .order_by(History.date_added.desc(), History.id.desc())
        .limit(ACTIVITY_LIST_LIMIT)
        .all()
    )
    daily = defaultdict(dict)
    for day, action, total in (
        db.session.query(func.date(History.date_added), History.action, func.count(History.id))
        .filter(*conditions)
        .group_by(func.date(History.date_added), History.action)
        .all()
    ):
        daily[day][action] = total
    return counts, rows, daily


@app.route("/dashboard")
@login_required
def dashboard():
    user_id = session["user_id"]
    username_filter = request.args.get("username", "").strip()[:100]
    action_filter = request.args.get("action", "all")
    kind_filter = request.args.get("kind", "followers")
    if action_filter not in {"all", "gained", "missing", "reappeared", "lost", "followed_again"}:
        action_filter = "all"
    if kind_filter not in {"followers", "following"}:
        kind_filter = "followers"

    today = local_now().replace(hour=0, minute=0, second=0, microsecond=0)
    start_date, start_text = _date_argument("start_date", today - timedelta(days=DEFAULT_PERIOD_DAYS))
    end_date, end_text = _date_argument("end_date", today)

    counts, history_logs, daily = _activity(user_id, kind_filter, start_date, end_date, username_filter, action_filter)
    snapshot = _relationship_snapshot(user_id)

    gained = counts.get("gained", 0)
    lost = counts.get("lost", 0)
    reappeared = counts.get("reappeared", 0)
    followed_again = counts.get("followed_again", 0)
    kpis = {
        "followers_total": snapshot["current_followers"],
        "following_total": snapshot["current_following"],
        "mutuals_total": len(snapshot["mutuals"]),
        "not_following_back_total": len(snapshot["not_following_back"]),
        "absent_total": snapshot["absent_total"],
        "unavailable_total": len(snapshot["unavailable"]),
        "gained_filtered": gained,
        "missing_filtered": counts.get("missing", 0),
        "lost_filtered": lost,
        "reappeared_filtered": reappeared,
        "followed_again_filtered": followed_again,
        "net_filtered": gained + reappeared + followed_again - lost,
    }

    latest_run = (
        AnalysisRun.query.filter_by(user_id=user_id)
        .filter(AnalysisRun.status.in_(("success", "cleanup_failed")))
        .order_by(AnalysisRun.completed_at.desc(), AnalysisRun.started_at.desc(), AnalysisRun.id.desc())
        .first()
    )
    latest_snapshot = (
        AnalysisRun.query.filter_by(user_id=user_id, import_mode="snapshot")
        .filter(AnalysisRun.status.in_(("success", "cleanup_failed")))
        .order_by(AnalysisRun.completed_at.desc(), AnalysisRun.started_at.desc(), AnalysisRun.id.desc())
        .first()
    )

    return render_template(
        "dashboard.html",
        history_logs=history_logs,
        history_truncated=sum(counts.values()) > len(history_logs),
        history_total=sum(counts.values()),
        history_limit=ACTIVITY_LIST_LIMIT,
        validation_progress=build_validation_progress(user_id),
        graph_html=generate_activity_graph(daily, kind_filter),
        plotly_version=plotly.__version__,
        kpis=kpis,
        latest_run=latest_run,
        latest_snapshot=latest_snapshot,
        filters={
            "start": start_text,
            "end": end_text,
            "username": username_filter,
            "action": action_filter,
            "kind": kind_filter,
        },
        **snapshot,
    )


@app.route("/processamentos")
@login_required
def processing_history():
    user_id = session["user_id"]
    page = request.args.get("page", 1, type=int)
    pagination = (
        AnalysisRun.query.filter_by(user_id=user_id)
        .order_by(AnalysisRun.started_at.desc(), AnalysisRun.id.desc())
        .paginate(page=page, per_page=30, error_out=False)
    )
    runs = pagination.items
    run_ids = [run.id for run in runs]

    confirmed = defaultdict(list)
    next_attempt = {}
    if run_ids:
        for run_id, username in (
            db.session.query(RelationshipValidation.analysis_run_id, RelationshipValidation.username)
            .filter(
                RelationshipValidation.analysis_run_id.in_(run_ids),
                RelationshipValidation.outcome == "relationship_removed",
            )
            .order_by(RelationshipValidation.completed_at.desc())
        ):
            confirmed[run_id].append(username)
        next_attempt = dict(
            db.session.query(RelationshipValidation.analysis_run_id, func.min(RelationshipValidation.next_attempt_at))
            .filter(
                RelationshipValidation.analysis_run_id.in_(run_ids),
                RelationshipValidation.status.in_(("queued", "retry", "running")),
            )
            .group_by(RelationshipValidation.analysis_run_id)
            .all()
        )

    for run in runs:
        run.gained_list = json_list(run.gained_usernames)
        run.missing_list = json_list(run.lost_usernames)
        run.reappeared_list = json_list(run.reappeared_usernames)
        run.followed_again_list = json_list(run.followed_again_usernames)
        run.confirmed_list = confirmed.get(run.id, [])
        run.validation_percent = (
            round((run.validation_completed / run.validation_total) * 100) if run.validation_total else 0
        )
        pending = run.validation_total and run.validation_completed < run.validation_total
        run.next_attempt_formatted = (
            format_validation_attempt_time(next_attempt.get(run.id)) if pending and next_attempt.get(run.id) else None
        )

    network = get_network_status()
    db.session.commit()
    return render_template("processing_history.html", pagination=pagination, network=network, now_utc=utc_now())


@app.route("/network/fallback", methods=["POST"])
@login_required
def network_fallback():
    allowed = request.form.get("allow_direct_fallback") == "1"
    set_direct_fallback(allowed)
    flash(
        "Se a VPN cair, as consultas sairão pela rede direta." if allowed
        else "Se a VPN cair, as consultas ficarão em espera até ela voltar.",
        "success",
    )
    return redirect(url_for("processing_history"))


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
