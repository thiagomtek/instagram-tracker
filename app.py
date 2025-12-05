from flask import Flask, render_template, request, redirect, url_for
from werkzeug.utils import secure_filename
from config import Config
from database import db
from services.parser import parse_instagram_html, detect_kind
from services.storage import save_followers, load_followers
from services.compare import compare_followers, cross_reference
import os
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
from datetime import datetime, timedelta

app = Flask(__name__)
app.config.from_object(Config)
os.makedirs("uploads", exist_ok=True)

db.init_app(app)

class History(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String, nullable=False)
    action = db.Column(db.String, nullable=False)
    kind = db.Column(db.String, nullable=False)
    date_added = db.Column(db.DateTime, default=datetime.now)

with app.app_context():
    db.create_all()

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

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/upload", methods=["POST"])
def upload():
    files = request.files.getlist("files")
    if not files or files[0].filename == '': return redirect(url_for('index'))

    for file in files:
        filename = secure_filename(file.filename)
        filepath = os.path.join("uploads", filename)
        file.save(filepath)
        kind = detect_kind(filepath, filename)
        if not kind: continue

        parsed_data = parse_instagram_html(filepath)

        if kind in ['followers', 'following']:
  
            gained_names, lost_names = save_followers(kind, parsed_data)

            for user in gained_names:
                user_date = next((item['date'] for item in parsed_data if item['username'] == user), datetime.now())
                db.session.add(History(username=user, action='gained', kind=kind, date_added=user_date))
            
            for user in lost_names:
                db.session.add(History(username=user, action='lost', kind=kind, date_added=datetime.now()))
            
            db.session.commit()
        
        elif kind == 'recently_unfollowed':
             for item in parsed_data:
                 exists = History.query.filter_by(username=item['username'], action='lost', kind='following').first()
                 if not exists:
                     db.session.add(History(username=item['username'], action='lost', kind='following', date_added=item['date']))
             db.session.commit()

    return redirect(url_for('dashboard'))

@app.route("/dashboard")
def dashboard():
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')
    username_filter = request.args.get('username', '').strip()
    action_filter = request.args.get('action', 'all') # all, gained, lost

    if not start_date_str: start_date = datetime.now() - timedelta(days=365*5)
    else: start_date = datetime.strptime(start_date_str, '%Y-%m-%d')

    if not end_date_str: end_date = datetime.now() + timedelta(days=1)
    else: end_date = datetime.strptime(end_date_str, '%Y-%m-%d') + timedelta(days=1)

    query = History.query.filter(History.date_added >= start_date, History.date_added <= end_date)

    if username_filter:
        query = query.filter(History.username.ilike(f"%{username_filter}%"))

    if action_filter != 'all':
        query = query.filter(History.action == action_filter)

    history_logs = query.order_by(History.date_added.desc()).all()


    current_followers = load_followers('followers')
    current_following = load_followers('following')
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
        height=450,
        margin=dict(l=20, r=20, t=60, b=20),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
    )

    fig.update_xaxes(showgrid=False, gridcolor='#444')
    fig.update_yaxes(showgrid=True, gridcolor='#444', zeroline=True, zerolinecolor='#666')

    return fig.to_html(full_html=False, config={'displayModeBar': False})

@app.route("/reset", methods=["POST"])
def reset_db():
    try:
        db.drop_all(); db.create_all()
    except Exception as e: print(e)
    return redirect(url_for('index'))

if __name__ == "__main__":
    app.run(debug=True)