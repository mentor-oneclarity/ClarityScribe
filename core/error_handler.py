import logging
from typing import Any, Optional

from fastapi import HTTPException, status
from pydantic import BaseModel

logger = logging.getLogger("error_handler")


class AppError(Exception):
    def __init__(
        self,
        message: str,
        status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail: Optional[str] = None,
    ):
        self.message = message
        self.status_code = status_code
        self.detail = detail or message
        super().__init__(self.message)


class ValidationError(AppError):
    def __init__(self, message: str, detail: Optional[str] = None):
        super().__init__(message, status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


class NotFoundError(AppError):
    def __init__(self, resource: str, resource_id: Any = None):
        message = f"{resource} not found"
        if resource_id:
            message = f"{resource} {resource_id} not found"
        super().__init__(message, status_code=status.HTTP_404_NOT_FOUND, detail=message)


class AuthenticationError(AppError):
    def __init__(self, message: str = "Unauthorized"):
        super().__init__(message, status_code=status.HTTP_401_UNAUTHORIZED, detail=message)


class PermissionError(AppError):
    def __init__(self, message: str = "Permission denied"):
        super().__init__(message, status_code=status.HTTP_403_FORBIDDEN, detail=message)


class ConflictError(AppError):
    def __init__(self, message: str, detail: Optional[str] = None):
        super().__init__(message, status_code=status.HTTP_409_CONFLICT, detail=detail)


class ExternalServiceError(AppError):
    def __init__(self, service: str, message: str = None):
        msg = f"{service} service error"
        if message:
            msg = f"{service}: {message}"
        super().__init__(msg, status_code=status.HTTP_502_BAD_GATEWAY, detail=msg)


class RateLimitError(AppError):
    def __init__(self, message: str = "Rate limit exceeded"):
        super().__init__(message, status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=message)


class ErrorResponse(BaseModel):
    error: str
    status_code: int
    detail: Optional[str] = None
    timestamp: Optional[str] = None


def handle_app_error(error: AppError) -> HTTPException:
    logger.warning(f"{error.__class__.__name__}: {error.message}")
    return HTTPException(status_code=error.status_code, detail=error.detail)


def handle_exception(exc: Exception) -> HTTPException:
    logger.error(f"Unhandled exception: {type(exc).__name__}: {exc}", exc_info=True)
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Internal server error. Please try again later.",
    )


def safe_execute(func, *args, default=None, log_error=True, **kwargs):
    try:
        return func(*args, **kwargs)
    except Exception as exc:
        if log_error:
            logger.error(f"Error in {func.__name__}: {exc}", exc_info=True)
        return default


async def safe_execute_async(coro, default=None, log_error=True):
    try:
        return await coro
    except Exception as exc:
        if log_error:
            logger.error(f"Error in async operation: {exc}", exc_info=True)
        return default
