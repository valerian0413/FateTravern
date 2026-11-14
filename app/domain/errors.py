"""Domain and application errors. Never expose credentials."""
from __future__ import annotations


class AppError(Exception):
    code = 'app_error'
    http_status = 500
    retryable = False

    def __init__(self, message: str, *, path: str | None = None, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.path = path
        self.details = details or {}

    def to_dict(self, request_id: str | None = None) -> dict:
        payload = {
            'code': self.code,
            'message': self.message,
            'retryable': self.retryable,
        }
        if self.path:
            payload['path'] = self.path
        if request_id:
            payload['request_id'] = request_id
        return payload


class ValidationError(AppError):
    code = 'validation_error'
    http_status = 400


class Conflict(AppError):
    code = 'conflict'
    http_status = 409


class NotFound(AppError):
    code = 'not_found'
    http_status = 404


class ModelUnavailable(AppError):
    code = 'model_unavailable'
    http_status = 502
    retryable = True


class StaleProposal(Conflict):
    code = 'stale_proposal'


class StaleTurn(Conflict):
    code = 'stale_turn'


class ModelRequestError(ModelUnavailable):
    """Provider failure with a stable category and explicit retry policy."""

    def __init__(self, message: str, *, code: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
