import { renderTranscript, streamTranscript } from './transcript.js';

function narrativeRow(message) {
  const element = document.createElement('article');
  element.className = `msg ${message.author || 'gm'}`;
  const role = document.createElement('span');
  role.className = 'role';
  role.textContent = message.author === 'player' ? '你的行动' : '主持人 · 世界回应';
  const body = document.createElement('div');
  body.className = 'message-text';
  element.append(role, body);
  return { element, body };
}

export function renderHistory(root, messages, options) {
  renderTranscript(root, messages, narrativeRow, options);
}

export function streamNarrative(root, requestId, text) {
  streamTranscript(root, requestId, text, narrativeRow);
}
