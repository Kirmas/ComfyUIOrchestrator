"""Protocol regression tests; no login, network or model calls required."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import providers


class CodexProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.provider = providers.CodexProvider()
        self.proc = SimpleNamespace(stdin=Mock(), returncode=None)
        self.proc.stdin.drain = AsyncMock()
        self.chat = SimpleNamespace(
            meta={'provider': 'codex', 'requested_model': 'gpt-6-luna',
                  'permission_mode': 'readOnly', 'effort': 'high', 'fast_mode': True},
            turn_state={}, save_meta=Mock(), emit=AsyncMock(),
        )

    def messages(self):
        return [json.loads(c.args[0]) for c in self.proc.stdin.write.call_args_list]

    async def test_handshake_resume_and_controls(self):
        for resumed in (False, True):
            with self.subTest(resumed=resumed):
                self.proc.stdin.reset_mock()
                self.chat.meta.update(session_started=resumed, session_id='existing')
                await self.provider.begin(self.chat, self.proc, providers.TurnSpec('system'), 'hello', [Path('/tmp/image.png')])
                await self.provider.on_event(self.chat, self.proc, {'id': 'init', 'result': {}}, Mock())
                message = self.messages()[-1]
                self.assertEqual(message['method'], 'thread/resume' if resumed else 'thread/start')
                self.assertEqual(message['params']['sandbox'], 'read-only')
                self.assertEqual(message['params']['approvalPolicy'], 'never')
                self.assertEqual(message['params']['serviceTier'], 'priority')
                self.assertEqual(message['params']['config']['model_reasoning_effort'], 'high')
                await self.provider.on_event(self.chat, self.proc, {'id': 'thread', 'result': {'thread': {'id': 'durable'}, 'model': 'gpt-6-luna'}}, Mock())
                self.assertEqual(self.messages()[-1]['method'], 'turn/start')
                self.assertEqual(self.messages()[-1]['params']['input'][1], {'type': 'localImage', 'path': '/tmp/image.png'})
                self.proc.stdin.close.assert_not_called()

    async def test_question_id_and_response(self):
        ask = Mock()
        event = {'id': 0, 'method': 'item/tool/requestUserInput', 'params': {
            'turnId': 'turn-two', 'questions': [{'id': 'q', 'header': 'Choice', 'question': 'Choose?', 'options': None}]}}
        await self.provider.on_event(self.chat, self.proc, event, ask)
        key, request = ask.call_args.args
        self.assertEqual(key, 'turn-two:0')
        self.assertEqual(request['input']['questions'][0]['options'], [])
        await self.provider.answer_request(self.proc, key, request, {'updatedInput': {'answers': {'Choose?': 'Free text'}}})
        self.assertEqual(self.messages()[-1], {'id': 0, 'result': {'answers': {'q': {'answers': ['Free text']}}}})

    async def test_project_forces_read_only_and_separate_config(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            home = root / "personal"
            home.mkdir()
            (home / "auth.json").write_text("{}")
            (home / "config.toml").write_text('sandbox_mode="danger-full-access"')
            (home / "sessions").mkdir()
            self.chat.dir = root
            self.chat.meta.update(permission_mode='full', fast_mode=False, effort=None)
            spec = providers.TurnSpec('project', isolated=True, mcp_url='http://localhost/mcp', mcp_token='test')
            with patch.dict(providers.os.environ, {'CODEX_HOME': str(home)}):
                launch = self.provider.launch(self.chat, spec, 'hello', [])
            isolated = Path(launch.env['CODEX_HOME'])
            self.assertFalse((isolated / 'config.toml').exists())
            self.assertTrue((isolated / 'auth.json').is_symlink())
            self.assertIn('features.plugins=false', launch.args)
            await self.provider.begin(self.chat, self.proc, spec, 'hello', [])
            params = self.chat.turn_state['rpc_thread']
            self.assertEqual(params['sandbox'], 'read-only')
            self.assertEqual(params['serviceTier'], 'default')
            self.assertNotIn('config', params)

    async def test_claude_response_unchanged(self):
        answer = {'behavior': 'deny', 'message': 'Denied'}
        await providers.ClaudeProvider().answer_request(self.proc, 'req', {}, answer)
        self.assertEqual(self.messages()[-1], {'type': 'control_response', 'response': {'subtype': 'success', 'request_id': 'req', 'response': answer}})

    async def test_command_events_and_failure(self):
        item = {'id': 'cmd', 'type': 'commandExecution', 'command': 'false', 'exitCode': 1, 'aggregatedOutput': 'failed'}
        for method in ('item/started', 'item/completed'):
            await self.provider.on_event(self.chat, self.proc, {'method': method, 'params': {'item': item}}, Mock())
        emitted = [c.args[0] for c in self.chat.emit.call_args_list]
        self.assertEqual([e['type'] for e in emitted], ['tool_use', 'tool_result'])
        self.assertTrue(emitted[-1]['is_error'])
        await self.provider.on_event(self.chat, self.proc, {'method': 'turn/completed', 'params': {'turn': {'status': 'failed', 'error': {'message': 'quota'}}}}, Mock())
        self.assertTrue(self.chat.turn_state['failed'])
        self.proc.stdin.close.assert_called_once()

    async def test_rpc_error_and_unknown_request(self):
        await self.provider.on_event(self.chat, self.proc, {'id': 7, 'method': 'unknown', 'params': {}}, Mock())
        self.assertIn('error', self.messages()[-1])
        await self.provider.on_event(self.chat, self.proc, {'id': 'thread', 'error': {'message': 'not found'}}, Mock())
        self.assertTrue(self.chat.turn_state['failed'])
        self.proc.stdin.close.assert_called_once()

    async def test_child_turn_completion_does_not_close_parent(self):
        self.chat.meta['session_id'] = 'parent'
        await self.provider.on_event(self.chat, self.proc, {
            'method': 'turn/completed',
            'params': {'threadId': 'child', 'turn': {'status': 'completed'}},
        }, Mock())
        self.proc.stdin.close.assert_not_called()
        self.chat.emit.assert_not_called()

    async def test_live_shape_limits(self):
        with tempfile.TemporaryDirectory() as d, patch.object(providers, 'limits', providers.LimitsStore(Path(d) / 'limits.json')):
            await self.provider.on_event(self.chat, self.proc, {'method': 'account/rateLimits/updated', 'params': {'rateLimits': {
                'primary': {'usedPercent': 25, 'windowDurationMins': 300, 'resetsAt': 123},
                'secondary': {'usedPercent': 75, 'windowDurationMins': 10080, 'resetsAt': 456}}}}, Mock())
            windows = providers.limits.data['codex']['windows']
            self.assertEqual([w['id'] for w in windows], ['five_hour', 'seven_day'])
            self.assertEqual(windows[0]['utilization'], .25)
            self.assertFalse(self.provider.record_limits({'primary': None}))
            self.assertEqual(providers.limits.data['codex']['windows'], windows)


if __name__ == '__main__':
    unittest.main()
