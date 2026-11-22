"""Ошибки фасада в формате OpenAI: {"error": {"message", "type", "code", "param"}}."""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse


class ApiError(Exception):
    """Ошибка, которую фасад возвращает клиенту как есть (сообщение безопасно показывать)."""

    def __init__(
        self,
        status: int,
        message: str,
        *,
        type_: str = "invalid_request_error",
        code: str | None = None,
        param: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.type = type_
        self.code = code
        self.param = param
        self.headers = headers or {}

    def body(self) -> dict[str, Any]:
        return error_body(self.message, self.type, self.code, self.param)

    def response(self) -> JSONResponse:
        return JSONResponse(self.body(), status_code=self.status, headers=self.headers)


def error_body(
    message: str, type_: str, code: str | None = None, param: str | None = None
) -> dict[str, Any]:
    return {"error": {"message": message, "type": type_, "code": code, "param": param}}


def unauthorized(message: str = "требуется действительный токен доступа") -> ApiError:
    return ApiError(
        401,
        message,
        type_="authentication_error",
        code="invalid_token",
        headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
    )


def forbidden(message: str, code: str = "forbidden") -> ApiError:
    return ApiError(403, message, type_="permission_error", code=code)
