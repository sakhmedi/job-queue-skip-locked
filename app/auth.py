"""Авторизация воркеров по общему токену: `Authorization: Bearer <WORKER_TOKEN>`."""

import secrets
from typing import Annotated

from fastapi import Header, HTTPException, status

from app.config import get_settings


def require_worker_token(authorization: Annotated[str | None, Header()] = None) -> None:
    """FastAPI-зависимость для эндпоинтов воркера. Неверный или отсутствующий токен → 401."""
    expected = get_settings().worker_token
    scheme, _, token = (authorization or "").partition(" ")
    # compare_digest сравнивает за время, не зависящее от того, в каком символе
    # первое расхождение. Обычное `==` выходит на первом несовпадении, и по времени
    # ответа токен можно подбирать посимвольно (timing attack).
    if (
        scheme.lower() != "bearer"
        or not token
        or not secrets.compare_digest(token.encode(), expected.encode())
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing worker token",
            headers={"WWW-Authenticate": "Bearer"},
        )
