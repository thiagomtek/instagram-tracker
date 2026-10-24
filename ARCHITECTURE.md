# Arquitetura do Instagram Tracker

Este documento descreve a arquitetura lógica, as convenções do código e os requisitos de operação. Endereços, credenciais, caminhos locais, nomes de máquinas e detalhes de acesso pertencem à documentação privada do ambiente e não devem ser versionados.

## Componentes

- **Web:** aplicação Flask executada pelo Gunicorn (2 processos, 4 threads).
- **Worker:** processo Python (`worker.py`) que, em loop de 5 s, (a) verifica o Google Drive a cada 1 hora e quando o usuário pede "Verificar agora", e (b) processa **uma** validação de perfil por vez, com intervalos aleatórios. Cada etapa é isolada: uma falha é registrada, a transação sofre rollback e o loop continua. O worker atualiza `/tmp/worker-heartbeat` a cada ciclo, usado pelo `healthcheck`.
- **Banco:** SQLite em WAL (`busy_timeout=30s`, chaves estrangeiras ligadas), compartilhado por web e worker através de um volume Docker.
- **PWA:** service worker (rede primeiro para `/static/`, página offline), manifest com ícones PNG e Web Push.
- **Proxy reverso HTTPS:** externo ao projeto.

A imagem Docker é única para web e worker (o Chromium do Playwright só é usado pelo worker).

## Persistência e migrações

O banco `data.db` fica em `DATA_DIR`, normalmente num volume persistente. Arquivos recebidos do Instagram são processados em diretórios temporários e descartados. Nunca execute `docker compose down -v` em produção.

`db.create_all()` cria tabelas novas; `services/schema.py` acrescenta colunas e índices ausentes de forma idempotente. Alterações de **dados** só rodam uma vez, registradas em `schema_migration`. Nunca coloque no `schema.py` um `UPDATE` incondicional: ele reescreveria dados novos a cada reinício (um bug assim já existiu com o status `lost`). Há também a remoção de duplicatas de `follower` antes de criar o índice único `(user_id, kind, username)`.

## Modelo de dados

| Tabela | Papel |
|---|---|
| `follower` | Uma relação (`followers`/`following`) com um perfil: `is_present`, `state`, `missing_count`, `missing_since`. |
| `history` | Mudanças observadas: `gained`, `missing`, `lost` (remoção confirmada), `reappeared`, `followed_again`. |
| `analysis_run` | Cada importação (manual ou Drive), com contadores e perfis alterados. A coluna `lost_usernames` guarda, por nome histórico, os perfis **ausentes** (`missing`). |
| `relationship_validation` | Fila de consultas a perfis ausentes, com tentativas e agenda. |
| `import_observation` | Impressão digital da última lista importada por usuário e tipo (detecta reenvio). |
| `drive_connection` / `drive_import` | Vínculo com o Drive e registro das pastas já processadas (idempotência). |
| `login_throttle` | Falhas de login por (IP, e-mail), persistidas para valerem entre processos. |
| `schema_migration` | Migrações de dados já aplicadas. |

## Regras de importação

`services/importer.py::process_import_paths` executa, nesta ordem:

1. Extrai somente `.html`/`.json` dos ZIPs (limites de arquivos e de bytes **realmente gravados**; caminhos validados com `is_relative_to`).
2. Agrupa os arquivos por tipo e remove duplicatas por username.
3. **Verificações somente de leitura** (nada é gravado se alguma falhar): reenvio idêntico dentro de `IMPORT_DUPLICATE_WINDOW_HOURS` é ignorado; lista vazia ou queda acima do limite levanta `ImportRejected(needs_confirmation=True)`.
4. Grava cada lista (`storage.save_followers`), registra o histórico e a observação.
5. Resolve os perfis ausentes: se a **lista oposta deste mesmo upload** (ou, na falta dela, a do banco) prova que a conta existe, aplica `resolve_existing_profile` sem consulta web; caso contrário, enfileira uma validação (se `PROFILE_VALIDATION_ENABLED`).
6. Um único `commit`. Qualquer exceção desfaz tudo.

`resolve_existing_profile` é a única implementação da regra de confirmação (`REMOVAL_CONFIRMATION_EXPORTS = 2`): existe **e** `missing_count >= 2` ⇒ `removed` + entrada `lost` (uma por ausência); caso contrário, `pending`.

### Validação de perfis (worker)

`process_next_validation` pega a próxima validação vencida, marca `running`, consulta o perfil e aplica o resultado: `exists` → mesma regra acima; `unavailable` → um reteste e depois `untestable`; inconclusivo → novas tentativas e um reteste. Validações presas em `running` por mais de `PROFILE_VALIDATION_STALE_SECONDS` (worker que caiu) voltam à fila em `recover_stale_validations`.

## Datas e fuso horário

Dois relógios, ambos independentes do fuso do contêiner (`services/clock.py`):

- `local_now()` — datetime ingênuo em `APP_TIMEZONE`. Usado por `follower` e `history` (datas do retrato, comparadas com os filtros do painel).
- `utc_now()` — datetime ingênuo em UTC. Usado por todo o resto (`analysis_run`, Drive, validações, push, usuários).

Para exibir um valor UTC use `utc_to_local` (filtro `local_datetime`). Valores de `follower`/`history` já são locais e usam o filtro `format_date`. Nunca compare um valor de um relógio com o do outro.

## Segurança

- Sessão por cookie assinado (`HttpOnly`, `SameSite=Lax`, `Secure` em produção), CSRF em todo `POST` (formulário ou `X-CSRF-Token`).
- `User.session_epoch` é incrementado ao trocar a senha e invalida os logins antigos.
- Login com bloqueio por tentativas e comparação de hash mesmo quando o e-mail não existe.
- Cadastro fechado por padrão; só a primeira conta nasce livre.
- Cabeçalhos: `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Permissions-Policy`, e `Cache-Control: no-store` nas páginas autenticadas. Bootstrap é carregado com SRI; o Plotly é servido localmente em `/vendor/plotly.min.js`.
- Erros internos mostram mensagem genérica ao usuário; o detalhe vai para o log.
- Refresh tokens do Google ficam criptografados (Fernet). Use `GOOGLE_TOKEN_ENCRYPTION_KEY` própria.

## Configuração

Use `.env.example` como referência de nomes, mas mantenha o `.env` real exclusivamente no servidor. Nunca versione:

- segredos da aplicação;
- senhas administrativas;
- IDs e secrets OAuth;
- chaves de criptografia ou VAPID;
- endereços internos, nomes de máquinas ou caminhos do servidor.

O callback OAuth deve seguir o formato `https://tracker.example.com/integrations/google/callback`, cadastrado exatamente igual no Google Cloud e em `GOOGLE_REDIRECT_URI`.

## OAuth do Google Drive

A integração solicita somente `https://www.googleapis.com/auth/drive`, pois o worker precisa baixar e remover as exportações depois do processamento confirmado. Por padrão a pasta vai para a **lixeira** (`GOOGLE_DRIVE_DELETE_MODE=trash`).

### PKCE obrigatório

Ao iniciar o vínculo guarde `state` e `code_verifier` na sessão; no callback, recupere o mesmo verificador antes de trocar o código por tokens. Sem essa persistência a troca falha com `Missing code verifier`. O `state` é comparado em tempo constante.

### Escopo isolado

Não use `include_granted_scopes=true`: a autorização incremental pode incorporar permissões já concedidas ao mesmo projeto e provocar `Scope has changed`. Mantenha `access_type="offline"` e `prompt="consent"` para obter o refresh token do worker.

## Testes

`python -m pytest tests`. `tests/conftest.py` aponta `DATA_DIR` para um diretório temporário antes de qualquer import de `app`, de modo que os testes nunca criam `data.db` no projeto. As consultas ao Instagram e ao Google são sempre simuladas.

## Publicação

O procedimento e os caminhos exatos de produção devem permanecer fora do Git. Em termos gerais:

1. execute os testes no checkout local;
2. sincronize somente arquivos versionados, preservando o `.env` de produção;
3. reconstrua os serviços com `docker compose up -d --build`;
4. confirme a saúde da aplicação e do worker (`docker compose ps` mostra os dois `healthy`);
5. inspecione os logs sem copiar tokens, códigos OAuth ou outros segredos para chamados e commits.

Ao atualizar de uma versão anterior, as migrações rodam sozinhas na inicialização. Faça backup do volume antes.

## Backup

Faça backup do volume persistente antes de qualquer operação que possa afetar dados. Os comandos, destinos e credenciais de backup são específicos do ambiente e devem permanecer na documentação privada.
