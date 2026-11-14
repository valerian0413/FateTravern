const PENDING_KEY = 'ai_trpg_pending_requests';

export function reduce(state, event) {
  const next = {
    ...state,
    messages: { ...state.messages },
    seenMessageIds: new Set(state.seenMessageIds || []),
  };
  if (typeof event.id === 'number') {
    next.lastSequence = Math.max(next.lastSequence || 0, event.id);
  }
  if (['interaction.completed','turn.committed','request.failed'].includes(event.type)) next.stage = null;
  if (event.type === 'proposal.ready') next.stage = 'awaiting_confirmation';
  if (event.type === 'progress') {
    next.stage = event.payload?.stage || next.stage;
  }
  return next;
}

export function getPendingRequest(worldId) {
  try {
    const all = JSON.parse(localStorage.getItem(PENDING_KEY) || '{}');
    return all[worldId] || null;
  } catch (_) {
    return null;
  }
}

export function savePendingRequest(record) {
  const all = JSON.parse(localStorage.getItem(PENDING_KEY) || '{}');
  if (!record) return;
  if (record.clear) {
    delete all[record.worldId];
  } else {
    all[record.worldId] = { requestId: record.requestId };
  }
  localStorage.setItem(PENDING_KEY, JSON.stringify(all));
}

export function rememberMessage(state, message) {
  if (!message?.id) return state;
  if (state.seenMessageIds.has(message.id)) {
    const channel = message.channel || 'interaction';
    return {...state, messages: {...state.messages, [channel]: state.messages[channel].map(m => m.id === message.id ? {...m, ...message} : m)}};
  }
  const channel = message.channel || 'interaction';
  const list = [...(state.messages[channel] || []), message];
  return {
    ...state,
    messages: { ...state.messages, [channel]: list },
    seenMessageIds: new Set([...state.seenMessageIds, message.id]),
  };
}
