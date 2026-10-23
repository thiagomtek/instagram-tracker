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

* **🛡️ Privado:** Acesso protegido por login; trabalha offline com arquivos `.html`.
* **👤 Dados isolados por conta:** Cada usuário visualiza, altera e limpa apenas os próprios dados importados.
* **🕵️ Análise de Relacionamento:**
    * **Traidores:** Quem você segue, mas não te segue de volta.
    * **Fãs:** Quem te segue, mas você não segue.
    * **Mútuos:** Amigos que se seguem mutuamente.
* **📸 Retrato atual correto:** Cada arquivo de seguidores/seguindo substitui a lista anterior daquele tipo. As datas históricas do Instagram não são confundidas com a situação atual.
* **📉 Mudanças entre importações:** A primeira importação cria a base; as seguintes registram quem entrou e saiu no momento em que a mudança foi observada.
* **📊 Gráfico interativo:** Entradas aparecem acima de zero e saídas abaixo, com filtros separados para “quem me segue” e “quem eu sigo”.
* **🔍 Filtros Avançados:** Filtre o histórico por Nome de Usuário, Data ou Tipo de Evento.
* **🎨 UI Moderna:** Interface "Glassmorphism" (efeito de vidro), modo escuro e responsiva.
* **⚡ Performance e privacidade:** Usa avatares locais com iniciais, sem buscar fotos de perfil em serviços externos.
* **☁️ Google Drive:** Observa automaticamente onde o Instagram entrega as exportações e remove cada pasta após confirmar a análise.
* **📲 PWA e Web Push:** Pode ser instalado no celular e avisa quando uma análise automática termina.
* **🧹 Sem arquivos residuais:** Uploads, ZIPs e pastas baixadas são processados em diretórios temporários e removidos ao final.

---

## 🛠️ Tecnologias Utilizadas

* **Backend:** Python, Flask, SQLAlchemy (SQLite).
* **Processamento de Dados:** Pandas, BeautifulSoup4.
* **Frontend:** HTML5, CSS3 (Glassmorphism), Bootstrap 5, Jinja2.
* **Visualização:** Plotly (Gráficos Dinâmicos).

---

## ⚙️ Instalação e Configuração

### Docker (recomendado para homelab)

Requer Docker Engine com Docker Compose v2.

1. Crie o arquivo de ambiente e preencha os segredos:

   ```bash
   cp .env.example .env
   openssl rand -hex 32
   ```

   Use o resultado do segundo comando em `SECRET_KEY`. Defina `INITIAL_ADMIN_EMAIL` e `INITIAL_ADMIN_PASSWORD` com as credenciais iniciais. Na primeira inicialização, a aplicação converte essa senha em hash e a armazena no banco SQLite; ela nunca é versionada no Git. Depois de confirmar o acesso, remova `INITIAL_ADMIN_PASSWORD` do `.env`.

2. Inicie o serviço:

   ```bash
   docker compose up -d --build
   ```

3. Acesse `http://IP_DO_SERVIDOR:8080` e entre com as credenciais configuradas.

O volume nomeado `tracker-data` preserva somente o SQLite ao atualizar ou recriar os containers. Os arquivos importados nunca são persistidos. Para backup, pare o serviço e copie esse volume; não use `docker compose down -v`, pois esse comando remove o volume e os dados.

Por padrão, a tela de login permite criar novas contas. Quando todos os usuários necessários tiverem sido cadastrados, altere `ALLOW_REGISTRATION=false` no `.env` e execute `docker compose up -d` para fechar novos cadastros.

Para expor publicamente, mantenha o app atrás de um proxy reverso com HTTPS (por exemplo, Nginx Proxy Manager, Caddy ou Traefik). Não exponha a porta `8080` diretamente na internet.

### Integração automática com o Google Drive

1. No [Google Cloud Console](https://console.cloud.google.com/), crie ou selecione um projeto e ative a **Google Drive API**.
2. Configure a tela de consentimento OAuth e crie um cliente OAuth 2.0 do tipo **Aplicativo da Web**.
3. Cadastre como URI de redirecionamento exatamente a URL pública exibida em `GOOGLE_REDIRECT_URI`, por exemplo:

   ```text
   https://tracker.example.com/integrations/google/callback
   ```

4. Preencha `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` e `GOOGLE_REDIRECT_URI` no `.env`.
5. Gere uma chave exclusiva para criptografar os refresh tokens armazenados:

   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   Copie o resultado para `GOOGLE_TOKEN_ENCRYPTION_KEY`. Não troque essa chave enquanto houver contas vinculadas.

6. Suba novamente os dois serviços:

   ```bash
   docker compose up -d --build
   ```

O serviço web recebe uploads e configurações. O serviço `instagram-tracker-worker` verifica automaticamente o Meu Drive a cada 1 hora. Para não tocar numa transferência ainda em andamento, o worker exige os arquivos de seguidores e seguindo e aguarda o período de estabilidade definido em `GOOGLE_DRIVE_SETTLE_SECONDS` antes de analisar e remover a exportação.

Depois do login, use **Vincular conta do Google**. O padrão observado nas transferências é `Meu Drive/meta-<data-hora>/instagram-<conta>-<data>/connections/followers_and_following/*.html`; por isso, o worker considera somente pastas `meta-*` diretamente no Meu Drive e desce até `followers_and_following`, ignorando a pasta `files` e as outras categorias. O botão **Verificar agora** permanece disponível para uma busca imediata entre os ciclos automáticos.

A permissão solicitada é `drive`, pois o worker precisa baixar e excluir arquivos criados pelo Instagram. A autorização não incorpora permissões concedidas anteriormente ao mesmo projeto Google, evitando misturar escopos de identidade (`openid`, perfil e e-mail) com o escopo do Drive. Depois que a análise e o registro do processamento são confirmados no SQLite, a pasta `meta-*` é excluída permanentemente do Google Drive. Se a exclusão falhar, o resultado continua salvo, o erro aparece no histórico e o worker tenta limpar a origem novamente. Contas vinculadas em uma versão anterior, que concediam somente leitura, precisam ser reconectadas uma vez.

### Histórico de processamentos

A tela **Processamentos** registra importações manuais e automáticas. Cada item mostra origem, horário, duração, quantidade de arquivos, total de seguidores encontrado e os seguidores ganhos/perdidos, incluindo os nomes. Falhas de análise ou de remoção do arquivo de origem também ficam visíveis para auditoria.

### PWA e notificações

O PWA requer HTTPS, exceto em `localhost`. Gere um único par VAPID e guarde-o no `.env`:

```bash
python scripts/generate_vapid_keys.py
```

Copie as duas linhas geradas para `PUSH_VAPID_PUBLIC_KEY` e `PUSH_VAPID_PRIVATE_KEY` e configure `PUSH_VAPID_SUBJECT` com um `mailto:` real. Depois, instale o app pelo menu do navegador e ative as notificações na tela inicial.

`PWA_ID=/instagram-tracker-pwa` dá ao app uma identidade própria. Quando outros PWAs usam o mesmo hostname, mantenha cada aplicação em uma origem HTTPS diferente. Como a porta faz parte da origem web, manifests, service workers, Cache Storage e inscrições Web Push não são compartilhados entre origens distintas. O Tracker pode, portanto, manter `start_url` e `scope` em `/` sem interferir nos outros PWAs.

### Sessão persistente

O login é renovado a cada acesso e, por padrão, dura 3.650 dias. Na prática ele permanece ativo até o usuário sair, limpar os dados do navegador, a conta ser removida ou o `SECRET_KEY` mudar. Mantenha `SECRET_KEY` estável. Em produção HTTPS use `SESSION_COOKIE_SECURE=true`; para desenvolvimento HTTP local, altere temporariamente para `false`.

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
2.  Arraste **TODOS** os arquivos HTML relevantes da exportação atual de uma vez para a caixa. Cada conjunto importado é tratado como o retrato atual daquele tipo. O sistema suporta:
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


