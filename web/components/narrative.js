export function renderHistory(root, messages) {
  root.textContent = '';
  (messages || []).forEach((msg) => appendCommittedTurn(root, msg));
}

export function appendCommittedTurn(root, turnOrMessage) {
  root.querySelector('[data-streaming]')?.remove();
  const msg = turnOrMessage;
  if (!msg || !msg.text) return;
  const el = document.createElement('article');
  el.className = `msg ${msg.author || 'gm'}`;
  const role = document.createElement('span');
  role.className = 'role';
  role.textContent = msg.author === 'player' ? '你的行动' : '主持人 · 世界回应';
  const body = document.createElement('div');
  body.textContent = msg.text;
  el.appendChild(role);
  el.appendChild(body);
  root.appendChild(el);
  root.scrollTop = root.scrollHeight;
}
