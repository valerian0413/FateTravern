const transcripts = new WeakMap();
const characterDelay = 22;
const segmenter = typeof Intl.Segmenter === 'function' ? new Intl.Segmenter('zh', { granularity: 'grapheme' }) : null;

function stateFor(root) {
  if (!transcripts.has(root)) transcripts.set(root, { rows: new Map(), order: [], live: null, timer: null });
  return transcripts.get(root);
}

function setText(row, text, animate) {
  if (row.text === text) return;
  const characters = segmenter ? Array.from(segmenter.segment(text), part => part.segment) : Array.from(text);
  let common = 0;
  while (common < row.shown && characters[common] === row.characters[common]) common++;
  row.text = text;
  row.characters = characters;
  row.shown = animate && !globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? common : characters.length;
  paint(row);
}

function paint(row) {
  row.body.textContent = row.characters.slice(0, row.shown).join('');
  if (row.shown < row.characters.length || row.live) row.element.dataset.writing = 'true';
  else delete row.element.dataset.writing;
}

function schedule(root, state) {
  const queued = () => state.order.find(row => row.shown < row.characters.length)
    || (state.live?.shown < state.live?.characters.length ? state.live : null);
  if (state.timer || !queued()) return;
  state.timer = setInterval(() => {
    const row = queued();
    if (!row) {
      clearInterval(state.timer);
      state.timer = null;
      return;
    }
    const nearEnd = root.scrollHeight - root.clientHeight - root.scrollTop < 48;
    row.shown++;
    paint(row);
    if (nearEnd) root.scrollTop = root.scrollHeight;
  }, characterDelay);
}

function createRow(makeRow, message) {
  return { ...makeRow(message), shown: 0, characters: [], text: undefined, live: false };
}

export function renderTranscript(root, messages, makeRow, { animate = false, finalId, finalRequest } = {}) {
  const state = stateFor(root);
  const rows = new Map();
  const live = state.live;
  const candidates = live ? messages.filter(message => message.author === 'gm'
    && message.request_id === live.requestId && !state.rows.has(message.id)) : [];
  const continuation = candidates.find(message => message.id === finalId)
    || candidates.find(message => message.text.startsWith(live.text) || live.text.startsWith(message.text))
    || (finalRequest !== undefined && finalRequest === live?.requestId ? candidates.at(-1) : null);

  state.order = messages.map((message, index) => {
    const key = message.id ?? index;
    let row = state.rows.get(key);
    if (message === continuation) {
      row = live;
      row.live = false;
      state.live = null;
      paint(row);
    }
    if (!row) row = createRow(makeRow, message);
    setText(row, message.text, message.author === 'gm' && (animate || message === continuation));
    row.update?.(message);
    rows.set(key, row);
    return row;
  });
  state.rows = rows;
  if (state.live && finalRequest !== undefined && finalRequest === state.live.requestId) {
    state.live = null;
  }
  root.replaceChildren(...state.order.map(row => row.element), ...(state.live ? [state.live.element] : []));
  root.scrollTop = root.scrollHeight;
  schedule(root, state);
}

export function streamTranscript(root, requestId, text, makeRow) {
  const state = stateFor(root);
  if (state.live?.requestId !== requestId || !state.live) {
    state.live?.element.remove();
    state.live = { ...createRow(makeRow, { author: 'gm', text }), requestId, live: true };
    root.appendChild(state.live.element);
    root.scrollTop = root.scrollHeight;
  }
  setText(state.live, text, true);
  schedule(root, state);
}

export function clearDraftText(root, requestId) {
  const state = transcripts.get(root);
  if (state?.live && (requestId === undefined || state.live.requestId === requestId)) {
    state.live.element.remove();
    state.live = null;
  }
}

export function resetTranscript(root) {
  const state = transcripts.get(root);
  if (state?.timer) clearInterval(state.timer);
  transcripts.delete(root);
  root.replaceChildren();
}
