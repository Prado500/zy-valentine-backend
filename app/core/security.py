import hashlib
import secrets
import time
from collections import OrderedDict

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pwdlib import PasswordHash

from app.core.errors import ApiError

password_hasher = PasswordHash.recommended()
DUMMY_HASH = password_hasher.hash("dummy-password-that-never-authenticates")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def issue_csrf(secret: str) -> str:
    return URLSafeTimedSerializer(secret, salt="zy-csrf-v1").dumps(secrets.token_urlsafe(32))


def verify_csrf(secret: str, token: str, max_age: int) -> bool:
    try:
        URLSafeTimedSerializer(secret, salt="zy-csrf-v1").loads(token, max_age=max_age)
        return True
    except (BadSignature, SignatureExpired):
        return False


class RateLimiter:
    """Per-process IP limit; bounded memory. Not a distributed ingress control."""

    def __init__(self, limit: int, window: int):
        self.limit, self.window = limit, window
        self.buckets: OrderedDict[str, tuple[float, int]] = OrderedDict()

    def check(self, key: str):
        now = time.monotonic()
        while self.buckets and next(iter(self.buckets.values()))[0] <= now - self.window:
            self.buckets.popitem(last=False)
        started, count = self.buckets.get(key, (now, 0))
        if count >= self.limit:
            raise ApiError(429, "RATE_LIMITED", "Demasiados intentos; espera antes de reintentar.")
        if key not in self.buckets and len(self.buckets) >= 10000:
            raise ApiError(429, "RATE_LIMITED", "Intenta más tarde.")
        self.buckets[key] = (started, count + 1)
