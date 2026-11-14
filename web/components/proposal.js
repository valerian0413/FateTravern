export function renderProposal(root, proposal, { onConfirm, onReject, onRevise }) {
  if (!proposal) {
    root.hidden = true;
    root.textContent = '';
    return;
  }
  root.hidden = false;
  root.textContent = '';
  const title = document.createElement('strong');
  title.textContent = '共创提案';
  root.appendChild(title);

  const original = document.createElement('div');
  original.textContent = '原文：' + (proposal.original_text || '');
  root.appendChild(original);

  const proposed = document.createElement('div');
  proposed.textContent = '建议：' + (proposal.proposed_text || '');
  root.appendChild(proposed);

  if (proposal.issues?.length) {
    const issues = document.createElement('div');
    issues.className = 'issues';
    issues.textContent = proposal.issues.map((i) => i.reason || i).join('；');
    root.appendChild(issues);
  }

  const row = document.createElement('div');
  row.className = 'row';
  const confirm = document.createElement('button');
  confirm.textContent = '确认';
  confirm.addEventListener('click', () => onConfirm(proposal.proposal_id || proposal.id, proposal.revision));
  const reject = document.createElement('button');
  reject.textContent = '拒绝';
  reject.addEventListener('click', () => onReject(proposal.proposal_id || proposal.id));
  const revise = document.createElement('button');
  revise.textContent = '改写后再提';
  revise.addEventListener('click', () => onRevise(proposal.proposed_text || ''));
  row.append(confirm, reject, revise);
  root.appendChild(row);
}
