/* Behavior checks for async editor guards; browser smoke testing covers rendering. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

class Element {
  constructor() { this.children = []; this.dataset = {}; this.value = ''; this.style = {}; this.classList = { toggle() {} }; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  showModal() { this.open = true; }
  close() { this.open = false; }
}
function fixture() {
  let Panel;
  const storage = new Map();
  const context = vm.createContext({
    HTMLElement: class {
      set editContext(_) { throw new Error('Native HTMLElement.editContext requires EditContext'); }
      attachShadow() { this.shadowRoot = {}; }
    },
    document: { createElement: () => new Element() },
    customElements: { get: () => null, define: (_, value) => { Panel = value; } },
    localStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) },
    crypto: { randomUUID: () => '00000000-0000-4000-8000-000000000001' },
    window: { confirm: () => true },
    Intl, Date, console,
  });
  vm.runInContext(fs.readFileSync('custom_components/recipe_keeper/frontend/recipe-keeper-panel.js', 'utf8'), context);
  const panel = new Panel();
  const controls = new Map();
  panel.$ = selector => { if (!controls.has(selector)) controls.set(selector, new Element()); return controls.get(selector); };
  panel.$('#account').value = 'account';
  panel.$('#start').value = '2026-10-20';
  panel.accounts = [{ config_entry_id: 'account', allow_edits: true }];
  panel._hass = { user: { is_admin: true } };
  panel.renderSavedEdits = () => {};
  panel.shadowRoot.querySelectorAll = () => panel.$('#edit-fields').children.map(label => label.children[0]);
  return { panel, storage };
}
function deferred() { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; }

 test('new recipe editor avoids native editContext property', async () => {
  const { panel } = fixture();
  await panel.edit('recipe');
  assert.equal(panel.$('#edit').open, true);
  assert.equal(panel.$('#edit-fields').children.length, 6);
});

test('slower editor response cannot replace a newer form or typed text', async () => {
  const { panel } = fixture();
  const first = deferred(), second = deferred();
  panel.action = (_, data) => data.entity_id === 'a' ? first.promise : second.promise;
  const old = panel.edit('recipe', 'a');
  const current = panel.edit('recipe', 'b');
  second.resolve({ fields: { title: 'Recipe B' }, revision: 'b'.repeat(64) });
  await current;
  panel.$('#edit-fields').children[0].children[0].value = 'Unsaved title';
  first.resolve({ fields: { title: 'Recipe A' }, revision: 'a'.repeat(64) });
  await old;
  assert.equal(panel.editSession.id, 'b');
  assert.equal(panel.$('#edit-fields').children[0].children[0].value, 'Unsaved title');
});

test('definitive validation rejection clears UUID and permits correction', async () => {
  const { panel, storage } = fixture();
  await panel.edit('recipe');
  panel.$('#edit-fields').children[0].children[0].value = '   ';
  panel.action = async () => { throw { code: 'service_validation_error', message: 'Invalid title' }; };
  await panel.save();
  assert.equal(panel.$('#save').disabled, false);
  assert.equal(panel.$('#delete').disabled, false);
  assert.equal(panel.pendingEdit, null);
  assert.equal(storage.get('recipe_keeper_pending_edits'), '[]');
});

test('unknown upload response retains UUID and disables further mutations', async () => {
  const { panel, storage } = fixture();
  await panel.edit('recipe');
  panel.$('#edit-fields').children[0].children[0].value = 'Soup';
  panel.action = async () => { throw { code: 'home_assistant_error', message: 'Connection lost' }; };
  await panel.save();
  assert.equal(panel.$('#save').disabled, true);
  assert.equal(panel.$('#delete').disabled, true);
  assert.ok(panel.pendingEdit.operation_id);
  assert.equal(JSON.parse(storage.get('recipe_keeper_pending_edits')).length, 1);
});
