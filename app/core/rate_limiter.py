"""IP-based rate limiter middleware for Constellation parse endpoints.

Uses an in-memory token bucket algorithm with automatic expiry.
Zero external dependencies — relies only on Python stdlib + FastAPI.
"""

import time
import asyncio
import logging
from typing import Dict, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config.settings import settings

logger = logging.getLogger(__name__)


class _TokenBucket:
    """Per-IP token bucket with automatic refill."""

    __slots__ = ("_buckets", "_lock")

    def __init__(self) -> None:
        # {ip: (remaining_tokens, last_refill_timestamp)}
        self._buckets: Dict[str, Tuple[int, float]] = {}
        self._lock = asyncio.Lock()

    async def is_allowed(self, ip: str) -> Tuple[bool, int, int]:
        """Check whether *ip* may proceed.

        Returns:
            (allowed, remaining_tokens, retry_after_seconds)
        """
        now = time.time()
        max_req = settings.rate_limit_max_requests
        window = settings.rate_limit_window_seconds

        async with self._lock:
            if ip in self._buckets:
                tokens, last_refill = self._buckets[ip]
                elapsed = now - last_refill

                if elapsed >= window:
                    # Window expired — full refill
                    tokens = max_req
                    last_refill = now
            else:
                tokens = max_req
                last_refill = now

            if tokens > 0:
                tokens -= 1
                self._buckets[ip] = (tokens, last_refill)
                return True, tokens, 0
            else:
                retry_after = int(window - (now - last_refill)) + 1
                self._buckets[ip] = (tokens, last_refill)
                return False, 0, retry_after

    async def cleanup_expired(self) -> None:
        """Remove entries older than 2x the window to prevent memory growth."""
        now = time.time()
        cutoff = settings.rate_limit_window_seconds * 2
        async with self._lock:
            expired = [
                ip for ip, (_, ts) in self._buckets.items()
                if now - ts > cutoff
            ]
            for ip in expired:
                del self._buckets[ip]


# Module-level singleton
_bucket = _TokenBucket()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Middleware that enforces per-IP rate limits on parse endpoints."""

    async def dispatch(self, request: Request, call_next):
        """Only rate-limit paths that start with /api/v1/parse."""
        path = request.url.path
        if not path.startswith("/api/v1/parse"):
            return await call_next(request)

        # Extract client IP (respect X-Forwarded-For behind reverse proxy)
        forwarded = request.headers.get("x-forwarded-for")
        client_ip = forwarded.split(",")[0].strip() if forwarded else (
            request.client.host if request.client else "unknown"
        )

        allowed, remaining, retry_after = await _bucket.is_allowed(client_ip)

        if not allowed:
            logger.warning(
                "[RateLimit] IP %s exceeded limit, retry after %ds",
                client_ip,
                retry_after,
            )
            return JSONResponse(
                status_code=429,
                content={
                    "success": False,
                    "detail": f"请求频率超限，请 {retry_after} 秒后重试",
                    "retry_after": retry_after,
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Limit"] = str(settings.rate_limit_max_requests)
        return response
