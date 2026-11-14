import { request } from '../api.js';

const SECTIONS = [
  ['background', '背景', '这个世界是什么样的地方，人们怎样生活。'],
  ['tone', '基调', '故事的气氛，例如冒险、轻松、阴郁或残酷。'],
  ['geography', '地理', '开场附近写具体，远处可以只写轮廓。'],
  ['supernatural', '超凡', '魔法、异能或神异。写明来源、限制和代价。没有就直接写没有。'],
  ['species', '物种', '有哪些智慧种族。只有人类也可以。'],
  ['factions', '势力', '开场就会遇到的组织、人群或势力。'],
  ['currency', '货币', '货币名称、换算，以及大致能买到什么。'],
  ['transport', '交通', '怎么移动，要多久，花费和危险。'],
  ['law', '律法', '什么被禁止，谁在维持秩序。'],
  ['open_questions', '留白', '故意留给冒险展开的未知。'],
];

const WORLD_PAGES = [
  ['概览', ['世界名', '背景', '基调']],
  ['开场', ['场景名', '场景', '开场']],
  ['地理与超凡', ['地理', '超凡']],
  ['物种与势力', ['物种', '势力']],
  ['生活与规则', ['货币', '交通', '律法']],
  ['留白', ['留白']],
];
const CHARACTER_PAGES = [
  ['身份与动机', ['姓名', '经历', '动机']],
  ['形象', ['高概念', '自由形象']],
  ['技能（一）', ['技能理由']],
  ['技能（二）', []],
  ['行装', ['装备', '金钱']],
];

const NAMED = new Set(['factions', 'currency']);
const SKILLS = ['学术', '运动', '盗窃', '人脉', '工匠', '欺瞒', '驾驶', '同理', '战斗', '调查', '传说', '感知', '体魄', '挑弄', '亲善', '资源', '射击', '隐匿', '意志'];
const STAGE_LABEL = { world: '世界设定', character: '创建角色', ready: '已创建存档' };

export async function mountWorkshop(root, onReady) {
  let draft = null;
  let choosing = true;
  let localBook = null;
  let localCharacter = null;
  let busy = false;
  let dirty = false;
  let editGen = 0;
  let savedPack = '';
  let paintedKey = null;
  let sheetPage = 0;
  const entryPages = new Map();
  let saveChain = Promise.resolve();
  let timer = 0;

  root.innerHTML = `
    <section id="world-library">
      <p id="library-status" role="status" hidden></p>
      <button type="button" id="new-draft" class="new-world">＋ 创建新的世界<span>从空白开始，把零散的想法交给主持人整理。</span></button>
      <h3>预设世界</h3><div id="preset-library" class="setting-library"></div>
      <div id="saved-library-section" hidden><h3>我的世界设定</h3><div id="saved-library" class="setting-library"></div></div>
      <div class="resume-draft"><label for="draft-list">继续未完成的创建</label>
        <select id="draft-list"><option value="">选择草稿…</option></select>
      </div>
    </section>
    <div id="world-editor" hidden>
    <div class="workshop-head">
      <button type="button" id="back-library">← 返回世界选择</button>
      <h2 id="editor-title">完善世界设定</h2>
      <p id="editor-description" class="hint"></p>
      <ol id="draft-steps" class="steps"></ol>
      <p id="draft-status" role="status"></p>
    </div>
    <div class="workshop-grid">
      <div class="chat-column">
        <h3>和主持人一起构思</h3>
        <p class="hint">写一句短的构想。主持人会把设定填进右侧表格，你可以再改。</p>
        <div id="draft-chat" class="draft-chat" role="log" aria-label="共创对话"></div>
        <div id="draft-options" class="action-choices"></div>
        <form id="draft-form">
          <textarea id="draft-input" rows="3" maxlength="12000" placeholder="先创建一份草稿" required></textarea>
          <button type="submit">发送</button>
        </form>
      </div>
      <div class="sheet-column">
        <div id="draft-sheet"></div>
      </div>
    </div>
    <footer id="draft-actions" class="creation-footer"></footer>
    </div>`;

  const $ = (id) => root.querySelector('#' + id);

  function newId(prefix) {
    return `${prefix}-${crypto.randomUUID().replace(/-/g, '').slice(0, 8)}`;
  }

  function blankEntry(key) {
    return {
      id: newId(key),
      text: '',
      name: '',
      visibility: 'public',
      status: key === 'open_questions' ? 'open' : 'proposed',
      mutability: 'evolving',
    };
  }

  function emptyCharacter() {
    return {
      name: '',
      background: '',
      motivation: '',
      skill_reason: '',
      aspects: [{ kind: 'high_concept', text: '' }, { kind: 'free', text: '' }],
      skills: Object.fromEntries(SKILLS.map((name) => [name, 0])),
      stunts: [],
      equipment: [],
      money: 0,
    };
  }

  function ensureBlankRows() {
    for (const [key] of SECTIONS) {
      const list = localBook.sections[key];
      if (!list.length) list.push(blankEntry(key));
    }
  }

  function packBook(book) {
    const sections = {};
    for (const [key] of SECTIONS) {
      sections[key] = (book.sections?.[key] || []).flatMap((entry) => {
        const text = (entry.text || '').trim();
        if (!text) return [];
        const row = {
          id: entry.id,
          text,
          visibility: entry.visibility || 'public',
          status: entry.status || (key === 'open_questions' ? 'open' : 'proposed'),
          mutability: entry.mutability || 'evolving',
        };
        const name = (entry.name || '').trim();
        if (name) row.name = name;
        return [row];
      });
    }
    return {
      schema_version: 1,
      name: book.name || '',
      sections,
      initial_scene: {
        name: book.initial_scene?.name || '',
        description: book.initial_scene?.description || '',
        opening: book.initial_scene?.opening || '',
      },
      skills: book.skills,
    };
  }

  function packCharacter(card) {
    const skills = {};
    for (const name of SKILLS) skills[name] = Number(card.skills?.[name] || 0);
    return {
      name: card.name || '',
      background: card.background || '',
      motivation: card.motivation || '',
      skill_reason: card.skill_reason || '',
      aspects: [
        { kind: 'high_concept', text: card.aspects?.[0]?.text || '' },
        { kind: 'free', text: card.aspects?.[1]?.text || '' },
      ],
      skills,
      stunts: [],
      equipment: (card.equipment || []).map((item) => String(item).trim()).filter(Boolean),
      money: Math.max(0, Number.parseInt(card.money, 10) || 0),
    };
  }

  function currentPack() {
    if (!draft || draft.stage === 'ready') return '';
    return JSON.stringify(draft.stage === 'world' ? packBook(localBook) : packCharacter(localCharacter));
  }

  function keyOf(data) {
    if (!data) return 'none';
    return `${data.id}:${data.stage}:${data.revision}:${data.pending ? 'pending' : 'ok'}`;
  }

  function adopt(next) {
    if (draft?.stage !== next.stage || draft?.id !== next.id) {
      sheetPage = 0;
      entryPages.clear();
    }
    draft = next;
    choosing = false;
    document.getElementById('create-page').scrollTop = 0;
    localBook = structuredClone(draft.book);
    localCharacter = draft.character ? structuredClone(draft.character) : emptyCharacter();
    localCharacter.skills = localCharacter.skills || {};
    for (const name of SKILLS) {
      if (localCharacter.skills[name] == null) localCharacter.skills[name] = 0;
    }
    if (draft.stage === 'world') ensureBlankRows();
    savedPack = currentPack();
    dirty = false;
    editGen += 1;
    paintedKey = null;
  }

  function markDirty() {
    dirty = true;
    editGen += 1;
    clearTimeout(timer);
    timer = setTimeout(() => {
      flushSave().catch((error) => { $('draft-status').textContent = error.message; });
    }, 450);
  }

  async function doFlush() {
    if (!dirty || !draft || draft.pending || draft.stage === 'ready') return;
    $('draft-status').textContent = '正在保存表格…';
    while (dirty && draft && !draft.pending && draft.stage !== 'ready') {
      const gen = editGen;
      const pack = currentPack();
      if (pack === savedPack) {
        dirty = false;
        break;
      }
      const parsed = JSON.parse(pack);
      const body = draft.stage === 'world'
        ? { revision: draft.revision, book: parsed }
        : { revision: draft.revision, character: parsed };
      const next = await request(`/workshop/${draft.id}/document`, body);
      draft.revision = next.revision;
      draft.book = next.book;
      draft.character = next.character;
      savedPack = pack;
      dirty = gen !== editGen;
    }
    paintedKey = keyOf(draft);
    renderChrome();
  }

  function flushSave() {
    const run = saveChain.then(() => doFlush());
    saveChain = run.catch(() => {});
    return run;
  }

  function render() {
    renderChrome();
    const key = keyOf(draft);
    if (key !== paintedKey) {
      renderSheet();
      paintedKey = key;
    }
  }

  function repaintSheet() {
    renderSheet();
    paintedKey = keyOf(draft);
  }

  function renderChrome() {
    $('world-library').hidden = !choosing;
    $('world-editor').hidden = choosing;
    document.getElementById('creation-intro').hidden = !choosing;
    $('new-draft').disabled = busy;
    $('draft-list').disabled = busy;
    root.querySelectorAll('[data-setting]').forEach(button => button.disabled = busy);
    $('back-library').disabled = busy;
    $('editor-title').textContent = draft?.stage === 'character' ? '创建角色卡' : draft?.stage === 'ready' ? '准备进入冒险' : '完善世界设定';
    $('editor-description').textContent = draft?.stage === 'character'
      ? `世界「${draft.book.name}」已保存。说说你想扮演谁，主持人会整理成右侧角色卡。`
      : '左边随意聊想法，右边查看和修改条目。关键缺失会附上建议供你选择。';
    renderSteps();
    const chat = $('draft-chat');
    chat.replaceChildren();
    $('draft-options').replaceChildren();
    $('draft-actions').replaceChildren();
    const locked = !draft || busy || !!draft.pending || draft.stage === 'ready';
    $('draft-input').disabled = locked;
    $('draft-form').querySelector('button').disabled = locked;
    if (!draft) {
      $('draft-status').textContent = '选择一个世界设定，或创建新的世界。';
      $('draft-input').placeholder = '先创建一份草稿';
      return;
    }
    $('draft-status').textContent = busy
      ? '正在把描述写入表格…'
      : draft.pending ? '等待确认修改' : '草稿已保存';
    $('draft-input').placeholder = draft.stage === 'character'
      ? '用几句话描述角色，例如：退役领航员，来找失踪的弟弟，认天气、水性好。'
      : '用几句话描述世界，例如：没有魔法的港城，货币是铜贝，出城靠河船。';
    for (const message of draft.history || []) {
      const p = document.createElement('p');
      p.className = 'msg ' + (message.role === 'player' ? 'player' : 'gm');
      p.textContent = (message.role === 'player' ? '你：' : '主持人：') + message.text;
      chat.append(p);
    }
    chat.scrollTop = chat.scrollHeight;
    if (draft.pending) {
      const note = document.createElement('p');
      note.className = 'issues';
      note.textContent = '这版修改和已有设定冲突：' + (draft.pending.conflicts || []).join('；');
      $('draft-actions').append(note);
      $('draft-actions').append(actionButton('接受这次修改', () => confirmAction('accept_change')));
      $('draft-actions').append(actionButton('保留原设定', () => confirmAction('reject_change')));
    } else if (draft.stage === 'world') {
      $('draft-actions').append(actionButton('保存世界并创建角色', () => confirmAction('confirm_world'), true));
    } else if (draft.stage === 'character') {
      $('draft-actions').append(actionButton('保存角色卡，创建冒险', () => confirmAction('confirm_character'), true));
    } else {
      $('draft-actions').append(actionButton('进入这个世界', () => onReady(draft.world_id), true));
    }
    if (!draft.pending && draft.stage !== 'ready') {
      for (const text of draft.options || []) {
        $('draft-options').append(actionButton(text, () => send(text)));
      }
    }
    $('draft-sheet').inert = busy;
  }

  function renderSteps() {
    const order = { world: 0, character: 1, ready: 2 };
    const current = draft ? order[draft.stage] : -1;
    const list = $('draft-steps');
    list.replaceChildren();
    ['世界设定', '角色卡', '进入冒险'].forEach((label, index) => {
      const item = document.createElement('li');
      item.textContent = `${index + 1} ${label}`;
      item.className = index === current ? 'current' : index < current ? 'done' : '';
      list.append(item);
    });
  }

  function actionButton(label, onClick, primary = false) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = label;
    button.disabled = busy;
    if (primary) button.className = 'primary';
    button.addEventListener('click', onClick);
    return button;
  }

  function renderSheet() {
    const sheet = $('draft-sheet');
    sheet.replaceChildren();
    if (!draft) {
      const p = document.createElement('p');
      p.className = 'hint';
      p.textContent = '创建草稿后，这里会出现世界设定表。手写，或让左侧对话补全。';
      sheet.append(p);
      return;
    }
    if (draft.pending) {
      const note = document.createElement('p');
      note.className = 'hint';
      note.textContent = '下面是待确认的版本。先接受或保留，再继续编辑。';
      sheet.append(note);
    }
    if (draft.stage === 'ready') {
      sheet.append(readySummary());
      return;
    }
    const readOnly = !!draft.pending;
    const pages = draft.stage === 'world' ? WORLD_PAGES : CHARACTER_PAGES;
    const nav = document.createElement('nav');
    nav.className = 'sheet-pages';
    nav.setAttribute('aria-label', '表单分页');
    pages.forEach(([label], index) => {
      const button = actionButton(label, () => {
        sheetPage = index;
        repaintSheet();
        sheet.querySelector('[aria-current="page"]')?.focus();
        sheet.closest('.sheet-column').scrollTop = 0;
      });
      if (index === sheetPage) button.setAttribute('aria-current', 'page');
      nav.append(button);
    });
    sheet.append(nav);
    if (draft.stage === 'world') sheet.append(renderWorldSheet(readOnly ? draft.pending.document : localBook, readOnly));
    else sheet.append(renderCharacterSheet(readOnly ? draft.pending.document : localCharacter, readOnly));
  }

  function addRow(table, label, hint, control) {
    const pages = draft.stage === 'world' ? WORLD_PAGES : CHARACTER_PAGES;
    if (!pages[sheetPage][1].includes(label)) return;
    const row = document.createElement('tr');
    const head = document.createElement('th');
    head.textContent = label;
    if (hint) {
      const note = document.createElement('span');
      note.className = 'hint';
      note.textContent = hint;
      head.append(note);
    }
    const cell = document.createElement('td');
    const fields = control.matches?.('input, textarea, select') ? [control] : control.querySelectorAll('input, textarea, select');
    fields.forEach((field, index) => field.setAttribute('aria-label', fields.length > 1 ? `${label} ${index + 1}` : label));
    cell.append(control);
    row.append(head, cell);
    table.append(row);
  }

  function textControl(value, readOnly, onInput, options = {}) {
    const input = document.createElement(options.multiline ? 'textarea' : 'input');
    input.value = value || '';
    if (options.multiline) input.rows = options.rows || 3;
    if (options.type) input.type = options.type;
    if (options.min != null) input.min = String(options.min);
    if (options.maxLength) input.maxLength = options.maxLength;
    input.readOnly = readOnly;
    if (!readOnly) input.addEventListener('input', () => onInput(input.value));
    return input;
  }

  function renderWorldSheet(book, readOnly) {
    const wrap = document.createElement('div');
    wrap.className = 'sheet';
    const title = document.createElement('h3');
    title.textContent = '世界设定';
    const intro = document.createElement('p');
    intro.className = 'hint';
    intro.textContent = '每一栏都可以写多条。空行不会保存。';
    wrap.append(title, intro);
    const table = document.createElement('table');
    table.className = 'sheet-table';
    addRow(table, '世界名', '玩家看到的名字。', textControl(book.name, readOnly, (value) => {
      localBook.name = value;
      markDirty();
    }, { maxLength: 120 }));
    addRow(table, '场景名', '开场地点的名字。', textControl(book.initial_scene?.name, readOnly, (value) => {
      localBook.initial_scene.name = value;
      markDirty();
    }, { maxLength: 120 }));
    addRow(table, '场景', '玩家第一眼能看见的环境。', textControl(book.initial_scene?.description, readOnly, (value) => {
      localBook.initial_scene.description = value;
      markDirty();
    }, { multiline: true, rows: 3 }));
    addRow(table, '开场', '主持人开场时展示的描述。第一步行动由玩家自己选择。', textControl(book.initial_scene?.opening, readOnly, (value) => {
      localBook.initial_scene.opening = value;
      markDirty();
    }, { multiline: true, rows: 2 }));
    for (const [key, label, hint] of SECTIONS) {
      addRow(table, label, hint, sectionEditor(key, book, readOnly));
    }
    wrap.append(table);
    return wrap;
  }

  function sectionEditor(key, book, readOnly) {
    const wrap = document.createElement('div');
    wrap.className = 'entries';
    wrap.dataset.section = key;
    const entries = book.sections?.[key] || [];
    if (!entries.length) {
      const empty = document.createElement('p');
      empty.className = 'hint';
      empty.textContent = '还没有内容';
      wrap.append(empty);
    }
    const index = Math.min(entryPages.get(key) || 0, Math.max(0, entries.length - 1));
    if (entries.length) wrap.append(entryRow(key, entries[index], index, readOnly));
    if (entries.length > 1) {
      const pager = document.createElement('div');
      pager.className = 'entry-pager';
      const move = (offset) => {
        entryPages.set(key, index + offset);
        repaintSheet();
        $('draft-sheet').querySelector('[data-section="' + key + '"] textarea')?.focus();
      };
      const previous = actionButton('上一条', () => move(-1));
      previous.disabled ||= index === 0;
      const next = actionButton('下一条', () => move(1));
      next.disabled ||= index === entries.length - 1;
      const count = document.createElement('span');
      count.textContent = (index + 1) + ' / ' + entries.length;
      pager.append(previous, count, next);
      wrap.append(pager);
    }
    if (!readOnly) {
      const add = document.createElement('button');
      add.type = 'button';
      add.textContent = '添加一条';
      add.addEventListener('click', () => {
        localBook.sections[key].push(blankEntry(key));
        entryPages.set(key, localBook.sections[key].length - 1);
        repaintSheet();
        const areas = $('draft-sheet').querySelectorAll(`[data-section="${key}"] textarea`);
        areas[areas.length - 1]?.focus();
      });
      wrap.append(add);
    }
    return wrap;
  }

  function entryRow(key, entry, index, readOnly) {
    const box = document.createElement('div');
    box.className = 'entry';
    if (NAMED.has(key)) {
      box.append(textControl(entry.name, readOnly, (value) => {
        localBook.sections[key][index].name = value;
        markDirty();
      }, { maxLength: 80 }));
      box.lastChild.placeholder = key === 'currency' ? '货币名称' : '势力名称';
    }
    const area = textControl(entry.text, readOnly, (value) => {
      localBook.sections[key][index].text = value;
      markDirty();
    }, { multiline: true, rows: 2 });
    area.placeholder = '写下这一条设定';
    box.append(area);
    const meta = document.createElement('div');
    meta.className = 'entry-meta';
    if (entry.visibility === 'gm') {
      const tag = document.createElement('span');
      tag.textContent = '仅主持人可见';
      meta.append(tag);
    }
    if (entry.status === 'confirmed') {
      const tag = document.createElement('span');
      tag.textContent = '已确认';
      meta.append(tag);
    }
    if (!readOnly && entry.status !== 'confirmed') {
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.textContent = '删除';
      remove.addEventListener('click', () => {
        localBook.sections[key].splice(index, 1);
        if (!localBook.sections[key].length) localBook.sections[key].push(blankEntry(key));
        markDirty();
        repaintSheet();
      });
      meta.append(remove);
    }
    if (meta.childNodes.length) box.append(meta);
    return box;
  }

  function skillHint(skills) {
    const plus2 = SKILLS.filter((name) => Number(skills?.[name] || 0) === 2);
    const plus1 = SKILLS.filter((name) => Number(skills?.[name] || 0) === 1);
    const ok = plus2.length === 1 && plus1.length === 2;
    return ok
      ? '技能分配符合规则：一个 +2，两个 +1。'
      : `当前 ${plus2.length} 个 +2、${plus1.length} 个 +1。确认前需要恰好一个 +2、两个 +1。`;
  }

  function renderCharacterSheet(card, readOnly) {
    const wrap = document.createElement('div');
    wrap.className = 'sheet';
    const title = document.createElement('h3');
    title.textContent = '角色';
    const intro = document.createElement('p');
    intro.className = 'hint';
    intro.textContent = '说出经历之后，也可以让对话按规则补上形象、技能和装备。';
    wrap.append(title, intro);
    const table = document.createElement('table');
    table.className = 'sheet-table';
    const fields = [
      ['姓名', 'name', '别人怎么称呼这个角色。', { maxLength: 80 }],
      ['经历', 'background', '成长和来历。', { multiline: true, rows: 3 }],
      ['动机', 'motivation', '为什么站在开场，想做什么。', { multiline: true, rows: 2 }],
      ['技能理由', 'skill_reason', '为什么是这几项技能，而不是别的。', { multiline: true, rows: 2 }],
    ];
    for (const [label, key, hint, options] of fields) {
      addRow(table, label, hint, textControl(card[key], readOnly, (value) => {
        localCharacter[key] = value;
        markDirty();
      }, options));
    }
    addRow(table, '高概念', '一句话概括这个人是谁。', textControl(card.aspects?.[0]?.text, readOnly, (value) => {
      localCharacter.aspects[0].text = value;
      markDirty();
    }, { maxLength: 200 }));
    addRow(table, '自由形象', '另一个显著的特质、关系或麻烦。', textControl(card.aspects?.[1]?.text, readOnly, (value) => {
      localCharacter.aspects[1].text = value;
      markDirty();
    }, { maxLength: 200 }));
    addRow(table, '装备', '每行一件普通装备。', textControl((card.equipment || []).join('\n'), readOnly, (value) => {
      localCharacter.equipment = value.split('\n').map((item) => item.trim()).filter(Boolean);
      markDirty();
    }, { multiline: true, rows: 3 }));
    addRow(table, '金钱', '使用世界货币的基本单位。', textControl(String(card.money ?? 0), readOnly, (value) => {
      localCharacter.money = value;
      markDirty();
    }, { type: 'number', min: 0 }));
    if (table.childElementCount) wrap.append(table);
    if (sheetPage !== 2 && sheetPage !== 3) return wrap;

    const skillTitle = document.createElement('h3');
    skillTitle.textContent = '技能';
    const hint = document.createElement('p');
    hint.className = 'hint';
    hint.id = 'skill-hint';
    hint.textContent = skillHint(card.skills);
    const grid = document.createElement('div');
    grid.className = 'skill-grid';
    for (const name of sheetPage === 2 ? SKILLS.slice(0, 10) : SKILLS.slice(10)) {
      const label = document.createElement('label');
      const span = document.createElement('span');
      span.textContent = name;
      const select = document.createElement('select');
      select.disabled = readOnly;
      for (const [value, text] of [['0', '0'], ['1', '+1'], ['2', '+2']]) {
        const option = document.createElement('option');
        option.value = value;
        option.textContent = text;
        if (Number(card.skills?.[name] || 0) === Number(value)) option.selected = true;
        select.append(option);
      }
      if (!readOnly) {
        select.addEventListener('change', () => {
          localCharacter.skills[name] = Number(select.value);
          const node = $('skill-hint');
          if (node) node.textContent = skillHint(localCharacter.skills);
          markDirty();
        });
      }
      label.append(span, select);
      grid.append(label);
    }
    wrap.append(skillTitle, hint, grid);
    return wrap;
  }

  function readySummary() {
    const wrap = document.createElement('div');
    wrap.className = 'sheet';
    const title = document.createElement('h3');
    title.textContent = '存档已创建';
    const world = document.createElement('p');
    world.textContent = `世界：${draft.book?.name || '未命名'}`;
    const person = document.createElement('p');
    person.textContent = `角色：${draft.character?.name || '未命名'}`;
    const concept = document.createElement('p');
    concept.textContent = draft.character?.aspects?.[0]?.text || '';
    wrap.append(title, world, person, concept);
    return wrap;
  }

  async function perform(fn) {
    if (busy) return;
    busy = true;
    $('library-status').hidden = true;
    renderChrome();
    try {
      await fn();
      busy = false;
      render();
      await refresh();
    } catch (error) {
      busy = false;
      render();
      const status = $(choosing ? 'library-status' : 'draft-status');
      status.hidden = false;
      status.textContent = error.message;
    }
  }

  async function send(text) {
    if (!draft || !text.trim() || busy) return;
    clearTimeout(timer);
    busy = true;
    renderChrome();
    const source = new EventSource(`/api/v1/workshop/${draft.id}/events`);
    source.addEventListener('assistant.delta', (ev) => {
      try {
        const data = JSON.parse(ev.data);
        const reply = data.payload?.text;
        if (!reply) return;
        const chat = $('draft-chat');
        let line = chat.querySelector('[data-streaming]');
        if (!line) {
          line = document.createElement('p');
          line.className = 'msg gm';
          line.dataset.streaming = 'true';
          chat.append(line);
        }
        line.textContent = '主持人：' + reply;
        chat.scrollTop = chat.scrollHeight;
      } catch (_) {}
    });
    try {
      await flushSave();
      const next = await request(`/workshop/${draft.id}/messages`, { revision: draft.revision, text: text.trim() });
      adopt(next);
      $('draft-input').value = '';
      busy = false;
      render();
      await refresh();
    } catch (error) {
      busy = false;
      render();
      $('draft-status').textContent = error.message;
    } finally {
      source.close();
    }
  }

  async function confirmAction(action) {
    if (!draft || busy) return;
    clearTimeout(timer);
    busy = true;
    renderChrome();
    try {
      await flushSave();
      const next = await request(`/workshop/${draft.id}/confirm`, { revision: draft.revision, action });
      adopt(next);
      busy = false;
      render();
      await refresh();
    } catch (error) {
      busy = false;
      render();
      $('draft-status').textContent = error.message;
    }
  }

  async function refresh() {
    const rows = await request('/workshop', undefined, undefined, 'GET');
    const list = $('draft-list');
    const current = draft?.id || '';
    list.replaceChildren(new Option('继续已有草稿…', ''));
    const unfinished = rows.filter(row => row.stage === 'world' || row.stage === 'character');
    list.closest('.resume-draft').hidden = !unfinished.length;
    for (const row of unfinished) {
      list.add(new Option(`${row.name || '未命名世界'} · ${STAGE_LABEL[row.stage] || row.stage}`, row.id));
    }
    list.value = choosing ? '' : current;
    const catalog = await request('/workshop/catalog', undefined, undefined, 'GET');
    for (const [id, entries] of [['preset-library', catalog.presets], ['saved-library', catalog.saved]]) {
      $(id).replaceChildren();
      for (const entry of entries) {
        const button = actionButton('', () => perform(async () => {
          await flushSave();
          adopt(await request('/workshop', {source: {kind: entry.kind, id: entry.id}}));
        }));
        button.className = 'setting-card';
        button.dataset.setting = 'true';
        const title = document.createElement('strong');
        title.textContent = entry.name;
        const description = document.createElement('span');
        description.textContent = entry.description;
        button.append(title, description);
        const book = document.createElement('article');
        book.className = 'shelf-book';
        book.append(button);
        if (entry.kind !== 'preset') {
          const remove = actionButton('删除', () => {
            const note = entry.kind === 'saved'
              ? '关联的未完成角色草稿也会移除。已开始的冒险仍可从「世界档案」继续。'
              : '关联的冒险仍可从「世界档案」继续。';
            if (!window.confirm(`从书架删除「${entry.name}」？${note}`)) return;
            perform(async () => {
              await request(`/workshop/catalog/${entry.kind}/${encodeURIComponent(entry.id)}`, undefined, undefined, 'DELETE');
              if (draft?.id === entry.id) draft = null;
              $('library-status').textContent = `已删除「${entry.name}」`;
              $('library-status').hidden = false;
            });
          });
          remove.className = 'remove-setting';
          remove.dataset.setting = 'true';
          remove.setAttribute('aria-label', `删除世界设定「${entry.name}」`);
          book.append(remove);
        }
        $(id).append(book);
      }
    }
    $('saved-library-section').hidden = !catalog.saved.length;
  }

  $('back-library').addEventListener('click', () => perform(async () => {
    clearTimeout(timer);
    await flushSave();
    choosing = true;
    document.getElementById('create-page').scrollTop = 0;
  }));
  $('new-draft').addEventListener('click', () => perform(async () => {
    clearTimeout(timer);
    await flushSave();
    adopt(await request('/workshop', {}));
  }));
  $('draft-list').addEventListener('change', () => {
    const id = $('draft-list').value;
    if (!id) return;
    perform(async () => {
      clearTimeout(timer);
      await flushSave();
      adopt(await request(`/workshop/${id}`, undefined, undefined, 'GET'));
    });
  });
  $('draft-form').addEventListener('submit', (event) => {
    event.preventDefault();
    send($('draft-input').value);
  });

  render();
  await refresh();
  return async (worldId) => {
    if (draft?.world_id === worldId) {
      draft = null;
      choosing = true;
      render();
    }
    await refresh();
  };
}
