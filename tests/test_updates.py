"""WM updater checks: no package installs, service calls or network access."""
import importlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import shutil
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'overlay/pymc_repeater'))
from repeater.web import update_endpoints as updates


class UpdateTests(unittest.TestCase):
    def test_import_and_command_boundary_have_no_generic_pip_fallback(self):
        with patch('subprocess.run') as run, patch('shutil.rmtree') as remove, \
                patch.object(Path, 'write_text') as write:
            importlib.reload(updates)
            run.assert_not_called()
            remove.assert_not_called()
            write.assert_not_called()
        with patch.object(updates.os.path, 'isfile', return_value=True), \
                patch.object(updates.os, 'geteuid', return_value=1000), \
                patch.object(updates.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'state', '')) as run:
            self.assertEqual(updates._run_helper('status'), 'state')
            self.assertEqual(run.call_args.args[0], ['sudo', '-n', updates._LAUNCHER, 'status'])
            self.assertIs(run.call_args.kwargs['stdin'], subprocess.DEVNULL)
            with self.assertRaises(ValueError):
                updates._run_helper('arbitrary-command')
            self.assertEqual(run.call_count, 1)
        with patch.object(updates.os.path, 'isfile', return_value=False), \
                patch.object(updates.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'not installed'):
                updates._run_helper('start')
            run.assert_not_called()

    def test_versions_follow_installed_fork_and_job_survives_python_restart(self):
        properties = 'Repository=example/pyMC_WM1303\nLoadState=loaded\nActiveState=active\nSubState=exited\nResult=success\nExecMainStatus=0\n'
        with tempfile.TemporaryDirectory() as directory:
            version_file = Path(directory) / 'version'
            version_file.write_text('2.6.99\n')
            with patch.object(updates, '_VERSION_FILES', (version_file,)), \
                    patch.object(updates, '_run_helper', return_value=properties) as helper, \
                    patch.object(updates, '_fetch_url', return_value='2.6.100\n') as fetch:
                state = updates._UpdateState()
                self.assertEqual(state.snapshot()['state'], 'complete')
                state._check_version(state.snapshot()['repository'])
                self.assertTrue(state.snapshot()['has_update'])
                self.assertIn('/example/pyMC_WM1303/main/VERSION', fetch.call_args.args[0])
                state.install()
                self.assertEqual(helper.call_args.args, ('start',))
                self.assertEqual(state.snapshot()['state'], 'installing')
                with self.assertRaisesRegex(ValueError, 'in progress'):
                    state.install(force=True)
                # New process discovers the completed unit, not lost thread state.
                restarted = updates._UpdateState()
                self.assertEqual(restarted.snapshot()['state'], 'complete')
                helper.return_value = properties.replace('ActiveState=active', 'ActiveState=failed')
                failed = updates._UpdateState().snapshot()
                self.assertEqual(failed['state'], 'error')
                self.assertIn('failed', failed['error'])
        self.assertFalse(updates._has_update('2.6.100', '2.6.99'))
        with patch.object(updates, '_run_helper', side_effect=RuntimeError('missing launcher')), \
                patch.object(updates, '_fetch_url') as fetch:
            with self.assertRaisesRegex(RuntimeError, 'missing launcher'):
                updates._UpdateState().check()
            fetch.assert_not_called()  # Don't check an unrelated default fork.

    def test_progress_reconnect_and_rolling_log_tail(self):
        state = Mock()
        state.snapshot.side_effect = [{'state': 'installing', 'error': None}, {'state': 'complete', 'error': None}]
        with patch.object(updates, '_state', state), \
                patch.object(updates, '_run_helper', side_effect=['first\nsecond\n', 'second\nthird\n']), \
                patch.object(updates.cherrypy, 'request', Mock(method='GET')), \
                patch.object(updates.time, 'sleep'):
            import json
            events = [json.loads(event.removeprefix('data: ')) for event in updates.UpdateAPIEndpoints().progress()]
        lines = [event['line'] for event in events if event['type'] == 'line']
        self.assertIn('Restarting service', lines[0])
        self.assertEqual(lines[1:], ['first', 'second', 'third'])
        self.assertEqual(events[-1]['state'], 'complete')

        # Exercise the browser adapter with native streams/timers replaced;
        # no browser, network, actual waits, or build pipeline is needed.
        if shutil.which('node'):
            adapter = Path(updates.__file__).parent / 'html/wm1303-updater.js'
            script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
global.window = global;
global.location = {href:'http://localhost/', origin:'http://localhost'};
const timers = new Map(); let timer = 0;
global.setTimeout = (fn, ms) => {timers.set(++timer, {fn, ms}); return timer;};
global.clearTimeout = id => timers.delete(id);
global.fetch = async () => ({status:200});
class Native {
  static CONNECTING=0; static OPEN=1; static CLOSED=2;
  close() { this.closed = true; }
  send(data) { this.onmessage({data:JSON.stringify(data)}); }
}
global.EventSource = Native;
vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
(async () => {
  assert(new EventSource('/other-stream') instanceof Native);
  assert(new EventSource('https://example.invalid/api/update/progress') instanceof Native);
  const stream = new EventSource('/api/update/progress?token=fixture');
  const messages=[]; stream.onmessage=e => messages.push(JSON.parse(e.data));
  stream.source.send({type:'line',line:'Restarting service is part of a WM1303 upgrade; reconnect'});
  stream.source.send({type:'line',line:'first'});
  stream.source.send({type:'line',line:'second'});
  stream.source.send({type:'line',line:'Restarting service now'});
  assert.equal(messages[2].line, 'Service restart now');
  const reconnect = async () => {
    await stream.source.onerror();
    const [id, retry] = [...timers].find(([,entry]) => entry.ms === 2000);
    timers.delete(id); retry.fn();
  };
  await reconnect();
  stream.source.send({type:'line',line:'Restarting service is part of a WM1303 upgrade; reconnect'});
  stream.source.send({type:'line',line:'second'});
  stream.source.send({type:'line',line:'Restarting service now'});
  stream.source.send({type:'line',line:'third'});
  assert.deepEqual(messages.map(x=>x.line), ['first','second','Service restart now','third']);
  stream.source.send({type:'done',state:'complete'});
  assert.equal(stream.readyState, 2);
  assert.equal(timers.size, 0);
  const expired = new EventSource('/api/update/progress?token=expired');
  expired.onmessage=e => messages.push(JSON.parse(e.data));
  global.fetch = async () => ({status:401});
  await expired.source.onerror();
  assert.equal(messages.at(-1).state, 'error');
  assert.match(messages.at(-1).error, /Sign in again/);
  assert.equal(expired.readyState, 2);
  assert.equal(timers.size, 0);
})().catch(error => { console.error(error); process.exitCode=1; });
'''
            result = subprocess.run(['node', '-e', script, str(adapter)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
