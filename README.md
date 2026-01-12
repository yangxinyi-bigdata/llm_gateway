# LLM Gateway (FastAPI + LiteLLM SDK)

A minimal OpenAI-compatible gateway that validates CloudBase access tokens, forwards
requests via LiteLLM SDK, and reports usage for custom billing.

## Endpoints
- `GET /health`
- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/completions`
- `POST /admin/reload`

## Environment
- `CLOUDBASE_ENV_ID`: CloudBase env id (required unless `DISABLE_AUTH=true`)
- `CLOUDBASE_REGION`: default `ap-shanghai`
- `DISABLE_AUTH`: set `true` to skip token checks (dev only)
- `GATEWAY_ADMIN_KEY`: admin key for `X-Gateway-Key`
- `CORS_ALLOW_ALL`: set `true` to allow all CORS origins
- `MODEL_LIST`: JSON array or comma list for `/v1/models`
- `MODEL_LIST_FILE`: JSON file path containing model list (array)
- `MODEL_PROVIDER_MAP`: JSON map to force provider/base/key per model
- `MODEL_PROVIDER_MAP_FILE`: JSON file path containing provider map
- `MYSQL_DSN`: MySQL DSN, e.g. `mysql://user:pass@host:3306/db`
- `MYSQL_HOST`: MySQL host (if not using DSN)
- `MYSQL_PORT`: MySQL port (default `3306`)
- `MYSQL_USER`: MySQL user
- `MYSQL_PASSWORD`: MySQL password
- `MYSQL_DB`: MySQL database (schema)
- `MYSQL_POOL_MIN`: pool min size (default `1`)
- `MYSQL_POOL_MAX`: pool max size (default `5`)
- `REMOTE_CONFIG_TABLE`: MySQL table name for remote config
- `POINTS_PER_1K_TOKENS`: billing conversion ratio (default `1.0`)
- `BILLING_WEBHOOK_URL`: optional webhook to record usage
- `BILLING_WEBHOOK_TOKEN`: optional bearer token for the webhook

## Usage
Authorization uses CloudBase access tokens:

```
POST /v1/chat/completions
Authorization: Bearer <cloudbase_access_token>
Content-Type: application/json

{
  "model": "glm-4.5",
  "messages": [{"role": "user", "content": "你好"}],
  "stream": true
}
```

If `BILLING_WEBHOOK_URL` is set, the gateway posts:

```
{
  "request_id": "...",
  "user_id": "...",
  "model": "...",
  "usage": {"prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46},
  "points": 0,
  "metadata": {"stream": true},
  "ts": 1710000000
}
```

Example `MODEL_PROVIDER_MAP`:

```
{
  "glm-4.5": {
    "provider": "openai",
    "api_base": "https://open.bigmodel.cn/api/paas/v4",
    "api_key_env": "GLM_API_KEY"
  },
  "deepseek-chat": {
    "provider": "openai",
    "api_base": "https://api.deepseek.com",
    "api_key_env": "DEEPSEEK_API_KEY"
  }
}
```

Notes:
- Client-supplied `api_key`/`api_base` are ignored. Use `MODEL_PROVIDER_MAP` + env keys.
- For local testing, set `DISABLE_AUTH=true`.
- If `MODEL_LIST`/`MODEL_LIST_FILE` is empty, the allowlist falls back to provider map keys.

## File-based config
The repository includes:
- `model_map.json`: provider map template
- `model_list.json`: model allowlist template

To use them, set:
```
MODEL_PROVIDER_MAP_FILE=/app/model_map.json
MODEL_LIST_FILE=/app/model_list.json
```

## Remote config (CloudBase MySQL)
Set these env vars to enable remote config:
```
MYSQL_DSN=mysql://user:pass@host:3306/db
REMOTE_CONFIG_TABLE=llm_gateway_model_map
```

Then call:
```
POST /admin/reload
X-Gateway-Key: <GATEWAY_ADMIN_KEY>
```

The table should include:
- `model_name` (unique)
- `provider`
- `api_base`
- `api_key_env`
- `api_key` (optional)
- `enabled` (0/1)
- `_openid`

Suggested schema:
```
CREATE TABLE llm_gateway_model_map (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  model_name VARCHAR(128) NOT NULL,
  provider VARCHAR(64) NOT NULL,
  api_base VARCHAR(255) NOT NULL,
  api_key_env VARCHAR(64) NOT NULL,
  api_key VARCHAR(255) DEFAULT NULL,
  enabled TINYINT(1) NOT NULL DEFAULT 1,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  _openid VARCHAR(64) DEFAULT '' NOT NULL,
  UNIQUE KEY uniq_model_name (model_name)
);
```
