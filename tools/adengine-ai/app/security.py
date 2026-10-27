import hmac

from fastapi import Header, HTTPException, status

from app.config import get_settings


def require_api_key(x_api_key: str = Header(default="")) -> None:
    """Constant-time comparison of the internal API key. Every protected route depends on this."""
    if not hmac.compare_digest(x_api_key.encode(), get_settings().api_key.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")
