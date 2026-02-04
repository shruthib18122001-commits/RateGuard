# RateGuard – Distributed Rate-Limited API Platform

RateGuard is a backend infrastructure service that enforces per-client
API rate limits using the **token bucket algorithm**. It is designed
as middleware to provide consistent, low-latency throttling across
all endpoints.

## Features
- Token bucket rate limiting with burst support
- Middleware-based enforcement in FastAPI
- HTTP 429 responses for quota exhaustion
- Rate limit metadata via response headers
- Environment-agnostic, production-style design

## Architecture
Client requests are intercepted by a FastAPI middleware layer, which
evaluates rate limits before forwarding requests to application handlers.

## Design Decisions
- **Token bucket** chosen over fixed window to allow controlled bursts
- **Middleware enforcement** avoids duplicated logic across endpoints
- **In-memory store** for low-latency local enforcement
- Designed to support **Redis-backed atomic updates** for horizontally
  scaled deployments