"""Resolve identity before generation; never treat a missing context row as a new person."""
from copy import deepcopy
from dataclasses import asdict
from app.domain.errors import ValidationError
from app.domain.changes import _rewrite


def names(entity):
    return {entity.name, *[a for a in entity.attributes.get('aliases', []) if isinstance(a,str)]}


def relevant_ids(snapshot, text, targets=(), history=()):
    resolved = []
    for target in targets:
        if target in snapshot.entities:
            resolved.append(target)
            continue
        matches = [e.id for e in snapshot.entities.values() if target in names(e)]
        if len(matches)>1:
            raise ValidationError('有多个同名对象，请说明具体指哪一位：'+target, path='target_ids')
        resolved.extend(matches)
    for e in snapshot.entities.values():
        if any(n and n in text for n in names(e)):
            matches = [other.id for other in snapshot.entities.values() if other.kind==e.kind and names(other)&names(e)]
            if len(matches)>1 and not any(x in resolved for x in matches):
                raise ValidationError('同名对象有歧义，请说明具体对象：'+e.name, path='target_ids')
            if len(matches)==1: resolved.append(e.id)
    # Only fall back to the most recent unambiguous NPC mention for pronouns.
    if not any(snapshot.entities[x].kind.value=='npc' for x in resolved) and any(x in text for x in ('他','她','它','对方','约定','分成')):
        for m in reversed(history):
            mentions = [e.id for e in snapshot.entities.values() if e.kind.value=='npc' and any(n and n in m['text'] for n in names(e))]
            if mentions:
                if len(mentions)==1: resolved.extend(mentions)
                break
    return list(dict.fromkeys(resolved))


def context_ids(snapshot, text, targets=(), history=()):
    ids = relevant_ids(snapshot,text,targets,history)
    player = snapshot.entities[snapshot.world.player_id]
    location = player.attributes.get('location_id')
    if location in snapshot.entities: ids.append(location)
    ids.append(player.id)
    for e in snapshot.entities.values():
        if location and e.attributes.get('location_id')==location: ids.append(e.id)
    ids.extend(snapshot.entities)
    return list(dict.fromkeys(ids))


def duplicates(registered, snapshot):
    return [candidate for candidate in (registered or {}).get('temp_entities', [])
            if any(e.kind.value==candidate.get('kind') and candidate.get('name') in names(e)
                   for e in snapshot.entities.values())]


def reconcile(batch, snapshot, relevant):
    data = deepcopy(batch if isinstance(batch,dict) else asdict(batch))
    mapping, kept = {}, []
    for candidate in data.get('temp_entities', []):
        matches = [e for e in snapshot.entities.values()
                   if e.kind.value==candidate.get('kind') and candidate.get('name') in names(e)]
        if matches:
            if len(matches)!=1 or matches[0].id not in relevant:
                raise ValidationError('新实体与已有对象同名，需要确认身份：'+candidate['name'], path='generation.identity')
            mapping[candidate['id']] = matches[0].id
        else:
            # Also prevent two matching candidates within this batch.
            if any(e['kind']==candidate['kind'] and e['name']==candidate['name'] for e in kept):
                raise ValidationError('同一批次出现重复实体', path='generation.identity')
            kept.append(candidate)
    data['temp_entities'] = kept
    data = _rewrite(data,mapping)
    facts = []
    for fact in data.get('facts', []):
        existing = [f for f in snapshot.facts.values() if f.subject_id==fact.get('subject_id')
                    and f.predicate==fact.get('predicate') and f.status.value=='active']
        if any(f.value==fact.get('value') for f in existing): continue
        if existing and fact.get('subject_id') in mapping.values():
            raise ValidationError('重复生成的对象带有冲突事实，请沿用已有事实', path='generation.facts')
        facts.append(fact)
    data['facts'] = facts
    return data
