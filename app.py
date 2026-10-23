from functools import wraps
import hmac
import json
from pathlib import Path
import re
import secrets
from tempfile import TemporaryDirectory

from flask import Flask, Response, flash, jsonify, render_template, request, redirect, session, url_for
from google_auth_oauthlib.flow import Flow
from googleapiclient.errors import HttpError
import requests
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename
from config import Config
from database import db
from models.follower import Follower
from models.history import History
from models.integration import AnalysisRun, DriveConnection, DriveImport, PushSubscription
from models.user import User
from services.storage import load_followers
from services.compare import cross_reference
from services.google_drive import (
    drive_service,
    google_drive_is_configured,
    sync_connection_safely,
    validate_folder,
)
from services.importer import process_import_paths
from services.analysis_runs import complete_analysis_run, fail_analysis_run, start_analysis_run
from services.token_crypto import decrypt_token, encrypt_token
import os
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

app = Flask(__name__)
app.config.from_object(Config)
if app.config["TRUST_PROXY_HEADERS"]:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
os.makedirs(os.environ.get("DATA_DIR", os.path.dirname(__file__)), exist_ok=True)

db.init_app(app)

def ensure_ownership_columns():
    """Small SQLite migration for installations created before multi-user support."""
    inspector = db.inspect(db.engine)
    for table_name in ("follower", "history"):
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        if "user_id" not in columns:
            db.session.execute(db.text(f"ALTER TABLE {table_name} ADD COLUMN user_id INTEGER"))
        db.session.execute(db.text(f"CREATE INDEX IF NOT EXISTS ix_{table_name}_user_id ON {table_name} (user_id)"))
    db.session.commit()


def assign_orphaned_data(user_id):
    """Claims legacy data only when the database receives its first account."""
    History.query.filter(History.user_id.is_(None)).update({History.user_id: user_id})
    Follower.query.filter(Follower.user_id.is_(None)).update({Follower.user_id: user_id})
    db.session.commit()


with app.app_context():
    db.create_all()
    ensure_ownership_columns()
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
            summary = process_import_paths(paths, user_id)
        complete_analysis_run(analysis_run, summary)
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


def _extract_drive_folder_id(value):
    value = value.strip()
    if value.lower() in {"root", "meu drive", "my drive"}:
        return "root"
    match = re.search(r"/folders/([A-Za-z0-9_-]+)", value)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", value):
        return value
    raise ValueError("Cole o link completo da pasta ou informe um ID válido.")


@app.route("/integrations/google/folder", methods=["POST"])
@login_required
def google_drive_folder():
    connection = DriveConnection.query.filter_by(user_id=session["user_id"]).first()
    if not connection:
        flash("Vincule uma conta do Google antes de escolher a pasta.", "error")
        return redirect(url_for("index"))
    try:
        folder_id = _extract_drive_folder_id(request.form.get("folder", ""))
        metadata = validate_folder(connection, folder_id)
        connection.folder_id = metadata["id"]
        connection.folder_name = metadata["name"]
        connection.enabled = True
        connection.last_error = None
        db.session.commit()
        flash(f"A pasta “{metadata['name']}” será monitorada automaticamente.", "success")
    except (ValueError, HttpError) as exc:
        db.session.rollback()
        flash(f"Não foi possível acessar essa pasta: {exc}", "error")
    return redirect(url_for("index"))


@app.route("/integrations/google/sync", methods=["POST"])
@login_required
def google_drive_sync_now():
    connection = DriveConnection.query.filter_by(user_id=session["user_id"]).first()
    if not connection or not connection.folder_id:
        flash("Escolha uma pasta do Google Drive primeiro.", "error")
        return redirect(url_for("index"))
    imported = sync_connection_safely(connection)
    current = db.session.get(DriveConnection, connection.id)
    if current and current.last_error:
        flash(f"A sincronização falhou: {current.last_error}", "error")
    elif imported:
        flash(f"Sincronização concluída: {len(imported)} nova(s) exportação(ões) processada(s).", "success")
    else:
        flash("A pasta está em dia; nenhuma exportação nova foi encontrada.", "success")
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

@app.route("/dashboard")
@login_required
def dashboard():
    user_id = session["user_id"]
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')
    username_filter = request.args.get('username', '').strip()
    action_filter = request.args.get('action', 'all') # all, gained, lost

    if not start_date_str: start_date = datetime.now() - timedelta(days=365*5)
    else: start_date = datetime.strptime(start_date_str, '%Y-%m-%d')

    if not end_date_str: end_date = datetime.now() + timedelta(days=1)
    else: end_date = datetime.strptime(end_date_str, '%Y-%m-%d') + timedelta(days=1)

    query = History.query.filter(History.user_id == user_id, History.date_added >= start_date, History.date_added <= end_date)

    if username_filter:
        query = query.filter(History.username.ilike(f"%{username_filter}%"))

    if action_filter != 'all':
        query = query.filter(History.action == action_filter)

    history_logs = query.order_by(History.date_added.desc()).all()


    current_followers = load_followers('followers', user_id)
    current_following = load_followers('following', user_id)
    not_following_back, fans, mutuals = cross_reference(current_followers, current_following)

    graph_html = generate_intelligent_graph(history_logs)

    kpis = {
        "followers_total": len(current_followers),
        "gained_filtered": len([h for h in history_logs if h.action == 'gained']),
        "lost_filtered": len([h for h in history_logs if h.action == 'lost'])
    }

    return render_template("dashboard.html", 
                           history_logs=history_logs,
                           not_following_back=not_following_back,
                           fans=fans,
                           mutuals=mutuals,
                           graph_html=graph_html,
                           kpis=kpis,
                           filters={
                               'start': start_date_str, 
                               'end': end_date_str,
                               'username': username_filter,
                               'action': action_filter
                           })


@app.route("/processamentos")
@login_required
def processing_history():
    page = request.args.get("page", 1, type=int)
    pagination = (
        AnalysisRun.query.filter_by(user_id=session["user_id"])
        .order_by(AnalysisRun.started_at.desc())
        .paginate(page=page, per_page=30, error_out=False)
    )
    for run in pagination.items:
        try:
            run.gained_list = json.loads(run.gained_usernames or "[]")
            run.lost_list = json.loads(run.lost_usernames or "[]")
        except json.JSONDecodeError:
            run.gained_list = []
            run.lost_list = []
    return render_template("processing_history.html", pagination=pagination)

def generate_intelligent_graph(history_logs):
    if not history_logs: return "<div class='text-center text-muted p-5'>Sem dados para exibir</div>"

    data = []
    for log in history_logs:

        tipo_legivel = "Ganhou" if log.action == 'gained' else "Perdeu"
        cat_legivel = "Seguidores" if log.kind == 'followers' else "Seguindo"
        
        data.append({
            "Data": log.date_added.strftime("%Y-%m-%d"),
            "Ação": tipo_legivel,
            "Categoria": cat_legivel,
            "Qtd": 1
        })

    df = pd.DataFrame(data)
    if df.empty: return ""
    

    df_grouped = df.groupby(["Data", "Categoria", "Ação"]).size().reset_index(name="Qtd")

 
    color_map = {
        "Ganhou": "#00e676", 
        "Perdeu": "#ff1744"   
    }

    fig = px.bar(df_grouped, x="Data", y="Qtd", color="Ação", 
                 facet_row="Categoria",
                 barmode="group",
                 color_discrete_map=color_map,
                 title="Fluxo Temporal (Filtrado)")

    fig.update_layout(
        font=dict(family="Inter, sans-serif", size=12, color="#ffffff"), 
        paper_bgcolor='rgba(0,0,0,0)', 
        plot_bgcolor='rgba(255,255,255,0.05)', 
        height=420,
        margin=dict(l=20, r=20, t=60, b=20),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
    )

    fig.update_xaxes(showgrid=False, gridcolor='#444')
    fig.update_yaxes(showgrid=True, gridcolor='#444', zeroline=True, zerolinecolor='#666')

    return fig.to_html(full_html=False, config={'displayModeBar': False, 'responsive': True})

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
