import { mountWorkshop } from './components/workshop.js';
import { request, sendInteraction, confirmProposal, rejectProposal, subscribe } from './api.js';
import { reduce, getPendingRequest, savePendingRequest, rememberMessage } from './state.js';
import { renderWorlds } from './components/world-picker.js';
import { renderHistory, streamNarrative } from './components/narrative.js';
import { renderMessages, setBusy, showError, streamInteraction } from './components/interaction.js';
import { clearDraftText, resetTranscript } from './components/transcript.js';
import { renderProposal } from './components/proposal.js';

let refreshWorkshopAfterDelete;

let state = {
  worlds: [],
  worldId: null,
  messages: { interaction: [], narrative: [] },
  seenMessageIds: new Set(),
  finishedRequests: new Set(),
  lastSequence: 0,
  modeHint: 'auto',
  suggestedAction: null,
  stage: null,
  unsubscribe: null,
};

const els = {
  picker: document.getElementById('world-picker'),
  narrative: document.getElementById('narrative'),
  interaction: document.getElementById('interaction'),
  proposal: document.getElementById('proposal'),
  status: document.getElementById('status'),
  input: document.getElementById('input'),
  send: document.getElementById('send'),
  execute: document.getElementById('execute-action'),
  composer: document.getElementById('composer'),
};

function uuid() {
  return crypto.randomUUID();
}

async function boot() {
  document.querySelectorAll('.mobile-tabs button').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.mobile-tabs button').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      document.querySelectorAll('.panel').forEach((p) => p.classList.remove('active'));
      document.querySelector(`.${btn.dataset.tab}-panel`)?.classList.add('active');
    });
  });
  document.querySelector('.narrative-panel')?.classList.add('active');

  els.composer.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    await onSend({ text: els.input.value.trim(), modeHint: state.modeHint });
  });
  els.execute.addEventListener('click', async () => {
    if (!state.suggestedAction) return;
    els.input.value = state.suggestedAction;
    await onSend({ text: state.suggestedAction, modeHint: '行动' });
  });

  document.querySelectorAll('[data-page]').forEach((b) => { b.onclick = () => showPage(b.dataset.page); });
  document.querySelectorAll('[data-preset]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const name = btn.querySelector('strong')?.textContent || '这个预设';
      if (btn.dataset.ready !== 'true') {
        els.status.textContent = `${name}还是空壳，详细设定补上之后才能载入。`;
        return;
      }
      onPreparePreset(btn.dataset.preset);
    });
  });
  window.addEventListener('hashchange', () => showPage(pageFromHash()));
  showPage(pageFromHash());
  await refreshWorlds();
  await renderDemos();
  refreshWorkshopAfterDelete = await mountWorkshop(document.getElementById('workshop'),async id => { await refreshWorlds(); await selectWorld(id); });
}

async function refreshWorlds() {
  state.worlds = await request('/worlds', undefined, undefined, 'GET');
  renderWorlds(els.picker, state.worlds, state.worldId, { onSelect: selectWorld, onDelete: deleteWorld });
}

async function deleteWorld(world) {
  if (!window.confirm(`永久删除「${world.name || world.id}」的冒险存档？该角色、进度和对话将被清除。`)) return;
  const status = document.getElementById('archive-status');
  status.textContent = '';
  try {
    await request(`/worlds/${encodeURIComponent(world.id)}`, undefined, undefined, 'DELETE');
    savePendingRequest({ worldId: world.id, clear: true });
    if (state.worldId === world.id) {
      state.unsubscribe?.();
      state.worldId = null;
      state.unsubscribe = null;
      state.stage = null;
      state.suggestedAction = null;
      state.messages = { interaction: [], narrative: [] };
      resetTranscript(els.narrative);
      resetTranscript(els.interaction);
      renderProposal(els.proposal, null, {});
      els.execute.hidden = true;
      els.execute.disabled = true;
      els.input.value = '';
      document.getElementById('character-name').textContent = '尚未选择角色';
      document.getElementById('fate-card').replaceChildren();
      document.getElementById('skills').replaceChildren();
      document.getElementById('items').replaceChildren();
      document.querySelectorAll('.character-card .vital').forEach(el => el.hidden = true);
      showPage('worlds');
    }
    await refreshWorlds();
    await refreshWorkshopAfterDelete?.(world.id);
    status.textContent = '存档已删除';
  } catch (error) {
    status.textContent = error.message;
  }
}

async function renderDemos() {
  const demos = await request('/demos', undefined, undefined, 'GET');
  for (const id of ['guide-demo-list', 'world-demo-list']) {
    const root = document.getElementById(id);
    if (!root) continue;
    root.replaceChildren();
    const shelf = root.closest('.demo-shelf');
    if (shelf) shelf.hidden = demos.length === 0;
    for (const demo of demos) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'demo-card';
      button.dataset.demo = demo.id;
      const title = document.createElement('strong');
      title.textContent = demo.name;
      const blurb = document.createElement('span');
      blurb.textContent = demo.blurb;
      const who = document.createElement('small');
      who.textContent = demo.character_name ? `扮演：${demo.character_name}` : '开始体验';
      button.append(title, blurb, who);
      button.addEventListener('click', () => playDemo(demo));
      root.append(button);
    }
  }
}

let demoBusy = false;

async function playDemo(demo) {
  if (demoBusy) return;
  demoBusy = true;
  document.querySelectorAll('[data-demo]').forEach((button) => { button.disabled = true; });
  els.status.textContent = `正在准备「${demo.name}」…`;
  try {
    const world = await request(`/demos/${encodeURIComponent(demo.id)}/play`, {});
    await refreshWorlds();
    await selectWorld(world.id);
  } catch (err) {
    setBusy(null, { sendBtn: els.send, actionBtn: els.execute, statusEl: els.status });
    els.status.textContent = err.message || String(err);
  } finally {
    demoBusy = false;
    document.querySelectorAll('[data-demo]').forEach((button) => { button.disabled = false; });
  }
}

async function onPreparePreset(presetId) {
  const world = await request('/worlds', { preset_id: presetId, overrides: {} }, uuid());
  await refreshWorlds();
  await selectWorld(world.id);

}

async function selectWorld(id) {
  const startId = 'opening-v2';
  const prepared = state.worlds.find(world => world.id === id)?.phase === 'prepared';
  if (prepared) {
    els.status.textContent = '正在生成开场…';
    try {
      await request(`/worlds/${id}/start`, { request_id: startId }, startId);
    } catch (error) {
      els.status.textContent = error.message;
      return;
    }
    state.worlds = state.worlds.map(world => world.id === id ? { ...world, phase: 'playing' } : world);
  }
  if (state.unsubscribe) state.unsubscribe();
  resetTranscript(els.narrative);
  resetTranscript(els.interaction);
  state = {
    ...state,
    worldId: id,
    messages: { interaction: [], narrative: [] },
    seenMessageIds: new Set(),
    finishedRequests: new Set(),
    lastSequence: 0,
    stage: null,
    suggestedAction: null,
  };
  renderWorlds(els.picker, state.worlds, state.worldId, { onSelect: selectWorld, onDelete: deleteWorld });
  const [interaction, narrative] = await Promise.all([
    readMessages(id, 'interaction'),
    readMessages(id, 'narrative'),
  ]);
  interaction.forEach((m) => { state = rememberMessage(state, m); });
  narrative.forEach((m) => { state = rememberMessage(state, m); });
  renderMessages(els.interaction, state.messages.interaction, onChoose, { animate: prepared });
  renderHistory(els.narrative, state.messages.narrative, { animate: prepared });

  const pending = getPendingRequest(id);
  if (pending) {
    try {
      const view = await request(`/worlds/${id}/requests/${pending.requestId}`, undefined, undefined, 'GET');
      if (view.status === 'completed') {
        savePendingRequest({ worldId: id, clear: true });
      } else if (view.retryable) {
        els.status.textContent = '发现未完成请求，可重试';
      }
    } catch (_) {}
  }

  await refreshCharacter();
  renderProposal(els.proposal,null,{});
  setBusy(null,{sendBtn:els.send,actionBtn:els.execute,statusEl:els.status});
  els.execute.disabled=true; els.execute.hidden=true;
  showPage('play');
  state.unsubscribe = subscribe(id, state.lastSequence, e=>{if(state.worldId===id)return onEvent(e);});
}

async function onSend({ text, modeHint, replyTo, combatDescription }) {
  if (!state.worldId) {els.status.textContent='请先在世界档案中选择一段冒险';return;}
  if (!text || els.send.disabled) return;
  const requestId = uuid();
  savePendingRequest({ worldId: state.worldId, requestId });
  setBusy('received', { sendBtn: els.send, actionBtn: els.execute, statusEl: els.status });
  try {
    await sendInteraction(state.worldId, {
      request_id: requestId,
      text,
      mode_hint: modeHint,
      reply_to: replyTo,
      combat_description: combatDescription,
    });
    els.input.value = '';
    await refreshInteraction();
  } catch (err) {
    setBusy(null, {sendBtn:els.send,actionBtn:els.execute,statusEl:els.status});
    showError(els.interaction, err, async () => {
      // Keep same request_id — do not mint a new one for actions.
      await request(`/worlds/${state.worldId}/requests/${err.requestId || requestId}/retry`, {}, err.requestId || requestId);
    });
  }
}

async function onEvent(event) {
  if (event.id && event.id <= state.lastSequence) return;
  if (event.type === 'assistant.delta') {
    const { text, channel } = event.payload || {};
    if (!text || state.finishedRequests.has(event.request_id)) return;
    if (channel === 'narrative') streamNarrative(els.narrative, event.request_id, text);
    if (channel === 'interaction') streamInteraction(els.interaction, event.request_id, text, onChoose);
    const waiting = document.getElementById('waiting');
    if (waiting) waiting.hidden = true;
    return;
  }
  state = reduce(state, event);
  setBusy(state.stage, { sendBtn: els.send, actionBtn: els.execute, statusEl: els.status });
  if (['interaction.completed', 'turn.committed', 'request.failed'].includes(event.type)) {
    state.finishedRequests.add(event.request_id);
  }
  if (event.type === 'progress') state.finishedRequests.delete(event.request_id);

  if (['action.acknowledged', 'check.announced', 'proposal.ready'].includes(event.type)) await refreshInteraction();

  if (event.type === 'interaction.completed') {
    await refreshInteraction({ finalId: event.payload.message_id, finalRequest: event.request_id });
    state.suggestedAction=null;
    els.execute.disabled=true; els.execute.hidden=true;
    if (event.payload.suggested_action) {
      state.suggestedAction = event.payload.suggested_action;
      els.execute.disabled = false;
      els.execute.hidden = false;
    }
    savePendingRequest({ worldId: state.worldId, clear: true });
  }

  if (event.type === 'proposal.ready') {
    renderProposal(els.proposal, event.payload, {
      onConfirm: async (id, revision) => {
        const requestId = uuid();
        await confirmProposal(state.worldId, id, revision, event.payload.base_version, requestId);
      },
      onReject: async (id) => {
        await rejectProposal(state.worldId, id, event.payload.revision, uuid());
        renderProposal(els.proposal, null, {});
      },
      onRevise: (text) => {
        els.input.value = text;
        state.modeHint = 'auto';
      },
    });
  }

  if (event.type === 'turn.committed') {
    await refreshCharacter();
    const messages = await readMessages(state.worldId, 'narrative', state.messages.narrative.at(-1)?.sequence || 0);
    messages.forEach(m => { state = rememberMessage(state, m); });
    renderHistory(els.narrative, state.messages.narrative, { animate: true, finalRequest: event.request_id });
    // Also refresh interaction player line already stored server-side
    await refreshInteraction();
    savePendingRequest({ worldId: state.worldId, clear: true });
    setBusy('completed', { sendBtn: els.send, actionBtn: els.execute, statusEl: els.status });
  }

  if (event.type === 'request.failed') {
    clearDraftText(els.interaction, event.request_id);
    clearDraftText(els.narrative, event.request_id);
    showError(els.interaction, { message: event.payload?.error || '请求失败' }, async()=>{await request(`/worlds/${state.worldId}/requests/${event.request_id}/retry`,{},event.request_id);});
  }
}

const PAGES = ['guide', 'create', 'worlds', 'play'];

function pageFromHash() {
  const name = location.hash.replace(/^#/, '');
  return PAGES.includes(name) ? name : 'guide';
}

function showPage(page){
  if (!PAGES.includes(page)) page = 'guide';
  for (const name of PAGES) {
    const root = document.getElementById(name === 'play' ? 'play-page' : `${name}-page`);
    if (root) root.hidden = name !== page;
  }
  document.querySelectorAll('[data-page]').forEach(b=>b.classList.toggle('selected',b.dataset.page===page));
  if(location.hash!=='#'+page) history.replaceState(null,'','#'+page);
}

boot().catch((err) => {
  els.status.textContent = err.message || String(err);
});

async function readMessages(worldId, channel, after = 0) {
  const messages = [];
  let page;
  do {
    page = await request(`/worlds/${worldId}/messages?channel=${channel}&after=${after}`, undefined, undefined, 'GET');
    messages.push(...page);
    if (page.length) after = page.at(-1).sequence;
  } while (page.length === 200);
  return messages;
}

async function refreshInteraction(options){
 const choices = state.messages.interaction.find(m => m.choices_active);
 const after = (choices?.sequence || state.messages.interaction.at(-1)?.sequence || 1) - 1;
 const rows = await readMessages(state.worldId, 'interaction', after);
 rows.forEach(m=>{state=rememberMessage(state,m)});
 state.messages.interaction.sort((a,b)=>(a.sequence||0)-(b.sequence||0));
 renderMessages(els.interaction,state.messages.interaction,onChoose, { animate: true, ...options });
}
async function refreshCharacter(){
 const view=await request(`/worlds/${state.worldId}/view`,undefined,undefined,'GET');
 renderEncounter(document.getElementById('encounter'), view.encounter);
 const c=view.character;
 const fate=document.getElementById('fate-card');fate.replaceChildren();
 document.querySelectorAll('.character-card .vital').forEach(el=>el.hidden=!!c.stress_tracks);
 for(const a of c.aspects||[]){const p=document.createElement('p');p.textContent=(a.kind==='high_concept'?'高概念：':'自由形象：')+a.text;fate.append(p);}
 if (c.stress_tracks?.unified) {
   const pressure = document.createElement('p');
   pressure.textContent = '压力：' + c.stress_tracks.unified.map((used, index) => `${used ? '☒' : '☐'} ${index + 1}`).join('　');
   fate.append(pressure);
   const points = document.createElement('p');
   points.textContent = `命运点：${c.fate_points ?? 0}`;
   fate.append(points);
 }
 for(const [slot,value] of Object.entries(c.consequences||{})){const p=document.createElement('p');p.textContent=({mild:'轻微',moderate:'中度',severe:'严重'}[slot])+`伤痕（${value.capacity}）：`+(value.text||'空');fate.append(p);}
 document.getElementById('character-name').textContent=c.name;
 document.getElementById('hp').textContent=`${c.resources.hp ?? 3} / 3`;
 document.getElementById('stress').textContent=`${c.resources.stress ?? 3} / 3`;
 const skills=document.getElementById('skills');skills.replaceChildren();
 for (const [name, value] of Object.entries(c.skills)) {
   if (value <= 0) continue;
   const item = document.createElement('li');
   item.textContent = `${name} +${value}`;
   skills.append(item);
 }
 const items=document.getElementById('items');items.replaceChildren();
 for (const item of c.inventory?.length ? c.inventory : [{ name: '暂无物品' }]) {
   const li = document.createElement('li');
   li.textContent = item.currency || item.quantity > 1 ? `${item.name} × ${item.quantity}` : item.name;
   items.append(li);
 }
}

function renderEncounter(root, encounter) {
  root.hidden = !encounter;
  root.replaceChildren();
  if (!encounter) return;
  const title = document.createElement('h2');
  title.textContent = encounter.title;
  const clock = document.createElement('p');
  clock.textContent = `${encounter.label} · 第 ${encounter.round} 轮 · 已过 ${encounter.elapsed_seconds} 秒`;
  const situation = document.createElement('p');
  situation.textContent = encounter.waiting_for === 'absorption' ? '等待处理受击' : encounter.goal;
  const units = document.createElement('ul');
  for (const unit of encounter.units.filter(unit => unit.id !== encounter.player_id)) {
    const item = document.createElement('li');
    const status = unit.status === 'out' ? '出局' : unit.status === 'withdrawn' ? '撤离'
      : encounter.kind === 'combat' ? (unit.distance === 'far' ? '远处' : '近处') : '在场';
    item.textContent = `${unit.name} · ${status}`;
    units.append(item);
  }
  root.append(title, clock, situation, units);
  if (encounter.progress) {
    const progress = document.createElement('p');
    progress.textContent = `你 ${encounter.progress.player}/${encounter.goal_progress} · 对手 ${encounter.progress.opponents}/${encounter.goal_progress}`;
    root.append(progress);
  }
  if (encounter.pending || encounter.prepared) {
    const prep = document.createElement('p');
    prep.textContent = encounter.pending
      ? `${encounter.pending.description}还需 ${encounter.pending.remaining} 轮`
      : `准备就绪 · ${encounter.prepared}`;
    root.append(prep);
  }
}

async function onChoose(text, replyTo, combatDescription) {
  await onSend({text, modeHint: '行动', replyTo, combatDescription});
}
