"""Owned item stacks and conversion of legacy single-currency balances."""
import re
import uuid

from app.domain.errors import ValidationError
from app.domain.types import Entity, EntityKind

UNSPECIFIED_COIN = '钱币'


def denominations(book):
    for entry in (book or {}).get('sections', {}).get('currency', []):
        name = entry.get('name')
        name = name.strip() if isinstance(name, str) else ''
        if entry.get('visibility') == 'public' and entry.get('status') != 'open' and name:
            return list(dict.fromkeys(part.strip() for part in re.split(r'[·、，,/／]', name) if part.strip()))
    return []


def currency_names(world):
    return (denominations(world.ruleset_config.get('world_book'))
            or world.ruleset_config.get('currency_denominations') or [UNSPECIFIED_COIN])


def item_stack(world, name, quantity, *, currency=False):
    eid = (str(uuid.uuid5(uuid.NAMESPACE_URL, f'{world.id}:{world.player_id}:currency:{name}'))
           if currency else str(uuid.uuid4()))
    attributes = {'owner': world.player_id, 'quantity': quantity}
    if currency:
        attributes['currency'] = True
    return Entity(id=eid, world_id=world.id, kind=EntityKind.ITEM, name=name, attributes=attributes, revision=1)


def currency_stack(world, entities, name):
    return next((entity for entity in entities.values() if entity.kind == EntityKind.ITEM
                 and entity.attributes.get('owner') == world.player_id
                 and entity.attributes.get('currency') is True and entity.name == name), None)


def unpack_legacy_coins(world, entities):
    player = entities.get(world.player_id)
    if not player:
        return
    resources = player.attributes.get('resources', {})
    if type(resources.get('coins')) is not int:
        return
    amount = resources.pop('coins')
    if amount > 0:
        name = currency_names(world)[-1]
        stack = currency_stack(world, entities, name)
        if stack:
            stack.attributes['quantity'] += amount
        else:
            stack = item_stack(world, name, amount, currency=True)
            entities[stack.id] = stack


def owned_items(snapshot):
    items = []
    for item in snapshot.entities.values():
        if item.kind != EntityKind.ITEM or item.attributes.get('owner') != snapshot.world.player_id:
            continue
        quantity = item.attributes.get('quantity', 1)
        if type(quantity) is not int:
            quantity = 1
        if quantity > 0:
            items.append({'id': item.id, 'name': item.name, 'quantity': quantity,
                          'currency': item.attributes.get('currency') is True})
    return items


def validate_item(entry):
    entry = {'name': entry, 'quantity': 1} if isinstance(entry, str) else entry
    if not isinstance(entry, dict) or set(entry) != {'name', 'quantity'}:
        raise ValidationError('物品需要名称和数量')
    name, quantity = entry['name'], entry['quantity']
    if not isinstance(name, str) or not name.strip() or len(name) > 200 or type(quantity) is not int or quantity < 1:
        raise ValidationError('物品需要名称和正整数数量')
    return name.strip(), quantity
