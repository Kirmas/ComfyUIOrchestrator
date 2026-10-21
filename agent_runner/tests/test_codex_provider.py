"""Protocol regression tests; no login, network or model calls required."""
import asyncio
import base64
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
                  'permission_mode': 'ask', 'effort': 'high', 'fast_mode': True},
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
                self.assertEqual(message['params']['sandbox'], 'workspace-write')
                self.assertEqual(message['params']['approvalPolicy'], 'on-request')
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
            spec = providers.TurnSpec("Your only tools are the orchestrator's MCP tools. You have no shell and no file\naccess.", isolated=True, mcp_url='http://localhost/mcp', mcp_token='test')
            with patch.dict(providers.os.environ, {'CODEX_HOME': str(home)}):
                launch = self.provider.launch(self.chat, spec, 'hello', [])
            isolated = Path(launch.env['CODEX_HOME'])
            self.assertFalse((isolated / 'config.toml').exists())
            self.assertTrue((isolated / 'auth.json').is_symlink())
            self.assertIn('features.plugins=false', launch.args)
            self.assertIn('features.apps=false', launch.args)
            self.assertIn('features.image_generation=true', launch.args)
            await self.provider.begin(self.chat, self.proc, spec, 'hello', [])
            params = self.chat.turn_state['rpc_thread']
            self.assertEqual(params['sandbox'], 'read-only')
            self.assertEqual(params['approvalPolicy'], 'never')
            self.assertIn('built-in image_gen tool', params['developerInstructions'])
            self.assertNotIn('Your only tools are', params['developerInstructions'])
            self.assertEqual(params['serviceTier'], 'default')
            self.assertNotIn('config', params)

    async def test_resumed_project_updates_instructions_before_turn(self):
        self.chat.meta.update(session_started=True, session_id='existing')
        spec = providers.TurnSpec("Your only tools are the orchestrator's MCP tools.", isolated=True)
        await self.provider.begin(self.chat, self.proc, spec, 'hello', [])
        await self.provider.on_event(self.chat, self.proc, {'id': 'thread', 'result': {'thread': {'id': 'existing'}}}, Mock())
        update = self.messages()[-1]
        self.assertEqual(update['method'], 'thread/inject_items')
        self.assertEqual(update['params']['items'][0]['role'], 'developer')
        self.assertIn('built-in image_gen', update['params']['items'][0]['content'][0]['text'])
        self.assertNotIn('codex_instructions_digest', self.chat.meta)
        await self.provider.on_event(self.chat, self.proc, {'id': 'project_instructions', 'result': {}}, Mock())
        self.assertEqual(self.messages()[-1]['method'], 'turn/start')
        self.assertTrue(self.chat.meta['codex_instructions_digest'])
        await self.provider.begin(self.chat, self.proc, spec, 'again', [])
        self.assertNotIn('instructions_update', self.chat.turn_state)

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

    async def test_permission_modes_and_legacy_default(self):
        expected = {
            'ask': ('workspace-write', 'on-request', 'user'),
            'auto': ('workspace-write', 'on-request', 'auto_review'),
            'full': ('danger-full-access', 'never', 'user'),
        }
        self.assertEqual(self.provider.permission_modes, ['ask', 'auto', 'full'])
        for legacy in (None, 'workspace', 'readOnly', 'unknown'):
            self.assertEqual(self.provider.mode_for({'permission_mode': legacy}), 'ask')
        for mode, values in expected.items():
            self.chat.meta['permission_mode'] = mode
            await self.provider.begin(self.chat, self.proc, providers.TurnSpec('test'), 'hello', [])
            params = self.chat.turn_state['rpc_thread']
            self.assertEqual(tuple(params[k] for k in ('sandbox', 'approvalPolicy', 'approvalsReviewer')), values)
            await self.provider.begin(self.chat, self.proc, providers.TurnSpec('test', isolated=True), 'hello', [])
            params = self.chat.turn_state['rpc_thread']
            self.assertEqual(params['sandbox'], 'read-only')
            self.assertEqual(params['approvalPolicy'], 'never')
            self.assertFalse(self.chat.turn_state['tool_approvals'])

    async def test_tool_approval_allow_deny_and_no_session_grants(self):
        self.chat.turn_state['tool_approvals'] = True
        for method in self.provider.APPROVAL_METHODS:
            ask = Mock()
            params = {'turnId': 't', 'itemId': 'tool', 'permissions': {'network': {'enabled': True}}}
            await self.provider.on_event(self.chat, self.proc, {'id': 3, 'method': method, 'params': params}, ask)
            key, request = ask.call_args.args
            self.assertEqual(key, 't:3')
            self.assertEqual(request['allowed_decisions'], ['allow', 'deny'])
            for behavior in ('allow', 'deny'):
                await self.provider.answer_request(self.proc, key, request, {'behavior': behavior})
                result = self.messages()[-1]['result']
                if method == 'item/permissions/requestApproval':
                    self.assertEqual(result['permissions'], params['permissions'] if behavior == 'allow' else {})
                    self.assertEqual(result['scope'], 'turn')
                else:
                    self.assertEqual(result['decision'], 'accept' if behavior == 'allow' else 'decline')

    async def test_fixed_mode_rejects_unexpected_approval(self):
        ask = Mock()
        await self.provider.on_event(self.chat, self.proc, {
            'id': 3, 'method': 'item/commandExecution/requestApproval',
            'params': {'turnId': 't', 'command': 'example'},
        }, ask)
        ask.assert_not_called()
        self.assertEqual(self.messages()[-1]['result'], {'decision': 'decline'})

    async def test_server_decision_restrictions(self):
        self.chat.turn_state['tool_approvals'] = True
        ask = Mock()
        await self.provider.on_event(self.chat, self.proc, {
            'id': 3, 'method': 'item/commandExecution/requestApproval',
            'params': {'turnId': 't', 'availableDecisions': ['cancel']},
        }, ask)
        key, request = ask.call_args.args
        self.assertEqual(request['allowed_decisions'], ['deny'])
        await self.provider.answer_request(self.proc, key, request, {'behavior': 'allow'})
        self.assertEqual(self.messages()[-1]['result'], {'decision': 'cancel'})

    async def test_http_rejects_disallowed_session_decision(self):
        import runner
        from starlette.requests import Request
        future = asyncio.get_running_loop().create_future()
        chat = SimpleNamespace(pending={'r': future}, turn_state={'permission_choices': {'r': ['allow', 'deny']}})
        async def receive():
            return {'type': 'http.request', 'body': b'{"decision":"allow_session"}'}
        request = Request({'type': 'http', 'method': 'POST', 'path': '/', 'headers': [],
                           'path_params': {'chat_id': 'probe', 'request_id': 'r'}}, receive)
        with patch.dict(runner.chats, {'probe': chat}):
            response = await runner.decide(request)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(future.done())
        future.cancel()

    async def test_native_compact_uses_rpc_not_a_model_prompt(self):
        self.chat.meta.update(session_started=True, session_id='existing')
        await self.provider.begin(self.chat, self.proc, providers.TurnSpec('test'), '/compact', [])
        await self.provider.on_event(self.chat, self.proc, {'id': 'init', 'result': {}}, Mock())
        await self.provider.on_event(self.chat, self.proc, {'id': 'thread', 'result': {'thread': {'id': 'existing'}}}, Mock())
        self.assertEqual(self.messages()[-1], {
            'id': 'compact', 'method': 'thread/compact/start', 'params': {'threadId': 'existing'},
        })
        self.assertFalse(any(m.get('method') == 'turn/start' for m in self.messages()))
        # RPC acknowledgement alone is not completion; progress must drain.
        await self.provider.on_event(self.chat, self.proc, {'id': 'compact', 'result': {}}, Mock())
        self.proc.stdin.close.assert_not_called()
        await self.provider.on_event(self.chat, self.proc, {'method': 'turn/completed', 'params': {
            'threadId': 'existing', 'turn': {'status': 'completed'},
        }}, Mock())
        self.proc.stdin.close.assert_called_once()
        self.assertTrue(self.chat.turn_state['result_received'])
        self.assertTrue(self.provider.describe()['supports_compact'])

    async def test_context_uses_last_total_including_post_compact_zero_inputs(self):
        self.chat.meta['session_id'] = 'thread'
        self.chat.turn_state['context_model'] = 'gpt-6-luna'
        event = {'method': 'thread/tokenUsage/updated', 'params': {'threadId': 'thread', 'tokenUsage': {
            'last': {'totalTokens': 1797, 'inputTokens': 0, 'outputTokens': 0},
            'total': {'totalTokens': 999999}, 'modelContextWindow': 258400,
        }}}
        await self.provider.on_event(self.chat, self.proc, event, Mock())
        self.assertEqual(self.chat.meta['context_tokens'], 1797)
        self.assertEqual(self.provider.get_context_window(self.chat), 258400)
        await self.provider.on_event(self.chat, self.proc, {'method': 'turn/completed', 'params': {
            'threadId': 'thread', 'turn': {'status': 'completed'},
        }}, Mock())
        result = self.chat.emit.call_args.args[0]
        self.assertEqual(result['context_tokens'], 1797)
        self.assertEqual(result['context_window'], 258400)

    async def test_context_window_tracks_runtime_override_and_model_switch(self):
        self.provider._record_context(self.chat, {'totalTokens': 100}, 800000, 'gpt-6-luna')
        self.assertEqual(self.provider.get_context_window(self.chat), 800000)
        self.chat.meta['requested_model'] = 'gpt-6.1-sol'
        self.assertEqual(self.provider.get_context_window(self.chat), 475000)
        self.provider._record_context(self.chat, {'totalTokens': 90}, None, 'gpt-6.1-sol')
        self.assertNotIn('context_window', self.chat.meta)
        self.assertEqual(self.provider.get_context_window(self.chat), 475000)

    async def test_context_backfill_reads_own_latest_valid_rollout(self):
        session = '12345678-1234-1234-1234-123456789012'
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / 'sessions/2026/10/21'
            folder.mkdir(parents=True)
            reading = {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                'last_token_usage': {'total_tokens': 88, 'input_tokens': 0},
                'total_token_usage': {'total_tokens': 999}, 'model_context_window': 123000,
            }}}
            (folder / f'rollout-{session}.jsonl').write_text('broken\n' + json.dumps(reading) + '\n123\n{"partial":')
            self.chat.meta['session_id'] = session
            with patch.dict(providers.os.environ, {'CODEX_HOME': d}):
                self.assertEqual(self.provider.get_context_tokens(self.chat), 88)
                self.assertEqual(self.provider.get_context_window(self.chat), 123000)
                self.chat.meta['session_id'] = '../*'
                self.assertIsNone(self.provider.get_context_tokens(self.chat))

    async def test_per_model_efforts_reject_ultra_on_luna(self):
        for model in ('gpt-6.1-sol', 'gpt-6-astra'):
            self.assertIn('ultra', self.provider.efforts_for(model))
            self.assertEqual(self.provider.effort_for({'requested_model': model, 'effort': 'ultra'}), 'ultra')
        self.assertNotIn('ultra', self.provider.efforts_for('gpt-6-luna'))
        self.assertIsNone(self.provider.effort_for({'requested_model': 'gpt-6-luna', 'effort': 'ultra'}))

    async def test_free_limits_polling_isolated_from_other_provider_failures(self):
        import runner
        codex = SimpleNamespace(available=True, probe_limits=AsyncMock())
        agy = SimpleNamespace(available=True, probe_limits=AsyncMock(side_effect=RuntimeError('test failure')))
        claude = SimpleNamespace(available=True, probe_limits=AsyncMock())
        with patch.dict(runner.PROVIDERS, {'codex': codex, 'agy': agy, 'claude': claude}), \
             patch.object(runner, 'LIMITS_INITIAL_DELAY_SECONDS', 0), \
             patch.object(runner, 'LIMITS_POLL_INTERVAL_SECONDS', 60), patch('builtins.print'):
            task = asyncio.create_task(runner.limits_scheduler())
            try:
                async with asyncio.timeout(1):
                    while not (codex.probe_limits.await_count and agy.probe_limits.await_count and claude.probe_limits.await_count):
                        await asyncio.sleep(0)
                codex.probe_limits.assert_awaited_once()
                agy.probe_limits.assert_awaited_once()
                # Claude's reading is free too (the OAuth usage endpoint, no model turn).
                claude.probe_limits.assert_awaited_once()
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    async def test_api_validates_effort_for_selected_model_before_model_change(self):
        import runner
        from starlette.requests import Request
        async def request(body):
            async def receive():
                return {'type': 'http.request', 'body': json.dumps(body).encode()}
            return Request({'type': 'http', 'method': 'PATCH', 'path': '/', 'headers': [],
                            'path_params': {'chat_id': 'probe'}}, receive)
        self.chat.meta.update(kind='dev', requested_model='gpt-6.1-sol')
        with patch.dict(runner.chats, {'probe': self.chat}), patch.object(runner, 'public_meta', return_value={}):
            result = await runner.update_chat(await request({'model': 'gpt-6-luna', 'effort': 'ultra'}))
            self.assertEqual(result.status_code, 400)
            self.assertEqual(self.chat.meta['requested_model'], 'gpt-6.1-sol')
            result = await runner.update_chat(await request({'effort': 'ultra'}))
            self.assertEqual(result.status_code, 200)
            self.assertEqual(self.chat.meta['effort'], 'ultra')

    async def test_limits_probe_never_starts_a_model_turn(self):
        proc = SimpleNamespace(stdin=Mock(), stdout=asyncio.StreamReader(), wait=AsyncMock(return_value=0))
        proc.stdin.drain = AsyncMock()
        proc.stdout.feed_data((json.dumps({'id': 'init', 'result': {}}) + '\n').encode())
        proc.stdout.feed_data((json.dumps({'id': 'limits', 'result': {'rateLimits': {
            'primary': {'usedPercent': 20, 'windowDurationMins': 300, 'resetsAt': 123},
        }}}) + '\n').encode())
        proc.stdout.feed_eof()
        with tempfile.TemporaryDirectory() as d, \
             patch.object(providers, 'limits', providers.LimitsStore(Path(d) / 'limits.json')), \
             patch.object(providers.asyncio, 'create_subprocess_exec', AsyncMock(return_value=proc)):
            await self.provider.probe_limits()
            messages = [json.loads(c.args[0]) for c in proc.stdin.write.call_args_list]
            self.assertEqual([m['method'] for m in messages], ['initialize', 'initialized', 'account/rateLimits/read'])
            self.assertEqual(providers.limits.data['codex']['windows'][0]['utilization'], .2)
            proc.stdin.close.assert_called_once()
            proc.wait.assert_awaited_once()

    def async_question_event(self):
        return {'method': 'item/completed', 'params': {
            'threadId': 'thread', 'turnId': 'turn',
            'item': {'id': 'async-call', 'type': 'agentMessage', 'delivery': 'async',
                     'text': 'Choose a color\n- Red\n- Blue',
                     'questions': [{'title': 'Choose a color', 'options': ['Red', 'Blue']}]},
        }}

    async def test_async_message_questions_become_cards_without_duplicate_text(self):
        self.chat.meta['session_id'] = 'thread'
        ask = Mock()
        event = self.async_question_event()
        await self.provider.on_event(self.chat, self.proc, event, ask)
        key, request = ask.call_args.args
        self.assertEqual(key, 'async:turn:async-call')
        self.assertTrue(request['requires_user_interaction'])
        self.assertEqual(request['input']['questions'][0]['options'], [
            {'label': 'Red', 'description': ''}, {'label': 'Blue', 'description': ''},
        ])
        self.chat.emit.assert_not_called()
        await self.provider.on_event(self.chat, self.proc, event, ask)
        ask.assert_called_once()

    async def test_async_question_survives_turn_end_and_delayed_answer(self):
        self.chat.meta['session_id'] = 'thread'
        ask = Mock()
        await self.provider.on_event(self.chat, self.proc, self.async_question_event(), ask)
        key, request = ask.call_args.args
        complete = {'method': 'turn/completed', 'params': {
            'threadId': 'thread', 'turn': {'status': 'completed'},
        }}
        await self.provider.on_event(self.chat, self.proc, complete, ask)
        self.proc.stdin.close.assert_not_called()
        self.assertNotIn('result_received', self.chat.turn_state)
        await self.provider.answer_request(self.proc, key, request, {
            'behavior': 'allow', 'updatedInput': {'answers': {'Choose a color': 'Blue'}},
        })
        sent = self.messages()[-1]
        self.assertEqual(sent['method'], 'turn/start')
        self.assertEqual(sent['params']['threadId'], 'thread')
        self.assertIn('Blue', sent['params']['input'][0]['text'])
        self.assertFalse(self.chat.turn_state['async_questions'])
        # Completion racing the queued response must not close stdin either.
        await self.provider.on_event(self.chat, self.proc, complete, ask)
        self.proc.stdin.close.assert_not_called()
        await self.provider.on_event(self.chat, self.proc, {'id': sent['id'], 'result': {'turn': {'id': 'next'}}}, ask)
        self.assertFalse(self.chat.turn_state['async_answers_inflight'])
        await self.provider.on_event(self.chat, self.proc, complete, ask)
        self.proc.stdin.close.assert_called_once()
        self.assertTrue(self.chat.turn_state['result_received'])

    async def test_async_failed_turn_does_not_wait_forever_for_answer(self):
        self.chat.meta['session_id'] = 'thread'
        await self.provider.on_event(self.chat, self.proc, self.async_question_event(), Mock())
        await self.provider.on_event(self.chat, self.proc, {'method': 'turn/completed', 'params': {
            'threadId': 'thread', 'turn': {'status': 'failed', 'error': {'message': 'quota'}},
        }}, Mock())
        self.proc.stdin.close.assert_called_once()
        self.assertTrue(self.chat.turn_state['failed'])

    IMAGE_PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a/1sAAAAASUVORK5CYII=')

    async def test_generated_image_is_persisted_and_emitted_without_base64(self):
        with tempfile.TemporaryDirectory() as d:
            self.chat.attachments_dir = Path(d) / 'attachments'
            item = {'type': 'imageGeneration', 'id': 'img', 'status': 'completed',
                    'result': base64.b64encode(self.IMAGE_PNG).decode(), 'revisedPrompt': 'robot'}
            await self.provider.on_event(self.chat, self.proc, {'method': 'item/completed', 'params': {'item': item}}, Mock())
            events = [c.args[0] for c in self.chat.emit.call_args_list]
            output = next(e for e in events if e['type'] == 'image')
            name = output['images'][0]
            self.assertEqual((self.chat.attachments_dir / name).read_bytes(), self.IMAGE_PNG)
            self.assertEqual(events[-1]['type'], 'tool_result')
            self.assertFalse(events[-1]['is_error'])
            self.assertLess(len(json.dumps(events)), 600)
            await self.provider.on_event(self.chat, self.proc, {'method': 'item/completed', 'params': {'item': item}}, Mock())
            self.assertEqual(sum(c.args[0]['type'] == 'image' for c in self.chat.emit.call_args_list), 1)

    async def test_extension_image_event_and_attachment_response(self):
        import runner
        from starlette.requests import Request
        with tempfile.TemporaryDirectory() as d:
            self.chat.attachments_dir = Path(d) / 'attachments'
            item = {'type': 'Extension', 'kind': 'image_gen.generation', 'id': 'img',
                    'status': 'completed', 'result': base64.b64encode(self.IMAGE_PNG).decode()}
            await self.provider.on_event(self.chat, self.proc, {'method': 'item/completed', 'params': {'item': item}}, Mock())
            name = self.chat.meta['generated_images'][0]
            with patch.dict(runner.chats, {'probe': self.chat}):
                request = Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': [],
                                   'path_params': {'chat_id': 'probe', 'name': name}})
                response = await runner.read_attachment(request)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.media_type, 'image/png')
                self.assertEqual(response.body, self.IMAGE_PNG)

    async def test_invalid_image_does_not_read_arbitrary_saved_path(self):
        with tempfile.TemporaryDirectory() as d:
            self.chat.attachments_dir = Path(d) / 'attachments'
            item = {'type': 'imageGeneration', 'id': 'img', 'status': 'completed',
                    'result': '', 'savedPath': '/etc/passwd'}
            await self.provider.on_event(self.chat, self.proc, {'method': 'item/completed', 'params': {'item': item}}, Mock())
            self.assertTrue(self.chat.turn_state['output_failed'])
            self.assertFalse(any(c.args[0]['type'] == 'image' for c in self.chat.emit.call_args_list))
            self.assertFalse(self.chat.attachments_dir.exists())

    async def test_recover_own_saved_images_and_skip_symlinks(self):
        session = '12345678-1234-1234-1234-123456789012'
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            images = root / 'generated_images' / session
            images.mkdir(parents=True)
            (images / 'image.png').write_bytes(self.IMAGE_PNG)
            (images / 'outside.png').symlink_to('/etc/passwd')
            unrelated = root / 'generated_images/other-thread'
            unrelated.mkdir()
            (unrelated / 'other.png').write_bytes(self.IMAGE_PNG + b'other')
            self.chat.dir = root / 'chat'
            self.chat.attachments_dir = self.chat.dir / 'attachments'
            self.chat.meta['session_id'] = session
            with patch.dict(providers.os.environ, {'CODEX_HOME': d}):
                await self.provider.recover_outputs(self.chat)
                await self.provider.recover_outputs(self.chat)
            self.assertEqual(len(self.chat.meta['generated_images']), 1)
            self.assertEqual(sum(c.args[0]['type'] == 'image' for c in self.chat.emit.call_args_list), 1)

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
