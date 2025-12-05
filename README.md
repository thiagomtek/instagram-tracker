# 📊 Instagram Analytics Hub

> Um dashboard local, privado e inteligente para análise de dados do Instagram. Monitore o crescimento, identifique quem não te segue de volta e visualize sua linha do tempo de seguidores com precisão histórica.

![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![Flask](https://img.shields.io/badge/Flask-2.0%2B-green)
![Status](https://img.shields.io/badge/Status-Stable-brightgreen)
![License](https://img.shields.io/badge/License-MIT-yellow)

---

## 🚀 Sobre o Projeto

Este software processa os arquivos de **Exportação de Dados do Instagram (formato HTML)** para gerar relatórios detalhados sobre o seu perfil. 

Diferente de aplicativos de terceiros que exigem sua senha e arriscam o bloqueio da sua conta, este sistema roda **localmente** na sua máquina e utiliza os dados oficiais fornecidos pelo Instagram, garantindo 100% de segurança e privacidade.

### ✨ Principais Funcionalidades

* **🛡️ 100% Seguro:** Não requer login ou senha. Trabalha offline com arquivos `.html`.
* **🕵️ Análise de Relacionamento:**
    * **Traidores:** Quem você segue, mas não te segue de volta.
    * **Fãs:** Quem te segue, mas você não segue.
    * **Mútuos:** Amigos que se seguem mutuamente.
* **🧠 Inteligência Temporal (Smart History):**
    * Lê as datas originais dos arquivos do Instagram para reconstruir o histórico fiel (ex: sabe que você seguiu alguém em 2019, mesmo importando o arquivo hoje).
    * **Smart Merge:** Permite upload de arquivos parciais (ex: apenas dados do último mês) sem apagar o histórico antigo do banco de dados.
* **📉 Linha do Tempo Detalhada:** Veja dia a dia quem começou a seguir ou deixou de seguir.
* **📊 Gráficos Interativos:** Visualização de fluxo (Ganhos vs Perdas) com Plotly.
* **🔍 Filtros Avançados:** Filtre o histórico por Nome de Usuário, Data ou Tipo de Evento.
* **🎨 UI Moderna:** Interface "Glassmorphism" (efeito de vidro), modo escuro e responsiva.
* **⚡ Performance:** Utiliza avatares gerados (iniciais) ou API pública (Unavatar) para evitar bloqueios de IP por scraping.

---

## 🛠️ Tecnologias Utilizadas

* **Backend:** Python, Flask, SQLAlchemy (SQLite).
* **Processamento de Dados:** Pandas, BeautifulSoup4.
* **Frontend:** HTML5, CSS3 (Glassmorphism), Bootstrap 5, Jinja2.
* **Visualização:** Plotly (Gráficos Dinâmicos).

---

## ⚙️ Instalação e Configuração

### Pré-requisitos
* Python 3.8 ou superior instalado.

### Passo a Passo

1.  **Clone o repositório:**
    ```bash
    git clone [https://github.com/seu-usuario/instagram-tracker.git](https://github.com/seu-usuario/instagram-tracker.git)
    cd instagram-tracker
    ```

2.  **Crie um ambiente virtual (Opcional, mas recomendado):**
    ```bash
    # Windows
    python -m venv venv
    venv\Scripts\activate

    # Mac/Linux
    python3 -m venv .venv
    source .venv/bin/activate
    ```

3.  **Instale as dependências:**
    ```bash
    pip install -r requirements.txt
    ```
    *(Se não tiver o arquivo requirements.txt, instale manualmente: `pip install flask flask-sqlalchemy beautifulsoup4 plotly pandas`)*

4.  **Execute a aplicação:**
    ```bash
    python app.py
    ```

5.  **Acesse no navegador:**
    Abra `http://127.0.0.1:5000`

---

## 📂 Como Obter seus Dados do Instagram

Para alimentar o sistema, você precisa baixar seus dados oficiais:

1.  Abra o App do Instagram ou vá em `instagram.com`.
2.  Vá em **Configurações** > **Sua atividade**.
3.  Procure por **Baixar suas informações**.
4.  Selecione **"Baixar ou transferir informações"** > **"Algumas de suas informações"**.
5.  Marque: **Seguidores e seguindo**. (Opcional: Marque também "Informações do perfil" e outros para dados extras).
6.  Selecione **Baixar para o dispositivo**.
7.  **IMPORTANTE:**
    * Intervalo de datas: **Desde o início** (para a primeira vez).
    * Formato: **HTML** (Não use JSON).
8.  Quando o Instagram enviar o email, baixe o ZIP e extraia.

---

## 🖥️ Como Usar

1.  Na tela inicial do sistema, você verá uma área de **Upload**.
2.  Arraste **TODOS** os arquivos HTML relevantes de uma vez para a caixa. O sistema suporta:
    * `followers_1.html` (e outros números)
    * `following.html`
    * `pending_follow_requests.html`
    * `recently_unfollowed_profiles.html`
    * `blocked_profiles.html`
    * `close_friends.html`
3.  Clique em **Processar Dados**.
4.  O Dashboard será gerado automaticamente com o histórico retroativo.

---

## 🗂️ Estrutura do Projeto
instagram-tracker/ │ ├── app.py # Arquivo principal (Rotas e Lógica) ├── config.py # Configurações do Flask ├── database.py # Instância do SQLAlchemy ├── data.db # Banco de dados SQLite (gerado automaticamente) │ ├── services/ │ ├── parser.py # Lê o HTML e extrai datas/usuários │ ├── storage.py # Lógica inteligente de salvar/mesclar no banco │ └── compare.py # Matemática de conjuntos (Ganhos/Perdas) │ ├── static/ │ └── style.css # Estilos CSS (Glassmorphism) │ └── templates/ ├── layout.html # Base do HTML ├── index.html # Tela de Upload └── dashboard.html # Painel de Análise


