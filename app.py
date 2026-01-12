from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
import uuid
from typing import Any, AsyncIterator, Dict, Optional, Tuple
from urllib.parse import urlparse

import asyncmy
import httpx
import litellm
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse


app = FastAPI(title="LLM Gateway", version="0.1.0")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()
CLOUDBASE_ENV_ID = os.getenv("CLOUDBASE_ENV_ID", "").strip()
CLOUDBASE_REGION = os.getenv("CLOUDBASE_REGION", "ap-shanghai").strip()
DISABLE_AUTH = _env_bool("DISABLE_AUTH", False)
GATEWAY_ADMIN_KEY = os.getenv("GATEWAY_ADMIN_KEY", "").strip()
BILLING_WEBHOOK_URL = os.getenv("BILLING_WEBHOOK_URL", "").strip()
BILLING_WEBHOOK_TOKEN = os.getenv("BILLING_WEBHOOK_TOKEN", "").strip()
POINTS_PER_1K_TOKENS = _env_float("POINTS_PER_1K_TOKENS", 1.0)
POINTS_PER_YUAN = _env_int("POINTS_PER_YUAN", 500)
TOKEN_CACHE_TTL = _env_int("TOKEN_CACHE_TTL", 300)
MODEL_LIST = os.getenv("MODEL_LIST", "").strip()
MODEL_LIST_FILE = os.getenv("MODEL_LIST_FILE", "").strip()
MODEL_PROVIDER_MAP_RAW = os.getenv("MODEL_PROVIDER_MAP", "").strip()
MODEL_PROVIDER_MAP_FILE = os.getenv("MODEL_PROVIDER_MAP_FILE", "").strip()
MYSQL_DSN = os.getenv("MYSQL_DSN", "").strip()
MYSQL_HOST = os.getenv("MYSQL_HOST", "").strip()
MYSQL_PORT = _env_int("MYSQL_PORT", 3306)
MYSQL_USER = os.getenv("MYSQL_USER", "").strip()
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "").strip()
MYSQL_DB = os.getenv("MYSQL_DB", "").strip()
MYSQL_POOL_MIN = _env_int("MYSQL_POOL_MIN", 1)
MYSQL_POOL_MAX = _env_int("MYSQL_POOL_MAX", 5)
REMOTE_CONFIG_TABLE = os.getenv("REMOTE_CONFIG_TABLE", "").strip()
REMOTE_CONFIG_ACCESS_TOKEN = os.getenv("REMOTE_CONFIG_ACCESS_TOKEN", "").strip()
REMOTE_CONFIG_SCHEMA = os.getenv("REMOTE_CONFIG_SCHEMA", "").strip()
REMOTE_CONFIG_INSTANCE = os.getenv("REMOTE_CONFIG_INSTANCE", "").strip()
REMOTE_CONFIG_BASE_URL = os.getenv("REMOTE_CONFIG_BASE_URL", "").strip()
REMOTE_CONFIG_LIMIT = _env_int("REMOTE_CONFIG_LIMIT", 200)
USER_PROFILE_TABLE = os.getenv("USER_PROFILE_TABLE", "user_profile").strip() or "user_profile"
USER_POINTS_LEDGER_TABLE = os.getenv("USER_POINTS_LEDGER_TABLE", "user_points_ledger").strip() or "user_points_ledger"

LOG_LEVEL_VALUE = getattr(logging, LOG_LEVEL, logging.INFO)
if not logging.getLogger().handlers:
    logging.basicConfig(level=LOG_LEVEL_VALUE, format="%(levelname)s: %(message)s")
logger = logging.getLogger("llm_gateway")
logger.setLevel(LOG_LEVEL_VALUE)


def _strip_wrapping_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _read_json_file(path: str) -> Optional[Any]:
    if not path:
        return None
    path = _strip_wrapping_quotes(path).strip()
    if not path:
        return None
    if not os.path.isabs(path):
        path = os.path.join(os.getcwd(), path)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def _parse_provider_map(raw: str) -> Dict[str, Dict[str, Any]]:
    if not raw:
        return {}
    raw = _strip_wrapping_quotes(raw)
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for key, value in parsed.items():
        if not isinstance(value, dict):
            continue
        out[str(key)] = value
    return out


def _load_provider_map() -> Dict[str, Dict[str, Any]]:
    from_file = _read_json_file(MODEL_PROVIDER_MAP_FILE) if MODEL_PROVIDER_MAP_FILE else None
    if isinstance(from_file, dict):
        out: Dict[str, Dict[str, Any]] = {}
        for key, value in from_file.items():
            if isinstance(value, dict):
                out[str(key)] = value
        if out:
            return out
    return _parse_provider_map(MODEL_PROVIDER_MAP_RAW)


def _load_model_list(provider_map: Dict[str, Dict[str, Any]]) -> list[str]:
    if MODEL_LIST_FILE:
        parsed = _read_json_file(MODEL_LIST_FILE)
        if isinstance(parsed, list):
            return [str(m) for m in parsed]
    if MODEL_LIST:
        raw = _strip_wrapping_quotes(MODEL_LIST)
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(m) for m in parsed]
        except Exception:
            return [m.strip() for m in raw.split(",") if m.strip()]
    if provider_map:
        return list(provider_map.keys())
    return []


MODEL_PROVIDER_MAP = _load_provider_map()
MODEL_LIST_DATA = _load_model_list(MODEL_PROVIDER_MAP)
MODEL_PRICING_MAP: Dict[str, Dict[str, float]] = {}
MYSQL_POOL: Optional[asyncmy.Pool] = None
TOKEN_CACHE: Dict[str, Tuple[Dict[str, Any], float]] = {}


def _remote_config_enabled() -> bool:
    return bool(REMOTE_CONFIG_ACCESS_TOKEN and REMOTE_CONFIG_TABLE)


def _mysql_configured() -> bool:
    if MYSQL_DSN:
        return True
    return bool(MYSQL_HOST and MYSQL_USER and MYSQL_PASSWORD and MYSQL_DB)


def _parse_mysql_dsn(dsn: str) -> Dict[str, Any]:
    parsed = urlparse(dsn)
    if not parsed.scheme.startswith("mysql"):
        raise ValueError("MYSQL_DSN must start with mysql://")
    db_name = parsed.path.lstrip("/") if parsed.path else ""
    if not parsed.hostname or not parsed.username or not db_name:
        raise ValueError("MYSQL_DSN missing host/user/db")
    return {
        "host": parsed.hostname,
        "port": parsed.port or 3306,
        "user": parsed.username,
        "password": parsed.password or "",
        "db": db_name,
    }


def _get_mysql_config() -> Optional[Dict[str, Any]]:
    if MYSQL_DSN:
        return _parse_mysql_dsn(MYSQL_DSN)
    if MYSQL_HOST and MYSQL_USER and MYSQL_PASSWORD and MYSQL_DB:
        return {
            "host": MYSQL_HOST,
            "port": MYSQL_PORT or 3306,
            "user": MYSQL_USER,
            "password": MYSQL_PASSWORD,
            "db": MYSQL_DB,
        }
    return None


async def _init_mysql_pool() -> None:
    global MYSQL_POOL
    if MYSQL_POOL is not None:
        return
    cfg = _get_mysql_config()
    if not cfg:
        return
    MYSQL_POOL = await asyncmy.create_pool(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        db=cfg["db"],
        autocommit=True,
        minsize=MYSQL_POOL_MIN,
        maxsize=MYSQL_POOL_MAX,
    )


def _build_remote_table_url(table_name: str) -> str:
    if REMOTE_CONFIG_BASE_URL:
        base = REMOTE_CONFIG_BASE_URL.rstrip("/")
    else:
        base = f"https://{CLOUDBASE_ENV_ID}.api.tcloudbasegateway.com/v1/rdb/rest"
    if REMOTE_CONFIG_INSTANCE and REMOTE_CONFIG_SCHEMA:
        return f"{base}/{REMOTE_CONFIG_INSTANCE}/{REMOTE_CONFIG_SCHEMA}/{table_name}"
    if REMOTE_CONFIG_SCHEMA:
        return f"{base}/{REMOTE_CONFIG_SCHEMA}/{table_name}"
    return f"{base}/{table_name}"


def _normalize_price(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _extract_pricing(provider_map: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    pricing: Dict[str, Dict[str, float]] = {}
    for model_name, cfg in provider_map.items():
        if not isinstance(cfg, dict):
            continue
        pricing[model_name] = {
            "input_price_per_1m": _normalize_price(cfg.get("input_price_per_1m")),
            "output_price_per_1m": _normalize_price(cfg.get("output_price_per_1m")),
            "cache_price_per_1m": _normalize_price(cfg.get("cache_price_per_1m")),
        }
    return pricing


async def _fetch_remote_provider_map() -> Dict[str, Dict[str, Any]]:
    if not _remote_config_enabled():
        return {}
    url = _build_remote_table_url(REMOTE_CONFIG_TABLE)
    headers = {
        "Authorization": f"Bearer {REMOTE_CONFIG_ACCESS_TOKEN}",
        "Accept": "application/json",
    }
    params = {"order": "model_name.asc", "limit": str(REMOTE_CONFIG_LIMIT)}
    async with httpx.AsyncClient(timeout=12) as client:
        resp = await client.get(url, headers=headers, params=params)
    if resp.status_code < 200 or resp.status_code >= 300:
        raise HTTPException(status_code=502, detail=f"Remote config fetch failed: {resp.status_code}")
    try:
        data = resp.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Remote config parse failed: {exc}")
    if not isinstance(data, list):
        return {}
    provider_map: Dict[str, Dict[str, Any]] = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        enabled = row.get("enabled", 1)
        if enabled in (0, False, "0", "false", "False"):
            continue
        model_name = str(row.get("model_name") or row.get("model") or "").strip()
        if not model_name:
            continue
        provider = row.get("provider") or row.get("custom_llm_provider") or ""
        api_base = row.get("api_base") or row.get("base_url") or ""
        api_key_env = row.get("api_key_env") or ""
        api_key = row.get("api_key") or ""
        input_price = row.get("input_price_per_1m")
        output_price = row.get("output_price_per_1m")
        cache_price = row.get("cache_price_per_1m")
        payload: Dict[str, Any] = {
            "provider": str(provider) if provider else "",
            "api_base": str(api_base) if api_base else "",
            "api_key_env": str(api_key_env) if api_key_env else "",
            "input_price_per_1m": _normalize_price(input_price),
            "output_price_per_1m": _normalize_price(output_price),
            "cache_price_per_1m": _normalize_price(cache_price),
        }
        if api_key:
            payload["api_key"] = str(api_key)
        provider_map[model_name] = payload
    return provider_map


CONFIG_STATE: Dict[str, Any] = {
    "source": "local",
    "last_loaded_at": None,
    "last_error": None,
    "models": len(MODEL_LIST_DATA),
}
CONFIG_LOCK = asyncio.Lock()


async def _fetch_mysql_provider_map() -> Dict[str, Dict[str, Any]]:
    if not _mysql_configured():
        return {}
    await _init_mysql_pool()
    if MYSQL_POOL is None:
        return {}
    table = REMOTE_CONFIG_TABLE or "llm_gateway_model_map"
    query = (
        "SELECT model_name, provider, api_base, api_key_env, api_key, "
        "input_price_per_1m, output_price_per_1m, cache_price_per_1m, enabled "
        f"FROM {table} WHERE enabled = 1 ORDER BY model_name"
    )
    async with MYSQL_POOL.acquire() as conn:
        async with conn.cursor(asyncmy.cursors.DictCursor) as cur:
            await cur.execute(query)
            rows = await cur.fetchall()
    provider_map: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        model_name = str(row.get("model_name") or "").strip()
        if not model_name:
            continue
        provider = row.get("provider") or row.get("custom_llm_provider") or ""
        api_base = row.get("api_base") or row.get("base_url") or ""
        api_key_env = row.get("api_key_env") or ""
        api_key = row.get("api_key") or ""
        input_price = row.get("input_price_per_1m")
        output_price = row.get("output_price_per_1m")
        cache_price = row.get("cache_price_per_1m")
        payload: Dict[str, Any] = {
            "provider": str(provider) if provider else "",
            "api_base": str(api_base) if api_base else "",
            "api_key_env": str(api_key_env) if api_key_env else "",
            "input_price_per_1m": _normalize_price(input_price),
            "output_price_per_1m": _normalize_price(output_price),
            "cache_price_per_1m": _normalize_price(cache_price),
        }
        if api_key:
            payload["api_key"] = str(api_key)
        provider_map[model_name] = payload
    return provider_map


async def _reload_config() -> Dict[str, Any]:
    global MODEL_PROVIDER_MAP, MODEL_LIST_DATA, MODEL_PRICING_MAP, CONFIG_STATE
    async with CONFIG_LOCK:
        provider_map: Dict[str, Dict[str, Any]] = {}
        source = "local"
        error: Optional[str] = None
        if _mysql_configured():
            try:
                provider_map = await _fetch_mysql_provider_map()
                if provider_map:
                    source = "mysql"
            except Exception as exc:
                error = str(exc)
        if not provider_map and _remote_config_enabled():
            try:
                provider_map = await _fetch_remote_provider_map()
                if provider_map:
                    source = "mysql"
            except Exception as exc:
                error = str(exc)
        if not provider_map:
            provider_map = _load_provider_map()
            source = "local"
        MODEL_PROVIDER_MAP = provider_map
        MODEL_LIST_DATA = _load_model_list(provider_map)
        MODEL_PRICING_MAP = _extract_pricing(provider_map)
        CONFIG_STATE = {
            "source": source,
            "last_loaded_at": int(time.time()),
            "last_error": error,
            "models": len(MODEL_LIST_DATA),
        }
        return CONFIG_STATE


def _require_admin(gateway_key: Optional[str]) -> None:
    if not GATEWAY_ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Admin key not configured")
    if not gateway_key or gateway_key != GATEWAY_ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid admin key")

if _env_bool("CORS_ALLOW_ALL", False):
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.on_event("startup")
async def _startup_reload() -> None:
    if _mysql_configured():
        await _init_mysql_pool()
    await _reload_config()


@app.on_event("shutdown")
async def _shutdown_pool() -> None:
    if MYSQL_POOL is not None:
        MYSQL_POOL.close()
        await MYSQL_POOL.wait_closed()


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "ts": int(time.time())}


@app.post("/admin/reload", response_model=None)
async def admin_reload(x_gateway_key: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    _require_admin(x_gateway_key)
    data = await _reload_config()
    return {"ok": True, "data": data}


def _parse_bearer(auth_header: Optional[str]) -> Optional[str]:
    if not auth_header:
        return None
    parts = auth_header.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()

def _token_cache_get(token: str) -> Optional[Dict[str, Any]]:
    if not token or TOKEN_CACHE_TTL <= 0:
        return None
    cached = TOKEN_CACHE.get(token)
    if not cached:
        return None
    payload, expires_at = cached
    if expires_at <= time.time():
        TOKEN_CACHE.pop(token, None)
        return None
    return payload


def _token_cache_set(token: str, payload: Dict[str, Any]) -> None:
    if not token or TOKEN_CACHE_TTL <= 0:
        return
    TOKEN_CACHE[token] = (payload, time.time() + TOKEN_CACHE_TTL)


async def _verify_cloudbase_token(access_token: str) -> Dict[str, Any]:
    cached = _token_cache_get(access_token)
    if cached is not None:
        return cached
    if not CLOUDBASE_ENV_ID:
        raise HTTPException(status_code=500, detail="CLOUDBASE_ENV_ID not set")
    url = f"https://{CLOUDBASE_ENV_ID}.api.tcloudbasegateway.com/auth/v1/user/me"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=12) as client:
        resp = await client.get(url, headers=headers)
    if resp.status_code < 200 or resp.status_code >= 300:
        raise HTTPException(status_code=401, detail="Invalid access token")
    try:
        payload = resp.json()
        if isinstance(payload, dict):
            _token_cache_set(access_token, payload)
        return payload
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid access token")


def _user_id_from_payload(payload: Dict[str, Any]) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("user_id", "sub"):
        value = payload.get(key)
        if value:
            return str(value)
    return None


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    if hasattr(obj, "json"):
        try:
            return json.loads(obj.json())
        except Exception:
            pass
    return json.loads(json.dumps(obj, default=str))


def _get_models() -> list[Dict[str, Any]]:
    return [{"id": m, "object": "model"} for m in MODEL_LIST_DATA]


def _model_allow_list() -> list[str]:
    return [item["id"] for item in _get_models() if item.get("id")]


def _sanitize_payload(payload: Dict[str, Any]) -> None:
    for key in ("api_key", "api_base", "base_url", "custom_llm_provider"):
        payload.pop(key, None)


def _apply_provider_overrides(model: str, payload: Dict[str, Any]) -> None:
    cfg = MODEL_PROVIDER_MAP.get(model)
    if not cfg:
        return
    provider = cfg.get("provider") or cfg.get("custom_llm_provider")
    api_base = cfg.get("api_base") or cfg.get("base_url")
    api_key_env = cfg.get("api_key_env")
    api_key = cfg.get("api_key")
    if api_key_env:
        api_key = os.getenv(str(api_key_env), "")
    if provider:
        payload["custom_llm_provider"] = str(provider)
    if api_base:
        payload["api_base"] = str(api_base)
    if api_key:
        payload["api_key"] = str(api_key)


def _ensure_include_usage(payload: Dict[str, Any]) -> None:
    stream_opts = payload.get("stream_options")
    if stream_opts is None:
        payload["stream_options"] = {"include_usage": True}
        return
    if isinstance(stream_opts, dict):
        stream_opts.setdefault("include_usage", True)


def _coerce_int(value: Any) -> int:
    try:
        return max(int(float(value)), 0)
    except (TypeError, ValueError):
        return 0


def _usage_tokens(usage: Dict[str, Any]) -> tuple[int, int]:
    prompt = _coerce_int(usage.get("prompt_tokens"))
    completion = _coerce_int(usage.get("completion_tokens"))
    return prompt, completion


def _calc_cost(model: str, usage: Dict[str, Any]) -> Optional[float]:
    pricing = MODEL_PRICING_MAP.get(model) or {}
    input_price = _normalize_price(pricing.get("input_price_per_1m"))
    output_price = _normalize_price(pricing.get("output_price_per_1m"))
    if not (input_price or output_price):
        return None
    prompt, completion = _usage_tokens(usage)
    cost = (prompt / 1_000_000.0) * input_price + (completion / 1_000_000.0) * output_price
    return max(cost, 0.0)


def _calc_points_from_cost(cost: Optional[float]) -> Optional[int]:
    if cost is None:
        return None
    points = int(math.ceil(cost * POINTS_PER_YUAN))
    return max(points, 0)


def _calc_points(usage: Dict[str, Any]) -> Optional[int]:
    total = usage.get("total_tokens")
    if total is None:
        prompt = usage.get("prompt_tokens") or 0
        completion = usage.get("completion_tokens") or 0
        total = prompt + completion
    try:
        total_val = float(total)
    except Exception:
        return None
    points = int(round(total_val / 1000.0 * POINTS_PER_1K_TOKENS))
    return max(points, 0)


async def _deduct_points(
    *,
    user_id: Optional[str],
    model: str,
    usage: Dict[str, Any],
    points: Optional[int],
) -> bool:
    if not user_id or not points or points <= 0:
        return False
    if not _mysql_configured():
        logger.warning("MySQL not configured; skip points deduction")
        return False
    await _init_mysql_pool()
    if MYSQL_POOL is None:
        logger.warning("MySQL pool not available; skip points deduction")
        return False
    prompt_tokens, completion_tokens = _usage_tokens(usage)
    async with MYSQL_POOL.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor(asyncmy.cursors.DictCursor) as cur:
                await cur.execute(
                    f"SELECT points_balance FROM {USER_PROFILE_TABLE} WHERE uid = %s FOR UPDATE",
                    (user_id,),
                )
                row = await cur.fetchone()
                if not row:
                    await conn.rollback()
                    return False
                balance = _coerce_int(row.get("points_balance"))
                if balance < points:
                    await conn.rollback()
                    return False
                new_balance = balance - points
                await cur.execute(
                    f"UPDATE {USER_PROFILE_TABLE} "
                    "SET points_balance = %s, chat_count_total = chat_count_total + 1 "
                    "WHERE uid = %s",
                    (new_balance, user_id),
                )
                await cur.execute(
                    f"INSERT INTO {USER_POINTS_LEDGER_TABLE} "
                    "(uid, delta_points, balance_after, reason, model_name, tokens_in, tokens_out) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        user_id,
                        -points,
                        new_balance,
                        "usage",
                        model,
                        prompt_tokens,
                        completion_tokens,
                    ),
                )
            await conn.commit()
            return True
        except Exception:
            await conn.rollback()
            logger.exception("Failed to deduct points")
            return False


async def _record_usage(
    *,
    request_id: str,
    user_id: Optional[str],
    model: str,
    usage: Optional[Dict[str, Any]],
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    if not usage:
        logger.info(
            "billing_usage request_id=%s user_id=%s model=%s tokens_in=%s tokens_out=%s cost_cny=%s points=%s deducted=%s usage_missing=%s",
            request_id,
            user_id or "",
            model,
            "null",
            "null",
            "null",
            "null",
            False,
            True,
        )
        return
    cost_cny = _calc_cost(model, usage)
    points = _calc_points_from_cost(cost_cny)
    prompt_tokens, completion_tokens = _usage_tokens(usage)
    deducted = await _deduct_points(user_id=user_id, model=model, usage=usage, points=points)
    cost_value = f"{cost_cny:.6f}" if cost_cny is not None else "null"
    points_value = points if points is not None else "null"
    logger.info(
        "billing_usage request_id=%s user_id=%s model=%s tokens_in=%s tokens_out=%s cost_cny=%s points=%s deducted=%s",
        request_id,
        user_id or "",
        model,
        prompt_tokens,
        completion_tokens,
        cost_value,
        points_value,
        deducted,
    )
    payload = {
        "request_id": request_id,
        "user_id": user_id,
        "model": model,
        "usage": usage,
        "cost_cny": cost_cny,
        "points": points,
        "metadata": metadata or {},
        "ts": int(time.time()),
    }
    if BILLING_WEBHOOK_URL:
        headers = {"Content-Type": "application/json"}
        if BILLING_WEBHOOK_TOKEN:
            headers["Authorization"] = f"Bearer {BILLING_WEBHOOK_TOKEN}"
        async with httpx.AsyncClient(timeout=8) as client:
            try:
                await client.post(BILLING_WEBHOOK_URL, json=payload, headers=headers)
            except Exception:
                return


@app.get("/v1/models")
async def list_models() -> Dict[str, Any]:
    return {"object": "list", "data": _get_models()}


async def _resolve_auth(
    authorization: Optional[str],
    gateway_key: Optional[str],
) -> Tuple[Optional[str], Dict[str, Any]]:
    if DISABLE_AUTH:
        return None, {}
    if GATEWAY_ADMIN_KEY and gateway_key and gateway_key == GATEWAY_ADMIN_KEY:
        return None, {"role": "admin"}
    token = _parse_bearer(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Missing access token")
    payload = await _verify_cloudbase_token(token)
    return _user_id_from_payload(payload), payload


@app.post("/v1/chat/completions", response_model=None)
async def chat_completions(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_gateway_key: Optional[str] = Header(default=None),
):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    stream = bool(body.get("stream", False))
    model = str(body.get("model") or "").strip()
    messages = body.get("messages")
    if not model or not messages:
        raise HTTPException(status_code=400, detail="Missing model or messages")
    allow_list = _model_allow_list()
    if allow_list and model not in allow_list:
        raise HTTPException(status_code=400, detail="Model not allowed")

    request_id = str(uuid.uuid4())
    user_id, _user_payload = await _resolve_auth(authorization, x_gateway_key)

    # Pass-through params (OpenAI-compatible fields)
    payload = dict(body)
    payload.pop("stream", None)
    payload.pop("messages", None)
    payload.pop("model", None)
    _sanitize_payload(payload)
    _apply_provider_overrides(model, payload)
    _ensure_include_usage(payload)
    if user_id and "user" not in payload:
        payload["user"] = user_id

    if stream:
        try:
            response = await litellm.acompletion(
                model=model,
                messages=messages,
                stream=True,
                **payload,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

        async def _event_stream() -> AsyncIterator[str]:
            usage: Optional[Dict[str, Any]] = None
            try:
                async for chunk in response:
                    data = _jsonable(chunk)
                    if isinstance(data, dict) and data.get("usage"):
                        usage = data.get("usage")
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            except asyncio.CancelledError:
                return
            except Exception as exc:
                err = {"error": {"message": str(exc), "type": "gateway_error"}}
                yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
            finally:
                await _record_usage(
                    request_id=request_id,
                    user_id=user_id,
                    model=model,
                    usage=usage,
                    metadata={"stream": True},
                )
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            _event_stream(),
            media_type="text/event-stream",
            headers={"X-Request-Id": request_id},
        )

    try:
        response = await litellm.acompletion(
            model=model,
            messages=messages,
            stream=False,
            **payload,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    data = _jsonable(response)
    usage = data.get("usage") if isinstance(data, dict) else None
    await _record_usage(
        request_id=request_id,
        user_id=user_id,
        model=model,
        usage=usage,
        metadata={"stream": False},
    )
    return JSONResponse(content=data, headers={"X-Request-Id": request_id})


@app.post("/v1/completions", response_model=None)
async def legacy_completions(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_gateway_key: Optional[str] = Header(default=None),
):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    stream = bool(body.get("stream", False))
    model = str(body.get("model") or "").strip()
    prompt = body.get("prompt")
    if not model or prompt is None:
        raise HTTPException(status_code=400, detail="Missing model or prompt")
    allow_list = _model_allow_list()
    if allow_list and model not in allow_list:
        raise HTTPException(status_code=400, detail="Model not allowed")

    request_id = str(uuid.uuid4())
    user_id, _user_payload = await _resolve_auth(authorization, x_gateway_key)

    payload = dict(body)
    payload.pop("stream", None)
    payload.pop("prompt", None)
    payload.pop("model", None)
    _sanitize_payload(payload)
    _apply_provider_overrides(model, payload)
    _ensure_include_usage(payload)
    if user_id and "user" not in payload:
        payload["user"] = user_id

    if stream:
        try:
            response = await litellm.acompletion(
                model=model,
                prompt=prompt,
                stream=True,
                **payload,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

        async def _event_stream() -> AsyncIterator[str]:
            usage: Optional[Dict[str, Any]] = None
            try:
                async for chunk in response:
                    data = _jsonable(chunk)
                    if isinstance(data, dict) and data.get("usage"):
                        usage = data.get("usage")
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            except asyncio.CancelledError:
                return
            except Exception as exc:
                err = {"error": {"message": str(exc), "type": "gateway_error"}}
                yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
            finally:
                await _record_usage(
                    request_id=request_id,
                    user_id=user_id,
                    model=model,
                    usage=usage,
                    metadata={"stream": True, "legacy": True},
                )
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            _event_stream(),
            media_type="text/event-stream",
            headers={"X-Request-Id": request_id},
        )

    try:
        response = await litellm.acompletion(
            model=model,
            prompt=prompt,
            stream=False,
            **payload,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    data = _jsonable(response)
    usage = data.get("usage") if isinstance(data, dict) else None
    await _record_usage(
        request_id=request_id,
        user_id=user_id,
        model=model,
        usage=usage,
        metadata={"stream": False, "legacy": True},
    )
    return JSONResponse(content=data, headers={"X-Request-Id": request_id})
