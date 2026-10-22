import importlib.util
import importlib.machinery
import http.client
import io
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / 'bin' / 'octop.py'


def load_module():
    loader = importlib.machinery.SourceFileLoader('octop', str(MODULE_PATH))
    spec = importlib.util.spec_from_loader('octop', loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeClient:
    def __init__(self, responses=None, error_paths=None):
        self.responses = responses or {}
        self.error_paths = set(error_paths or [])
        self.calls = []

    def get(self, path, params=None, timeout=8):
        self.calls.append((path, params, timeout))
        key = (path, tuple(sorted((params or {}).items())))
        if path in self.error_paths or key in self.error_paths:
            raise RuntimeError(f'boom: {path}')
        return self.responses.get(key, self.responses.get(path, []))


class FakeRenderer:
    def __init__(self):
        self.frames = []

    def render(self, frame):
        self.frames.append(frame)
        return True


class OctopTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.module.ACTIVE_FALLBACK_IDS = None
        self.module.ACTIVE_FALLBACK_UPDATED = 0
        self.module.opencode_api_get = lambda path: None

    def test_client_reports_interrupted_response_as_runtime_error(self):
        class InterruptedResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                raise http.client.IncompleteRead(b'partial', 10)

        client = self.module.Client('http://127.0.0.1:4096', 'password')
        with patch.object(self.module.urllib.request, 'urlopen', return_value=InterruptedResponse()):
            with self.assertRaisesRegex(
                RuntimeError, 'Incomplete HTTP response for /api/session/ses_test/context'
            ) as caught:
                client.get('/api/session/ses_test/context')

        self.assertIsInstance(caught.exception.__cause__, http.client.IncompleteRead)

    def test_client_reports_urlopen_timeout_as_runtime_error(self):
        timeout = TimeoutError('timed out')
        client = self.module.Client('http://127.0.0.1:4096', 'password')
        with patch.object(self.module.urllib.request, 'urlopen', side_effect=timeout):
            with self.assertRaisesRegex(
                RuntimeError, r'Request timed out for /api/session/ses_test/context after 8s'
            ) as caught:
                client.get('/api/session/ses_test/context')

        self.assertIs(caught.exception.__cause__, timeout)

    def test_client_reports_urlerror_wrapped_timeout_as_runtime_error(self):
        timeout = TimeoutError('timed out')
        client = self.module.Client('http://127.0.0.1:4096', 'password')
        with patch.object(
            self.module.urllib.request,
            'urlopen',
            side_effect=self.module.urllib.error.URLError(timeout),
        ):
            with self.assertRaisesRegex(
                RuntimeError, r'Request timed out for /api/session/ses_test/context after 8s'
            ) as caught:
                client.get('/api/session/ses_test/context')

        self.assertIs(caught.exception.__cause__, timeout)

    def test_client_reports_response_read_timeout_as_runtime_error(self):
        class TimedOutResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                raise TimeoutError('timed out')

        client = self.module.Client('http://127.0.0.1:4096', 'password')
        with patch.object(self.module.urllib.request, 'urlopen', return_value=TimedOutResponse()):
            with self.assertRaisesRegex(
                RuntimeError, r'Request timed out for /api/session/ses_test/context after 8s'
            ) as caught:
                client.get('/api/session/ses_test/context')

        self.assertIsInstance(caught.exception.__cause__, TimeoutError)

    def test_active_session_ids_falls_back_to_managed_when_private_times_out(self):
        client = self.module.Client('http://127.0.0.1:4096', 'password')
        self.module.opencode_api_get = lambda path: {'ses_managed': {'type': 'running'}}

        with patch.object(
            self.module.urllib.request, 'urlopen', side_effect=TimeoutError('timed out')
        ):
            self.assertEqual(self.module.active_session_ids(client), {'ses_managed'})

    def test_active_session_ids_uses_private_service_without_managed_subprocess(self):
        client = FakeClient({'/api/session/active': {'ses_private': {'type': 'running'}}})
        self.module.opencode_api_get = lambda path: self.fail(f'unexpected managed probe: {path}')

        self.assertEqual(self.module.active_session_ids(client), {'ses_private'})

    def test_active_session_ids_falls_back_to_managed_when_private_empty(self):
        client = FakeClient({'/api/session/active': {}})
        self.module.opencode_api_get = lambda path: {'ses_managed': {'type': 'running'}}

        self.assertEqual(self.module.active_session_ids(client), {'ses_managed'})

    def test_active_session_ids_throttles_empty_private_fallback(self):
        client = FakeClient({'/api/session/active': {}})
        calls = []
        self.module.opencode_api_get = lambda path: calls.append(path) or {'ses_managed': {'type': 'running'}}

        self.assertEqual(self.module.active_session_ids(client), {'ses_managed'})
        self.assertEqual(self.module.active_session_ids(client), {'ses_managed'})

        self.assertEqual(calls, ['/api/session/active'])

    def test_session_directory_reads_location_directory(self):
        session = {'location': {'directory': '/repo'}}

        self.assertEqual(self.module.session_directory(session), '/repo')

    def test_filter_sessions_by_directory_hides_other_dirs_by_default(self):
        sessions = [
            {'id': 'here', 'location': {'directory': '/repo'}},
            {'id': 'other', 'location': {'directory': '/other'}},
        ]

        visible, hidden = self.module.filter_sessions_by_directory(sessions, '/repo', False)

        self.assertEqual([session['id'] for session in visible], ['here'])
        self.assertEqual(hidden, 1)

    def test_filter_sessions_by_directory_can_include_other_dirs(self):
        sessions = [
            {'id': 'here', 'location': {'directory': '/repo'}},
            {'id': 'other', 'location': {'directory': '/other'}},
        ]

        visible, hidden = self.module.filter_sessions_by_directory(sessions, '/repo', True)

        self.assertEqual([session['id'] for session in visible], ['here', 'other'])
        self.assertEqual(hidden, 0)

    def test_choose_session_filters_current_directory_until_toggled(self):
        sessions = [
            {
                'id': 'here',
                'title': 'Here session',
                'location': {'directory': os.getcwd()},
                'time': {'updated': 2},
            },
            {
                'id': 'other',
                'title': 'Other session',
                'location': {'directory': '/other'},
                'time': {'updated': 3},
            },
        ]
        client = FakeClient({'/api/session': {'data': sessions}})

        with patch('builtins.input', side_effect=['1']), redirect_stdout(io.StringIO()) as output:
            selected = self.module.choose_session(client)

        self.assertEqual(selected, 'here')
        self.assertIn('(1 hidden)', output.getvalue())

    def test_choose_session_toggle_allows_other_directory_selection(self):
        sessions = [
            {
                'id': 'here',
                'title': 'Here session',
                'location': {'directory': os.getcwd()},
                'time': {'updated': 2},
            },
            {
                'id': 'other',
                'title': 'Other session',
                'location': {'directory': '/other'},
                'time': {'updated': 3},
            },
        ]
        client = FakeClient({'/api/session': {'data': sessions}})

        with patch('builtins.input', side_effect=['t', '1']), redirect_stdout(io.StringIO()) as output:
            selected = self.module.choose_session(client)

        self.assertEqual(selected, 'other')
        self.assertIn('[x] show sessions from other directories', output.getvalue())

    def test_choose_session_can_export_numbered_session_to_file(self):
        sessions = [{
            'id': 'ses_export',
            'title': 'Export me',
            'location': {'directory': os.getcwd()},
            'time': {'updated': 2},
        }]
        client = FakeClient({'/api/session': {'data': sessions}})
        proc = type('Proc', (), {'returncode': 0, 'stdout': '{"id":"ses_export"}\n', 'stderr': ''})()

        with tempfile.TemporaryDirectory() as tmp:
            export_path = str(Path(tmp) / 'session.json')
            with patch('builtins.input', side_effect=[f'e 1 {export_path}', '1']), \
                    patch.object(self.module.subprocess, 'run', return_value=proc) as run, \
                    redirect_stdout(io.StringIO()) as output:
                selected = self.module.choose_session(client)
            self.assertEqual(Path(export_path).read_text(encoding='utf-8'), '{"id":"ses_export"}')

        self.assertEqual(selected, 'ses_export')
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ['opencode', 'session', 'export', 'ses_export'])
        self.assertIn('Export complete.', output.getvalue())
        self.assertIn(f'Saved to {export_path}', output.getvalue())

    def test_choose_session_import_refreshes_session_list(self):
        imported = {
            'id': 'ses_imported',
            'title': 'Imported',
            'location': {'directory': os.getcwd()},
            'time': {'updated': 3},
        }

        class RefreshingClient:
            def __init__(self):
                self.calls = 0

            def get(self, path, params=None, timeout=8):
                self.calls += 1
                if self.calls == 1:
                    return {'data': []}
                return {'data': [imported]}

        proc = type('Proc', (), {'returncode': 0, 'stdout': 'imported\n', 'stderr': ''})()

        with patch('builtins.input', side_effect=['i', '/tmp/session.json', '1']), \
                patch.object(self.module.subprocess, 'run', return_value=proc) as run, \
                redirect_stdout(io.StringIO()) as output:
            selected = self.module.choose_session(RefreshingClient())

        self.assertEqual(selected, 'ses_imported')
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ['opencode', 'session', 'import', '/tmp/session.json'])
        self.assertIn('Import complete.', output.getvalue())

    def test_choose_session_reports_export_errors(self):
        sessions = [{
            'id': 'ses_export',
            'title': 'Export me',
            'location': {'directory': os.getcwd()},
            'time': {'updated': 2},
        }]
        client = FakeClient({'/api/session': {'data': sessions}})
        proc = type('Proc', (), {'returncode': 1, 'stdout': '', 'stderr': 'boom'})()

        with patch('builtins.input', side_effect=['e 1 out.json', '1']), \
                patch.object(self.module.subprocess, 'run', return_value=proc), \
                redirect_stdout(io.StringIO()) as output:
            selected = self.module.choose_session(client)

        self.assertEqual(selected, 'ses_export')
        self.assertIn('Export error: opencode session failed: boom', output.getvalue())

    def test_active_session_ids_uses_managed_service_when_private_fails(self):
        client = FakeClient(error_paths={'/api/session/active'})
        self.module.opencode_api_get = lambda path: {'ses_managed': {'type': 'running'}}

        self.assertEqual(self.module.active_session_ids(client), {'ses_managed'})

    def test_session_wait_state_prefers_active_over_stale_succeeded_outcome(self):
        client = FakeClient()
        session = {
            'id': 'ses_parent',
            'outcome': 'succeeded',
            'time': {'created': 1, 'updated': 3, 'idle': 2},
        }

        state = self.module.session_wait_state(
            client,
            '/api/session/ses_parent',
            {'ses_parent'},
            session,
            None,
        )

        self.assertEqual(state, 'running')

    def test_pending_items_falls_back_to_managed_service(self):
        client = FakeClient(error_paths={'/api/session/ses_child/permission'})
        self.module.opencode_api_get = lambda path: {
            'data': [{'id': 'per_1', 'action': 'shell'}],
        }

        items = self.module.pending_items(client, '/api/session/ses_child', '/permission')

        self.assertEqual(items, [{'id': 'per_1', 'action': 'shell'}])

    def test_session_wait_state_uses_managed_permission_when_private_empty(self):
        client = FakeClient({
            '/api/session/ses_child/permission': {'data': []},
            '/api/session/ses_child/form': {'data': []},
        })
        calls = []

        def managed(path):
            calls.append(path)
            if path.endswith('/permission'):
                return {'data': [{'id': 'per_1', 'action': 'shell', 'resources': ['cmd']}]}
            return {'data': []}

        self.module.opencode_api_get = managed

        state = self.module.session_wait_state(
            client,
            '/api/session/ses_child',
            {'ses_child'},
            {'id': 'ses_child'},
            None,
        )

        self.assertIn('waiting for permission', state)
        self.assertIn('/api/session/ses_child/permission', calls)

    def test_session_wait_state_uses_managed_form_when_private_empty(self):
        client = FakeClient({
            '/api/session/ses_child/permission': {'data': []},
            '/api/session/ses_child/form': {'data': []},
        })

        def managed(path):
            if path.endswith('/form'):
                return {'data': [{'id': 'form_1'}]}
            return {'data': []}

        self.module.opencode_api_get = managed

        state = self.module.session_wait_state(
            client,
            '/api/session/ses_child',
            {'ses_child'},
            {'id': 'ses_child'},
            None,
        )

        self.assertEqual(state, 'waiting for input (1 form)')

    def test_pending_items_private_non_empty_wins_without_managed_probe(self):
        client = FakeClient({
            '/api/session/ses_child/permission': {'data': [{'id': 'per_private', 'action': 'read'}]},
        })
        self.module.opencode_api_get = lambda path: self.fail(f'unexpected managed probe: {path}')

        items = self.module.pending_items(client, '/api/session/ses_child', '/permission')

        self.assertEqual(items, [{'id': 'per_private', 'action': 'read'}])

    def test_session_wait_state_reports_managed_permission_request(self):
        client = FakeClient(error_paths={'/api/session/ses_child/permission'})
        self.module.opencode_api_get = lambda path: {
            'data': [{'id': 'per_1', 'action': 'shell', 'resources': ['cmd']}],
        } if path.endswith('/permission') else {'data': []}

        state = self.module.session_wait_state(
            client,
            '/api/session/ses_child',
            set(),
            {'id': 'ses_child'},
            None,
        )

        self.assertIn('waiting for permission', state)

    def test_session_wait_state_skips_pending_probe_for_finished_session(self):
        client = FakeClient()
        self.module.opencode_api_get = lambda path: self.fail(f'unexpected managed probe: {path}')

        state = self.module.session_wait_state(
            client,
            '/api/session/ses_child',
            set(),
            {
                'id': 'ses_child',
                'outcome': 'succeeded',
                'time': {'created': 1, 'updated': 2, 'idle': 3},
            },
            None,
        )

        self.assertEqual(state, 'succeeded')
        self.assertFalse(any(call[0].endswith('/permission') for call in client.calls))

    def test_session_wait_state_checks_pending_for_active_session(self):
        client = FakeClient({
            '/api/session/ses_child/permission': {'data': [{'id': 'per_1', 'action': 'shell'}]},
        })

        state = self.module.session_wait_state(
            client,
            '/api/session/ses_child',
            {'ses_child'},
            {'id': 'ses_child'},
            None,
        )

        self.assertIn('waiting for permission', state)

    def test_session_wait_state_uses_context_pending_tool_without_endpoint_items(self):
        client = FakeClient({
            '/api/session/ses_child/permission': {'data': []},
            '/api/session/ses_child/form': {'data': []},
        })
        messages = [{
            'info': {
                'status': 'running',
                'parts': [{
                    'type': 'tool',
                    'tool': 'shell',
                    'state': {'status': 'pending', 'input': {'command': 'ls'}, 'raw': '{}'},
                }],
            },
        }]

        state = self.module.session_wait_state(
            client,
            '/api/session/ses_child',
            {'ses_child'},
            {'id': 'ses_child'},
            self.module.infer_context_state(messages),
        )

        self.assertEqual(self.module.compact_state(state), 'permission')
        self.assertIn('shell', state)

    def test_infer_context_state_detects_pending_tool_content(self):
        messages = [{
            'type': 'assistant',
            'content': [{
                'type': 'tool',
                'name': 'shell',
                'state': {'status': 'pending', 'input': {'command': 'ls'}},
            }],
        }]

        state = self.module.infer_context_state(messages)

        self.assertEqual(self.module.compact_state(state), 'permission')
        self.assertIn('shell', state)

    def test_session_wait_state_uses_session_waiting_status_without_endpoint_items(self):
        client = FakeClient({
            '/api/session/ses_child/permission': {'data': []},
            '/api/session/ses_child/form': {'data': []},
        })

        state = self.module.session_wait_state(
            client,
            '/api/session/ses_child',
            set(),
            {'id': 'ses_child', 'status': {'type': 'permission_required'}},
            None,
        )

        self.assertEqual(state, 'permission')

    def test_session_output_lines_styles_context_pending_tool_as_waiting(self):
        client = FakeClient({
            '/api/session/ses_child/context': [{
                'info': {
                    'type': 'assistant',
                    'parts': [{
                        'type': 'tool',
                        'tool': 'shell',
                        'state': {'status': 'pending', 'input': {'command': 'pwd'}, 'raw': '{}'},
                    }],
                },
            }],
            '/api/session/ses_child/permission': {'data': []},
            '/api/session/ses_child/form': {'data': []},
        })

        lines = self.module.session_output_lines(
            client,
            {'id': 'ses_child', 'parentID': 'ses_parent', 'title': 'Child'},
            {},
            0,
            {'ses_child'},
            row_options=self.module.RowOptions(show_status=True, show_model=False),
        )

        self.assertTrue(lines[0].startswith('\033[30;41m'))
        self.assertIn('[permission ', lines[0])
        self.assertIn(('/api/session/ses_child/permission', None, 1), client.calls)
        self.assertIn(('/api/session/ses_child/form', None, 1), client.calls)

    def test_session_output_lines_uses_context_waiting_over_running_override(self):
        client = FakeClient({
            '/api/session/ses_child/context': [{
                'type': 'assistant',
                'content': [{
                    'type': 'tool',
                    'name': 'shell',
                    'state': {'status': 'pending', 'input': {'command': 'pwd'}},
                }],
            }],
            '/api/session/ses_child/permission': {'data': []},
            '/api/session/ses_child/form': {'data': []},
        })

        lines = self.module.session_output_lines(
            client,
            {'id': 'ses_child', 'parentID': 'ses_parent', 'title': 'Child'},
            {},
            0,
            {'ses_child'},
            state_override='running',
            row_options=self.module.RowOptions(show_status=True, show_model=False),
        )

        self.assertTrue(lines[0].startswith('\033[30;41m'))
        self.assertIn('[permission ', lines[0])

    def test_compact_state_keeps_permission_and_input_visible(self):
        self.assertEqual(self.module.compact_state('waiting for permission (1): shell'), 'permission')
        self.assertEqual(self.module.compact_state('waiting for input (1 form)'), 'input')
        self.assertEqual(self.module.compact_state('waiting for tool'), 'waiting')

    def test_style_waiting_line_uses_black_on_red(self):
        styled = self.module.style_session_line('row', 'permission')

        self.assertEqual(styled, '\033[30;41mrow\033[0m')

    def test_style_session_line_leaves_non_waiting_rows_plain(self):
        self.assertEqual(self.module.style_session_line('row', 'running'), 'row')

    def test_style_parent_idle_line_uses_orange(self):
        styled = self.module.style_session_line('row', 'idle', highlight_idle=True)

        self.assertEqual(styled, '\033[30;43mrow\033[0m')

    def test_style_parent_stopped_line_uses_red(self):
        styled = self.module.style_session_line('row', 'stopped', highlight_stopped=True)

        self.assertEqual(styled, '\033[31mrow\033[0m')

    def test_style_subagent_idle_line_stays_plain(self):
        self.assertEqual(self.module.style_session_line('row', 'idle'), 'row')

    def test_keyboard_shortcuts_toggle_row_options(self):
        options = self.module.RowOptions()

        options = self.module.toggle_row_option(options, 's')
        self.assertTrue(options.show_status)
        options = self.module.toggle_row_option(options, 'h')
        self.assertTrue(options.show_header)
        options = self.module.toggle_row_option(options, 'b')
        self.assertFalse(options.show_bar)
        options = self.module.toggle_row_option(options, 'p')
        self.assertTrue(options.show_percent)
        options = self.module.toggle_row_option(options, 't')
        self.assertTrue(options.show_tokens)
        options = self.module.toggle_row_option(options, 'm')
        self.assertFalse(options.show_model)
        options = self.module.toggle_row_option(options, 'd')
        self.assertTrue(options.show_date)
        self.assertFalse(options.show_time)
        options = self.module.toggle_row_option(options, 'c')
        self.assertTrue(options.show_time)
        options = self.module.toggle_row_option(options, 'f')
        self.assertTrue(options.full_title)
        options = self.module.toggle_row_option(options, 'a')
        self.assertFalse(options.abbreviate_agent)
        self.assertEqual(self.module.toggle_row_option(options, 'x'), options)

    def test_default_row_options_hide_header_and_show_bar_model(self):
        options = self.module.RowOptions()

        self.assertFalse(options.show_status)
        self.assertFalse(options.show_header)
        self.assertTrue(options.show_bar)
        self.assertTrue(options.show_model)
        self.assertFalse(options.show_date)
        self.assertFalse(options.show_time)
        self.assertFalse(options.show_percent)
        self.assertFalse(options.show_tokens)
        self.assertFalse(options.full_title)
        self.assertTrue(options.abbreviate_agent)

    def test_date_and_time_start_hidden_unless_requested(self):
        with patch('sys.argv', ['octop']):
            options = self.module.row_options_from_args(self.module.arguments())
            self.assertFalse(options.show_date)
            self.assertFalse(options.show_time)
        with patch('sys.argv', ['octop', '--show-date', '--show-time']):
            options = self.module.row_options_from_args(self.module.arguments())
            self.assertTrue(options.show_date)
            self.assertTrue(options.show_time)

    def test_column_status_reports_visible_parts(self):
        status = self.module.column_status(self.module.RowOptions(show_status=False, show_bar=False, show_percent=True, show_tokens=False, show_model=True))

        self.assertEqual(status, 'status:off bar:off pct:on tokens:off model:on date:off time:off title:short agent:abbr')

    def test_short_model_name_compacts_common_prefixes(self):
        self.assertEqual(self.module.short_model_name('lms', 'qwen3.8-27b'), 'qwen3.8-27b')

    def test_short_model_name_has_room_for_longer_models(self):
        self.assertEqual(self.module.short_model_name('anthropic', 'claude-sonnet-4.5'), 'sonnet-4.5')

    def test_current_step_usage_ignores_large_cache_read_after_compaction(self):
        messages = [{
            'info': {
                'type': 'assistant',
                'tokens': {
                    'input': 24000,
                    'output': 19,
                    'cache': {'read': 6222954, 'write': 0},
                },
            },
        }]

        used, _ = self.module.current_step_usage(messages)

        self.assertEqual(used, 24019)

    def test_parent_model_uses_session_selection_not_subagent_usage_model(self):
        local_message = [{
            'info': {
                'type': 'assistant',
                'model': {'providerID': 'lms', 'id': 'qwen3.8-27b'},
                'tokens': {'input': 1000, 'output': 20},
            },
        }]
        client = FakeClient({
            '/api/session/ses_parent/context': local_message,
            '/api/session/ses_child/context': local_message,
        })
        catalog = {
            ('openai', 'gpt-6-sol'): {'limit': {'context': 200000}},
            ('lms', 'qwen3.8-27b'): {'limit': {'context': 82000}},
        }
        options = self.module.RowOptions(show_tokens=True, row_width=120)
        parent = {'id': 'ses_parent', 'model': {'providerID': 'openai', 'id': 'gpt-6-sol'}}
        child = {'id': 'ses_child', 'parentID': 'ses_parent',
                 'model': {'providerID': 'lms', 'id': 'qwen3.8-27b'}}

        parent_line = self.module.ANSI_RE.sub('', self.module.session_output_lines(
            client, parent, catalog, 0, set(), label='Parent', row_options=options,
        )[0])
        child_line = self.module.ANSI_RE.sub('', self.module.session_output_lines(
            client, child, catalog, 0, set(), row_options=options,
        )[0])

        self.assertIn('gpt-6-sol', parent_line)
        self.assertNotIn('qwen3.8-27b', parent_line)
        self.assertIn('qwen3.8-27b', child_line)

    def test_session_model_falls_back_to_context_when_selection_missing(self):
        client = FakeClient({'/api/session/ses_parent/context': [{
            'info': {
                'type': 'assistant',
                'model': {'providerID': 'openai', 'id': 'gpt-6-sol'},
                'tokens': {'input': 1000, 'output': 20},
            },
        }]})
        line = self.module.ANSI_RE.sub('', self.module.session_output_lines(
            client, {'id': 'ses_parent'}, {}, 0, set(),
            row_options=self.module.RowOptions(row_width=120),
        )[0])

        self.assertIn('gpt-6-sol', line)

    def test_session_output_uses_context_usage_without_cache_read_overshoot(self):
        client = FakeClient({
            '/api/session/ses_parent/context': [{
                'info': {
                    'type': 'assistant',
                    'model': {'providerID': 'lms', 'id': 'qwen3.8-27b'},
                    'tokens': {
                        'input': 24000,
                        'output': 19,
                        'cache': {'read': 6222954, 'write': 0},
                    },
                },
            }],
        })
        catalog = {('lms', 'qwen3.8-27b'): {'limit': {'context': 82000}}}

        line = self.module.ANSI_RE.sub('', self.module.session_output_lines(
            client,
            {'id': 'ses_parent', 'title': 'Parent'},
            catalog,
            0,
            {'ses_parent'},
            row_options=self.module.RowOptions(show_percent=True, show_tokens=True, show_model=False, row_width=120),
        )[0])

        self.assertIn(' 29.3%', line)
        self.assertIn('24,019/82,000', line)
        self.assertNotIn('6,246,973', line)

    def test_session_output_clamps_cumulative_session_tokens_over_limit(self):
        client = FakeClient({'/api/session/ses_parent/context': []})
        catalog = {('lms', 'qwen3.8-27b'): {'limit': {'context': 82000}}}

        line = self.module.ANSI_RE.sub('', self.module.session_output_lines(
            client,
            {
                'id': 'ses_parent',
                'title': 'Parent',
                'model': {'providerID': 'lms', 'id': 'qwen3.8-27b'},
                'tokens': {'input': 6246973, 'output': 0, 'cache': {'read': 0, 'write': 0}},
            },
            catalog,
            0,
            set(),
            row_options=self.module.RowOptions(show_percent=True, show_tokens=True, show_model=False, row_width=120),
        )[0])

        self.assertIn('100.0%', line)
        self.assertIn('82,000/82,000', line)
        self.assertNotIn('usage pending', line)
        self.assertNotIn('6,246,973/82,000', line)
        self.assertNotIn('7618.3%', line)

    def test_session_output_uses_session_tokens_without_cache_read_after_restart(self):
        client = FakeClient({'/api/session/ses_parent/context': []})
        catalog = {('lms', 'qwen3.8-27b'): {'limit': {'context': 82000}}}

        line = self.module.ANSI_RE.sub('', self.module.session_output_lines(
            client,
            {
                'id': 'ses_parent',
                'title': 'Parent',
                'model': {'providerID': 'lms', 'id': 'qwen3.8-27b'},
                'tokens': {'input': 24000, 'output': 19, 'cache': {'read': 6222954, 'write': 1000}},
            },
            catalog,
            0,
            set(),
            row_options=self.module.RowOptions(show_percent=True, show_tokens=True, show_model=False, row_width=120),
        )[0])

        self.assertIn(' 30.5%', line)
        self.assertIn('25,019/82,000', line)
        self.assertNotIn('6,247,973', line)
        self.assertNotIn('usage pending', line)

    def test_session_output_uses_bounded_cache_write_when_session_input_is_cumulative(self):
        client = FakeClient({'/api/session/ses_parent/context': []})
        catalog = {('lms', 'qwen3.8-27b'): {'limit': {'context': 82000}}}

        line = self.module.ANSI_RE.sub('', self.module.session_output_lines(
            client,
            {
                'id': 'ses_parent',
                'title': 'Parent',
                'model': {'providerID': 'lms', 'id': 'qwen3.8-27b'},
                'tokens': {'input': 6246973, 'output': 19, 'cache': {'read': 0, 'write': 24000}},
            },
            catalog,
            0,
            set(),
            row_options=self.module.RowOptions(show_percent=True, show_tokens=True, show_model=False, row_width=120),
        )[0])

        self.assertIn(' 29.3%', line)
        self.assertIn('24,019/82,000', line)
        self.assertNotIn('6,270,992/82,000', line)
        self.assertNotIn('usage pending', line)

    def test_format_session_lines_clamps_display_to_context_limit(self):
        line = self.module.ANSI_RE.sub('', self.module.format_session_lines(
            'Parent',
            'running',
            6246973,
            82000,
            'catalog',
            'lms',
            'qwen3.8-27b',
            row_options=self.module.RowOptions(show_percent=True, show_tokens=True, show_model=False, row_width=120),
        )[0])

        self.assertIn('100.0%', line)
        self.assertIn('82,000/82,000', line)
        self.assertNotIn('7618.3%', line)

    def test_session_display_name_adds_subagent_agent_name(self):
        session = {
            'id': 'ses_child',
            'parentID': 'ses_parent',
            'agent': 'test-automation-engineer',
            'title': 'Fix probe defects, re-run for red',
        }

        self.assertEqual(
            self.module.session_display_name(session),
            '[Test-Automation-Engineer] Fix probe defects, re-run for red',
        )

    def test_session_display_name_can_abbreviate_subagent_agent_name(self):
        session = {
            'id': 'ses_child',
            'parentID': 'ses_parent',
            'agent': 'test-automation-engineer',
            'title': 'Fix probe defects, re-run for red',
        }

        self.assertEqual(
            self.module.session_display_name(session, abbreviate_agent=True),
            '[TAE] Fix probe defects, re-run for red',
        )

    def test_session_display_name_pads_abbreviated_agent_column(self):
        session = {
            'id': 'ses_child',
            'parentID': 'ses_parent',
            'agent': 'coder',
            'title': 'Implement fix',
        }

        self.assertEqual(
            self.module.session_display_name(session, abbreviate_agent=True, agent_label_width=5),
            '[C  ] Implement fix',
        )

    def test_visible_agent_label_width_uses_widest_visible_subagent_label(self):
        sessions = [
            {'id': 'one', 'parentID': 'parent', 'agent': 'coder'},
            {'id': 'two', 'parentID': 'parent', 'agent': 'test-automation-engineer'},
            {'id': 'parent', 'agent': 'conductor'},
        ]

        self.assertEqual(self.module.visible_agent_label_width(sessions, abbreviate_agent=True), len('[TAE]'))

    def test_session_display_name_leaves_parent_title_plain(self):
        session = {
            'id': 'ses_parent',
            'agent': 'conductor',
            'title': 'Plan next phase',
        }

        self.assertEqual(self.module.session_display_name(session), 'Plan next phase')

    def test_session_lifecycle_treats_updated_after_idle_as_running(self):
        session = {
            'id': 'ses_parent',
            'outcome': 'succeeded',
            'time': {'created': 1, 'updated': 3, 'idle': 2},
        }

        self.assertEqual(self.module.session_lifecycle_state(session), 'running')

    def test_parent_display_state_marks_idle_parent_idle_despite_succeeded_outcome(self):
        parent = {
            'id': 'ses_parent',
            'outcome': 'succeeded',
            'time': {'created': 1, 'updated': 3, 'idle': 4},
        }

        state = self.module.parent_display_state('ses_parent', set(), parent, [])

        self.assertEqual(state, 'idle')

    def test_parent_display_state_uses_running_child_for_parent_spinner(self):
        parent = {
            'id': 'ses_parent',
            'outcome': 'succeeded',
            'time': {'created': 1, 'updated': 3, 'idle': 4},
        }

        state = self.module.parent_display_state('ses_parent', set(), parent, ['running'])

        self.assertEqual(state, 'running')

    def test_parent_display_state_marks_waiting_child_as_idle_parent(self):
        parent = {'id': 'ses_parent', 'time': {'created': 1, 'updated': 3, 'idle': 4}}

        state = self.module.parent_display_state('ses_parent', set(), parent, ['permission'])

        self.assertEqual(state, 'idle')

    def test_parent_display_state_keeps_active_parent_running(self):
        parent = {'id': 'ses_parent', 'time': {'created': 1, 'updated': 3, 'idle': 4}}

        state = self.module.parent_display_state('ses_parent', {'ses_parent'}, parent, [])

        self.assertEqual(state, 'running')

    def test_display_parent_pending_form_overrides_running_child(self):
        child = {'id': 'child', 'parentID': 'parent', 'title': 'Child'}
        client = FakeClient({
            '/api/session/parent/form': {'data': [{'id': 'form-1'}]},
        })

        self.module.child_sessions = lambda client, parent: [child]
        self.module.model_catalog = lambda client: {}
        self.module.active_session_ids = lambda client: {'child'}
        self.module.max_visible_subagents = lambda height: 10
        self.module.session_output_lines = lambda client, session, catalog, fallback, active, label=None, state_override=None, **kwargs: [state_override or 'no-state']
        renderer = FakeRenderer()

        self.module.display(client, 'parent', 0, renderer)

        self.assertIn('input', renderer.frames[0])

    def test_child_sessions_reads_data_wrapped_parent_response(self):
        parent = 'ses_parent'
        child = {'id': 'ses_child', 'parentID': parent, 'title': 'child'}
        client = FakeClient({
            ('/api/session', (('limit', '200'), ('parentID', parent))): {'data': [child]},
        })

        self.assertEqual(self.module.child_sessions(client, parent), [child])

    def test_child_sessions_falls_back_to_managed_parent_query(self):
        parent = 'ses_parent'
        child = {'id': 'ses_child', 'parentID': parent, 'title': 'child'}
        client = FakeClient()
        self.module.opencode_api_get = lambda path: {'data': [child]} if 'parentID=ses_parent' in path else None

        self.assertEqual(self.module.child_sessions(client, parent), [child])

    def test_parent_summaries_with_children_suggests_other_recent_parent(self):
        sessions = {
            'data': [
                {'id': 'selected', 'title': 'Selected parent', 'time': {'updated': 10}},
                {'id': 'other', 'title': 'Other parent', 'time': {'updated': 20}},
                {'id': 'child-a', 'parentID': 'other', 'time': {'updated': 30}},
                {'id': 'child-b', 'parentID': 'other', 'time': {'updated': 40}},
            ]
        }
        client = FakeClient()
        self.module.opencode_api_get = lambda path: sessions

        summaries = self.module.parent_summaries_with_children(client, 'selected')

        self.assertEqual(summaries, [{'id': 'other', 'title': 'Other parent', 'count': 2}])

    def test_parent_summaries_excludes_selected_parent_children(self):
        sessions = {
            'data': [
                {'id': 'selected', 'title': 'Selected parent'},
                {'id': 'selected-child', 'parentID': 'selected', 'time': {'updated': 30}},
            ]
        }
        client = FakeClient()
        self.module.opencode_api_get = lambda path: sessions

        self.assertEqual(self.module.parent_summaries_with_children(client, 'selected'), [])

    def test_format_session_lines_stays_within_80_columns(self):
        lines = self.module.format_session_lines(
            'Very long subagent title that would otherwise consume the whole row',
            'waiting for permission (2): read /a/very/long/path/example.py',
            123456,
            200000,
            'catalog',
            'very-long-provider-name',
            'very-long-model-name',
        )

        self.assertEqual(len(lines), 1)
        self.assertTrue(all(self.module.visible_width(line) <= 80 for line in lines), lines)

    def test_format_session_lines_uses_one_line_for_normal_limit_row(self):
        lines = self.module.format_session_lines(
            'Implement P1 STEP-001 requirements',
            'succeeded',
            24019,
            82000,
            'catalog',
            'lms',
            'qwen3.8-27b',
            row_options=self.module.RowOptions(show_status=True, show_date=False, show_time=False),
        )

        self.assertEqual(len(lines), 1)
        self.assertLessEqual(self.module.visible_width(lines[0]), 80)
        self.assertIn('qwen3.8-27b', lines[0])
        self.assertNotIn('24,019/82,000', lines[0])

    def test_format_session_lines_preserves_more_session_title(self):
        lines = self.module.format_session_lines(
            'STEP-001e: interior second floor rooms and stair landing',
            'running',
            9860,
            82000,
            'catalog',
            'lms',
            'qwen3.8-27b',
            row_options=self.module.RowOptions(show_status=True, show_date=False, show_time=False),
        )

        self.assertEqual(len(lines), 1)
        self.assertLessEqual(self.module.visible_width(lines[0]), 80)
        self.assertIn('STEP-001e: interior second floor', lines[0])
        self.assertNotIn('lms/qwen3.8-27b', lines[0])

    def test_format_session_lines_styles_waiting_rows(self):
        lines = self.module.format_session_lines(
            'STEP-001e: interactables flagged',
            'waiting for permission (1): shell',
            9860,
            82000,
            'catalog',
            'lms',
            'qwen3.8-27b',
            row_options=self.module.RowOptions(show_status=True),
        )

        self.assertTrue(lines[0].startswith('\033[30;41m'))
        self.assertTrue(lines[0].endswith('\033[0m'))
        self.assertIn('[permission ', lines[0])

    def test_format_session_lines_can_style_idle_parent(self):
        lines = self.module.format_session_lines(
            'Parent session',
            'idle',
            1000,
            2000,
            'catalog',
            'openai',
            'gpt-5.5',
            highlight_idle=True,
        )

        self.assertTrue(lines[0].startswith('\033[30;43m'))
        self.assertTrue(lines[0].endswith('\033[0m'))

    def test_session_output_lines_adds_active_subagent_indicator_column(self):
        session = {'id': 'child', 'parentID': 'parent', 'title': 'Child'}

        lines = self.module.session_output_lines(
            FakeClient(),
            session,
            {},
            0,
            set(),
            state_override='running',
            row_options=self.module.RowOptions(show_model=False),
        )

        self.assertTrue(lines[0].startswith(f'{self.module.ACTIVE_INDICATOR_PLACEHOLDER} Child'))

    def test_session_output_lines_keeps_blank_done_subagent_indicator_column(self):
        session = {'id': 'child', 'parentID': 'parent', 'title': 'Child'}

        lines = self.module.session_output_lines(
            FakeClient(),
            session,
            {},
            0,
            set(),
            state_override='succeeded',
            row_options=self.module.RowOptions(show_model=False),
        )

        self.assertTrue(lines[0].startswith('  Child'))

    def test_session_rows_show_start_timestamp_and_can_hide_it(self):
        created = int(datetime(2023, 12, 1, 12, 34, 56).timestamp() * 1000)
        updated = int(datetime(2024, 1, 15, 12).timestamp() * 1000)
        for label in (None, 'Parent session'):
            with self.subTest(label=label):
                session = {'id': 'child', 'title': 'Child', 'time': {'created': created, 'updated': updated}}
                kwargs = {'label': label, 'row_options': self.module.RowOptions(show_model=False, show_date=True, show_time=True)}
                lines = self.module.session_output_lines(FakeClient(), session, {}, 0, set(), **kwargs)
                self.assertIn('2023-12-01 12:34:56', lines[-1])
                self.assertNotIn('2024-01-15', lines[-1])
                kwargs['row_options'] = self.module.RowOptions(show_model=False, show_time=True)
                lines = self.module.session_output_lines(FakeClient(), session, {}, 0, set(), **kwargs)
                self.assertNotIn('2023-12-01', lines[-1])
                self.assertIn('12:34:56', lines[-1])
                kwargs['row_options'] = self.module.RowOptions(show_model=False, show_date=True)
                lines = self.module.session_output_lines(FakeClient(), session, {}, 0, set(), **kwargs)
                self.assertIn('2023-12-01', lines[-1])
                self.assertNotIn('12:34:56', lines[-1])

    def test_date_column_handles_missing_time_and_full_title(self):
        self.assertEqual(self.module.session_started_date({'time': {'updated': 1700000000000}}), '')
        self.assertEqual(self.module.session_started_date({'time': {'created': True}}), '')
        self.assertEqual(self.module.session_started_date({}), '')
        lines = self.module.format_session_lines(
            'Long session title', 'idle', None, 0, 'unknown', None, None,
            row_options=self.module.RowOptions(full_title=True, show_model=False, show_date=True, show_time=True),
            date='2023-12-01', clock='12:34:56',
        )
        self.assertEqual(lines[0], 'Long session title')
        self.assertIn('2023-12-01 12:34:56', lines[-1])

    def test_format_session_lines_uses_stable_columns(self):
        options = self.module.RowOptions(show_status=True, show_percent=True, show_tokens=True, show_model=False)
        normal = self.module.ANSI_RE.sub('', self.module.format_session_lines(
            'Short name',
            'running',
            17023,
            82000,
            'catalog',
            'lms',
            'qwen3.8-27b',
            row_options=options,
        )[0])
        long_name = self.module.ANSI_RE.sub('', self.module.format_session_lines(
            'A much longer session name that should clip predictably',
            'succeeded',
            9561,
            82000,
            'catalog',
            'lms',
            'qwen3.8-27b',
            row_options=options,
        )[0])
        pending = self.module.ANSI_RE.sub('', self.module.format_session_lines(
            'Pending usage row',
            'interrupted',
            None,
            82000,
            'catalog',
            'lms',
            'qwen3.8-27b',
            row_options=options,
        )[0])

        self.assertEqual(normal.index('['), long_name.index('['))
        self.assertEqual(normal.index('['), pending.index('['))
        self.assertEqual(normal.index('█'), long_name.index('█'))
        self.assertEqual(normal.index('█'), pending.index('usage pending') - 18)
        self.assertLessEqual(self.module.visible_width(normal), 80)
        self.assertLessEqual(self.module.visible_width(long_name), 80)
        self.assertLessEqual(self.module.visible_width(pending), 80)

    def test_format_session_lines_hiding_bar_expands_name(self):
        title = 'STEP-001e: interior second floor rooms and stair landing'
        full = self.module.ANSI_RE.sub('', self.module.format_session_lines(
            title,
            'running',
            9860,
            82000,
            'catalog',
            'lms',
            'qwen3.8-27b',
            row_options=self.module.RowOptions(show_status=True),
        )[0])
        without_bar = self.module.ANSI_RE.sub('', self.module.format_session_lines(
            title,
            'running',
            9860,
            82000,
            'catalog',
            'lms',
            'qwen3.8-27b',
            row_options=self.module.RowOptions(show_status=True, show_bar=False, show_date=False, show_time=False),
        )[0])

        self.assertNotIn('█', without_bar)
        self.assertGreater(without_bar.index('['), full.index('['))
        self.assertIn('STEP-001e: interior second floor rooms', without_bar)
        self.assertLessEqual(self.module.visible_width(without_bar), 80)

    def test_format_session_lines_can_hide_percent_and_tokens(self):
        line = self.module.ANSI_RE.sub('', self.module.format_session_lines(
            'STEP-001e: interior second floor rooms and stair landing',
            'running',
            9860,
            82000,
            'catalog',
            'lms',
            'qwen3.8-27b',
            row_options=self.module.RowOptions(show_percent=False, show_tokens=False, show_date=False, show_time=False),
        )[0])

        self.assertIn('█', line)
        self.assertNotIn('12.0%', line)
        self.assertNotIn('9,860/82,000', line)
        self.assertIn('STEP-001e: interior second floor ro', line)
        self.assertLessEqual(self.module.visible_width(line), 80)

    def test_format_session_lines_can_show_short_model(self):
        line = self.module.ANSI_RE.sub('', self.module.format_session_lines(
            'STEP-001e: interior second floor rooms and stair landing',
            'running',
            9860,
            82000,
            'catalog',
            'lms',
            'qwen3.8-27b',
            row_options=self.module.RowOptions(show_model=True),
        )[0])

        self.assertIn('qwen3.8-27b', line)
        self.assertLessEqual(self.module.visible_width(line), 80)

    def test_format_session_lines_full_title_uses_separate_detail_line(self):
        title = 'Very long subagent title that should be visible instead of clipped away'
        lines = [self.module.ANSI_RE.sub('', line) for line in self.module.format_session_lines(
            title,
            'running',
            9860,
            82000,
            'catalog',
            'lms',
            'qwen3.8-27b',
            row_options=self.module.RowOptions(show_status=True, full_title=True, row_width=48),
        )]

        self.assertGreaterEqual(len(lines), 2)
        self.assertEqual(' '.join(lines[:-1]), title)
        self.assertIn('[running', lines[-1])
        self.assertIn('qwen3.8-27b', lines[-1])
        self.assertTrue(all(self.module.visible_width(line) <= 48 for line in lines))

    def test_format_session_lines_hiding_status_expands_name(self):
        title = 'STEP-001e: interior second floor rooms and stair landing exterior balcony railing'
        full = self.module.ANSI_RE.sub('', self.module.format_session_lines(
            title,
            'running',
            9860,
            82000,
            'catalog',
            'lms',
            'qwen3.8-27b',
            row_options=self.module.RowOptions(show_status=True),
        )[0])
        without_status = self.module.ANSI_RE.sub('', self.module.format_session_lines(
            title,
            'running',
            9860,
            82000,
            'catalog',
            'lms',
            'qwen3.8-27b',
            row_options=self.module.RowOptions(show_status=False, show_date=False, show_time=False),
        )[0])

        self.assertNotIn('[running', without_status)
        self.assertIn('stair', without_status)
        self.assertNotIn('stair', full)
        self.assertLessEqual(self.module.visible_width(without_status), 80)

    def test_format_session_lines_uses_screen_width_for_name(self):
        title = 'STEP-001e: interior second floor rooms and stair landing exterior balcony railing'
        line = self.module.ANSI_RE.sub('', self.module.format_session_lines(
            title,
            'running',
            9860,
            82000,
            'catalog',
            'lms',
            'qwen3.8-27b',
            row_options=self.module.RowOptions(row_width=120, show_date=False, show_time=False),
        )[0])

        self.assertIn('exterior balcony', line)
        self.assertLessEqual(self.module.visible_width(line), 120)

    def test_format_session_lines_uses_wide_screen_to_push_columns_right(self):
        narrow = self.module.ANSI_RE.sub('', self.module.format_session_lines(
            'Short name',
            'running',
            41000,
            82000,
            'catalog',
            'lms',
            'qwen3.8-27b',
            row_options=self.module.RowOptions(show_status=True, row_width=80),
        )[0])
        line = self.module.ANSI_RE.sub('', self.module.format_session_lines(
            'Short name',
            'running',
            41000,
            82000,
            'catalog',
            'lms',
            'qwen3.8-27b',
            row_options=self.module.RowOptions(show_status=True, row_width=140),
        )[0])

        self.assertEqual(line.count('█') + line.count('░'), 10)
        self.assertGreater(line.index('[running'), narrow.index('[running'))
        self.assertIn('qwen3.8-27b', line)
        self.assertLessEqual(self.module.visible_width(line), 140)

    def test_render_frame_has_content_without_terminal_control_codes(self):
        frame = self.module.render_frame(
            'ses_parent',
            ['parent line'],
            [['child line', '  detail']],
            now='12:34:56',
            row_options=self.module.RowOptions(show_header=True),
        )

        self.assertIn('Parent: ses_parent', frame)
        self.assertIn('parent line', frame)
        self.assertIn('child line', frame)
        self.assertNotIn('\033[2J', frame)
        self.assertNotIn('\033[H', frame)

    def test_render_snapshot_animates_running_parent_indicator(self):
        snapshot = self.module.DisplaySnapshot(
            f'{self.module.PARENT_INDICATOR_PLACEHOLDER} ses_parent\n',
            'running',
        )

        with patch.object(self.module.time, 'monotonic', return_value=0):
            first = self.module.render_snapshot(snapshot)
        with patch.object(self.module.time, 'monotonic', return_value=0.3):
            second = self.module.render_snapshot(snapshot)

        self.assertIn('⠋ ses_parent', first)
        self.assertIn('⠙ ses_parent', second)

    def test_render_snapshot_animates_compacting_parent_indicator(self):
        snapshot = self.module.DisplaySnapshot(
            f'{self.module.PARENT_INDICATOR_PLACEHOLDER} ses_parent\n',
            'compacting',
        )

        with patch.object(self.module.time, 'monotonic', return_value=0):
            frame = self.module.render_snapshot(snapshot)

        self.assertIn('⠋ ses_parent', frame)

    def test_render_snapshot_animates_active_subagent_indicators(self):
        snapshot = self.module.DisplaySnapshot(
            f'{self.module.ACTIVE_INDICATOR_PLACEHOLDER} child\n  done\n',
            'idle',
        )

        with patch.object(self.module.time, 'monotonic', return_value=0):
            frame = self.module.render_snapshot(snapshot)

        self.assertIn('⠋ child', frame)
        self.assertIn('  done', frame)

    def test_infer_context_state_detects_compacting(self):
        messages = [{'info': {'status': 'compacting'}}]

        self.assertEqual(self.module.infer_context_state(messages), 'compacting')

    def test_render_snapshot_uses_filled_indicator_for_idle_parent(self):
        snapshot = self.module.DisplaySnapshot(
            f'{self.module.PARENT_INDICATOR_PLACEHOLDER} ses_parent\n',
            'idle',
        )

        self.assertIn('⣿ ses_parent', self.module.render_snapshot(snapshot))

    def test_render_snapshot_can_replace_performance_line(self):
        snapshot = self.module.DisplaySnapshot(
            f'{self.module.PARENT_INDICATOR_PLACEHOLDER} ses_parent\n{self.module.PERFORMANCE_PLACEHOLDER}\n',
            'idle',
        )

        frame = self.module.render_snapshot(snapshot, 'Perf: cpu  0.1%')

        self.assertIn('Perf: cpu  0.1%', frame)
        self.assertNotIn(self.module.PERFORMANCE_PLACEHOLDER, frame)

    def test_render_frame_keeps_performance_line_in_short_terminal(self):
        frame = self.module.render_frame(
            'ses_parent',
            ['parent line'],
            [[f'child {i}'] for i in range(10)],
            now='12:34:56',
            max_lines=7,
            performance_line='Perf: cpu  0.1%',
            row_options=self.module.RowOptions(show_header=True),
        )

        self.assertIn('Perf: cpu  0.1%', frame)

    def test_performance_monitor_reports_render_and_poll_timing(self):
        monitor = self.module.PerformanceMonitor()

        monitor.record_render(0.001)
        monitor.record_poll(0.2)
        line = monitor.line()

        self.assertIn('Perf: cpu', line)
        self.assertIn('render avg  1.0ms', line)
        self.assertIn('poll avg 200.0ms', line)

    def test_snapshot_poller_request_does_not_block_on_slow_poll(self):
        poller = self.module.SnapshotPoller(interval=10)
        started = threading.Event()
        release = threading.Event()

        def collect():
            started.set()
            release.wait(timeout=1)
            return self.module.DisplaySnapshot('frame', 'idle')

        before = time.monotonic()
        self.assertTrue(poller.request(collect))
        elapsed = time.monotonic() - before

        self.assertLess(elapsed, 0.1)
        self.assertTrue(started.wait(timeout=1))
        self.assertIsNone(poller.current_snapshot())
        release.set()
        for _ in range(20):
            if poller.current_snapshot() is not None:
                break
            time.sleep(0.01)
        self.assertEqual(poller.current_snapshot(), self.module.DisplaySnapshot('frame', 'idle'))

    def test_snapshot_poller_keeps_last_snapshot_on_poll_error(self):
        poller = self.module.SnapshotPoller(interval=10)
        poller.request(lambda: self.module.DisplaySnapshot('old', 'idle'))
        for _ in range(20):
            if poller.current_snapshot() is not None:
                break
            time.sleep(0.01)

        poller.request(lambda: (_ for _ in ()).throw(RuntimeError('boom')), force=True)
        error = None
        for _ in range(20):
            error = poller.consume_error()
            if error:
                break
            time.sleep(0.01)

        self.assertEqual(poller.current_snapshot(), self.module.DisplaySnapshot('old', 'idle'))
        self.assertEqual(str(error), 'boom')

    def test_snapshot_poller_records_poll_timing(self):
        monitor = self.module.PerformanceMonitor()
        poller = self.module.SnapshotPoller(interval=10, performance=monitor)

        poller.request(lambda: self.module.DisplaySnapshot('frame', 'idle'))
        for _ in range(20):
            if poller.current_snapshot() is not None:
                break
            time.sleep(0.01)

        self.assertIn('poll avg', monitor.line())
        self.assertEqual(monitor.poll_count, 1)

    def test_keyboard_disables_flow_control_for_ctrl_s_and_restores_tty(self):
        class Input:
            def isatty(self):
                return True

            def fileno(self):
                return 7

            def read(self, _size):
                return '\x13'

        stream = Input()
        original = [self.module.termios.IXON, 0, 0, 0, 0, 0, []]
        cbreak = original.copy()
        with patch.object(self.module.termios, 'tcgetattr', side_effect=[original, cbreak]), \
                patch.object(self.module.termios, 'tcsetattr') as set_attributes, \
                patch.object(self.module.tty, 'setcbreak'), \
                patch.object(self.module.select, 'select', return_value=([stream], [], [])):
            with self.module.KeyboardShortcuts(stream) as keyboard:
                self.assertEqual(keyboard.read_key(), '\x13')

        self.assertEqual(cbreak[0], 0)
        self.assertEqual(original[0], self.module.termios.IXON)
        set_attributes.assert_any_call(7, self.module.termios.TCSANOW, cbreak)
        set_attributes.assert_any_call(7, self.module.termios.TCSADRAIN, original)

    def test_ctrl_s_returns_to_picker_and_monitors_selected_session(self):
        events = []
        snapshots = []
        client = FakeClient()
        with patch('sys.argv', ['octop', 'ses_first']):
            args = self.module.arguments()

        class TestKeyboard:
            entries = 0

            def __enter__(self):
                self.index = TestKeyboard.entries
                TestKeyboard.entries += 1
                self.keys = iter(('h', '\x13') if self.index == 0 else (None,))
                events.append(f'keyboard enter {self.index}')
                return self

            def __exit__(self, *_args):
                events.append(f'keyboard exit {self.index}')

            def read_key(self):
                try:
                    return next(self.keys)
                except StopIteration:
                    raise KeyboardInterrupt

        class TestRenderer:
            def render(self, frame):
                events.append(f'render {frame}')

            def close(self):
                events.append('renderer close')

        class TestPoller:
            def __init__(self, *_args, **_kwargs):
                pass

            def request(self, *_args, **_kwargs):
                return False

            def current_snapshot(self):
                return None

            def consume_error(self):
                return None

        def collect(_client, session, _limit, **kwargs):
            snapshots.append((session, kwargs['row_options'].show_header))
            return self.module.DisplaySnapshot(session, 'waiting')

        def choose(_client):
            self.assertEqual(events[-2:], ['keyboard exit 0', 'renderer close'])
            events.append('picker')
            return 'ses_second'

        with patch.object(self.module, 'arguments') as arguments, \
                patch.object(self.module, 'Client', return_value=client), \
                patch.object(self.module, 'TerminalRenderer', TestRenderer), \
                patch.object(self.module, 'KeyboardShortcuts', TestKeyboard), \
                patch.object(self.module, 'SnapshotPoller', TestPoller), \
                patch.object(self.module, 'collect_display_snapshot', side_effect=collect), \
                patch.object(self.module, 'choose_session', side_effect=choose), \
                patch.object(self.module.time, 'sleep'), \
                patch.dict(os.environ, {'OPENCODE_SERVER_PASSWORD': 'test'}):
            arguments.return_value = args
            self.assertEqual(self.module.main(), 0)

        self.assertEqual(snapshots, [('ses_first', False), ('ses_second', True)])
        self.assertIn('picker', events)
        self.assertIn('render ses_second', events)

    def test_render_frame_puts_shortcut_keys_on_separate_line(self):
        frame = self.module.render_frame(
            'ses_parent',
            ['parent line'],
            [['child line']],
            now='12:34:56',
            row_options=self.module.RowOptions(show_header=True),
        )
        lines = frame.splitlines()

        self.assertIn('Columns: status:off bar:on pct:off tokens:off model:on date:off time:off title:short agent:abbr', lines)
        self.assertIn('Keys: h header, s status, b bar, p pct, t tokens, m model, d date, c clock, f full title, a abbrev agent', lines)
        self.assertNotIn('(keys:', next(line for line in lines if line.startswith('Columns:')))

    def test_render_frame_wraps_header_before_subagents_on_narrow_terminal(self):
        frame = self.module.render_frame(
            'ses_parent',
            ['parent line'],
            [['child line']],
            now='12:34:56',
            row_options=self.module.RowOptions(show_header=True),
            terminal_width=64,
        )
        lines = frame.splitlines()
        header = lines[:lines.index('PARENT')]

        self.assertTrue(all(len(line) < 64 for line in header))
        self.assertEqual(header[0], 'octop — OpenCode top-style context monitor (active context)')
        self.assertEqual(header[1], 'Parent: ses_parent')
        self.assertEqual(header[2], 'Updated: 12:34:56')
        self.assertEqual(' '.join(header[3:5]), f'Columns: {self.module.column_status(self.module.RowOptions(show_header=True))}')
        self.assertEqual(' '.join(header[5:-1]),
                         'Keys: h header, s status, b bar, p pct, t tokens, m model, d date, c clock, f full title, a abbrev agent')
        self.assertEqual(lines[lines.index('SUBAGENTS') + 1], 'child line')
        self.assertEqual(lines.count('child line'), 1)

    def test_render_frame_wraps_long_parent_id_and_preserves_parent_on_short_screen(self):
        parent = 'ses_' + 'a' * 70
        frame = self.module.render_frame(
            parent,
            ['parent line'],
            [['child line']],
            now='12:34:56',
            row_options=self.module.RowOptions(show_header=True),
            terminal_width=32,
            max_lines=12,
        )
        lines = frame.splitlines()

        self.assertLessEqual(len(lines), 12)
        self.assertTrue(all(len(line) < 32 for line in lines))
        self.assertIn('PARENT', lines)
        self.assertIn('parent line', lines)

    def test_render_frame_wraps_long_parent_id_without_losing_content(self):
        parent = 'ses_' + 'a' * 70
        frame = self.module.render_frame(
            parent,
            ['parent line'],
            [],
            row_options=self.module.RowOptions(show_header=True),
            terminal_width=32,
        )
        header = frame.splitlines()[:frame.splitlines().index('PARENT')]
        parent_start = next(i for i, line in enumerate(header) if line.startswith('Parent:'))
        updated_start = next(i for i, line in enumerate(header) if line.startswith('Updated:'))

        self.assertTrue(all(len(line) < 32 for line in header))
        self.assertEqual(''.join(header[parent_start:updated_start]).replace(' ', ''), f'Parent:{parent}')

    def test_render_frame_hides_header_by_default_and_starts_at_parent(self):
        frame = self.module.render_frame(
            'ses_parent',
            ['parent line'],
            [['child line']],
            now='12:34:56',
        )
        lines = frame.splitlines()

        self.assertEqual(lines[0], 'PARENT')
        self.assertIn('parent line', lines)
        self.assertIn('child line', lines)
        self.assertIn('Ctrl+C to stop. Ctrl-S for session. h for header.', lines)
        self.assertNotIn('octop — OpenCode top-style context monitor (active context)', lines)
        self.assertNotIn('Parent: ses_parent', lines)
        self.assertNotIn('Updated: 12:34:56', lines)
        self.assertFalse(any(line.startswith('Columns:') for line in lines))
        self.assertFalse(any(line.startswith('Keys:') for line in lines))

    def test_render_frame_omits_verbose_footer_in_hidden_and_visible_header_modes(self):
        verbose_footer = [
            '~ Latest model-step tokens / model context limit; approximate, not exact.',
            'Uses last assistant step in active post-compaction context; refreshes after model steps.',
            'Status uses /active, context/session state, and pending /permission or /form requests.',
        ]
        frames = [
            self.module.render_frame('ses_parent', ['parent line'], [['child line']]),
            self.module.render_frame(
                'ses_parent',
                ['parent line'],
                [['child line']],
                row_options=self.module.RowOptions(show_header=True),
            ),
        ]

        for frame in frames:
            self.assertIn('Ctrl+C to stop. Ctrl-S for session. h for header.', frame)
            for footer_line in verbose_footer:
                self.assertNotIn(footer_line, frame)

    def test_render_frame_does_not_insert_blank_lines_between_subagents(self):
        frame = self.module.render_frame(
            'ses_parent',
            ['parent line'],
            [['child one'], ['child two']],
            now='12:34:56',
        )

        self.assertIn('child one\nchild two', frame)

    def test_render_frame_reports_hidden_subagents(self):
        frame = self.module.render_frame(
            'ses_parent',
            ['parent line'],
            [['child one']],
            now='12:34:56',
            omitted_count=3,
        )

        self.assertIn('3 older subagent(s) hidden', frame)

    def test_render_frame_shows_alternate_parents_when_no_direct_children(self):
        frame = self.module.render_frame(
            'selected',
            ['parent line'],
            [],
            now='12:34:56',
            alternate_parents=[{'id': 'other', 'title': 'Other parent', 'count': 2}],
        )

        self.assertIn('No direct child sessions found for this parent.', frame)
        self.assertIn('Recent parent sessions with subagents:', frame)
        self.assertIn('other', frame)
        self.assertIn('2 child(s)', frame)

    def test_render_frame_omits_hidden_message_when_all_subagents_visible(self):
        frame = self.module.render_frame(
            'ses_parent',
            ['parent line'],
            [['child one']],
            now='12:34:56',
        )

        self.assertNotIn('hidden to fit terminal', frame)

    def test_render_frame_can_shorten_bottom_to_terminal_height(self):
        frame = self.module.render_frame(
            'ses_parent',
            ['parent line'],
            [[f'child {i}'] for i in range(10)],
            now='12:34:56',
            max_lines=9,
            row_options=self.module.RowOptions(show_header=True),
        )
        lines = frame.splitlines()

        self.assertLessEqual(len(lines), 9)
        self.assertEqual(lines[0], 'octop — OpenCode top-style context monitor (active context)')
        self.assertIn('Parent: ses_parent', lines)
        self.assertEqual(lines[-1], '… output shortened to fit terminal')

    def test_render_frame_preserves_parent_row_when_shortened(self):
        frame = self.module.render_frame(
            'ses_parent',
            ['parent line'],
            [[f'child {i}'] for i in range(10)],
            now='12:34:56',
            max_lines=7,
            row_options=self.module.RowOptions(show_header=True),
        )
        lines = frame.splitlines()

        self.assertLessEqual(len(lines), 7)
        self.assertIn('PARENT', lines)
        self.assertIn('parent line', lines)

    def test_select_visible_subagents_prioritizes_active_then_recent(self):
        sessions = [
            {'id': 'old', 'time': {'updated': 1}},
            {'id': 'active', 'time': {'updated': 2}},
            {'id': 'new', 'time': {'updated': 3}},
        ]

        visible, omitted = self.module.select_visible_subagents(sessions, {'active'}, 2)

        self.assertEqual([session['id'] for session in visible], ['active', 'new'])
        self.assertEqual(omitted, 1)

    def test_select_visible_subagents_uses_recent_order_without_active(self):
        sessions = [
            {'id': 'old', 'time': {'updated': 1}},
            {'id': 'middle', 'time': {'updated': 2}},
            {'id': 'new', 'time': {'updated': 3}},
        ]

        visible, omitted = self.module.select_visible_subagents(sessions, set(), 2)

        self.assertEqual([session['id'] for session in visible], ['new', 'middle'])
        self.assertEqual(omitted, 1)

    def test_select_visible_subagents_handles_zero_capacity(self):
        sessions = [{'id': 'one'}, {'id': 'two'}]

        visible, omitted = self.module.select_visible_subagents(sessions, set(), 0)

        self.assertEqual(visible, [])
        self.assertEqual(omitted, 2)

    def test_max_visible_subagents_preserves_header_and_footer_space(self):
        self.assertEqual(self.module.max_visible_subagents(8), 0)
        self.assertEqual(self.module.max_visible_subagents(24), 10)

    def test_display_computes_each_visible_child_state_once(self):
        child = {'id': 'child', 'parentID': 'parent', 'title': 'Child'}
        calls = []

        self.module.child_sessions = lambda client, parent: [child]
        self.module.model_catalog = lambda client: {}
        self.module.active_session_ids = lambda client: {'child'}
        self.module.max_visible_subagents = lambda height: 10
        self.module.session_wait_state = lambda client, path, active, session, context_state: calls.append(session['id']) or 'running'
        self.module.session_output_lines = lambda client, session, catalog, fallback, active, label=None, state_override=None, **kwargs: [state_override or 'no-state']

        self.module.display(FakeClient(), 'parent', 0, FakeRenderer())

        self.assertEqual(calls.count('child'), 1)

    def test_display_frame_fits_terminal_height(self):
        children = [{'id': f'child-{i}', 'parentID': 'parent', 'title': f'Child {i}'} for i in range(20)]

        self.module.child_sessions = lambda client, parent: children
        self.module.model_catalog = lambda client: {}
        self.module.active_session_ids = lambda client: set()
        self.module.shutil.get_terminal_size = lambda fallback: os.terminal_size((80, 12))
        self.module.session_wait_state = lambda client, path, active, session, context_state: 'succeeded'
        self.module.session_output_lines = lambda client, session, catalog, fallback, active, label=None, state_override=None, **kwargs: [session.get('title') or label]
        renderer = FakeRenderer()

        self.module.display(FakeClient(), 'parent', 0, renderer)

        self.assertLessEqual(len(renderer.frames[0].splitlines()), 12)
        self.assertIn('PARENT', renderer.frames[0])
        self.assertNotIn('Parent: parent', renderer.frames[0])

    def test_display_clips_header_to_terminal_width(self):
        self.module.child_sessions = lambda client, parent: []
        self.module.model_catalog = lambda client: {}
        self.module.active_session_ids = lambda client: set()
        self.module.shutil.get_terminal_size = lambda fallback: os.terminal_size((64, 24))
        self.module.session_wait_state = lambda client, path, active, session, context_state: 'idle'
        self.module.session_output_lines = lambda client, session, catalog, fallback, active, **kwargs: ['parent line']

        snapshot = self.module.collect_display_snapshot(
            FakeClient(), 'ses_parent', 0, row_options=self.module.RowOptions(show_header=True)
        )

        self.assertTrue(all(len(line) < 64 for line in snapshot.frame_template.splitlines()[:5]))

    def test_display_parent_row_includes_session_id(self):
        captured = []

        self.module.child_sessions = lambda client, parent: []
        self.module.model_catalog = lambda client: {}
        self.module.active_session_ids = lambda client: set()
        self.module.session_output_lines = lambda client, session, catalog, fallback, active, label=None, state_override=None, **kwargs: captured.append(label) or [label]

        self.module.display(FakeClient(), 'ses_parent', 0, FakeRenderer())

        self.assertIn(f'{self.module.PARENT_INDICATOR_PLACEHOLDER} ses_parent', captured)

    def test_display_passes_terminal_width_to_rows(self):
        widths = []

        self.module.child_sessions = lambda client, parent: []
        self.module.model_catalog = lambda client: {}
        self.module.active_session_ids = lambda client: set()
        self.module.shutil.get_terminal_size = lambda fallback: os.terminal_size((120, 24))
        self.module.session_output_lines = lambda client, session, catalog, fallback, active, label=None, state_override=None, row_options=None: widths.append(row_options.row_width) or [label]

        self.module.display(FakeClient(), 'ses_parent', 0, FakeRenderer())

        self.assertEqual(widths, [119])

    def test_display_can_use_custom_right_margin(self):
        widths = []

        self.module.child_sessions = lambda client, parent: []
        self.module.model_catalog = lambda client: {}
        self.module.active_session_ids = lambda client: set()
        self.module.shutil.get_terminal_size = lambda fallback: os.terminal_size((120, 24))
        self.module.session_output_lines = lambda client, session, catalog, fallback, active, label=None, state_override=None, row_options=None: widths.append(row_options.row_width) or [label]

        self.module.display(FakeClient(), 'ses_parent', 0, FakeRenderer(), right_margin=0, name_boost=0)

        self.assertEqual(widths, [120])

    def test_display_can_use_custom_name_boost(self):
        widths = []

        self.module.child_sessions = lambda client, parent: []
        self.module.model_catalog = lambda client: {}
        self.module.active_session_ids = lambda client: set()
        self.module.shutil.get_terminal_size = lambda fallback: os.terminal_size((120, 24))
        self.module.session_output_lines = lambda client, session, catalog, fallback, active, label=None, state_override=None, row_options=None: widths.append(row_options.row_width) or [label]

        self.module.display(FakeClient(), 'ses_parent', 0, FakeRenderer(), right_margin=2, name_boost=24)

        self.assertEqual(widths, [118])

    def test_row_options_with_width_keeps_safety_margin(self):
        options = self.module.row_options_with_width(self.module.RowOptions(), 120, right_margin=2, name_boost=80)

        self.assertEqual(options.row_width, 118)

    def test_start_local_server_with_retries_does_not_mutate_args_port(self):
        args = type('Args', (), {'host': '127.0.0.1', 'port': 4096, 'port_attempts': 2})()
        calls = []

        def fake_start(passed_args, port=None):
            calls.append((passed_args.port, port))
            if port == 4096:
                raise RuntimeError('Port is occupied by another authenticated server')
            return 'proc', 'client'

        self.module.start_local_server = fake_start
        with redirect_stdout(io.StringIO()) as output:
            result = self.module.start_local_server_with_retries(args)

        self.assertEqual(result, ('proc', 'client'))
        self.assertEqual(args.port, 4096)
        self.assertEqual(calls, [(4096, 4096), (4096, 4097)])
        self.assertIn('Starting private OpenCode server on 127.0.0.1:4096', output.getvalue())
        self.assertIn('Port 4096 is occupied by an authenticated server; trying 4097', output.getvalue())

    def test_terminal_renderer_skips_unchanged_frames_and_uses_alternate_screen(self):
        output = io.StringIO()
        renderer = self.module.TerminalRenderer(output)

        self.assertTrue(renderer.render('frame one'))
        self.assertFalse(renderer.render('frame one'))
        self.assertTrue(renderer.render('frame two'))

        rendered = output.getvalue()
        self.assertNotIn('\033[2J', rendered)
        self.assertEqual(rendered.count('\033[?1049h'), 1)
        self.assertEqual(rendered.count('frame one'), 1)
        self.assertEqual(rendered.count('frame two'), 1)

    def test_terminal_renderer_clears_each_line_before_writing(self):
        output = io.StringIO()
        renderer = self.module.TerminalRenderer(output)

        renderer.render('long previous line\nshort\n')
        renderer.render('short\n')

        rendered = output.getvalue()
        self.assertIn('\033[2Kshort', rendered)
        self.assertNotIn('\033[2J', rendered)

    def test_terminal_renderer_does_not_newline_after_last_line(self):
        output = io.StringIO()
        renderer = self.module.TerminalRenderer(output)

        renderer.render('one\ntwo\n')

        rendered = output.getvalue()
        self.assertIn('one\n\033[2Ktwo\033[J', rendered)

    def test_terminal_renderer_restores_screen_on_close(self):
        output = io.StringIO()
        renderer = self.module.TerminalRenderer(output)

        renderer.render('frame')
        renderer.close()

        rendered = output.getvalue()
        self.assertIn('\033[?25h', rendered)
        self.assertIn('\033[?1049l', rendered)


if __name__ == '__main__':
    unittest.main()
