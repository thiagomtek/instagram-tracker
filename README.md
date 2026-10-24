# Instagram Tracker

Painel privado, hospedado por você, para analisar a **exportação oficial de dados do Instagram**: veja quem não te segue de volta, quem entrou, quem saiu e o histórico dessas mudanças entre uma importação e outra.

![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![Flask](https://img.shields.io/badge/Flask-3-green)
![License](https://img.shields.io/badge/License-GPLv3-yellow)

O Tracker **nunca pede a senha do Instagram**. Ele trabalha com os arquivos que o próprio Instagram entrega (HTML ou JSON) e guarda no seu servidor somente o resultado da análise.

---

## Funcionalidades

- **Retrato atual de cada lista.** Cada exportação de seguidores/seguindo substitui a lista anterior daquele tipo. As datas históricas dentro do arquivo não são confundidas com a situação de hoje.
- **Comparação entre as listas:** quem você segue e não te segue de volta, quem te segue e você não segue, e os mútuos.
- **Mudanças entre importações:** a primeira importação cria a base; as seguintes registram quem entrou, ficou ausente, teve a saída confirmada, reapareceu ou voltou a seguir. Gráfico e lista com filtros por tipo, período, perfil e tipo de mudança.
- **Confirmação de remoções** (veja [Como as remoções são confirmadas](#como-as-remoções-são-confirmadas)).
- **Importação manual** de ZIP, HTML ou JSON, com proteções contra exportações incompletas e reenvios.
- **Google Drive (opcional):** o worker observa as pastas `meta-*` que o Instagram entrega no Meu Drive, analisa e envia a pasta processada para a lixeira.
- **PWA e Web Push:** instalável no celular, com aviso quando uma análise ou uma validação termina.
- **Várias contas, dados isolados:** cada usuário vê, altera e apaga apenas os próprios dados. Há troca de senha e exclusão de conta.
- **Sem arquivos residuais:** uploads, ZIPs e pastas baixadas são processados em diretórios temporários e removidos ao final.

## O que sai do seu servidor

Seja transparente com você mesmo sobre estas duas integrações:

| Integração | Quando | O que acontece |
|---|---|---|
| **Google Drive** | Somente se você vincular uma conta | O worker lê o Meu Drive e move para a lixeira as pastas `meta-*` processadas. O escopo OAuth é `drive` (acesso total), pois o Google não oferece um escopo menor que baixe e remova arquivos criados por outro app. O refresh token fica criptografado no SQLite. |
| **Consulta a perfis no Instagram** | Quando um perfil some da lista e a outra lista não prova que ele existe | O worker abre a página pública do perfil em um Chromium sem login, **uma consulta por vez, com intervalos de 10 a 30 minutos**. Isso não é uma API oficial e pode contrariar os termos do Instagram. Desative com `PROFILE_VALIDATION_ENABLED=false`. |

Fora isso, o app carrega apenas Bootstrap (CDN, com verificação SRI) e a fonte Inter (Google Fonts). O Plotly é servido pelo próprio app.

---

## Instalação com Docker (recomendado)

Requer Docker Engine com Docker Compose v2.

1. Crie o arquivo de ambiente e preencha os segredos:

   ```bash
   cp .env.example .env
   openssl rand -hex 32
   ```

   Use o resultado do segundo comando em `SECRET_KEY` e **mantenha-o estável** (trocá-lo desconecta todos). Defina `INITIAL_ADMIN_EMAIL` e `INITIAL_ADMIN_PASSWORD`: na primeira inicialização a senha vira hash no SQLite e nunca é versionada. Depois de confirmar o acesso, remova `INITIAL_ADMIN_PASSWORD` do `.env`.

2. Suba os serviços (aplicação web e worker):

   ```bash
   docker compose up -d --build
   ```

3. Acesse `http://IP_DO_SERVIDOR:8080` e entre com as credenciais configuradas.

**Dados.** O volume nomeado `tracker-data` guarda somente o SQLite. Para backup, pare o serviço e copie o volume. Nunca use `docker compose down -v`: ele apaga o volume e os dados.

**Cadastro.** Fechado por padrão (`ALLOW_REGISTRATION=false`); só a primeira conta pode ser criada livremente. Ative temporariamente se precisar criar outras contas.

**Internet.** Para expor publicamente, mantenha o app atrás de um proxy reverso com HTTPS (Nginx Proxy Manager, Caddy ou Traefik) e não publique a porta `8080` diretamente. Com HTTPS, mantenha `SESSION_COOKIE_SECURE=true`.

**Saúde.** O serviço web expõe `/healthz`. O worker atualiza um arquivo de batimento a cada ciclo e o `healthcheck` do compose o marca como não saudável se ele travar.

### Variáveis de ambiente

Todas estão em [`.env.example`](.env.example) com comentários. As principais:

| Variável | Padrão | Função |
|---|---|---|
| `SECRET_KEY` | obrigatória | Assina a sessão. Mantenha estável. |
| `ALLOW_REGISTRATION` | `false` | Permite criar contas pela tela de login. |
| `LOGIN_MAX_FAILURES` / `LOGIN_LOCK_SECONDS` | `5` / `900` | Bloqueio por tentativas de login (por IP e e-mail). |
| `SESSION_DAYS` | `3650` | Duração do login. Trocar a senha desconecta os outros dispositivos. |
| `GOOGLE_DRIVE_DELETE_MODE` | `trash` | `trash` envia a pasta processada para a lixeira (recuperável); `delete` exclui em definitivo. |
| `IMPORT_MASS_DROP_RATIO` / `IMPORT_MASS_DROP_MIN` | `0.3` / `20` | Exportação que removeria essa fração (e ao menos esse número) de perfis é recusada até você confirmar. |
| `IMPORT_DUPLICATE_WINDOW_HOURS` | `12` | A mesma lista reenviada dentro dessa janela é ignorada. |
| `PROFILE_VALIDATION_ENABLED` | `true` | Liga/desliga a consulta a perfis no Instagram. |
| `APP_TIMEZONE` | `America/Sao_Paulo` | Fuso usado para exibir datas e para os filtros do painel. |

### Integração com o Google Drive

1. No [Google Cloud Console](https://console.cloud.google.com/), crie ou selecione um projeto e ative a **Google Drive API**.
2. Configure a tela de consentimento OAuth e crie um cliente OAuth 2.0 do tipo **Aplicativo da Web**.
3. Cadastre como URI de redirecionamento exatamente o valor de `GOOGLE_REDIRECT_URI`, por exemplo `https://tracker.example.com/integrations/google/callback`.
4. Preencha `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` e `GOOGLE_REDIRECT_URI`.
5. Gere a chave que criptografa o refresh token e guarde em `GOOGLE_TOKEN_ENCRYPTION_KEY` (não troque enquanto houver contas vinculadas):

   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

6. Reinicie com `docker compose up -d --build` e use **Vincular conta do Google** na tela inicial.

O worker verifica o Drive a cada 1 hora. O padrão observado nas transferências é `Meu Drive/meta-<data-hora>/instagram-<conta>-<data>/connections/followers_and_following/*.html`; por isso só pastas `meta-*` diretamente no Meu Drive são consideradas, e apenas `followers_and_following` é lida. Para não tocar numa transferência em andamento, o worker exige os arquivos de seguidores e seguindo e aguarda `GOOGLE_DRIVE_SETTLE_SECONDS` sem modificações.

O botão **Verificar agora** apenas pede ao worker uma verificação imediata (o resultado aparece em *Histórico*). Depois que a análise é confirmada no SQLite, a pasta processada vai para a lixeira. Se isso falhar, o resultado continua salvo, o erro aparece no histórico e o worker tenta de novo. Se uma exportação do Drive for recusada pelas proteções de importação, a verificação para com uma mensagem explicando como resolver (importar manualmente com confirmação e remover a pasta).

### PWA e notificações

O PWA requer HTTPS, exceto em `localhost`. Gere um par VAPID uma única vez e guarde no `.env`:

```bash
python scripts/generate_vapid_keys.py
```

Copie as duas linhas para `PUSH_VAPID_PUBLIC_KEY` e `PUSH_VAPID_PRIVATE_KEY` e defina `PUSH_VAPID_SUBJECT` com um `mailto:` real. Instale o app pelo menu do navegador e ative as notificações na tela *Importar*.

`PWA_ID` dá ao app uma identidade própria. Como a porta faz parte da origem web, PWAs em portas diferentes do mesmo host não compartilham manifest, service worker nem inscrições push.

---

## Desenvolvimento local

Requer Python 3.9 ou superior (o Docker usa 3.12).

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
SESSION_COOKIE_SECURE=false SECRET_KEY=dev \
INITIAL_ADMIN_EMAIL=voce@example.com INITIAL_ADMIN_PASSWORD=uma-senha-de-12+ \
FLASK_DEBUG=1 python app.py
```

Abra `http://127.0.0.1:5000`. Sem `DATA_DIR`, o banco `data.db` é criado na pasta do projeto (ignorado pelo Git). A consulta a perfis precisa do Chromium do Playwright (`python -m playwright install chromium`); sem ele, as consultas ficam "inconclusivas".

Testes (não acessam a internet nem criam arquivos no projeto):

```bash
python -m pytest tests
```

Para rodar o worker localmente: `python worker.py`.

---

## Como obter seus dados do Instagram

1. No app ou em `instagram.com`, vá em **Configurações** > **Central de Contas** > **Suas informações e permissões** > **Baixar suas informações** (o caminho varia conforme a versão).
2. Escolha **Baixar ou transferir informações** > **Algumas de suas informações** e marque **Seguidores e seguindo**.
3. Escolha **Baixar para o dispositivo**, intervalo **Desde o início**, formato **HTML** (JSON também é aceito).
4. Quando o Instagram avisar, baixe o ZIP. Você pode enviá-lo inteiro: o Tracker lê apenas os HTML/JSON e ignora o resto.

Seguidores grandes são divididos em `followers_1.html`, `followers_2.html`… **Envie todos os arquivos juntos.** Se faltar algum, o Tracker percebe a queda brusca e pede confirmação antes de aplicar.

## Como usar

1. Em **Importar**, envie o ZIP ou todos os arquivos da exportação atual e clique em **Analisar arquivos** (ou deixe o Google Drive fazer isso).
2. O **Painel** mostra a situação atual, as listas comparadas, o progresso das validações e a atividade observada.
3. **Histórico** lista cada processamento, manual ou automático, com horário, duração e perfis alterados.
4. **Conta** permite trocar a senha (desconecta os outros dispositivos) e excluir a conta com todos os dados.

---

## Como as remoções são confirmadas

Um perfil que some de uma exportação **não significa necessariamente** que deixou de seguir: a conta pode ter sido desativada, excluída, ou ter bloqueado você. Por isso cada relação segue um ciclo de estados:

```text
present ──(sumiu da exportação)──▶ pending ──┬─(outra lista prova que a conta existe)────────┐
   ▲                                          │                                               ▼
   │                                          └─(consulta ao Instagram)─▶ existe ───────▶ confirmação
   │                                                     │
   │                                                     └─▶ indisponível ─▶ absent ─(reteste)─▶ untestable
   └────────────(voltou a aparecer em qualquer exportação: "reapareceu")
```

| Estado | Significado | Onde aparece |
|---|---|---|
| `present` | Está na exportação atual. | Listas e KPIs. |
| `pending` | Sumiu de uma exportação; aguardando validação. | *Ausentes* (e mudança "ficou ausente"). |
| `absent` | O perfil estava indisponível numa consulta; um reteste está agendado. | *Ausentes*. |
| `removed` | Remoção **confirmada**. Se a pessoa voltar, é registrado "voltou a seguir". | Atividade ("confirmado"); sai das *Ausentes*. |
| `untestable` | Continuou indisponível após o reteste (conta excluída, desativada ou que bloqueou você). | Card **Perfis indisponíveis**. |

A **remoção é confirmada** quando duas condições se cumprem:

1. o perfil **existe** — provado pela presença dele na lista oposta (ex.: some de *seguidores* mas continua em *seguindo*) ou por uma consulta ao Instagram; e
2. ele continua fora da lista em **2 exportações distintas**. Enviar o mesmo arquivo duas vezes não conta como duas exportações.

Daí os números do painel: "novos", "aguardando validação", "remoções confirmadas", "reapareceram" e "voltaram a seguir". Uma consulta inconclusiva é repetida até `PROFILE_VALIDATION_MAX_ATTEMPTS` vezes e depois ganha um único reteste.

### Proteções de importação

- **Lista vazia ou queda brusca** (por padrão, ao menos 20 perfis e 30% da lista) é recusada com a mensagem "Nada foi alterado" até você marcar a confirmação. Isso protege contra um `followers_2.html` esquecido ou contra mudança no layout do arquivo do Instagram.
- **Mesmo arquivo reenviado** dentro de 12 horas é ignorado, sem alterar contadores.
- Uma importação recusada ou com erro **não altera nenhum dado**.

## Datas e fuso horário

Datas do retrato (listas e atividade) ficam na hora local de `APP_TIMEZONE`, igual ao que o Instagram mostra e aos filtros do painel. Horários operacionais (processamentos, Drive, agendamento das consultas) ficam em UTC e são exibidos no fuso configurado. O fuso do contêiner não interfere. Veja [ARCHITECTURE.md](ARCHITECTURE.md).

## Estrutura do projeto

```text
app.py                  Rotas, autenticação, painel e cabeçalhos de segurança
worker.py               Loop do Google Drive e das validações de perfil
config.py               Configuração por variáveis de ambiente
database.py             Instância do SQLAlchemy (SQLite em WAL)
models/                 follower, history, user, integration (execuções, Drive, push,
                        validações), system (migrações, observações de importação, login)
services/
  importer.py           ZIP/HTML/JSON → listas; proteções e regras de confirmação
  parser.py             Leitura dos arquivos do Instagram (pt-BR e inglês)
  storage.py            Substitui cada lista pelo retrato atual
  profile_validation.py Fila, regras e consulta de perfis (Playwright)
  presentation.py       Dados do painel, progresso das validações e gráfico
  google_drive.py       Varredura, download e limpeza das pastas meta-*
  schema.py             Migrações SQLite idempotentes
  security.py           Bloqueio de tentativas de login
  clock.py              Convenções de data (UTC operacional, hora local do retrato)
static/ templates/      Interface, PWA e service worker
tests/                  Testes de regras, importação, Drive e telas
```

## Licença

GPLv3. Veja [LICENSE](LICENSE).
