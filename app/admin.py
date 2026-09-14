import json
import os

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.middleware import limiter

router = APIRouter()


class LimitOverride(BaseModel):
    # 0 is valid for either field (e.g. rate=0 freezes refills for a client
    # until their existing tokens run out); only negative values make no sense.
    rate: float = Field(ge=0, description="Tokens refilled per second")
    capacity: float = Field(ge=0, description="Bucket capacity (max burst)")


def require_admin(x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    # Read the env var per-request (rather than once at import time) so it
    # can be configured/rotated without caring about module import order.
    admin_key = os.environ.get("ADMIN_API_KEY")
    if not admin_key or x_admin_key != admin_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-Admin-Key",
        )


@router.get("/admin/limits", dependencies=[Depends(require_admin)])
async def list_limits():
    raw = await limiter.redis.hgetall(limiter.overrides_key)
    return {client_key: json.loads(value) for client_key, value in raw.items()}


@router.get("/admin/limits/{client_key}", dependencies=[Depends(require_admin)])
async def get_limit(client_key: str):
    raw = await limiter.redis.hget(limiter.overrides_key, client_key)
    if raw is None:
        raise HTTPException(status_code=404, detail="No override for this client key")
    return json.loads(raw)


@router.post("/admin/limits/{client_key}", dependencies=[Depends(require_admin)])
async def set_limit(client_key: str, override: LimitOverride):
    await limiter.redis.hset(
        limiter.overrides_key,
        client_key,
        json.dumps({"rate": override.rate, "capacity": override.capacity}),
    )
    return {"client_key": client_key, "rate": override.rate, "capacity": override.capacity}


@router.delete(
    "/admin/limits/{client_key}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
)
async def delete_limit(client_key: str):
    deleted = await limiter.redis.hdel(limiter.overrides_key, client_key)
    if not deleted:
        raise HTTPException(status_code=404, detail="No override for this client key")
