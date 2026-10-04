/* Repeater credentials for MeshCore companions, shared by Console and Manager. */
(() => {
  'use strict';
  const panelId = 'wm1303-mesh-passwords';
  const tokenKey = 'pymc_jwt_token';
  let scheduled = false;

  async function request(path, body) {
    const token = localStorage.getItem(tokenKey);
    if (!token) throw new Error('Sign in through the Console to manage passwords.');
    const options = {method: body === undefined ? 'GET' : 'POST',
      headers: {Authorization: 'Bearer ' + token, 'Content-Type': 'application/json'}};
    if (body !== undefined) options.body = JSON.stringify(body);
    const response = await fetch(path, options);
    let result;
    try { result = await response.json(); }
    catch { throw new Error('Unable to read the server response (HTTP ' + response.status + ').'); }
    if (!response.ok || result.success === false || result.error) {
      throw new Error(result.error || result.message || 'Request failed (HTTP ' + response.status + ').');
    }
    return result;
  }

  function validatePassword(password, administrator) {
    if (administrator && password.length < 8) throw new Error('Administrator password must be at least 8 characters.');
    if (!administrator && !password) throw new Error('Enter a new guest password, or disable guest password login.');
    if (password.includes('\0') || new TextEncoder().encode(password).length > 15) {
      throw new Error('MeshCore passwords must fit 15 UTF-8 bytes without NUL.');
    }
  }

  function createPanel(manager) {
    const panel = document.createElement('section');
    panel.id = panelId;
    panel.className = manager ? 'gl p-5' : 'cfg-section space-y-4 mt-6';
    panel.innerHTML = `
      <h3 class="text-lg font-semibold text-content-primary">MeshCore Companion Passwords</h3>
      <p class="text-sm text-content-secondary dark:text-content-muted">
        Set the passwords companions use to log in to this repeater over MeshCore.
        The administrator password also signs in to the Console. Guest access uses a separate password.
      </p>
      <p class="text-xs text-content-muted" data-password-state role="status">Loading password settings…</p>
      <button class="cfg-btn-secondary" data-password-refresh type="button">Refresh Password Status</button>
      <div class="grid grid-cols-1 xl:grid-cols-2 gap-6" style="margin-top:16px">
        <form data-admin-form class="space-y-3">
          <h4 class="font-medium text-content-primary">Administrator Password</h4>
          <label class="cfg-label" for="mesh-admin-current">Current administrator password</label>
          <input id="mesh-admin-current" class="cfg-input w-full" type="password" autocomplete="current-password" required>
          <label class="cfg-label" for="mesh-admin-new">New administrator password</label>
          <input id="mesh-admin-new" class="cfg-input w-full" type="password" autocomplete="new-password" minlength="8" required>
          <label class="cfg-label" for="mesh-admin-confirm">Confirm administrator password</label>
          <input id="mesh-admin-confirm" class="cfg-input w-full" type="password" autocomplete="new-password" minlength="8" required>
          <p class="text-xs text-content-muted">At least 8 characters, up to 15 UTF-8 bytes. Changing this also changes your Console password.</p>
          <button class="cfg-btn-primary" type="submit">Change Administrator Password</button>
          <p data-admin-message class="text-sm" role="status" aria-live="polite"></p>
        </form>
        <form data-guest-form class="space-y-3">
          <h4 class="font-medium text-content-primary">Guest Password</h4>
          <label class="cfg-label" for="mesh-guest-current">Current administrator password</label>
          <input id="mesh-guest-current" class="cfg-input w-full" type="password" autocomplete="current-password" required>
          <label class="flex items-center gap-2 text-sm text-content-primary" for="mesh-guest-enabled">
            <input id="mesh-guest-enabled" type="checkbox" checked> Enable guest password login
          </label>
          <label class="cfg-label" for="mesh-guest-new">New guest password</label>
          <input id="mesh-guest-new" class="cfg-input w-full" type="password" autocomplete="new-password" required>
          <label class="cfg-label" for="mesh-guest-confirm">Confirm guest password</label>
          <input id="mesh-guest-confirm" class="cfg-input w-full" type="password" autocomplete="new-password" required>
          <p class="text-xs text-content-muted">Up to 15 UTF-8 bytes. Use a different password from the administrator password.</p>
          <button class="cfg-btn-primary" type="submit">Save Guest Password</button>
          <p data-guest-message class="text-sm" role="status" aria-live="polite"></p>
        </form>
      </div>`;
    if (manager) {
      panel.querySelectorAll('.cfg-input').forEach(input => { input.className = 'ci'; input.style.width = '100%'; });
      panel.querySelectorAll('.cfg-btn-primary').forEach(button => { button.className = 'btn pri'; });
      panel.querySelectorAll('.cfg-btn-secondary').forEach(button => { button.className = 'btn sec'; });
      panel.querySelectorAll('label').forEach(label => { label.style.display = 'block'; label.style.marginTop = '10px'; });
      panel.querySelectorAll('form').forEach(form => { form.style.minWidth = '240px'; form.style.flex = '1'; });
      const columns = panel.querySelector('.grid');
      columns.style.display = 'flex'; columns.style.flexWrap = 'wrap'; columns.style.gap = '24px';
    }
    const get = selector => panel.querySelector(selector);
    const state = get('[data-password-state]');
    const enabled = get('#mesh-guest-enabled');
    let loading = false, loaded = false, busy = false;
    const updateEnabled = () => {
      for (const id of ['#mesh-guest-new', '#mesh-guest-confirm']) {
        get(id).disabled = !enabled.checked;
        get(id).required = enabled.checked;
        if (!enabled.checked) get(id).value = '';
      }
    };
    const updateControls = () => {
      panel.querySelectorAll('input, button').forEach(control => { control.disabled = busy; });
      get('[data-guest-form]').querySelectorAll('input, button').forEach(control => {
        control.disabled = busy || loading || !loaded;
      });
      get('[data-password-refresh]').disabled = busy || loading;
      if (!busy && !loading && loaded) updateEnabled();
    };
    enabled.addEventListener('change', updateEnabled);
    const showMessage = (element, message, error = false) => {
      element.textContent = message;
      element.style.color = error ? 'var(--color-accent-red, #f87171)' : 'var(--color-accent-green, #4ade80)';
    };
    const loadState = async () => {
      if (busy || loading) return;
      loading = true; loaded = false;
      updateControls();
      try {
        const {data} = await request('/api/repeater_security');
        if (!panel.isConnected) return;
        state.textContent = 'Guest password login is ' + (data.has_guest_password ? 'enabled.' : 'disabled.');
        state.style.color = '';
        enabled.checked = data.has_guest_password;
        loaded = true;
      } catch (error) { showMessage(state, error.message, true); }
      finally { loading = false; updateControls(); }
    };
    get('[data-password-refresh]').addEventListener('click', loadState);
    get('[data-admin-form]').addEventListener('submit', async event => {
      event.preventDefault();
      const form = event.currentTarget, button = form.querySelector('button');
      if (button.disabled || busy) return;
      const message = get('[data-admin-message]');
      message.textContent = '';
      const current = get('#mesh-admin-current').value, password = get('#mesh-admin-new').value;
      try {
        validatePassword(password, true);
        if (password !== get('#mesh-admin-confirm').value) throw new Error('Administrator passwords do not match.');
        if (password === current) throw new Error('New administrator password must be different from the current password.');
        busy = true; updateControls();
        const result = await request('/auth/change_password', {current_password: current, new_password: password});
        form.reset();
        showMessage(message, result.message || 'Administrator password saved. Sign in with your new password.');
        // Follow the Console's password-change flow and require a fresh login.
        localStorage.removeItem(tokenKey);
        setTimeout(() => { location.assign('/login'); }, 1500);
      } catch (error) { showMessage(message, error.message, true); busy = false; updateControls(); }
    });
    get('[data-guest-form]').addEventListener('submit', async event => {
      event.preventDefault();
      const form = event.currentTarget, button = form.querySelector('button');
      if (button.disabled || busy || loading || !loaded) return;
      const message = get('[data-guest-message]');
      message.textContent = '';
      try {
        const password = enabled.checked ? get('#mesh-guest-new').value : '';
        if (enabled.checked) {
          validatePassword(password, false);
          if (password !== get('#mesh-guest-confirm').value) throw new Error('Guest passwords do not match.');
        }
        busy = true; updateControls();
        const result = await request('/api/repeater_security', {
          current_password: get('#mesh-guest-current').value, guest_password: password,
        });
        get('#mesh-guest-current').value = get('#mesh-guest-new').value = get('#mesh-guest-confirm').value = '';
        showMessage(message, result.message || 'Guest password saved.');
        state.textContent = 'Guest password login is ' + (password ? 'enabled.' : 'disabled.');
      } catch (error) { showMessage(message, error.message, true); }
      finally { busy = false; updateControls(); }
    });
    panel.loadState = loadState;
    return panel;
  }

  function mount() {
    const manager = document.getElementById('wm1303-access-host');
    const route = location.pathname.replace(/\/$/, '');
    const selected = new URLSearchParams(location.search).get('tab');
    const accessRoute = route === '/system/configuration/access/policies' ||
      (route === '/configuration' && selected === 'policy-engine');
    const existing = document.getElementById(panelId);
    if (!manager && !accessRoute) { existing?.remove(); return; }
    if (existing) return;
    // Wait for Vue to finish loading the actual Policies component; attaching
    // as its sibling avoids interfering with its internal DOM reconciliation.
    const heading = [...document.querySelectorAll('.cfg-page-heading h3, .cfg-page-heading h2')]
      .find(element => element.textContent.trim() === 'Policy Engine');
    const policy = heading?.closest('.cfg-page-heading')?.parentElement;
    if (!manager && !policy) return;
    const panel = createPanel(!!manager);
    if (manager) manager.appendChild(panel);
    else policy.insertAdjacentElement('afterend', panel);
    panel.loadState();
  }
  function schedule() {
    if (scheduled) return;
    scheduled = true;
    queueMicrotask(() => { scheduled = false; mount(); });
  }
  function start() {
    new MutationObserver(schedule).observe(document.body, {childList: true, subtree: true});
    addEventListener('popstate', schedule);
    mount();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once: true});
  else start();
})();
