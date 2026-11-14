"""Request/response schema helpers. Reject unknown fields."""
from __future__ import annotations

from app.domain.errors import ValidationError


def _check(body: dict, allowed: set[str], required: set[str]) -> None:
    if not isinstance(body, dict):
        raise ValidationError('请求必须是对象')
    unknown = set(body) - allowed
    if unknown:
        raise ValidationError(f'未知字段: {sorted(unknown)}', path=next(iter(unknown)))
    missing = required - set(body)
    if missing:
        raise ValidationError(f'缺少字段: {sorted(missing)}', path=next(iter(missing)))


def decode_request(route: str, body: dict) -> dict:
    if route == 'create_world':
        _check(body, {'preset_id', 'overrides', 'request_id'}, {'preset_id'})
        if not isinstance(body.get('preset_id'), str):
            raise ValidationError('preset_id 必须是字符串', path='preset_id')
        return {
            'preset_id': body['preset_id'],
            'overrides': body.get('overrides') or {},
            'request_id': body.get('request_id'),
        }
    if route == 'start_world':
        _check(body, {'request_id'}, {'request_id'})
        return {'request_id': body['request_id']}
    if route == 'send_message':
        _check(body, {'request_id', 'text', 'mode_hint', 'reply_to', 'combat_description'}, {'request_id', 'text'})
        return {
            'request_id': body['request_id'],
            'text': body['text'],
            'mode_hint': body.get('mode_hint'),
            'reply_to': body.get('reply_to'),
            'combat_description': body.get('combat_description'),
        }
    if route == 'confirm_proposal':
        _check(body, {'request_id', 'revision', 'base_version'}, {'request_id', 'revision', 'base_version'})
        return {
            'request_id': body['request_id'],
            'revision': body['revision'],
            'base_version': body['base_version'],
        }
    if route == 'reject_proposal':
        _check(body, {'request_id', 'revision'}, {'request_id', 'revision'})
        return {'request_id': body['request_id'], 'revision': body['revision']}
    return body


def encode_response(value) -> dict:
    if hasattr(value, 'to_dict'):
        return value.to_dict()
    if hasattr(value, '__dataclass_fields__'):
        from app.domain.types import to_dict
        return to_dict(value)
    return value
