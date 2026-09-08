/* Adapt the shipped Console's short pip-upgrade stream to a long WM/HAL job. */
(() => {
  const NativeSource = window.EventSource;
  if (!NativeSource) return;

  class UpdateSource extends EventTarget {
    constructor(url, options) {
      super();
      this.url = url.href;
      this.withCredentials = !!options?.withCredentials;
      this.readyState = 0;
      this.deadline = Date.now() + 45 * 60 * 1000;
      this.timer = null;
      this.closed = false;
      this.lines = [];
      this.connect();
    }
    emit(type, event) {
      this.dispatchEvent(event);
      if (typeof this['on' + type] === 'function') this['on' + type](event);
    }
    message(data) {
      this.emit('message', new MessageEvent('message', {data: JSON.stringify(data), origin: location.origin}));
    }
    fail(message) {
      if (this.closed) return;
      this.message({type: 'done', state: 'error', error: message});
      this.close();
    }
    connect() {
      if (this.closed) return;
      this.readyState = 0;
      const source = this.source = new NativeSource(this.url, {withCredentials: this.withCredentials});
      // Each reconnect replays a tail. Suppress its already-seen prefix;
      // preserve repeated lines within a live connection.
      let replay = null;
      source.onopen = () => {
        this.readyState = 1;
        this.emit('open', new Event('open'));
      };
      source.onmessage = event => {
        if (this.closed) return;
        let data;
        try { data = JSON.parse(event.data); } catch { return; }
        if (data.type === 'line') {
          const line = data.line || '';
          // Transport guidance is replayed on every connection, not part of
          // the persistent job log (and our reconnect handling replaces it).
          if (line.startsWith('Restarting service is part of a WM1303 upgrade;')) return;
          if (replay === null) replay = this.lines.indexOf(line);
          if (replay >= 0 && this.lines[replay] === line) { replay++; return; }
          replay = -1;
          this.lines.push(line);
          if (this.lines.length > 1000) this.lines.shift();
          // Console otherwise starts its fixed 60-second restart timeout
          // before HAL has finished building. Let terminal 'done' trigger it.
          data.line = line.replace(/Restarting service/g, 'Service restart');
        }
        this.message(data);
        if (data.type === 'done') this.close();
      };
      source.onerror = async () => {
        source.close();
        this.readyState = 0;
        if (this.closed) return;
        if (Date.now() >= this.deadline) {
          this.fail('WM1303 upgrade is still unavailable after 45 minutes. Check its systemd job and log; it has not been cancelled.');
          return;
        }
        // Distinguish an expired session from expected service downtime.
        const status = new URL('/api/update/status', this.url);
        const token = new URL(this.url).searchParams.get('token');
        const controller = this.probe = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 5000);
        try {
          const response = await fetch(status, {signal: controller.signal,
            headers: token ? {Authorization: 'Bearer ' + token} : {}});
          if (response.status === 401 || response.status === 403) {
            this.fail('Sign in again to view the WM1303 update. The background job is not cancelled.');
            return;
          }
        } catch { /* The daemon is normally unavailable during this job. */ }
        finally { clearTimeout(timeout); this.probe = null; }
        if (!this.closed) this.timer = setTimeout(() => this.connect(), 2000);
      };
    }
    close() {
      this.closed = true;
      this.readyState = 2;
      clearTimeout(this.timer);
      this.source?.close();
      this.probe?.abort();
    }
  }

  function Source(url, options) {
    const parsed = new URL(url, location.href);
    return parsed.origin === location.origin && parsed.pathname === '/api/update/progress'
      ? new UpdateSource(parsed, options) : new NativeSource(url, options);
  }
  Source.prototype = NativeSource.prototype;
  for (const key of ['CONNECTING', 'OPEN', 'CLOSED']) {
    Source[key] = UpdateSource[key] = UpdateSource.prototype[key] = NativeSource[key];
  }
  window.EventSource = Source;
})();
