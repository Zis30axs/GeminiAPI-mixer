"""Start, stop, and inspect this project's local account pool."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import sysconfig
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
AUTH = ROOT / 'auth'
POOL_FILE = AUTH / 'pool.json'
STATE_FILE = AUTH / 'pool-state.json'
CONFIG_FILE = ROOT / 'config.json'


@contextmanager
def control_lock():
    AUTH.mkdir(exist_ok=True)
    with (AUTH / '.pool-control.lock').open('a+b') as handle:
        handle.seek(0)
        handle.write(b'0')
        handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError('Another pool start/stop operation is in progress.') from None
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    try:
        temporary.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load_plan():
    config = read_json(CONFIG_FILE)
    pool = read_json(POOL_FILE)
    if config.get('host') not in ('127.0.0.1', '0.0.0.0'):
        raise ValueError('The account pool must listen on 127.0.0.1 or 0.0.0.0.')
    gateway_port = int(config['port'])
    if not 1 <= gateway_port <= 65535:
        raise ValueError('Invalid gateway port.')
    plan, ids, ports = [], set(), {gateway_port}
    for backend in pool['backends']:
        name = backend['id']
        if not isinstance(name, str) or not re.fullmatch(r'u\d+', name) or name in ids:
            raise ValueError('Account ids must be unique uN names.')
        parsed = urllib.parse.urlsplit(backend['url'])
        port = parsed.port
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not port or
                parsed.username or parsed.password or parsed.path not in ('', '/') or
                parsed.query or parsed.fragment or port in ports):
            raise ValueError('Each backend needs a unique local HTTP port.')
        profile_path = AUTH / f'config-{name}.json'
        profile = read_json(profile_path)
        if str(profile.get('auth_user')) != str(int(name[1:])):
            raise ValueError(f'Account index mismatch in {profile_path.name}.')
        cookie_file = Path(profile['cookie_file']).resolve()
        if not cookie_file.is_relative_to(AUTH.resolve()) or not cookie_file.is_file():
            raise ValueError('An imported cookie file is missing or outside the auth directory.')
        ids.add(name)
        ports.add(port)
        plan.append({'id': name, 'port': port, 'script': str(ROOT / 'gemini_web2api.py'),
                     'config': str(profile_path)})
    if not plan:
        raise ValueError('No account backends configured.')
    plan.append({'id': 'gateway', 'port': gateway_port, 'script': str(ROOT / 'account_pool.py'),
                 'config': str(CONFIG_FILE)})
    return config, pool, plan


def get_json(url, api_key=None, timeout=1):
    headers = {'Authorization': 'Bearer ' + api_key} if api_key else {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
        return json.load(response)


def port_open(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.3):
            return True
    except OSError:
        return False


def save_state(components):
    atomic_json(STATE_FILE, {'components': components})


def stop():
    if os.name != 'nt':
        raise RuntimeError('This workspace launcher supports Windows process management.')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                             '-File', str(ROOT / 'stop.ps1')], check=False)
    if result.returncode:
        raise RuntimeError('Safe stop failed; no unrelated process will be terminated.')


def start():
    config, pool, plan = load_plan()
    base_url = f'http://127.0.0.1:{config["port"]}'
    if port_open(config['port']):
        try:
            health = get_json(base_url + '/')
            if health.get('mode') == 'account_pool':
                print(f'Already running: {base_url}/v1', flush=True)
                return
        except (OSError, ValueError):
            pass
        raise RuntimeError('The gateway port is occupied. Stop the existing service first.')
    for component in plan:
        if port_open(component['port']):
            raise RuntimeError(f'Port {component["port"]} is occupied; nothing was started.')
    for component in plan[:-1]:
        profile_path = Path(component['config'])
        profile = read_json(profile_path)
        for field in ('api_keys', 'proxy', 'retry_attempts', 'retry_delay_sec',
                      'request_timeout_sec', 'default_model', 'log_requests', 'temporary_chats'):
            if field in config:
                profile[field] = config[field]
        profile.update(host='127.0.0.1', port=component['port'])
        atomic_json(profile_path, profile)
    environment = os.environ.copy()
    # Use the actual interpreter rather than Windows' venv launcher so each
    # recorded PID owns the server and has no hidden interpreter child.
    environment['PYTHONPATH'] = sysconfig.get_path('purelib')
    interpreter = getattr(sys, '_base_executable', sys.executable)
    log_dir = ROOT / 'logs'
    log_dir.mkdir(exist_ok=True)
    components, processes = [], {}
    try:
        for component in plan:
            command = [interpreter, '-u', component['script'], '--config', component['config']]
            if component['id'] == 'gateway':
                command.extend(['--pool', str(POOL_FILE)])
            with (log_dir / f'pool-{component["id"]}.stdout.log').open('ab') as stdout, \
                    (log_dir / f'pool-{component["id"]}.stderr.log').open('ab') as stderr:
                process = subprocess.Popen(command, cwd=str(ROOT), env=environment, stdin=subprocess.DEVNULL,
                                           stdout=stdout, stderr=stderr,
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            components.append({**component, 'pid': process.pid})
            processes[component['id']] = process
            save_state(components)
        pending = {component['id']: component for component in plan}
        deadline = time.monotonic() + 40
        while pending and time.monotonic() < deadline:
            for name, component in list(pending.items()):
                if processes[name].poll() is not None:
                    raise RuntimeError(f'{name} exited; see logs/pool-{name}.stderr.log.')
                try:
                    health = get_json(f'http://127.0.0.1:{component["port"]}/')
                    if health.get('status') == 'ok':
                        del pending[name]
                except (OSError, ValueError):
                    pass
            if pending:
                time.sleep(0.2)
        if pending:
            raise RuntimeError('Server startup timed out: ' + ', '.join(pending))
        print(f'Running {len(pool["backends"])} account backends: {base_url}/v1', flush=True)
        print('API key: config.json. Stop: stop.cmd. Status: start.cmd status.', flush=True)
    except BaseException:
        for process in processes.values():
            try:
                if process.poll() is None:
                    process.terminate()
            except OSError:
                pass
        survivors = []
        for process in processes.values():
            try:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                survivors.append(process.pid)
        if survivors:
            print('Startup cleanup needs attention for process ids: ' + ', '.join(map(str, survivors)),
                  file=sys.stderr, flush=True)
        else:
            STATE_FILE.unlink(missing_ok=True)
        raise


def status():
    config = read_json(CONFIG_FILE)
    keys = config.get('api_keys') or []
    result = get_json(f'http://127.0.0.1:{config["port"]}/pool/status', keys[0] if keys else None, timeout=3)
    print(json.dumps(result, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'stop', 'status'], nargs='?', default='start')
    action = parser.parse_args().action
    try:
        if action == 'status':
            status()
        else:
            with control_lock():
                (start if action == 'start' else stop)()
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print(f'Pool operation failed: {exc}', file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
