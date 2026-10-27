"""Encryption at rest for client tokens.

Every token column uses EncryptedText, which encrypts on write and decrypts on read with a Fernet key from
ENCRYPTION_KEY. Plaintext tokens never reach the database or the logs. If the key is missing or wrong, reads fail
loudly instead of returning garbage.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.types import Text, TypeDecorator

from app.config import get_settings


class CredentialCorruptedError(Exception):
    """A stored token cannot be decrypted. The key changed, or the row was altered."""


@lru_cache
def _fernet() -> Fernet:
    key = get_settings().encryption_key
    if not key:
        raise RuntimeError("ENCRYPTION_KEY is not set. Organic publishing cannot store or read tokens.")
    return Fernet(key.encode())


class EncryptedText(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        return _fernet().encrypt(str(value).encode()).decode()

    def process_result_value(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        try:
            return _fernet().decrypt(value.encode()).decode()
        except InvalidToken as exc:
            raise CredentialCorruptedError("Stored token could not be decrypted with the current ENCRYPTION_KEY") from exc
