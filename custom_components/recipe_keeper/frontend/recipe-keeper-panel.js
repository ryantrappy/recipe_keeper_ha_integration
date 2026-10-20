/* Self-contained HA panel. Recipe text is always inserted as text, never HTML. */
class RecipeKeeperPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.accounts = [];
    this.items = [];
    this.selected = new Set();
    this.recipe = null;
    this.pendingEdit = null;
    this.pendingImport = null;
  }
  set hass(value) {
    this._hass = value;
    if (this.isConnected && !this.started) this.start();
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    if (menu) menu.hass = value;
  }
  connectedCallback() { if (this._hass && !this.started) this.start(); }
  $(selector) { return this.shadowRoot.querySelector(selector); }
  node(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  async action(service, data = {}, account = true) {
    const result = await this._hass.callWS({ type: "call_service", domain: "recipe_keeper", service, service_data: account ? { config_entry_id: this.$("#account").value, ...data } : data, return_response: true });
    return result.response ?? result;
  }
  message(text, error = false) {
    this.$("#message").textContent = text;
    this.$("#message").classList.toggle("error", error);
  }
  async task(function_) {
    try { await function_(); }
    catch (error) { this.message(error.message || "The request could not finish. Refresh or check the saved operation before retrying.", true); }
  }
  today() {
    const parts = new Intl.DateTimeFormat("en", { timeZone: this._hass.config.time_zone, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date());
    const get = type => parts.find(part => part.type === type).value;
    return `${get("year")}-${get("month")}-${get("day")}`;
  }
  dates() { return { start_date: this.$("#start").value, end_date: this.$("#end").value }; }
  uuid() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16)); bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    const hex = [...bytes].map(value => value.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
  }
  savedEdits() { try { const value = JSON.parse(localStorage.getItem("recipe_keeper_pending_edits") || "[]"); return Array.isArray(value) ? value.filter(row => row && typeof row.operation_id === "string" && ["recipe", "meal"].includes(row.kind) && typeof row.account === "string").slice(-100) : []; } catch { return []; } }
  rememberEdit(operation_id, kind, account) {
    const rows = this.savedEdits();
    if (!rows.some(row => row.operation_id === operation_id)) rows.push({ operation_id, kind, account });
    localStorage.setItem("recipe_keeper_pending_edits", JSON.stringify(rows)); this.renderSavedEdits();
  }
  forgetEdit(operation_id) { localStorage.setItem("recipe_keeper_pending_edits", JSON.stringify(this.savedEdits().filter(row => row.operation_id !== operation_id))); this.renderSavedEdits(); }
  renderSavedEdits() {
    const container = this.$("#saved-edits"); if (!container) return; container.replaceChildren();
    for (const row of this.savedEdits().filter(row => row.account === this.$("#account").value)) {
      const line = this.node("div", undefined, "toolbar"); line.append(this.node("span", `Saved ${row.kind} edit awaiting confirmation`, "muted"));
      const check = this.node("button", "Check saved edit"); check.onclick = () => this.task(async () => {
        const result = await this.action("reconcile_edit", { config_entry_id: row.account, operation_id: row.operation_id });
        if (["confirmed", "conflict", "cancelled"].includes(result.state)) { this.forgetEdit(row.operation_id); this.message(result.state === "confirmed" ? "Saved edit confirmed." : result.state === "cancelled" ? "The saved edit was cancelled before upload." : "The saved edit conflicts with another change. Reopen the record to review it."); await this.loadWeek(); }
        else this.message("This edit is still unconfirmed. It has not been automatically retried.");
      }); line.append(check); container.append(line);
    }
  }
  canEdit() { return this._hass.user?.is_admin && this.accounts.find(account => account.config_entry_id === this.$("#account").value)?.allow_edits === true; }
  start() {
    this.started = true;
    this.shadowRoot.innerHTML = `
      <style>
        :host{display:block;height:100%;overflow:auto;background:var(--primary-background-color,#f6f8fb);color:var(--primary-text-color,#172832);font-family:var(--primary-font-family,sans-serif);font-size:16px}
        *{box-sizing:border-box} header{display:flex;align-items:center;gap:12px;padding:12px 20px;border-bottom:1px solid var(--divider-color,#dae2e5);background:var(--card-background-color,#fff)} h1{font-size:24px;font-weight:550;margin:0;flex:1}main{max-width:1280px;margin:auto;padding:24px}h2{font-size:21px;font-weight:550;margin:0 0 16px}h3{font-size:18px;margin:18px 0 10px}.controls,.row,.toolbar{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.controls{margin-bottom:20px}.controls label{display:grid;gap:5px;font-size:13px}.row>*:first-child{flex:1}.workspace{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(320px,.9fr);gap:32px}.shopping{border-left:1px solid var(--divider-color,#dae2e5);padding-left:28px}input,select,textarea,button{font:inherit}input:not([type=checkbox]),select,textarea{border:1px solid var(--divider-color,#bccbd0);border-radius:5px;background:var(--card-background-color,#fff);color:inherit;padding:9px;max-width:100%}textarea{width:100%;min-height:92px;resize:vertical}button{border:1px solid var(--divider-color,#bccbd0);border-radius:5px;padding:9px 13px;background:var(--card-background-color,#fff);color:inherit;cursor:pointer}button.primary{background:var(--primary-color,#1c626c);color:var(--text-primary-color,#fff);border-color:transparent}button:disabled{opacity:.45;cursor:default}button:focus-visible,input:focus-visible,textarea:focus-visible,select:focus-visible{outline:3px solid var(--primary-color,#1c626c);outline-offset:2px}.danger{color:var(--error-color,#b23b3b)}.muted{color:var(--secondary-text-color,#526575);font-size:14px}#message{min-height:24px;margin-bottom:16px;white-space:pre-wrap}.error{color:var(--error-color,#b23b3b)}.meal{padding:12px 0;border-bottom:1px solid var(--divider-color,#dae2e5)}.meal .date{font-size:13px;color:var(--secondary-text-color,#526575)}.link{border:0;padding:3px 0;background:none;text-align:left;color:var(--primary-color,#1c626c)}#results{display:grid;gap:5px;margin:12px 0}#recipe{margin-top:24px;border-top:2px solid var(--primary-color,#1c626c);padding-top:20px}.copy{white-space:pre-wrap;line-height:1.65;max-width:72ch}.ingredient{display:flex;align-items:flex-start;gap:12px;padding:11px 0;border-bottom:1px solid var(--divider-color,#dae2e5);cursor:pointer}.ingredient input{margin-top:5px;accent-color:var(--primary-color,#1c626c);width:19px;height:19px;flex:none}.ingredient .text{line-height:1.45}.ingredient .detail{display:block;margin-top:3px;color:var(--secondary-text-color,#526575);font-size:12px}.pantry{color:var(--secondary-text-color,#526575)}#issues{color:var(--warning-color,#9b6a12);font-size:14px;line-height:1.5}.toolbar{margin:16px 0}#partial{margin:12px 0;display:none}#partial input{margin-right:8px}dialog{background:var(--card-background-color,#fff);color:var(--primary-text-color,#172832);border:1px solid var(--divider-color,#dae2e5);border-radius:10px;width:min(680px,95vw);max-height:90vh;padding:24px}dialog::backdrop{background:rgba(0,0,0,.45)}dialog form,#edit-fields{display:grid;gap:14px}dialog label{display:grid;gap:5px;font-size:14px}dialog .buttons{display:flex;gap:10px;justify-content:flex-end;flex-wrap:wrap;margin-top:8px}.edit-message{white-space:pre-wrap;font-size:14px}#edit-status{display:none}#search{flex:1;min-width:150px}#todo{width:100%}@media(max-width:800px){main{padding:18px}.workspace{grid-template-columns:1fr;gap:24px}.shopping{padding:24px 0 0;border-left:0;border-top:1px solid var(--divider-color,#dae2e5)}header{padding:10px}h1{font-size:21px}.controls{gap:9px}.controls label{flex:1;min-width:140px}}@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto}}
      </style>
      <header><ha-menu-button></ha-menu-button><h1>Recipe Keeper</h1><select id="account" aria-label="Recipe Keeper account"></select></header>
      <main><div id="message" role="status" aria-live="polite"></div><div id="saved-edits"></div>
        <div class="controls"><label>From<input id="start" type="date"></label><label>Through<input id="end" type="date"></label><button id="refresh">Refresh meal plan</button><span id="freshness" class="muted"></span></div>
        <div class="workspace"><section aria-label="Meals and recipes"><div class="row"><h2>Meal plan</h2><button id="new-meal">Plan meal</button></div><div id="meals"></div>
          <h2 style="margin-top:28px">Your recipes</h2><form id="search-form" class="row"><input id="search" type="search" placeholder="Search recipes or ingredients" aria-label="Search recipes"><button type="submit">Search</button><button id="new-recipe" type="button">New recipe</button></form><div id="results"></div><div id="recipe" hidden></div>
        </section><section class="shopping" aria-label="Shopping ingredients"><h2>Choose what to buy</h2><p class="muted">Pantry staples stay unchecked. Include or leave out any ingredient for this shop.</p><div id="issues"></div><label id="partial"><input type="checkbox" id="acknowledge">I reviewed the skipped meals; import the available ingredients.</label><div class="toolbar"><button id="select-all">Select all</button><button id="clear">Clear</button><span id="selection-count" class="muted"></span></div><div id="ingredients"></div><div class="toolbar"><label style="width:100%">Shopping list<select id="todo"></select></label><button class="primary" id="import">Add selected ingredients</button></div><p id="import-result" class="muted" role="status"></p></section></div>
      </main>
      <dialog id="edit"><form id="edit-form"><h2 id="edit-title"></h2><div id="edit-fields"></div><div id="edit-message" class="edit-message" role="status"></div><div class="buttons"><button type="button" id="delete" class="danger">Delete</button><button type="button" id="cancel">Close</button><button type="button" id="edit-status">Check saved edit</button><button type="submit" class="primary" id="save">Save changes</button></div></form></dialog>`;
    this.$("ha-menu-button").hass = this._hass;
    const start = this.today();
    this.$("#start").value = start;
    const end = new Date(`${start}T12:00:00`); end.setDate(end.getDate() + 6);
    this.$("#end").value = `${end.getFullYear()}-${String(end.getMonth()+1).padStart(2,"0")}-${String(end.getDate()).padStart(2,"0")}`;
    this.$("#refresh").onclick = () => this.task(() => this.loadWeek(true));
    this.$("#account").onchange = () => { this.editorSequence = (this.editorSequence || 0) + 1; this.$("#edit").close(); this.recipe = null; this.$("#recipe").hidden = true; this.task(async () => { await this.loadWeek(); await this.search(); }); };
    this.$("#search-form").onsubmit = event => { event.preventDefault(); this.task(() => this.search()); };
    this.$("#select-all").onclick = () => { this.selected = new Set(this.items.map(item => item.key)); this.renderIngredients(); };
    this.$("#clear").onclick = () => { this.selected.clear(); this.renderIngredients(); };
    this.$("#import").onclick = () => this.task(() => this.importIngredients());
    this.$("#new-meal").onclick = () => this.task(() => this.edit("meal"));
    this.$("#new-recipe").onclick = () => this.task(() => this.edit("recipe"));
    this.$("#cancel").onclick = () => { this.editorSequence = (this.editorSequence || 0) + 1; this.$("#edit").close(); };
    this.$("#edit").oncancel = () => { this.editorSequence = (this.editorSequence || 0) + 1; };
    this.$("#edit-form").onsubmit = event => { event.preventDefault(); this.task(() => this.save()); };
    this.$("#delete").onclick = () => this.task(() => this.save(true));
    this.$("#edit-status").onclick = () => this.task(() => this.checkEdit());
    this.task(async () => {
      const result = await this.action("get_accounts", {}, false);
      this.accounts = result.accounts;
      for (const account of this.accounts) { const option = this.node("option", account.title); option.value = account.config_entry_id; this.$("#account").append(option); }
      if (!this.accounts.length) { this.message("Connect Recipe Keeper in Settings → Devices & services to get started."); return; }
      await this.loadWeek();
      await this.search();
    });
  }
  async loadWeek(force = false) {
    const accountId = this.$("#account").value;
    const dates = this.dates(); const sequence = this.weekSequence = (this.weekSequence || 0) + 1;
    this.$("#import").disabled = true;
    if (force) { this.accounts = (await this.action("get_accounts", {}, false)).accounts; await this.action("refresh", { config_entry_id: accountId }); }
    const [plan, preview] = await Promise.all([this.action("get_meal_plan", { ...dates, config_entry_id: accountId }), this.action("preview_ingredients", { ...dates, config_entry_id: accountId })]);
    if (accountId !== this.$("#account").value || sequence !== this.weekSequence) return;
    this.$("#new-meal").disabled = !this.canEdit(); this.$("#new-recipe").disabled = !this.canEdit();
    this.$("#freshness").textContent = preview.stale ? "Cached data. Reconnect before importing." : "Synced with Recipe Keeper";
    this.$("#meals").replaceChildren();
    for (const meal of plan.items) {
      const row = this.node("div", undefined, "meal");
      row.append(this.node("div", `${meal.date} · ${meal.meal_type}`, "date"));
      const content = this.node("div", undefined, "row"); const title = this.node("button", meal.title, "link");
      title.onclick = () => meal.recipe ? this.task(() => this.showRecipe(meal.recipe.id)) : this.message("This meal has no unique recipe match. Review its title in Recipe Keeper."); content.append(title);
      if (this.canEdit()) { const edit = this.node("button", "Edit meal"); edit.onclick = () => this.task(() => this.edit("meal", meal.id)); content.append(edit); }
      row.append(content); this.$("#meals").append(row);
    }
    if (!plan.items.length) this.$("#meals").append(this.node("p", "No meals planned in this date range.", "muted"));
    this.previewContext = JSON.stringify([accountId, dates]);
    this.items = preview.items; this.selected = new Set(preview.selected_items.map(item => item.key));
    this.$("#issues").replaceChildren();
    for (const issue of preview.issues) { const meal = plan.items.find(item => item.id === issue.meal_id); this.$("#issues").append(this.node("p", `${meal?.title || "Meal"}: ${issue.reason}`)); }
    this.$("#partial").style.display = preview.issues.length ? "block" : "none"; this.$("#acknowledge").checked = false;
    this.renderIngredients();
    this.$("#todo").replaceChildren();
    for (const [id, state] of Object.entries(this._hass.states).filter(([id]) => id.startsWith("todo."))) { const option = this.node("option", state.attributes.friendly_name || id); option.value = id; this.$("#todo").append(option); }
    const account = this.accounts.find(item => item.config_entry_id === this.$("#account").value);
    if (account?.todo_entity && this._hass.states[account.todo_entity]) this.$("#todo").value = account.todo_entity;
    this.$("#import").disabled = preview.stale || !this._hass.user?.is_admin;
    this.pendingImport = null;
    this.renderSavedEdits();
    if (force && this.recipe) await this.showRecipe(this.recipe.id);
    this.message("");
  }
  renderIngredients() {
    const list = this.$("#ingredients"); list.replaceChildren();
    for (const item of this.items) {
      const label = this.node("label", undefined, `ingredient${item.pantry_match ? " pantry" : ""}`);
      const checkbox = this.node("input"); checkbox.type = "checkbox"; checkbox.checked = this.selected.has(item.key);
      checkbox.onchange = () => { checkbox.checked ? this.selected.add(item.key) : this.selected.delete(item.key); this.selectionCount(); };
      const copy = this.node("span", item.text, "text"); copy.append(this.node("span", `${item.recipe_title}${item.pantry_match ? ` · Pantry: ${item.pantry_match}` : ""}`, "detail")); label.append(checkbox, copy); list.append(label);
    }
    if (!this.items.length) list.append(this.node("p", "Plan a meal with a matched recipe to preview its ingredients.", "muted"));
    this.selectionCount();
  }
  selectionCount() { this.$("#selection-count").textContent = `${this.selected.size} of ${this.items.length} selected`; }
  async search() {
    const account = this.$("#account").value; const sequence = this.searchSequence = (this.searchSequence || 0) + 1;
    const result = await this.action("search_recipes", { config_entry_id: account, query: this.$("#search").value, limit: 100 });
    if (account !== this.$("#account").value || sequence !== this.searchSequence) return;
    this.$("#results").replaceChildren();
    for (const recipe of result.items) { const button = this.node("button", recipe.title, "link"); button.onclick = () => this.task(() => this.showRecipe(recipe.id)); this.$("#results").append(button); }
    if (!result.items.length) this.$("#results").append(this.node("p", "No matching recipes.", "muted"));
  }
  async showRecipe(id) {
    const account = this.$("#account").value; const sequence = this.recipeSequence = (this.recipeSequence || 0) + 1;
    const { recipe } = await this.action("get_recipe", { config_entry_id: account, recipe_id: id });
    if (account !== this.$("#account").value || sequence !== this.recipeSequence) return;
    this.recipe = recipe;
    const container = this.$("#recipe"); container.hidden = false; container.replaceChildren(this.node("h2", recipe.title));
    if (recipe.serving_size) container.append(this.node("p", recipe.serving_size, "muted"));
    for (const [label, value] of [["Ingredients", recipe.ingredients], ["Directions", recipe.directions], ["Notes", recipe.notes], ["Source", recipe.source]]) {
      if (value) container.append(this.node("h3", label), this.node("div", value, "copy"));
    }
    if (this.canEdit()) {
      const controls = this.node("div", undefined, "toolbar"); const plan = this.node("button", "Plan this recipe"); plan.onclick = () => this.task(() => this.edit("meal", null, { title: recipe.title }));
      const edit = this.node("button", "Edit recipe"); edit.onclick = () => this.task(() => this.edit("recipe", recipe.id)); controls.append(plan, edit); container.append(controls);
    }
  }
  async importIngredients() {
    const context = JSON.stringify([this.$("#account").value, this.dates()]);
    if (context !== this.previewContext) { this.message("Refresh the meal plan after changing the account or date range.", true); return; }
    if (!this.selected.size) { this.message("Select at least one ingredient."); return; }
    if (!this.$("#todo").value) { this.message("Choose a shopping list first."); return; }
    const body = { config_entry_id: this.$("#account").value, ...this.dates(), selected_keys: [...this.selected], todo_entity: this.$("#todo").value, acknowledge_issues: this.$("#acknowledge").checked };
    const fingerprint = JSON.stringify(body);
    if (!this.pendingImport || this.pendingImport.fingerprint !== fingerprint) this.pendingImport = { fingerprint, import_id: this.uuid() };
    this.$("#import").disabled = true;
    try {
      const result = await this.action("import_ingredients", { ...body, import_id: this.pendingImport.import_id });
      if (context !== JSON.stringify([this.$("#account").value, this.dates()])) return;
      this.$("#import-result").textContent = `${result.added_keys.length} added, ${result.skipped_keys.length} already imported. ${result.not_attempted_keys.length} not attempted.${result.uncertain_keys.length ? " Some additions could not be confirmed. Check the list before adding them manually." : ""}`;
      for (const key of [...result.added_keys, ...result.skipped_keys]) this.selected.delete(key);
      this.renderIngredients();
    } finally { this.$("#import").disabled = false; }
  }
  async edit(kind, id = null, defaults = {}) {
    if (!this.canEdit() || this.editInFlight) return;
    const account = this.$("#account").value; const sequence = this.editorSequence = (this.editorSequence || 0) + 1;
    const view = id ? await this.action("get_editing_view", { entity_type: kind, entity_id: id }) : { fields: { title: "", date: this.$("#start").value, meal_type: "Dinner", scale: "1", ...defaults } };
    if (account !== this.$("#account").value || sequence !== this.editorSequence) return;
    this.editSession = { kind, id, view, account }; this.pendingEdit = null;
    this.$("#edit-title").textContent = `${id ? "Edit" : "New"} ${kind}`;
    this.$("#edit-message").textContent = ""; this.$("#edit-status").style.display = "none";
    this.$("#edit-fields").replaceChildren();
    const fields = kind === "recipe" ? [["title", "Recipe name"], ["serving_size", "Servings"], ["ingredients", "Ingredients", true], ["directions", "Directions", true], ["notes", "Notes", true], ["source", "Source"]] : [["title", "Meal or recipe name"], ["date", "Date"], ["meal_type", "Meal type"], ["scale", "Serving scale"], ["notes", "Notes", true]];
    for (const [name, text, multiline] of fields) {
      const label = this.node("label", text); const input = this.node(multiline ? "textarea" : "input"); input.dataset.field = name; input.value = view.fields[name] ?? ""; if (name === "date") input.type = "date"; input.required = ["title", "date", "meal_type", "scale"].includes(name); label.append(input); this.$("#edit-fields").append(label);
    }
    this.$("#delete").hidden = !id; this.$("#save").disabled = false; this.$("#delete").disabled = false;
    this.$("#edit").showModal();
  }
  async save(deleting = false) {
    if (this.editInFlight || this.$("#save").disabled) return;
    const context = this.editSession; const { kind, id, view, account } = context;
    if (deleting && !window.confirm(`Delete ${view.fields.title}?`)) return;
    const changes = {};
    for (const input of this.shadowRoot.querySelectorAll("[data-field]")) if (!id || input.value !== (view.fields[input.dataset.field] ?? "")) changes[input.dataset.field] = input.value;
    if (!deleting && id && !Object.keys(changes).length) { this.$("#edit").close(); return; }
    const action = `${deleting ? "delete" : id ? "update" : "create"}_${kind}`;
    const body = { ...(id ? { [`${kind}_id`]: id, revision: view.revision } : {}), ...(!deleting ? { changes } : {}) };
    const fingerprint = JSON.stringify([action, body]);
    if (!this.pendingEdit || this.pendingEdit.fingerprint !== fingerprint) this.pendingEdit = { fingerprint, operation_id: this.uuid(), action, body };
    const pending = this.pendingEdit;
    this.rememberEdit(pending.operation_id, kind, account); this.editInFlight = true;
    this.$("#save").disabled = true; this.$("#delete").disabled = true;
    try { this.editOutcome(await this.action(action, { ...body, config_entry_id: account, operation_id: pending.operation_id }), context, pending); }
    catch (error) {
      if (["service_validation_error", "invalid_format", "unauthorized"].includes(error.code)) {
        this.forgetEdit(pending.operation_id); this.pendingEdit = null;
        this.$("#save").disabled = false; this.$("#delete").disabled = false;
        this.$("#edit-message").textContent = error.message || "The edit was rejected before upload. Correct the fields and try again.";
      } else { this.$("#edit-message").textContent = `${error.message || "The edit could not be confirmed."} Check the saved edit before retrying.`; this.$("#edit-status").style.display = "inline-block"; }
    }
    finally { this.editInFlight = false; }
  }
  async checkEdit() { const context = this.editSession, pending = this.pendingEdit; if (pending) this.editOutcome(await this.action("reconcile_edit", { config_entry_id: context.account, operation_id: pending.operation_id }), context, pending); }
  editOutcome(result, context, pending) {
    if (["confirmed", "cancelled"].includes(result.state)) this.forgetEdit(result.operation_id);
    if (context !== this.editSession || context.account !== this.$("#account").value) return;
    if (result.state === "cancelled") { this.$("#edit").close(); this.message("Editing was disabled before upload. Nothing was changed by this operation."); return; }
    if (result.state === "confirmed") {
      this.forgetEdit(result.operation_id);
      this.$("#edit").close(); this.message("Saved to Recipe Keeper.");
      if (pending.action === "delete_recipe") { this.recipe = null; this.$("#recipe").hidden = true; }
      const recipeId = this.recipe?.id;
      this.task(async () => { await this.loadWeek(); await this.search(); if (context.account === this.$("#account").value && recipeId && context.kind === "recipe" && context.id === recipeId && pending.action !== "delete_recipe") await this.showRecipe(recipeId); });
    } else { this.$("#edit-message").textContent = result.state === "conflict" ? "The record changed. Close this form and open it again to review the latest version." : "The result is not confirmed yet. Check the saved edit before making another change."; this.$("#edit-status").style.display = "inline-block"; }
  }
}
if (!customElements.get("recipe-keeper-panel")) customElements.define("recipe-keeper-panel", RecipeKeeperPanel);
