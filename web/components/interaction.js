import { renderTranscript, streamTranscript } from './transcript.js';

function describeChoice(element, choices, button, question, onSubmit) {
  const form = document.createElement('form');
  form.className = 'choice-description';
  const label = document.createElement('label');
  label.textContent = question;
  const input = document.createElement('textarea');
  input.rows = 2;
  input.maxLength = 300;
  input.required = true;
  label.append(input);
  const submit = document.createElement('button');
  submit.type = 'submit';
  submit.textContent = '确认';
  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.textContent = '取消';
  cancel.addEventListener('click', () => {
    form.remove();
    choices.hidden = false;
    button.focus();
  });
  form.addEventListener('submit', event => {
    event.preventDefault();
    const description = input.value.trim();
    if (!description || document.getElementById('send')?.disabled) return;
    form.remove();
    choices.hidden = false;
    onSubmit(description);
  });
  if (element.querySelector('.choice-description')) return;
  choices.hidden = true;
  form.append(label, submit, cancel);
  element.append(form);
  input.focus();
}

function interactionRow(message, onChoose) {
  const element = document.createElement('article');
  element.className = `msg ${message.author || 'gm'}`;
  const role = document.createElement('span');
  role.className = 'role';
  role.textContent = message.author === 'player' ? '你 · 冒险者' : '主持人';
  const body = document.createElement('div');
  body.className = 'message-text';
  element.append(role, body);
  let previousChoices = '';
  let controls;
  return { element, body, update(msg) {
    const signature = JSON.stringify([msg.options, msg.choices_active, msg.combat_choice_kind, msg.choice_request_id]);
    if (signature === previousChoices) return;
    previousChoices = signature;
    controls?.remove();
    if (!msg.options?.length) return;
    controls = document.createElement('section');
    controls.className = 'choice-controls';
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
        let question;
        if (msg.combat_choice_kind === 'before_roll' && index === 1) {
          question = '你打算怎样退出这场冲突？';
        } else if (['after_hit', 'encounter_hit'].includes(msg.combat_choice_kind) && text.includes('伤痕')) {
          question = '这次攻击留下了怎样的伤痕？角色承受伤痕后仍能行动。';
        }
        const choose = description => onChoose?.(text, msg.choice_request_id, description);
        if (question) describeChoice(controls, choices, button, question, choose);
        else choose();
      });
      choices.appendChild(button);
    });
    const hint = document.createElement('small');
    hint.textContent = !msg.choices_active ? '此前的建议'
      : msg.combat_choice_kind === 'encounter_hit' ? '选择一种方式处理当前受击。'
        : '点击即可行动，也可以在下方自由输入。';
    controls.append(choices, hint);
    element.append(controls);
  } };
}

export function renderMessages(root, messages, onChoose, options) {
  renderTranscript(root, messages, message => interactionRow(message, onChoose), options);
}

export function streamInteraction(root, requestId, text, onChoose) {
  streamTranscript(root, requestId, text, message => interactionRow(message, onChoose));
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
