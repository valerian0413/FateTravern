export function renderWorlds(root, list, selected, { onSelect, onDelete }) {
  root.innerHTML = '';
  const title = document.createElement('h2');
  title.textContent = '已有存档';
  root.appendChild(title);

  if (!list.length) {
    const empty = document.createElement('p');
    empty.className = 'hint';
    empty.style.gridColumn = '1 / -1';
    empty.textContent = '还没有存档。到「创建世界」写一份新的设定。';
    root.appendChild(empty);
    return;
  }

  list.forEach((world) => {
    const card = document.createElement('div');
    card.className = 'archive-card';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'world' + (world.id === selected ? ' selected' : '');
    btn.innerHTML = `<div>${escapeHtml(world.name || world.id)}</div><div class="phase">${escapeHtml(world.phase || '')}</div>`;
    btn.addEventListener('click', () => onSelect(world.id));
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'archive-delete';
    remove.textContent = '删除';
    remove.setAttribute('aria-label', `删除冒险存档「${world.name || world.id}」`);
    remove.addEventListener('click', async () => {
      btn.disabled = remove.disabled = true;
      try {
        await onDelete(world);
      } finally {
        btn.disabled = remove.disabled = false;
      }
    });
    card.append(btn, remove);
    root.appendChild(card);
  });
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;');
}
