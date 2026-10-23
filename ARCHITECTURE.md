# Arquitetura do Instagram Tracker

Este documento descreve somente a arquitetura lógica e os requisitos de operação. Endereços, credenciais, caminhos locais, nomes de máquinas e detalhes de acesso pertencem à documentação privada do ambiente e não devem ser versionados.

## Componentes

- Aplicação Flask executada pelo Gunicorn.
- Worker Python para consultar o Google Drive e processar novas exportações.
- SQLite persistido em volume Docker.
- PWA com service worker, cache próprio e notificações Web Push.
- Proxy reverso HTTPS externo ao projeto.

## Persistência

O banco `data.db` fica no diretório configurado por `DATA_DIR`, normalmente montado em um volume persistente. Arquivos recebidos do Instagram são processados em diretórios temporários e descartados após a importação.

Nunca execute `docker compose down -v` em produção: essa opção remove o volume persistente e pode apagar o banco.

## Configuração

Use `.env.example` como referência de nomes, mas mantenha o `.env` real exclusivamente no servidor. Nunca versione:

- segredos da aplicação;
- senhas administrativas;
- IDs e secrets OAuth;
- chaves de criptografia ou VAPID;
- endereços internos, nomes de máquinas ou caminhos do servidor.

O callback OAuth deve seguir o formato:

```text
https://tracker.example.com/integrations/google/callback
```

Cadastre exatamente a mesma URL no cliente OAuth do Google Cloud e em `GOOGLE_REDIRECT_URI`.

## OAuth do Google Drive

A integração solicita somente `https://www.googleapis.com/auth/drive`, pois o worker precisa baixar e excluir as exportações depois do processamento confirmado.

### PKCE obrigatório

Ao iniciar o vínculo:

```python
authorization_url, state = flow.authorization_url(...)
session["google_oauth_state"] = state
if getattr(flow, "code_verifier", None):
    session["google_oauth_code_verifier"] = flow.code_verifier
```

No callback, recupere o mesmo verificador antes de trocar o código por tokens:

```python
code_verifier = session.pop("google_oauth_code_verifier", None)
flow = Flow.from_client_config(..., state=expected_state)
flow.redirect_uri = _google_redirect_uri()
if code_verifier:
    flow.code_verifier = code_verifier
    flow.fetch_token(authorization_response=request.url, code_verifier=code_verifier)
```

Sem essa persistência, a troca falha com `Missing code verifier`.

### Escopo isolado

Não use `include_granted_scopes=true`. A autorização incremental pode incorporar permissões concedidas anteriormente ao mesmo projeto Google e provocar `Scope has changed` na validação estrita do cliente OAuth. Mantenha `access_type="offline"` e `prompt="consent"` para obter o refresh token usado pelo worker.

## Publicação

O procedimento e os caminhos exatos de produção devem permanecer fora do Git. Em termos gerais:

1. execute os testes no checkout local;
2. sincronize somente arquivos versionados, preservando o `.env` de produção;
3. reconstrua os serviços com `docker compose up -d --build`;
4. confirme a saúde da aplicação e do worker;
5. inspecione os logs sem copiar tokens, códigos OAuth ou outros segredos para chamados e commits.

## Backup

Faça backup do volume persistente antes de qualquer operação que possa afetar dados. Os comandos, destinos e credenciais de backup são específicos do ambiente e devem permanecer na documentação privada.
