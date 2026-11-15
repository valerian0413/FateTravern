// DOM contract smoke test; no browser or model required.
import assert from 'node:assert/strict';
import {renderMessages, setBusy, streamInteraction} from '../web/components/interaction.js';
import {resetTranscript} from '../web/components/transcript.js';
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.disabled = false; this.handlers = {}; }
  set textContent(value) { this.text = value; this.children = []; }
  get textContent() { return this.text; }
  appendChild(child) { child.parent = this; this.children.push(child); }
  append(...children) { children.forEach(child => this.appendChild(child)); }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  remove() { this.parent.children = this.parent.children.filter(child => child !== this); }
  descendants() { return this.children.flatMap(child => [child, ...child.descendants()]); }
  querySelector(selector) { return this.descendants().find(child => child.className === selector.slice(1)); }
  focus() {}
  addEventListener(name, callback) { this.handlers[name] = callback; }
  setAttribute() {}
}
const root = new Element();
const send = new Element();
const allButtons = () => root.descendants().filter(node => node.dataset.actionChoice);
globalThis.document = {
  createElement: tag => new Element(tag),
  getElementById: id => id === 'send' ? send : null,
  querySelectorAll: () => allButtons(),
};
let picked;
renderMessages(root, [{author:'gm', text:'铜铃挂在门边', options:['观察铜铃','继续潜入'],
                       choices_active:true, choice_request_id:'pause-1'}],
               (text, replyTo) => { picked = {text, replyTo}; });
const buttons = allButtons();
assert.equal(buttons.length, 2);
buttons[0].handlers.click();
assert.deepEqual(picked, {text:'观察铜铃', replyTo:'pause-1'});
setBusy('preparing', {sendBtn:send});
assert.ok(buttons.every(button => button.disabled));
setBusy('completed', {sendBtn:send});
assert.ok(buttons.every(button => !button.disabled));
renderMessages(root, [{author:'gm', text:'旧情境', options:['观察铜铃','继续潜入'], choices_active:false}]);
setBusy('completed', {sendBtn:send});
assert.ok(allButtons().every(button => button.disabled));
console.log('PASS choice rendering, request linkage, busy and stale controls');

renderMessages(root, [{ id: 'hit', author: 'gm', text: '你受伤了', options: ['承受轻微伤痕'],
  choices_active: true, combat_choice_kind: 'encounter_hit', choice_request_id: 'hit' }],
  (text, replyTo, description) => { picked = { text, replyTo, description }; });
allButtons()[0].handlers.click();
const form = root.querySelector('.choice-description');
assert.equal(form.tag, 'form');
form.descendants().find(node => node.tag === 'textarea').value = '手臂擦伤';
form.handlers.submit({ preventDefault() {} });
assert.deepEqual(picked, { text: '承受轻微伤痕', replyTo: 'hit', description: '手臂擦伤' });

resetTranscript(root);
const realSetInterval = globalThis.setInterval;
const realClearInterval = globalThis.clearInterval;
let tick;
globalThis.setInterval = callback => { tick = callback; return 1; };
globalThis.clearInterval = () => { tick = null; };
try {
  streamInteraction(root, 'request', '好，你', () => {});
  const body = root.querySelector('.message-text');
  assert.equal(body.textContent, '');
  tick();
  assert.equal(body.textContent, '好');
  const final = { id: 'final', request_id: 'request', author: 'gm', text: '好，你试着开门。' };
  renderMessages(root, [final], () => {}, { animate: true, finalId: final.id });
  assert.equal(root.children.length, 1);
  assert.equal(root.querySelector('.message-text'), body);
  assert.equal(body.textContent, '好');
  tick();
  assert.equal(body.textContent, '好，');
  while (tick) tick();
  assert.equal(body.textContent, final.text);
  assert.equal(root.children[0].dataset.writing, undefined);
} finally {
  globalThis.setInterval = realSetInterval;
  globalThis.clearInterval = realClearInterval;
}
console.log('PASS inline combat descriptions and incremental text through final message');
