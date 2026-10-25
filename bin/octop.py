#!/usr/bin/env python3
"""octop: OpenCode top-style monitor for session and subagent context usage.

Usage:
  octop                  # interactive session picker
  octop ID=ses_...
  octop ID=ses_... --port 4096 --interval 2

Starts its own local OpenCode server with a generated password.
Existing servers can be used with OPENCODE_SERVER_PASSWORD.
"""
import argparse
import base64
from dataclasses import dataclass, replace
from datetime import datetime
import http.client
import json
import os
import re
import secrets
import select
import shutil
import socket
import subprocess
import sys
import termios
import threading
import time
import tty
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


OPENCODE_API_TIMEOUT = 2
OPENCODE_CLI_TIMEOUT = 120
ROW_WIDTH = 80
RIGHT_MARGIN = 1
NAME_COLUMN_BOOST = 16
STATE_COLUMN_WIDTH = 11
BAR_COLUMN_WIDTH = 10
PCT_COLUMN_WIDTH = 6
TOKEN_COLUMN_WIDTH = 14
MODEL_COLUMN_WIDTH = 18
DATE_COLUMN_WIDTH = 10
TIME_COLUMN_WIDTH = 8
PARENT_SPINNER_FRAMES = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
PARENT_IDLE_INDICATOR = '⣿'
PARENT_INDICATOR_PLACEHOLDER = '◌'
ACTIVE_INDICATOR_PLACEHOLDER = '◍'
PERFORMANCE_PLACEHOLDER = '{performance}'
ANIMATION_INTERVAL = 0.2
ACTIVE_FALLBACK_INTERVAL = 5.0
ACTIVE_FALLBACK_IDS = None
ACTIVE_FALLBACK_UPDATED = 0
STATE_RUNNING = 'running'
STATE_STREAMING = 'streaming'
STATE_COMPACTING = 'compacting'
STATE_IDLE = 'idle'
STATE_PERMISSION = 'permission'
STATE_INPUT = 'input'
STATE_WAITING = 'waiting'
STATE_STOPPED = 'stopped'
STATE_FAILED = 'failed'
STATE_INTERRUPTED = 'interrupted'
STATE_CANCELED = 'canceled'
STATE_CANCELLED = 'cancelled'
ACTIVE_STATES = (STATE_RUNNING, STATE_STREAMING, STATE_COMPACTING)
WAITING_STATES = (STATE_PERMISSION, STATE_INPUT, STATE_WAITING)
STOPPED_STATES = (STATE_STOPPED, STATE_FAILED, STATE_INTERRUPTED, STATE_CANCELED, STATE_CANCELLED)
FINISHED_STATES = ('succeeded', *STOPPED_STATES, STATE_IDLE)
WAITING_STATE_VALUES = {
    'permission': STATE_PERMISSION,
    'permission_required': STATE_PERMISSION,
    'requires_permission': STATE_PERMISSION,
    'needs_permission': STATE_PERMISSION,
    'input': STATE_INPUT,
    'form': STATE_INPUT,
    'question': STATE_INPUT,
    'needs_input': STATE_INPUT,
    'pending': STATE_WAITING,
    'waiting': STATE_WAITING,
    'blocked': STATE_WAITING,
}
TOGGLE_ROW_OPTION_FIELDS = {
    'h': 'show_header',
    's': 'show_status',
    'b': 'show_bar',
    'p': 'show_percent',
    't': 'show_tokens',
    'm': 'show_model',
    'd': 'show_date',
    'c': 'show_time',
    'f': 'full_title',
    'a': 'abbreviate_agent',
}


@dataclass(frozen=True)
class RowOptions:
    show_header: bool = False
    show_status: bool = False
    show_bar: bool = True
    show_percent: bool = False
    show_tokens: bool = False
    show_model: bool = True
    full_model: bool = False
    show_date: bool = False
    show_time: bool = False
    full_title: bool = False
    abbreviate_agent: bool = True
    row_width: int = ROW_WIDTH
    agent_label_width: int = 0


@dataclass(frozen=True)
class DisplaySnapshot:
    frame_template: str
    parent_state: str


class PerformanceMonitor:
    def __init__(self):
        self.lock = threading.Lock()
        self.last_wall = time.monotonic()
        self.last_cpu = time.process_time()
        self.cpu_percent = 0.0
        self.render_count = 0
        self.render_total = 0.0
        self.render_max = 0.0
        self.poll_count = 0
        self.poll_total = 0.0
        self.poll_max = 0.0

    def record_render(self, elapsed):
        self._record('render', elapsed)
        self._update_cpu()

    def record_poll(self, elapsed):
        self._record('poll', elapsed)

    def _record(self, prefix, elapsed):
        with self.lock:
            count = getattr(self, f'{prefix}_count') + 1
            total = getattr(self, f'{prefix}_total') + elapsed
            max_value = max(getattr(self, f'{prefix}_max'), elapsed)
            setattr(self, f'{prefix}_count', count)
            setattr(self, f'{prefix}_total', total)
            setattr(self, f'{prefix}_max', max_value)

    def _update_cpu(self):
        wall = time.monotonic()
        cpu = time.process_time()
        with self.lock:
            wall_delta = wall - self.last_wall
            cpu_delta = cpu - self.last_cpu
            if wall_delta > 0:
                self.cpu_percent = max(0.0, 100 * cpu_delta / wall_delta)
            self.last_wall = wall
            self.last_cpu = cpu

    def line(self):
        with self.lock:
            render_avg = self.render_total / self.render_count if self.render_count else 0
            poll_avg = self.poll_total / self.poll_count if self.poll_count else 0
            return (
                f'Perf: cpu {self.cpu_percent:4.1f}%  '
                f'render avg {render_avg * 1000:4.1f}ms max {self.render_max * 1000:4.1f}ms  '
                f'poll avg {poll_avg * 1000:5.1f}ms max {self.poll_max * 1000:5.1f}ms'
            )


class SnapshotPoller:
    def __init__(self, interval, performance=None):
        self.interval = interval
        self.performance = performance
        self.snapshot = None
        self.error = None
        self.next_poll = 0
        self.polling = False
        self.lock = threading.Lock()

    def request(self, collect, *args, force=False, **kwargs):
        now = time.monotonic()
        with self.lock:
            if self.polling or (not force and self.snapshot is not None and now < self.next_poll):
                return False
            self.polling = True
        thread = threading.Thread(target=self._poll, args=(collect, args, kwargs), daemon=True)
        thread.start()
        return True

    def _poll(self, collect, args, kwargs):
        started = time.monotonic()
        try:
            snapshot = collect(*args, **kwargs)
            error = None
        except RuntimeError as exc:
            snapshot = None
            error = exc
        if self.performance is not None:
            self.performance.record_poll(time.monotonic() - started)
        with self.lock:
            if snapshot is not None:
                self.snapshot = snapshot
            self.error = error
            self.next_poll = time.monotonic() + self.interval
            self.polling = False

    def current_snapshot(self):
        with self.lock:
            return self.snapshot

    def consume_error(self):
        with self.lock:
            error = self.error
            self.error = None
            return error


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('session', nargs='?', default=None, help='Optional ID=ses_... or ses_... (omit to pick from a list)')
    parser.add_argument('--port', type=int, default=4096)
    parser.add_argument('--port-attempts', type=int, default=10,
                        help='Number of consecutive ports to try when starting a private server')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--interval', type=float, default=2.0)
    parser.add_argument('--animation-interval', type=float, default=ANIMATION_INTERVAL,
                        help='Seconds between lightweight spinner redraws')
    parser.add_argument('--profile-render', action='store_true',
                        help='Show process CPU and render/poll timing in the footer')
    parser.add_argument('--limit', type=int, default=0, help='Fallback model context window in tokens (0 = unknown)')
    parser.add_argument('--right-margin', type=int, default=RIGHT_MARGIN,
                        help='Columns to leave unused on the right edge')
    parser.add_argument('--name-boost', type=int, default=NAME_COLUMN_BOOST,
                        help='Extra columns to reserve for session names before clipping')
    parser.add_argument('--show-status', action='store_true', help='Show status column at startup')
    parser.add_argument('--hide-bar', action='store_true', help='Hide progress bars and give width to session names')
    parser.add_argument('--hide-percent', action='store_true', help='Hide percentages and give width to session names')
    parser.add_argument('--hide-tokens', action='store_true', help='Hide token counts and give width to session names')
    parser.add_argument('--show-percent', action='store_true', help='Show percentages at startup')
    parser.add_argument('--show-tokens', action='store_true', help='Show token counts at startup')
    parser.add_argument('--show-model', action='store_true', help='Show short provider/model column')
    parser.add_argument('--hide-model', action='store_true', help='Hide short provider/model column')
    parser.add_argument('--show-date', action='store_true', help='Show session start date at startup')
    parser.add_argument('--show-time', action='store_true', help='Show session start time at startup')
    parser.add_argument('--hide-date', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--hide-time', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--full-title', action='store_true', help='Show full session titles on separate lines at startup')
    parser.add_argument('--full-agent', action='store_true', help='Show full subagent labels at startup instead of abbreviations')
    args = parser.parse_args()
    if args.session is not None:
        args.session = args.session.removeprefix('ID=')
        if not args.session:
            parser.error('Session ID cannot be empty')
    if args.interval <= 0:
        parser.error('--interval must be positive')
    if args.animation_interval <= 0:
        parser.error('--animation-interval must be positive')
    if args.port_attempts <= 0:
        parser.error('--port-attempts must be positive')
    if args.right_margin < 0:
        parser.error('--right-margin cannot be negative')
    if args.name_boost < 0:
        parser.error('--name-boost cannot be negative')
    return args


def row_options_from_args(args):
    return RowOptions(
        show_status=args.show_status,
        show_bar=not args.hide_bar,
        show_percent=args.show_percent and not args.hide_percent,
        show_tokens=args.show_tokens and not args.hide_tokens,
        show_model=(args.show_model or not args.hide_model),
        show_date=args.show_date and not args.hide_date,
        show_time=args.show_time and not args.hide_time,
        full_title=args.full_title,
        abbreviate_agent=not args.full_agent,
    )


def toggle_row_option(options, key):
    if key == 'M':
        return replace(options, full_model=not options.full_model)
    field = TOGGLE_ROW_OPTION_FIELDS.get(key.lower())
    if field:
        return replace(options, **{field: not getattr(options, field)})
    return options


def session_path(sid, suffix=''):
    return f'/api/session/{urllib.parse.quote(sid, safe="")}{suffix}'


def row_options_with_width(options, width, right_margin=RIGHT_MARGIN, name_boost=NAME_COLUMN_BOOST):
    terminal_width = int(width or ROW_WIDTH)
    boosted = terminal_width - int(right_margin or 0) + int(name_boost or 0)
    max_width = max(40, terminal_width - min(int(right_margin or 0), terminal_width - 40))
    return replace(options, row_width=max(40, min(max_width, boosted)))


def unwrap(value):
    if isinstance(value, dict) and 'data' in value:
        return value['data']
    return value


def opencode_api_get(path):
    try:
        proc = subprocess.run(
            ['opencode', 'api', 'get', path],
            check=False,
            capture_output=True,
            text=True,
            timeout=OPENCODE_API_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        return unwrap(json.loads(proc.stdout))
    except json.JSONDecodeError:
        return None


def run_opencode_cli(args):
    try:
        proc = subprocess.run(
            ['opencode', *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=OPENCODE_CLI_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise RuntimeError('opencode executable not found in PATH') from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f'opencode {args[0]} timed out') from exc
    output = (proc.stdout or '').strip()
    error = (proc.stderr or '').strip()
    if proc.returncode != 0:
        detail = error or output or f'exited with status {proc.returncode}'
        raise RuntimeError(f'opencode {args[0]} failed: {detail}')
    return output or error


def default_export_path(session_id):
    return f'{session_id}.json'


def export_session(session_id, path):
    path = Path(str(path).strip() or default_export_path(session_id))
    data = run_opencode_cli(['session', 'export', session_id])
    path.write_text(data, encoding='utf-8')
    return str(path)


def import_session(path):
    path = str(path).strip()
    if not path:
        raise RuntimeError('Import path cannot be empty')
    return run_opencode_cli(['session', 'import', path])


class Client:
    def __init__(self, base, password):
        self.base = base
        self.password = password

    def get(self, path, params=None, timeout=8):
        url = self.base + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        token = base64.b64encode(('opencode:' + self.password).encode()).decode()
        req = urllib.request.Request(url, headers={
            'Authorization': 'Basic ' + token,
            'Accept': 'application/json',
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read()
                content_type = response.headers.get('Content-Type', '')
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise RuntimeError('HTTP 401: incorrect password or missing access') from exc
            raise RuntimeError(f'HTTP {exc.code} for {path}') from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                # Chain the underlying timeout so __cause__ stays a TimeoutError,
                # matching the raw-timeout contract below.
                raise RuntimeError(f'Request timed out for {path} after {timeout}s') from exc.reason
            raise RuntimeError(f'Cannot connect to {self.base}: {exc.reason}') from exc
        except http.client.IncompleteRead as exc:
            raise RuntimeError(f'Incomplete HTTP response for {path}') from exc
        # socket.timeout is an alias of TimeoutError on Python 3.10+; keep the
        # explicit name so timeouts are still converted on older interpreters.
        except (socket.timeout, TimeoutError) as exc:
            raise RuntimeError(f'Request timed out for {path} after {timeout}s') from exc
        if 'json' not in content_type.lower():
            raise RuntimeError(f'{path} returned {content_type}, not JSON')
        try:
            return unwrap(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f'{path} returned invalid JSON') from exc


def extract_sessions(response, parent):
    sessions = extract_list(response, ('data', 'items', 'sessions', 'results'))
    return [s for s in sessions if isinstance(s, dict) and s.get('parentID') == parent]


def response_cursor(response):
    if not isinstance(response, dict):
        return None
    cursor = response.get('cursor')
    if not isinstance(cursor, dict):
        return None
    next_cursor = cursor.get('next')
    return next_cursor if isinstance(next_cursor, str) and next_cursor else None


def child_sessions(client, parent):
    response = client.get('/api/session', {'parentID': parent, 'limit': '200'})
    sessions = extract_sessions(response, parent)
    if not sessions:
        response = client.get('/api/session', {'parentID': parent})
        sessions = extract_sessions(response, parent)
    if not sessions:
        sessions = extract_sessions(opencode_api_get(f'/api/session?parentID={urllib.parse.quote(parent, safe="")}&limit=200'), parent)
    if sessions:
        return sessions

    sessions = []
    cursor = None
    for _ in range(25):
        params = {'limit': '200'}
        if cursor:
            params['cursor'] = cursor
        response = client.get('/api/session', params)
        sessions.extend(extract_sessions(response, parent))
        cursor = response_cursor(response)
        if not cursor:
            break
    if not sessions:
        sessions = extract_sessions(opencode_api_get('/api/session?limit=200'), parent)
    return sessions


def parent_summaries_with_children(client, exclude_parent, limit=3):
    response = opencode_api_get('/api/session?limit=200')
    if response is None:
        try:
            response = client.get('/api/session', {'limit': '200'})
        except RuntimeError:
            response = None
    sessions = extract_list(response, ('data', 'items', 'sessions', 'results'))
    by_id = {s.get('id'): s for s in sessions if isinstance(s, dict) and s.get('id')}
    counts = {}
    latest = {}
    for session in sessions:
        if not isinstance(session, dict):
            continue
        parent = session.get('parentID')
        if not parent or parent == exclude_parent:
            continue
        counts[parent] = counts.get(parent, 0) + 1
        latest[parent] = max(latest.get(parent, 0), session_updated_time(session))
    parents = sorted(counts, key=lambda sid: latest.get(sid, 0), reverse=True)
    summaries = []
    for parent in parents[:limit]:
        session = by_id.get(parent, {})
        title = session.get('title') or parent
        summaries.append({'id': parent, 'title': title, 'count': counts[parent]})
    return summaries


def extract_list(response, keys=('items', 'data', 'sessions', 'results')):
    if isinstance(response, list):
        return response
    if isinstance(response, dict):
        for key in keys:
            if isinstance(response.get(key), list):
                return response[key]
    return []


def session_directory(session):
    location = session.get('location') if isinstance(session, dict) else None
    if isinstance(location, dict) and isinstance(location.get('directory'), str):
        return location['directory']
    directory = session.get('directory') if isinstance(session, dict) else None
    return directory if isinstance(directory, str) else None


def filter_sessions_by_directory(sessions, directory, include_other_dirs):
    if include_other_dirs:
        return sessions, 0
    visible = [session for session in sessions if session_directory(session) == directory]
    return visible, len(sessions) - len(visible)


def load_picker_sessions(client):
    result = client.get('/api/session')
    sessions = extract_list(result, ('data', 'items', 'sessions', 'results'))
    sessions = [s for s in sessions if isinstance(s, dict) and s.get('id')]
    roots = [s for s in sessions if not s.get('parentID')]
    return roots or sessions


def session_from_number(answer, visible_sessions):
    if not answer.isdigit():
        return None
    index = int(answer)
    if 1 <= index <= len(visible_sessions):
        return visible_sessions[index - 1]
    return None


def print_cli_result(action, detail):
    print(f'{action} complete.')
    if detail:
        print(detail)


def choose_session(client):
    """List root sessions in the current OpenCode project and prompt for one."""
    current_dir = os.getcwd()
    include_other_dirs = False

    def updated(s):
        t = s.get('time') or {}
        if not isinstance(t, dict):
            t = {}
        return t.get('updated') or t.get('created') or 0

    while True:
        sessions = load_picker_sessions(client)
        visible_sessions, hidden_count = filter_sessions_by_directory(sessions, current_dir, include_other_dirs)
        if not visible_sessions and not include_other_dirs:
            visible_sessions, hidden_count = sessions, 0
            include_other_dirs = True
            if visible_sessions:
                print(f'No root sessions found for {current_dir}; showing all directories.')
        visible_sessions.sort(key=updated, reverse=True)
        print('\nOpenCode sessions (most recently updated first)')
        print(f'Directory: {current_dir}')
        mark = 'x' if include_other_dirs else ' '
        print(f'[{mark}] show sessions from other directories', end='')
        if hidden_count:
            print(f' ({hidden_count} hidden)')
        else:
            print()
        print()
        numbered = list(enumerate(visible_sessions, 1))
        for i, session in reversed(numbered):
            title = str(session.get('title') or '(untitled)').replace('\n', ' ')
            directory = session_directory(session)
            suffix = f'  — {directory}' if include_other_dirs and directory and directory != current_dir else ''
            print(f'{i:3}. {title[:65]}{suffix}')
            print(f'     {session["id"]}')
        if not visible_sessions:
            print('No sessions found. Import a session JSON or quit.')
        print('\nSelect a session number, e <number> to export, i to import, t to toggle other directories, or q to quit.')
        try:
            answer = input('Session: ').strip()
        except EOFError:
            raise SystemExit(1)
        command, _, value = answer.partition(' ')
        command = command.lower()
        if answer.lower() in ('q', 'quit', 'exit'):
            raise SystemExit(0)
        if answer.lower() in ('t', 'toggle'):
            include_other_dirs = not include_other_dirs
            continue
        if command in ('e', 'export'):
            parts = value.split(maxsplit=1)
            choice = parts[0] if parts else input('Export session number: ').strip()
            session = session_from_number(choice, visible_sessions)
            if session is None:
                print(f'Enter a session number from 1 to {len(visible_sessions)} to export.')
                continue
            default_path = default_export_path(session['id'])
            path = parts[1] if len(parts) > 1 else input(f'Export JSON path [{default_path}]: ').strip()
            path = path or default_path
            try:
                saved_path = export_session(session['id'], path)
                print_cli_result('Export', f'Saved to {saved_path}')
            except RuntimeError as exc:
                print(f'Export error: {exc}')
            continue
        if command in ('i', 'import'):
            path = value.strip() or input('Import JSON path: ').strip()
            try:
                print_cli_result('Import', import_session(path))
            except RuntimeError as exc:
                print(f'Import error: {exc}')
            continue
        session = session_from_number(answer, visible_sessions)
        if session is not None:
            return session['id']
        print(f'Enter a number from 1 to {len(visible_sessions)}, e <number>, i, t, or q.')


def extract_messages(response):
    return extract_list(response, ('items', 'messages', 'results', 'data'))


def active_session_ids(client):
    ids = set()
    failed = False
    try:
        active = client.get('/api/session/active', timeout=1)
    except RuntimeError:
        active = None
        failed = True
    if isinstance(active, dict):
        ids.update(sid for sid in active if isinstance(sid, str))

    if failed or not ids:
        ids.update(cached_managed_active_session_ids())

    return ids or None


def cached_managed_active_session_ids():
    global ACTIVE_FALLBACK_IDS, ACTIVE_FALLBACK_UPDATED
    now = time.monotonic()
    if ACTIVE_FALLBACK_IDS is not None and now - ACTIVE_FALLBACK_UPDATED < ACTIVE_FALLBACK_INTERVAL:
        return ACTIVE_FALLBACK_IDS
    managed = opencode_api_get('/api/session/active')
    if isinstance(managed, dict):
        ACTIVE_FALLBACK_IDS = {sid for sid in managed if isinstance(sid, str)}
    else:
        ACTIVE_FALLBACK_IDS = set()
    ACTIVE_FALLBACK_UPDATED = now
    return ACTIVE_FALLBACK_IDS


def pending_items(client, path, suffix):
    try:
        private_items = extract_list(client.get(path + suffix, timeout=1), ('data', 'items', 'results'))
    except RuntimeError:
        return extract_list(opencode_api_get(path + suffix), ('data', 'items', 'results'))
    if private_items:
        return private_items
    managed_items = extract_list(opencode_api_get(path + suffix), ('data', 'items', 'results'))
    return managed_items or private_items


def normalized_state_value(value):
    if not isinstance(value, str):
        return None
    return value.strip().lower().replace('-', '_').replace(' ', '_')


def mapped_waiting_state(value):
    return WAITING_STATE_VALUES.get(normalized_state_value(value))


def wait_state_from_record(record):
    if not isinstance(record, dict):
        return mapped_waiting_state(record)
    for key in ('status', 'state', 'type', 'phase'):
        value = record.get(key)
        if isinstance(value, dict):
            state = wait_state_from_record(value)
        else:
            state = mapped_waiting_state(value)
        if state:
            return state
    if isinstance(record.get('permissionID'), str):
        return STATE_PERMISSION
    if isinstance(record.get('questions'), list) or isinstance(record.get('form'), dict):
        return STATE_INPUT
    return None


def context_part_state(part):
    wait_state = wait_state_from_record(part)
    if wait_state:
        if part.get('type') == 'tool' and wait_state == STATE_WAITING:
            tool = part.get('tool') or part.get('name')
            return f'waiting for permission: {tool}' if isinstance(tool, str) and tool else STATE_PERMISSION
        return wait_state
    state = part.get('state')
    if isinstance(state, dict) and state.get('status') in ACTIVE_STATES:
        return state.get('status')
    return None


def infer_context_state(messages):
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        info = msg.get('info', msg)
        if not isinstance(info, dict):
            continue
        parts = info.get('parts') or info.get('content')
        if isinstance(parts, list):
            for part in reversed(parts):
                if not isinstance(part, dict):
                    continue
                state = context_part_state(part)
                if state:
                    return state
        wait_state = wait_state_from_record(info)
        if wait_state:
            return wait_state
        status = info.get('status')
        if status in ACTIVE_STATES:
            return status
    return None


def session_lifecycle_state(session):
    time_info = session.get('time')
    if not isinstance(time_info, dict):
        return None
    idle = time_info.get('idle')
    updated = time_info.get('updated') or time_info.get('created') or 0
    if isinstance(updated, bool) or not isinstance(updated, (int, float)):
        updated = 0
    if isinstance(idle, (int, float)) and not isinstance(idle, bool):
        if idle < updated:
            return STATE_RUNNING
        outcome = session.get('outcome')
        if isinstance(outcome, str) and outcome:
            return outcome
        return STATE_IDLE
    outcome = session.get('outcome')
    if isinstance(outcome, str) and outcome:
        return outcome
    return STATE_RUNNING if updated else None


def is_finished_state(state):
    return state in FINISHED_STATES


def is_stopped_state(state):
    return compact_state(state) in STOPPED_STATES


def is_active_state(state):
    return compact_state(state) in ACTIVE_STATES


def parent_indicator(state):
    if is_active_state(state):
        return PARENT_SPINNER_FRAMES[int(time.monotonic() * 4) % len(PARENT_SPINNER_FRAMES)]
    return PARENT_IDLE_INDICATOR


def row_indicator(state, parent=False):
    if parent:
        return parent_indicator(state)
    if is_active_state(state):
        return parent_indicator(state)
    return ' '


def row_indicator_placeholder(state):
    return ACTIVE_INDICATOR_PLACEHOLDER if is_active_state(state) else ' '


def render_snapshot(snapshot, performance_line=None):
    frame = snapshot.frame_template.replace(PARENT_INDICATOR_PLACEHOLDER, parent_indicator(snapshot.parent_state), 1)
    frame = frame.replace(ACTIVE_INDICATOR_PLACEHOLDER, row_indicator(STATE_RUNNING))
    if performance_line is not None:
        return frame.replace(PERFORMANCE_PLACEHOLDER, performance_line, 1)
    return frame


def parent_is_idle(parent_session):
    time_info = parent_session.get('time') if isinstance(parent_session, dict) else None
    if not isinstance(time_info, dict):
        return False
    idle = time_info.get('idle')
    updated = time_info.get('updated') or time_info.get('created') or 0
    if isinstance(updated, bool) or not isinstance(updated, (int, float)):
        updated = 0
    return isinstance(idle, (int, float)) and not isinstance(idle, bool) and idle >= updated


def parent_display_state(parent_id, active, parent_session, child_states):
    if active is not None and parent_id in active:
        return STATE_RUNNING
    if any(compact_state(state) in WAITING_STATES for state in child_states):
        return STATE_IDLE
    if child_states and any(not is_finished_state(state) for state in child_states):
        return STATE_RUNNING
    if parent_is_idle(parent_session):
        return STATE_IDLE
    own_state = session_lifecycle_state(parent_session)
    if is_finished_state(own_state):
        return own_state
    return None


def session_updated_time(session):
    time_info = session.get('time')
    if not isinstance(time_info, dict):
        return 0
    value = time_info.get('updated') or time_info.get('created') or 0
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def select_visible_subagents(sessions, active, max_count):
    if max_count <= 0:
        return [], len(sessions)
    active = active or set()
    ordered = sorted(
        sessions,
        key=lambda session: (session.get('id') in active, session_updated_time(session)),
        reverse=True,
    )
    return ordered[:max_count], max(0, len(ordered) - max_count)


def max_visible_subagents(terminal_height):
    # Header, parent row, section labels, footer, and a possible "hidden" row
    # need about 14 rows. Never force subagent rows onto a short screen; the
    # frame fitter will keep the top info visible if the pane is very small.
    return max(0, terminal_height - 14)


def session_wait_state(client, path, active, session, context_state):
    sid = session.get('id')
    lifecycle = session_lifecycle_state(session)
    session_hint = wait_state_from_record(session)
    should_probe_pending = (
        (active is not None and sid in active)
        or session_hint is not None
        or context_state == STATE_RUNNING
        or compact_state(context_state) in WAITING_STATES
        or lifecycle in (None, STATE_RUNNING)
    )
    if not should_probe_pending:
        return lifecycle

    permissions = pending_items(client, path, '/permission')
    forms = pending_items(client, path, '/form')
    if permissions:
        first = permissions[0] if isinstance(permissions[0], dict) else {}
        action = first.get('action') or STATE_PERMISSION
        resources = first.get('resources') if isinstance(first.get('resources'), list) else []
        detail = ', '.join(str(resource) for resource in resources[:2])
        suffix = f': {action}' + (f' {detail}' if detail else '')
        return f'waiting for permission ({len(permissions)}){suffix}'
    if forms:
        plural = '' if len(forms) == 1 else 's'
        return f'waiting for input ({len(forms)} form{plural})'
    if session_hint:
        return session_hint
    if active is not None and sid in active:
        return context_state if compact_state(context_state) in WAITING_STATES else STATE_RUNNING
    if context_state:
        return context_state
    if lifecycle:
        return lifecycle
    return 'status unknown'



def model_catalog(client):
    response = client.get('/api/model')
    models = response if isinstance(response, list) else []
    return {(m.get('providerID'), m.get('id')): m for m in models if isinstance(m, dict)}


def session_model(session):
    model = session.get('model')
    if isinstance(model, dict):
        return model.get('providerID'), model.get('id')
    return None, None


def latest_model(client, session):
    # Some sessions do not include the model ref; use the latest assistant
    # message in the active context to identify the model actually used.
    provider, model_id = session_model(session)
    if provider and model_id:
        return provider, model_id
    sid = session.get('id')
    if not sid:
        return None, None
    context = client.get(session_path(sid, '/context'))
    if isinstance(context, list):
        for message in reversed(context):
            if not isinstance(message, dict):
                continue
            info = message.get('info', message)
            if not isinstance(info, dict):
                continue
            ref = info.get('model')
            if isinstance(ref, dict) and ref.get('providerID') and ref.get('id'):
                return ref['providerID'], ref['id']
    return None, None


def context_limit(catalog, provider, model_id, fallback):
    model = catalog.get((provider, model_id))
    if isinstance(model, dict):
        limit = model.get('limit') or {}
        if isinstance(limit, dict) and isinstance(limit.get('context'), (int, float)) and limit['context'] > 0:
            return int(limit['context']), 'catalog'
    return fallback, 'fallback' if fallback else 'unknown'


def clip(text, width):
    text = str(text)
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[:width - 1] + '…'


ANSI_RE = re.compile(r'\033\[[0-9;?]*[A-Za-z]')


def visible_width(text):
    return len(ANSI_RE.sub('', text))


def format_session_lines(
    name,
    state,
    used,
    limit,
    source,
    provider,
    model_id,
    highlight_idle=False,
    highlight_stopped=False,
    row_options=None,
    date='',
    clock='',
):
    model = short_model_name(provider, model_id)
    full_model = f'{provider or "?"}/{model_id or "?"}'
    options = row_options or RowOptions()
    state = clip(compact_state(state), 11)
    if used is None:
        return format_session_output(name, state, '', '', 'usage pending' if options.show_tokens else '', model, options, highlight_idle, highlight_stopped, date, clock, full_model=full_model)
    if not limit:
        usage = f'~{used:,} tok'
        return format_session_output(name, state, '', '', usage if options.show_tokens else '', model, options, highlight_idle, highlight_stopped, date, clock, full_model=full_model)

    used = display_used_tokens(used, limit)
    pct = 100 * used / limit
    filled = min(BAR_COLUMN_WIDTH, max(0, round(pct / 10)))
    bar = '█' * filled + '░' * (BAR_COLUMN_WIDTH - filled)
    usage = f'{used:,}/{limit:,}'
    return format_session_output(
        name,
        state,
        bar if options.show_bar else '',
        f'{pct:5.1f}%' if options.show_percent else '',
        usage if options.show_tokens else '',
        model,
        options,
        highlight_idle,
        highlight_stopped,
        date,
        clock,
        full_model=full_model,
    )


def format_session_output(
    name,
    state,
    bar,
    pct,
    usage,
    model,
    options,
    highlight_idle=False,
    highlight_stopped=False,
    date='',
    clock='',
    full_model='',
):
    name_lines = str(name).splitlines() or ['']
    if options.full_title:
        lines = []
        for name_line in name_lines:
            lines.extend(wrap_title_lines(name_line, options.row_width))
        detail = format_detail_columns(state, bar, pct, usage, model, options, date, clock)
        if detail:
            lines.append(detail)
    else:
        lines = name_lines[:-1]
        lines.append(format_columns(name_lines[-1], state, bar, pct, usage, model, options, date, clock))
    if options.full_model:
        lines.extend(wrap_title_lines(f'  Model: {full_model}', options.row_width))
    return [
        style_session_line(line, state, highlight_idle=highlight_idle, highlight_stopped=highlight_stopped)
        for line in lines
    ]


def wrap_title_lines(name, width):
    width = max(1, int(width or ROW_WIDTH))
    text = str(name)
    if not text:
        return ['']
    lines = []
    remaining = text
    while len(remaining) > width:
        split_at = remaining.rfind(' ', 0, width + 1)
        if split_at <= 0:
            split_at = width
        lines.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    lines.append(remaining)
    return lines


def short_model_name(provider, model_id):
    model_id = model_id or '?'
    short = str(model_id)
    if short.startswith('claude-'):
        short = short.removeprefix('claude-')
    return clip(short, MODEL_COLUMN_WIDTH)


def fixed_column_width(options, bar_width=BAR_COLUMN_WIDTH):
    fixed = 0
    if options.show_status:
        fixed += 1 + 2 + STATE_COLUMN_WIDTH
    if options.show_bar:
        fixed += 1 + bar_width
    if options.show_percent:
        fixed += 1 + PCT_COLUMN_WIDTH
    if options.show_tokens:
        fixed += 1 + TOKEN_COLUMN_WIDTH
    if options.show_model:
        fixed += 1 + MODEL_COLUMN_WIDTH
    if options.show_date:
        fixed += 1 + DATE_COLUMN_WIDTH
    if options.show_time:
        fixed += 1 + TIME_COLUMN_WIDTH
    return fixed


def name_column_width(options):
    return max(8, options.row_width - fixed_column_width(options))


def bar_column_width(options, name_width):
    return BAR_COLUMN_WIDTH if options.show_bar else 0


def format_columns(name, state, bar, pct, usage, model='', options=None, date='', clock=''):
    options = options or RowOptions()
    width = name_column_width(options)
    bar_width = bar_column_width(options, width)
    parts = [f'{clip(name, width):{width}}']
    if options.show_status:
        parts.append(f'[{state:{STATE_COLUMN_WIDTH}}]')
    if options.show_bar:
        parts.append(f'{fit_bar(bar, bar_width):{bar_width}}')
    if options.show_percent:
        parts.append(f'{pct:>{PCT_COLUMN_WIDTH}}')
    if options.show_tokens:
        parts.append(usage)
    if options.show_date:
        parts.append(f'{date:>{DATE_COLUMN_WIDTH}}')
    if options.show_time:
        parts.append(f'{clock:>{TIME_COLUMN_WIDTH}}')
    if options.show_model:
        parts.append(f'{model:{MODEL_COLUMN_WIDTH}}')
    line = ' '.join(parts)
    return line[:options.row_width].rstrip()


def format_detail_columns(state, bar, pct, usage, model='', options=None, date='', clock=''):
    options = options or RowOptions()
    bar_width = bar_column_width(options, 0)
    parts = []
    if options.show_status:
        parts.append(f'[{state:{STATE_COLUMN_WIDTH}}]')
    if options.show_bar:
        parts.append(f'{fit_bar(bar, bar_width):{bar_width}}')
    if options.show_percent:
        parts.append(f'{pct:>{PCT_COLUMN_WIDTH}}')
    if options.show_tokens:
        parts.append(usage)
    if options.show_date and date:
        parts.append(f'{date:>{DATE_COLUMN_WIDTH}}')
    if options.show_time and clock:
        parts.append(f'{clock:>{TIME_COLUMN_WIDTH}}')
    if options.show_model:
        parts.append(f'{model:{MODEL_COLUMN_WIDTH}}')
    return ('  ' + ' '.join(parts))[:options.row_width].rstrip() if parts else ''


def fit_bar(bar, width):
    bar = str(bar or '')
    if not bar:
        return ''
    if len(bar) == width:
        return bar
    filled = bar.count('█')
    ratio = filled / len(bar) if bar else 0
    new_filled = min(width, max(0, round(width * ratio)))
    return '█' * new_filled + '░' * (width - new_filled)


def compact_state(state):
    state = str(state)
    if state.startswith('waiting for permission'):
        return STATE_PERMISSION
    if state.startswith('waiting for input'):
        return STATE_INPUT
    if state.startswith('waiting'):
        return STATE_WAITING
    return state


def style_session_line(line, state, highlight_idle=False, highlight_stopped=False):
    state = compact_state(state)
    if state in WAITING_STATES:
        return f'\033[30;41m{line}\033[0m'
    if highlight_stopped and is_stopped_state(state):
        return f'\033[31m{line}\033[0m'
    if highlight_idle and state == STATE_IDLE:
        return f'\033[30;43m{line}\033[0m'
    return line


def fit_frame_lines(lines, max_lines, preserve_count=0):
    if max_lines is None or max_lines <= 0 or len(lines) <= max_lines:
        return lines
    if max_lines == 1:
        return [lines[0]]
    if preserve_count and max_lines <= preserve_count:
        preserved = lines[:max_lines]
        parent_index = next((i for i, line in enumerate(lines) if line == 'PARENT'), None)
        if parent_index is not None and parent_index + 1 < len(lines) and max_lines >= 2:
            preserved[-2:] = lines[parent_index:parent_index + 2]
        return preserved
    return lines[:max_lines - 1] + ['… output shortened to fit terminal']


def column_status(options):
    return 'status:{0} bar:{1} pct:{2} tokens:{3} model:{4} date:{5} time:{6} title:{7} agent:{8}'.format(
        'on' if options.show_status else 'off',
        'on' if options.show_bar else 'off',
        'on' if options.show_percent else 'off',
        'on' if options.show_tokens else 'off',
        'full' if options.full_model else ('on' if options.show_model else 'off'),
        'on' if options.show_date else 'off',
        'on' if options.show_time else 'off',
        'full' if options.full_title else 'short',
        'abbr' if options.abbreviate_agent else 'full',
    )


def render_frame(
    parent,
    parent_lines,
    subagent_lines,
    now=None,
    omitted_count=0,
    alternate_parents=None,
    max_lines=None,
    row_options=None,
    performance_line=None,
    terminal_width=None,
):
    options = row_options or RowOptions()
    lines = [
        'octop — OpenCode top-style context monitor (active context)',
        f'Parent: {parent}',
        f'Updated: {now or time.strftime("%H:%M:%S")}',
        f'Columns: {column_status(options)}',
    ]
    if options.show_header:
        if performance_line:
            lines.append(performance_line)
        else:
            lines.append('Keys: h header, s status, b bar, p pct, t tokens, m model, M full model, d date, c clock, f full title, a abbrev agent')
        if terminal_width is not None:
            width = max(1, terminal_width - 1)
            lines = [wrapped for line in lines for wrapped in wrap_title_lines(line, width)]
        lines.extend(['', 'PARENT'])
    else:
        lines = ['PARENT']
    lines.extend(parent_lines)
    preserve_count = len(lines)
    lines.extend(['', 'SUBAGENTS'])
    if not subagent_lines:
        lines.append('No direct child sessions found for this parent.')
        if alternate_parents:
            lines.append('Recent parent sessions with subagents:')
            for summary in alternate_parents:
                title = clip(summary.get('title') or summary.get('id'), 34)
                lines.append(f'  {summary.get("id")}  {summary.get("count")} child(s)  {title}')
    else:
        for index, session_lines in enumerate(subagent_lines):
            lines.extend(session_lines)
        if omitted_count:
            lines.append(f'… {omitted_count} older subagent(s) hidden to fit terminal')
    lines.append('')
    lines.append('Ctrl+C to stop. Ctrl-S for session. h for header.')
    lines = fit_frame_lines(lines, max_lines, preserve_count=preserve_count)
    return '\n'.join(lines) + '\n'


class TerminalRenderer:
    def __init__(self, output=None):
        self.output = output or sys.stdout
        self.previous = None
        self.cursor_hidden = False
        self.alternate_screen = False

    def render(self, frame):
        if frame == self.previous:
            return False
        if not self.alternate_screen:
            self.output.write('\033[?1049h')
            self.alternate_screen = True
        if not self.cursor_hidden:
            self.output.write('\033[?25l')
            self.cursor_hidden = True
        self.output.write('\033[H')
        frame_lines = frame.splitlines()
        for index, line in enumerate(frame_lines):
            self.output.write('\033[2K')
            self.output.write(line)
            if index + 1 < len(frame_lines):
                self.output.write('\n')
        self.output.write('\033[J')
        self.output.flush()
        self.previous = frame
        return True

    def close(self):
        if self.cursor_hidden:
            self.output.write('\033[?25h')
            self.cursor_hidden = False
        if self.alternate_screen:
            self.output.write('\033[?1049l')
            self.alternate_screen = False
        self.output.flush()


class KeyboardShortcuts:
    def __init__(self, input_stream=None):
        self.input = input_stream or sys.stdin
        self.fd = None
        self.original = None

    def __enter__(self):
        if not self.input.isatty():
            return self
        self.fd = self.input.fileno()
        self.original = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        mode = termios.tcgetattr(self.fd)
        mode[0] &= ~termios.IXON
        termios.tcsetattr(self.fd, termios.TCSANOW, mode)
        return self

    def read_key(self):
        if self.fd is None:
            return None
        ready, _, _ = select.select([self.input], [], [], 0)
        if not ready:
            return None
        return self.input.read(1)

    def __exit__(self, exc_type, exc, tb):
        if self.fd is not None and self.original is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.original)


def token_number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def token_dict_total(tokens):
    if not isinstance(tokens, dict):
        return 0
    cache = tokens.get('cache') or {}
    if not isinstance(cache, dict):
        cache = {}
    # OpenCode/provider token payloads can report cache.read as reused prompt
    # history, and after compaction it may be cumulative across prior steps.
    # For active-context occupancy, count fresh prompt/output plus cache writes,
    # but do not add cache reads to the displayed context usage.
    return (
        token_number(tokens.get('input'))
        + token_number(tokens.get('output'))
        + token_number(cache.get('write'))
    )


def session_token_fallback(tokens, limit):
    if not isinstance(tokens, dict):
        return None
    cache = tokens.get('cache') or {}
    if not isinstance(cache, dict):
        cache = {}
    input_tokens = token_number(tokens.get('input'))
    output_tokens = token_number(tokens.get('output'))
    cache_write = token_number(cache.get('write'))
    total = input_tokens + output_tokens + cache_write
    if total <= 0:
        return None
    if not limit or total <= limit:
        return int(total)

    # Session totals can be cumulative after restart/compaction. If the full
    # non-cache-read total is impossible for the model window, keep using any
    # bounded current-context signal rather than showing a blank usage row.
    for candidate in (cache_write + output_tokens, cache_write, input_tokens + output_tokens, input_tokens):
        if candidate > 0 and candidate <= limit:
            return int(candidate)
    # If every non-cache-read signal is cumulative/over-limit after restart,
    # still show bounded stale usage instead of an empty pending row.
    return int(limit)


def display_used_tokens(used, limit):
    if used is None:
        return None
    used = int(used)
    if limit and used > limit:
        return int(limit)
    return used


def current_step_usage(messages):
    """Read the latest assistant record from OpenCode 2 active context.

    A completed model response reports input tokens for that step. Cache reads
    can be reused/cumulative history after compaction, so they are not counted
    as active occupancy. Output and reasoning may be included in the
    approximation of occupancy at the end of the step. Never sum steps.
    """
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        info = msg.get('info', msg)
        if not isinstance(info, dict) or info.get('type', info.get('role')) != 'assistant':
            continue
        tok = info.get('tokens')
        if not isinstance(tok, dict):
            continue
        # Input prompt + cache writes, plus generated output. Reasoning may
        # already be included in output on some providers: avoid double count.
        used = token_dict_total(tok)
        if used > 0:
            return int(used), info
    return None, None


def session_output_lines(
    client,
    session,
    catalog,
    fallback,
    active,
    label=None,
    state_override=None,
    row_options=None,
    highlight_stopped=False,
):
    sid = session.get('id')
    if not sid:
        return []
    if label and len(session) == 1:
        fetched = None
        try:
            fetched = client.get(session_path(sid))
        except RuntimeError:
            fetched = opencode_api_get(session_path(sid))
        if isinstance(fetched, dict):
            session = fetched
    options = row_options or RowOptions()
    name = session_display_name(
        session,
        label,
        abbreviate_agent=options.abbreviate_agent,
        agent_label_width=options.agent_label_width,
    )
    path = session_path(sid)
    # /context includes active messages after compaction. /message is
    # paginated (often 50 items), and must not be used for latest usage.
    try:
        messages = extract_messages(client.get(path + '/context'))
    except RuntimeError:
        messages = []
    context_state = infer_context_state(messages)
    if state_override and compact_state(context_state) not in WAITING_STATES:
        state = state_override
    else:
        state = session_wait_state(client, path, active, session, context_state)
    if label is None:
        name = f'{row_indicator_placeholder(state)} {name}'
    used, usage_info = current_step_usage(messages)
    provider, model_id = session_model(session)
    if (not provider or not model_id) and isinstance(usage_info, dict):
        provider = usage_info.get('providerID')
        model_id = usage_info.get('modelID')
        ref = usage_info.get('model')
        if isinstance(ref, dict):
            provider = ref.get('providerID') or provider
            model_id = ref.get('id') or model_id
    limit, source = context_limit(catalog, provider, model_id, fallback)
    if used is None:
        used = session_token_fallback(session.get('tokens'), limit)
    return format_session_lines(
        name,
        state,
        used,
        limit,
        source,
        provider,
        model_id,
        highlight_idle=(label == 'Parent session'),
        highlight_stopped=highlight_stopped or bool(label and label.startswith(PARENT_INDICATOR_PLACEHOLDER + ' ')),
        row_options=row_options,
        date=session_started_date(session),
        clock=session_started_time(session),
    )


def session_started_date(session):
    started = session_started_at(session)
    return started.strftime('%Y-%m-%d') if started else ''


def session_started_time(session):
    started = session_started_at(session)
    return started.strftime('%H:%M:%S') if started else ''


def session_started_at(session):
    time_info = session.get('time') if isinstance(session, dict) else None
    created = time_info.get('created') if isinstance(time_info, dict) else None
    if isinstance(created, bool) or not isinstance(created, (int, float)) or created <= 0:
        return ''
    try:
        return datetime.fromtimestamp(created / 1000)
    except (OverflowError, OSError, ValueError):
        return ''


def agent_display_name(agent, abbreviate=False):
    text = str(agent or '').strip()
    if not text:
        return ''
    words = re.split(r'([-_/])', text)
    parts = [part for part in words if part and part not in '-_/']
    if abbreviate:
        return ''.join(part[0].upper() for part in parts if part)
    return ''.join(part.capitalize() if part not in '-_/' else part for part in words)


def session_display_name(session, label=None, abbreviate_agent=False, agent_label_width=0):
    if label:
        return str(label)
    title = str(session.get('title') or session.get('id') or '')
    agent = agent_display_name(session.get('agent'), abbreviate=abbreviate_agent) if session.get('parentID') else ''
    if not agent:
        return title
    raw_prefix = f'[{agent}]'
    if agent_label_width:
        inner_width = max(0, agent_label_width - 2)
        prefix = f'[{agent:<{inner_width}}]'
    else:
        prefix = raw_prefix
    if title.startswith(raw_prefix):
        return title
    return f'{prefix} {title}' if title else prefix


def visible_agent_label_width(sessions, abbreviate_agent):
    labels = []
    for session in sessions:
        if not isinstance(session, dict) or not session.get('parentID'):
            continue
        agent = agent_display_name(session.get('agent'), abbreviate=abbreviate_agent)
        if agent:
            labels.append(len(f'[{agent}]'))
    return max(labels, default=0)


def get_session_info(client, sid):
    path = session_path(sid)
    try:
        fetched = client.get(path)
    except RuntimeError:
        fetched = opencode_api_get(path)
    return fetched if isinstance(fetched, dict) else {'id': sid}


def show_session(client, session, catalog, fallback, active, label=None, state_override=None, row_options=None):
    for line in session_output_lines(client, session, catalog, fallback, active, label, state_override, row_options):
        print(line)


def collect_display_snapshot(
    client,
    parent,
    fallback,
    row_options=None,
    right_margin=RIGHT_MARGIN,
    name_boost=NAME_COLUMN_BOOST,
    profile_render=False,
):
    options = row_options or RowOptions()
    sessions = child_sessions(client, parent)
    catalog = model_catalog(client)
    active = active_session_ids(client)
    parent_session = get_session_info(client, parent)
    terminal_size = shutil.get_terminal_size((80, 24))
    terminal_height = terminal_size.lines
    options = row_options_with_width(options, terminal_size.columns, right_margin=right_margin, name_boost=name_boost)
    max_subagents = max_visible_subagents(terminal_height)
    if options.full_model:
        max_subagents //= 2
    visible_sessions, omitted_count = select_visible_subagents(sessions, active, max_subagents)
    options = replace(options, agent_label_width=visible_agent_label_width(visible_sessions, options.abbreviate_agent))
    child_states = []
    child_state_by_id = {}
    for session in visible_sessions:
        sid = session.get('id')
        if not sid:
            continue
        path = session_path(sid)
        state = session_wait_state(client, path, active, session, None)
        child_states.append(state)
        child_state_by_id[sid] = state
    parent_path = session_path(parent)
    parent_own_state = session_wait_state(client, parent_path, active, parent_session, None)
    parent_state = parent_own_state if compact_state(parent_own_state) in WAITING_STATES else parent_display_state(parent, active, parent_session, child_states)
    parent_lines = session_output_lines(
        client,
        parent_session,
        catalog,
        fallback,
        active,
        label=f'{PARENT_INDICATOR_PLACEHOLDER} {parent}',
        state_override=parent_state,
        row_options=options,
    )
    subagent_lines = [
        session_output_lines(
            client,
            session,
            catalog,
            fallback,
            active,
            state_override=child_state_by_id.get(session.get('id')),
            row_options=options,
        )
        for session in visible_sessions
    ]
    alternates = parent_summaries_with_children(client, parent) if not sessions else None
    return DisplaySnapshot(render_frame(
        parent,
        parent_lines,
        subagent_lines,
        omitted_count=omitted_count,
        alternate_parents=alternates,
        max_lines=terminal_height,
        row_options=options,
        performance_line=PERFORMANCE_PLACEHOLDER if profile_render else None,
        terminal_width=terminal_size.columns,
    ), parent_state)


def display(client, parent, fallback, renderer, row_options=None, right_margin=RIGHT_MARGIN, name_boost=NAME_COLUMN_BOOST):
    snapshot = collect_display_snapshot(client, parent, fallback, row_options, right_margin, name_boost)
    renderer.render(render_snapshot(snapshot))


def start_local_server(args, port=None):
    """Start a private server with a password known to this process.

    OpenCode may generate and print a password, but capturing that terminal
    output is fragile. Instead we generate a strong password and pass it to
    the child process using the documented environment variable.
    """
    if args.host not in ('127.0.0.1', 'localhost'):
        raise RuntimeError('Automatic server startup is restricted to localhost')
    port = args.port if port is None else port
    password = secrets.token_urlsafe(36)
    env = os.environ.copy()
    env['OPENCODE_SERVER_PASSWORD'] = password
    command = ['opencode', 'serve', '--hostname', args.host, '--port', str(port)]
    try:
        proc = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise RuntimeError('opencode executable not found in PATH') from exc
    client = Client(f'http://{args.host}:{port}', password)
    for _ in range(60):
        if proc.poll() is not None:
            detail = proc.stderr.read().decode(errors='replace')[-700:]
            raise RuntimeError(f'OpenCode server exited: {detail}')
        try:
            client.get('/api/session')
            return proc, client
        except RuntimeError as exc:
            if 'HTTP 401' in str(exc):
                proc.terminate()
                raise RuntimeError('Port is occupied by another authenticated server. '
                                   'Use --port 4097 or provide OPENCODE_SERVER_PASSWORD.') from exc
            time.sleep(0.2)
    proc.terminate()
    raise RuntimeError('Timed out waiting for OpenCode API server')


def start_local_server_with_retries(args):
    start_port = args.port
    last_error = None
    for offset in range(args.port_attempts):
        attempted_port = start_port + offset
        print(f'Starting private OpenCode server on {args.host}:{attempted_port}...')
        try:
            return start_local_server(args, port=attempted_port)
        except RuntimeError as exc:
            last_error = exc
            if 'Port is occupied by another authenticated server' not in str(exc):
                raise
            if offset + 1 < args.port_attempts:
                print(f'Port {attempted_port} is occupied by an authenticated server; trying {attempted_port + 1}...')
    raise RuntimeError(f'Could not start private OpenCode server after {args.port_attempts} ports: {last_error}')


def stop_server(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=4)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def main():
    args = arguments()
    proc = None
    try:
        password = os.getenv('OPENCODE_SERVER_PASSWORD')
        if password is None:
            proc, client = start_local_server_with_retries(args)
        else:
            client = Client(f'http://{args.host}:{args.port}', password)
            client.get('/api/session')
        if args.session is None:
            args.session = choose_session(client)
        row_options = row_options_from_args(args)
        performance = PerformanceMonitor() if args.profile_render else None
        while True:
            renderer = TerminalRenderer()
            snapshot = collect_display_snapshot(
                client,
                args.session,
                args.limit,
                row_options=row_options,
                right_margin=args.right_margin,
                name_boost=args.name_boost,
                profile_render=args.profile_render,
            )
            poller = SnapshotPoller(args.interval, performance=performance)
            with KeyboardShortcuts() as keyboard:
                while True:
                    key = keyboard.read_key()
                    if key == '\x13':
                        break
                    if key:
                        row_options = toggle_row_option(row_options, key)
                    poller.request(
                        collect_display_snapshot,
                        client,
                        args.session,
                        args.limit,
                        row_options=row_options,
                        right_margin=args.right_margin,
                        name_boost=args.name_boost,
                        profile_render=args.profile_render,
                        force=bool(key),
                    )
                    snapshot = poller.current_snapshot() or snapshot
                    error = poller.consume_error()
                    if error:
                        print(f'\nMonitor error: {error}', file=sys.stderr)
                    try:
                        started = time.monotonic()
                        renderer.render(render_snapshot(snapshot, performance.line() if performance else None))
                        if performance is not None:
                            performance.record_render(time.monotonic() - started)
                    except RuntimeError as exc:
                        print(f'\nMonitor error: {exc}', file=sys.stderr)
                    time.sleep(args.animation_interval)
            renderer.close()
            args.session = choose_session(client)
    except KeyboardInterrupt:
        print('\nMonitor stopped.')
    except RuntimeError as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    finally:
        if 'renderer' in locals():
            renderer.close()
        stop_server(proc)
    return 0


if __name__ == '__main__':
    sys.exit(main())
