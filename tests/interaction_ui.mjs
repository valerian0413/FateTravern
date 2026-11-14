// DOM contract smoke test; no browser or model required.
import assert from 'node:assert/strict';
import {renderMessages, setBusy} from '../web/components/interaction.js';
class Element {
  constructor() { this.children = []; this.dataset = {}; this.disabled = false; this.handlers = {}; }
  set textContent(value) { this.text = value; this.children = []; }
  get textContent() { return this.text; }
  appendChild(child) { this.children.push(child); }
  append(...children) { this.children.push(...children); }
  addEventListener(name, callback) { this.handlers[name] = callback; }
  setAttribute() {}
}
const root = new Element();
const send = new Element();
const allButtons = () => root.children.flatMap(m => m.children.flatMap(x => x.children || []));
globalThis.document = {
  createElement: () => new Element(),
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
