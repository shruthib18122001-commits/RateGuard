from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.admin import router as admin_router
from app.middleware import RateLimitMiddleware

app = FastAPI(title="RateGuard")

# Attach rate-limiting middleware
app.add_middleware(RateLimitMiddleware)

app.include_router(admin_router)

_ADMIN_PAGE = Path(__file__).parent / "static" / "admin.html"


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return _ADMIN_PAGE.read_text()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/data")
def data():
    return {"message": "This is rate-limited data"}


@app.get("/metrics")
def metrics():
    """Prometheus scrape endpoint (text exposition format)."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
