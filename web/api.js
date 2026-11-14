const API = '/api/v1';

export async function request(path, body, requestId, method = 'POST') {
  const headers = { 'Content-Type': 'application/json' };
  const options = { method, headers };
  if (body !== undefined) options.body = JSON.stringify(body);
  const res = await fetch(API + path, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.message || '请求失败');
    err.code = data.code;
    err.retryable = data.retryable;
    err.requestId = requestId || data.request_id;
    err.status = res.status;
    throw err;
  }
  return data;
}

export function sendInteraction(worldId, payload) {
  return request(`/worlds/${worldId}/messages`, payload, payload.request_id);
}

export function confirmProposal(worldId, id, revision, baseVersion, requestId) {
  return request(`/worlds/${worldId}/proposals/${id}/confirm`, {
    request_id: requestId,
    revision,
    base_version: baseVersion,
  }, requestId);
}

export function rejectProposal(worldId, id, revision, requestId) {
  return request(`/worlds/${worldId}/proposals/${id}/reject`, {
    request_id: requestId,
    revision,
  }, requestId);
}

export function subscribe(worldId, lastSequence, onEvent) {
  const url = `${API}/worlds/${worldId}/events?after=${lastSequence || 0}`;
  const source = new EventSource(url);
  let chain = Promise.resolve();
  const handler = (ev) => {
    if (!ev.data) return;
    try {
      const payload = JSON.parse(ev.data);
      chain = chain.then(()=>onEvent(payload)).catch(error=>console.error('事件处理失败',error));
    } catch (_) {}
  };
  ['check.announced', 'progress', 'interaction.completed', 'proposal.ready', 'turn.committed', 'request.failed', 'assistant.delta'].forEach((type) => {
    source.addEventListener(type, handler);
  });
  return () => source.close();
}
