export function renderMessages(root, messages, onChoose) {
  root.textContent = '';
  (messages || []).forEach((msg) => {
    const el = document.createElement('article');
    el.className = `msg ${msg.author || 'gm'}`;
    const role = document.createElement('span');
    role.className = 'role';
    role.textContent = msg.author === 'player' ? '你 · 冒险者' : '主持人';
    const body = document.createElement('div');
    body.textContent = msg.text;
    el.appendChild(role);
    el.appendChild(body);
    if (msg.options?.length) {
      const choices = document.createElement('div');
      choices.className = 'action-choices';
      msg.options.forEach((text, index) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = `${index + 1}. ${text}`;
        button.dataset.actionChoice = 'true';
        button.dataset.active = String(Boolean(msg.choices_active));
        button.disabled = !msg.choices_active || Boolean(document.getElementById('send')?.disabled);
        button.addEventListener('click', () => {
          let description;
          if (msg.combat_choice_kind === 'before_roll' && index === 1) {
            description = window.prompt('你打算怎样退出这场冲突？');
            if (!description?.trim()) return;
          } else if (['after_hit', 'encounter_hit'].includes(msg.combat_choice_kind) && text.includes('伤痕')) {
            description = window.prompt('这次攻击给你留下了怎样的伤痕？');
            if (!description?.trim()) return;
          }
          if (description === undefined) onChoose?.(text, msg.choice_request_id);
          else onChoose?.(text, msg.choice_request_id, description.trim());
        });
        choices.appendChild(button);
      });
      const hint = document.createElement('small');
      hint.textContent = !msg.choices_active ? '此前的建议'
        : msg.combat_choice_kind === 'encounter_hit' ? '选择一种方式处理当前受击。'
          : '点击即可行动，也可以在下方自由输入。';
      el.append(choices, hint);
    }
    root.appendChild(el);
  });
  root.scrollTop = root.scrollHeight;
}

export function setBusy(stage, { sendBtn, actionBtn, statusEl }) {
  const busy = Boolean(stage) && !['completed', 'cancelled', 'awaiting_confirmation','failed_retryable'].includes(stage);
  if (sendBtn) { sendBtn.disabled = busy; sendBtn.textContent = busy ? '等待回复' : '发送'; }
  const waiting = document.getElementById('waiting');
  if (waiting) waiting.hidden = !busy;
  document.getElementById('composer')?.setAttribute('aria-busy', String(busy));
  if (actionBtn) actionBtn.disabled = busy;
  document.querySelectorAll('[data-action-choice]').forEach(button => {
    button.disabled = busy || button.dataset.active !== 'true';
  });
  if (statusEl) statusEl.textContent = busy ? '主持人正在描绘世界…' : stage === 'awaiting_confirmation' ? '等待你确认提案' : '轮到你了';
}

export function showStreaming(root, text, role) {
  let el = root.querySelector('[data-streaming]');
  if (!el) {
    el = document.createElement('article');
    el.className = 'msg gm streaming';
    el.dataset.streaming = 'true';
    const label = document.createElement('span');
    label.className = 'role';
    label.textContent = role;
    const body = document.createElement('div');
    el.append(label, body);
    root.append(el);
  }
  el.querySelector('div').textContent = text;
  root.scrollTop = root.scrollHeight;
}

export function clearStreaming(root) {
  root?.querySelectorAll('[data-streaming]').forEach((el) => el.remove());
}

export function showError(root, error, retry) {
  const el = document.createElement('article');
  el.className = 'msg error';
  el.textContent = error?.message || String(error);
  if (retry) {
    const btn = document.createElement('button');
    btn.textContent = '重试';
    btn.addEventListener('click', retry);
    el.appendChild(document.createElement('br'));
    el.appendChild(btn);
  }
  root.appendChild(el);
}
