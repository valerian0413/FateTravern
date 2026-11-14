"""Persistent world-book/character drafts; explicit confirmation before play."""
import json
import uuid
from copy import deepcopy

from app.domain.errors import Conflict, NotFound, ValidationError
from app.domain.types import Entity, EntityKind, Fact, FactStatus, Phase, World
from app.rules.character import CONFIG, RULESET, SKILLS, validate_character
from app.application.streaming import text_sink
from app.providers.base import ModelRequest, call_model
from app.providers.json_stream import TextFieldWatch
from app.prompts.loader import load
from app.storage.repositories import EntityRepo, FactRepo, WorldRepo

SECTIONS = ['background','tone','geography','supernatural','species','factions','currency','transport','law','open_questions']
CHARACTER_DRAFT_KEYS = {
    'name', 'background', 'motivation', 'skill_reason', 'aspects', 'skills',
    'stunts', 'equipment', 'money', 'ruleset_id', 'stress_tracks', 'consequences',
}


def validate_book(book, complete=False):
    if not isinstance(book, dict): raise ValidationError('世界书必须是对象')
    if set(book) - {'schema_version','name','sections','initial_scene','skills'}:
        raise ValidationError('世界书包含未知字段')
    if book.get('schema_version') != 1: raise ValidationError('世界书版本须为 1')
    if not isinstance(book.get('name'),str): raise ValidationError('世界名必须是文本')
    sections = book.get('sections')
    if not isinstance(sections,dict) or set(sections) != set(SECTIONS):
        raise ValidationError('世界书章节不完整')
    ids = set()
    for key, entries in sections.items():
        if not isinstance(entries,list): raise ValidationError('章节必须是条目数组')
        for e in entries:
            if not isinstance(e,dict) or not isinstance(e.get('id'),str) or not e['id'] or e['id'] in ids:
                raise ValidationError('设定条目需要唯一稳定 ID')
            ids.add(e['id'])
            if not isinstance(e.get('text'),str) or not e['text'].strip(): raise ValidationError('设定正文不能为空')
            if e.get('visibility') not in ('public','gm') or e.get('status') not in ('proposed','confirmed','open') or e.get('mutability') not in ('fixed','evolving','open'):
                raise ValidationError('设定可见范围/确认状态/变更规则无效')
    scene = book.get('initial_scene')
    if not isinstance(scene,dict) or set(scene) != {'name','description','opening'} or any(not isinstance(v,str) for v in scene.values()):
        raise ValidationError('初始场景需要 name/description/opening')
    if complete and (missing := missing_world_fields(book)):
        raise ValidationError('请先补充：' + '、'.join(missing))
    if book.get('skills') != SKILLS: raise ValidationError('首版使用固定技能目录')
    return book


def missing_world_fields(book):
    scene = book['initial_scene']
    fields = [('世界名', book['name']), ('开场地点', scene['name']),
              ('开场场景', scene['description']), ('开场白', scene['opening'])]
    missing = [label for label, value in fields if not value.strip()]
    if not any(e['status'] != 'open' for e in book['sections']['background']):
        missing.append('世界背景')
    return missing


def enter_character(data, revision):
    validate_book(data['book'], complete=True)
    for entries in data['book']['sections'].values():
        for entry in entries:
            if entry['status'] != 'open':
                entry['status'] = 'confirmed'
    data['world_revision'] = revision + 1
    data['stage'] = 'character'
    data['world_history'] = data['history']
    data['history'] = [{'role': 'gm', 'text': '世界设定已保存。你想扮演怎样的人？说说你的来历，或者为什么来到开场地点。'}]
    data['options'] = ['我是在这里长大的当地人，最近遇上了麻烦。',
                       '我刚抵达这里，正在找一位失踪的熟人。',
                       '我带着一份委托来到这里，想先查清发生了什么。']


async def get(runtime, draft_id):
    cursor = await runtime.db.conn.execute('SELECT payload FROM workshop WHERE id=?',(draft_id,))
    row = await cursor.fetchone()
    if not row: raise NotFound('创建草稿不存在')
    return json.loads(row[0])


async def save(runtime, data, expected):
    async with runtime.db.transaction():
        cursor = await runtime.db.conn.execute(
            'UPDATE workshop SET revision=?,stage=?,payload=? WHERE id=? AND revision=?',
            (data['revision'],data['stage'],json.dumps(data,ensure_ascii=False),data['id'],expected))
        if cursor.rowcount != 1: raise Conflict('草稿已更新，请刷新后重试')


def preset_book(preset):
    if preset.get('world_book'):
        return deepcopy(preset['world_book'])
    sections = {s: [] for s in SECTIONS}
    if preset.get('setting'):
        sections['background'] = [{'id': 'background', 'text': preset['setting'],
            'visibility': 'public', 'status': 'proposed', 'mutability': 'evolving'}]
    place = (preset.get('places') or [{}])[0]
    return {'schema_version': 1, 'name': preset['name'], 'sections': sections,
            'skills': SKILLS, 'initial_scene': {'name': place.get('name', ''),
            'description': place.get('attributes', {}).get('description', ''),
            'opening': preset.get('opening_intent', '')}}


async def catalog(runtime):
    presets = []
    for path in sorted(runtime.settings.presets_dir.glob('*.json')):
        preset = json.loads(path.read_text(encoding='utf-8'))
        presets.append({'id': preset['preset_id'], 'name': preset['name'],
                        'description': preset['setting'], 'kind': 'preset'})
    cursor = await runtime.db.conn.execute('SELECT payload FROM workshop ORDER BY rowid DESC')
    drafts = [json.loads(row[0]) for row in await cursor.fetchall()]
    saved = [{'id': d['id'], 'name': d['book']['name'], 'kind': 'saved',
              'description': ' · '.join(e['text'] for e in d['book']['sections']['background'])}
             for d in drafts if d['stage'] in ('character', 'ready', 'saved')]
    linked = {d.get('world_id') for d in drafts}
    hidden = {row[0] for row in await runtime.db.fetchall('SELECT world_id FROM hidden_world_settings')}
    for world in await WorldRepo(runtime.db.conn).list():
        if world['id'] in linked or world['id'] in hidden: continue
        snapshot = await WorldRepo(runtime.db.conn).get_snapshot(world['id'])
        saved.append({'id': world['id'], 'name': snapshot.world.name,
                      'kind': 'archive', 'description': snapshot.world.setting})
    return {'presets': presets, 'saved': saved}


async def delete_setting(runtime, kind, setting_id):
    if kind not in ('saved', 'archive'):
        raise ValidationError('只能删除玩家保存的世界设定')
    async with runtime.db.transaction():
        if kind == 'saved':
            draft = await get(runtime, setting_id)
            if draft['stage'] not in ('character', 'ready', 'saved'):
                raise Conflict('这个世界设定尚未保存')
            world_id = draft.get('world_id')
            result = await runtime.db.conn.execute(
                'DELETE FROM workshop WHERE id=? AND revision=?', (setting_id, draft['revision']))
            if result.rowcount != 1:
                raise Conflict('草稿已更新，请刷新后重试')
        else:
            await WorldRepo(runtime.db.conn).get_snapshot(setting_id)
            world_id = setting_id
        if world_id:
            await runtime.db.conn.execute(
                'INSERT OR IGNORE INTO hidden_world_settings (world_id) VALUES (?)', (world_id,))


async def create(runtime, source=None):
    data = {'id':str(uuid.uuid4()),'revision':0,'stage':'world','history':[], 'pending':None,
            'book':{'schema_version':1,'name':'','sections':{s:[] for s in SECTIONS},
                    'initial_scene':{'name':'','description':'','opening':''},'skills':SKILLS},
            'character':None,'world_id':None}
    if source:
        if not isinstance(source, dict) or source.get('kind') not in ('preset', 'saved', 'archive') or not isinstance(source.get('id'), str):
            raise ValidationError('请选择有效的世界设定')
        if source['kind'] == 'preset':
            from app.application.creation import _load_preset
            data['book'] = preset_book(_load_preset(runtime.settings.presets_dir, source['id']))
        elif source['kind'] == 'archive':
            if await runtime.db.fetchall('SELECT 1 FROM hidden_world_settings WHERE world_id=?', (source['id'],)):
                raise NotFound('世界设定已删除')
            snapshot = await WorldRepo(runtime.db.conn).get_snapshot(source['id'])
            existing = snapshot.world.ruleset_config.get('world_book')
            if existing:
                data['book'] = {k: deepcopy(v) for k, v in existing.items() if k != 'revision'}
            else:
                player = snapshot.entities[snapshot.world.player_id]
                place = snapshot.entities.get(player.attributes.get('location_id'))
                data['book'] = preset_book({'name': snapshot.world.name, 'setting': snapshot.world.setting,
                    'opening_intent': player.attributes.get('opening_intent', ''),
                    'places': [{'name': place.name, 'attributes': place.attributes}] if place else []})
        else:
            original = await get(runtime, source['id'])
            if original['stage'] not in ('character', 'ready', 'saved'):
                raise Conflict('这个世界设定尚未保存')
            data['book'] = deepcopy(original['book'])
        for entries in data['book']['sections'].values():
            for entry in entries:
                if entry['status'] == 'confirmed': entry['status'] = 'proposed'
        validate_book(data['book'])
        data['history'] = [{'role': 'gm', 'text': '已载入世界设定。你可以直接修改右侧条目，或告诉我想保留、改变和补充什么。'}]
    async with runtime.db.transaction():
        await runtime.db.conn.execute('INSERT INTO workshop VALUES (?,?,?,?)',
            (data['id'],0,'world',json.dumps(data,ensure_ascii=False)))
    return data


async def message(runtime, draft_id, body):
    if not isinstance(body,dict): raise ValidationError('请求必须是对象')
    data = await get(runtime,draft_id)
    revision = body.get('revision')
    if revision != data['revision']: raise Conflict('草稿版本过期')
    if data['stage'] in ('ready', 'saved'): raise Conflict('该草稿已创建存档')
    text = body.get('text')
    if not isinstance(text,str) or not 0 < len(text.strip()) <= 12000: raise ValidationError('请输入设定或角色想法')
    if data['pending']: raise Conflict('请先确认或放弃待确认的修改')
    spec = load('workshop',runtime.settings.prompt_version,runtime.settings.prompts_dir)
    if runtime.provider is None: raise ValidationError('创建向导需要真实模型服务')
    response = await call_model(runtime.provider, ModelRequest(
        module='workshop',prompt_version=runtime.settings.prompt_version,schema_version='1',
        world_id=None,turn_id=None,call_id=uuid.uuid4().hex,output_budget=6000,
        messages=[{'role':'system','content':spec.text},
                  {'role':'user','content':json.dumps({'stage':data['stage'],'book':data['book'],
                    'character':data['character'],'history':data['history'][-12:],'text':text,
                    'missing_world_fields':missing_world_fields(data['book']) if data['stage']=='world' else []},ensure_ascii=False)}]),
        on_delta=TextFieldWatch('reply', text_sink(runtime, f'draft:{draft_id}', 'workshop', None)))
    result = response.content
    if not isinstance(result,dict) or not isinstance(result.get('reply'),str) or not isinstance(result.get('conflicts'),list):
        raise ValidationError('创建向导返回格式无效')
    candidate = result.get('document')
    if data['stage'] == 'world': validate_book(candidate)
    else: candidate = validate_character(candidate)
    data['history'].append({'role': 'player', 'text': text})
    finish_world = data['stage'] == 'world' and result.get('intent') == 'finish_world'
    reply = result['reply']
    options = result.get('options',[])
    if not isinstance(options,list) or (options and not 2 <= len(options) <= 4) or any(not isinstance(x,str) for x in options):
        raise ValidationError('建议应为 2—4 条文本')
    # Once an individual entry is confirmed, changing/removing it requires explicit approval.
    protected_change = False
    if data['stage'] == 'world':
        proposed = {e['id']:e for entries in candidate['sections'].values() for e in entries}
        protected_change = any(e['status']=='confirmed' and proposed.get(e['id']) != e
                               for entries in data['book']['sections'].values() for e in entries)
    if result['conflicts'] or protected_change:
        data['pending'] = {'document': candidate, 'conflicts': result['conflicts'] or ['修改了已确认设定'],
                           'finish_world': finish_world}
    else:
        data['book' if data['stage']=='world' else 'character'] = candidate
    if finish_world and data['pending']:
        reply = '还有一处设定冲突需要你确认。处理后可以继续创建角色卡。'
        options = []
    elif finish_world and (missing := missing_world_fields(data['book'])):
        reply = '还需要补充：' + '、'.join(missing) + '。' + (reply if options else '你可以直接编辑条目，或用一句话补充。')
    elif finish_world:
        enter_character(data, revision)
    if data['stage'] != 'character' or not finish_world:
        data['history'].append({'role': 'gm', 'text': reply})
        data['options'] = options
    data['revision'] += 1
    await save(runtime,data,revision)
    return data


def _confirmed_entries(book):
    found = {}
    for entries in book['sections'].values():
        for entry in entries:
            if entry.get('status') == 'confirmed':
                found[entry['id']] = entry
    return found


async def update_document(runtime, draft_id, body):
    """Save a form edit without calling the model."""
    if not isinstance(body, dict):
        raise ValidationError('请求必须是对象')
    data = await get(runtime, draft_id)
    revision = body.get('revision')
    if revision != data['revision']:
        raise Conflict('草稿版本过期')
    if data['stage'] in ('ready', 'saved'):
        raise Conflict('该草稿已创建存档')
    if data['pending']:
        raise Conflict('请先确认或放弃待确认的修改')
    if data['stage'] == 'world':
        candidate = validate_book(body.get('book'))
        old = _confirmed_entries(data['book'])
        proposed = {e['id']: e for entries in candidate['sections'].values() for e in entries}
        if any(proposed.get(entry_id) != entry for entry_id, entry in old.items()):
            raise Conflict('已确认的设定不能在表格里直接改，请在对话中提出修改')
        data['book'] = candidate
    elif data['stage'] == 'character':
        card = body.get('character')
        if not isinstance(card, dict):
            raise ValidationError('角色卡必须是对象')
        if set(card) - CHARACTER_DRAFT_KEYS:
            raise ValidationError('角色卡包含未知字段')
        data['character'] = card
    else:
        raise Conflict('当前阶段不能修改草稿')
    data['revision'] += 1
    await save(runtime, data, revision)
    return data


async def confirm(runtime, draft_id, body):
    if not isinstance(body,dict): raise ValidationError('请求必须是对象')
    data = await get(runtime,draft_id)
    if body.get('revision') != data['revision']: raise Conflict('草稿版本过期')
    revision = data['revision']
    action = body.get('action')
    if action in ('accept_change','reject_change'):
        if not data['pending']: raise Conflict('没有待确认修改')
        pending = data['pending']
        if action == 'accept_change':
            data['book' if data['stage'] == 'world' else 'character'] = pending['document']
        data['pending'] = None
        if data['stage'] == 'world' and pending.get('finish_world') and not missing_world_fields(data['book']):
            enter_character(data, revision)
    elif action == 'confirm_world':
        if data['stage'] != 'world' or data['pending']: raise Conflict('请先完成世界草稿确认')
        enter_character(data, revision)
    elif action == 'confirm_character':
        if data['stage']=='ready': return data
        if data['stage']!='character' or data['pending']: raise Conflict('请先完成角色草稿')
        card = validate_character(data['character'])
        await launch(runtime,data,card,revision)
        return data
    else: raise ValidationError('未知确认操作')
    data['revision'] += 1
    await save(runtime,data,revision)
    return data


async def create_prepared_world(conn, book, card, world_revision: int, *, opening_options=None, opening_guide=None) -> World:
    """Insert a prepared adventure from a confirmed world book and character."""
    book = deepcopy(book)
    book['revision'] = world_revision
    wid, pid, location = [str(uuid.uuid4()) for _ in range(3)]
    world = World(
        id=wid, name=book['name'],
        setting='\n'.join(e['text'] for e in book['sections']['background'] if e['visibility'] == 'public'),
        phase=Phase.PREPARED, version=0, clock=0, ruleset_id=RULESET,
        ruleset_config={**CONFIG, 'world_book': book, 'opening_options': opening_options or [],
                        'opening_guide': opening_guide or {}}, player_id=pid,
    )
    await WorldRepo(conn).create(world)
    await EntityRepo(conn).upsert(Entity(
        id=location, world_id=wid, kind=EntityKind.PLACE, name=book['initial_scene']['name'],
        attributes={'description': book['initial_scene']['description']}, revision=1,
    ))
    await EntityRepo(conn).upsert(Entity(
        id=pid, world_id=wid, kind=EntityKind.PLAYER, name=card['name'],
        attributes={**card, 'character_template': deepcopy(card), 'resources': {'coins': card.get('money', 0)},
                    'location_id': location, 'opening_intent': book['initial_scene']['opening']},
        revision=1,
    ))
    await FactRepo(conn).upsert(Fact(
        id=str(uuid.uuid4()), world_id=wid, subject_id=location, predicate='scene',
        value=book['initial_scene']['description'], status=FactStatus.ACTIVE,
        disclosure_rule={'disclosure': 'public'},
    ))
    for entry in book['sections']['factions']:
        if entry['status'] == 'open':
            continue
        faction_id = str(uuid.uuid5(uuid.UUID(wid), entry['id']))
        await EntityRepo(conn).upsert(Entity(
            id=faction_id, world_id=wid, kind=EntityKind.FACTION,
            name=entry.get('name') or entry['id'],
            attributes={'description': entry['text'], 'world_book_id': entry['id'], 'location_id': location},
            revision=1,
        ))
        await FactRepo(conn).upsert(Fact(
            id=str(uuid.uuid4()), world_id=wid, subject_id=faction_id, predicate='profile',
            value=entry['text'], status=FactStatus.ACTIVE,
            disclosure_rule={'disclosure': 'public' if entry['visibility'] == 'public' else 'hidden'},
        ))
    for equipment in card.get('equipment', []):
        await EntityRepo(conn).upsert(Entity(
            id=str(uuid.uuid4()), world_id=wid, kind=EntityKind.ITEM,
            name=equipment, attributes={'owner': pid}, revision=1,
        ))
    return world


async def launch(runtime, data, card, revision):
    async with runtime.db.transaction():
        # Claim revision and create the archive in the same transaction.
        world = await create_prepared_world(runtime.db.conn, data['book'], card, data.get('world_revision', revision))
        data.update(stage='ready', world_id=world.id, character=card, revision=revision+1)
        cursor = await runtime.db.conn.execute(
            'UPDATE workshop SET revision=?,stage=?,payload=? WHERE id=? AND revision=?',
            (data['revision'], 'ready', json.dumps(data, ensure_ascii=False), data['id'], revision),
        )
        if cursor.rowcount != 1:
            raise Conflict('草稿已更新')
