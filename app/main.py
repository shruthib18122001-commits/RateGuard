from fastapi import FastAPI
from app.middleware import RateLimitMiddleware

app = FastAPI(title="RateGuard")

# Attach rate-limiting middleware
app.add_middleware(RateLimitMiddleware)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/data")
def data():
    return {"message": "This is rate-limited data"}
