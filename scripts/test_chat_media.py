"""Chat media integration checks: MCP -> API -> runner, originals and previews.

Run: backend/.venv/bin/python scripts/test_chat_media.py
Uses temporary files and in-process HTTP only; no production DB/model calls.
"""
import io
import json
import sys
import tempfile
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'backend'), str(ROOT / 'agent_runner')]
import runner
from app.api.routes import agent_chats, assets
from app.db.base import get_db
from app.db.models import Asset
from app.mcp import client as mcp_client
from app.mcp import tools
from app.mcp.server import mcp_server


class ChatMediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        directory = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='chat-media-'))
        self.root = Path(directory)
        self.stack.enter_context(patch.object(runner, 'CHATS_DIR', self.root))
        self.chat_id = 'abcdef123456'
        folder = self.root / self.chat_id
        folder.mkdir()
        (folder / 'meta.json').write_text(json.dumps({'id': self.chat_id, 'kind': 'project', 'provider': 'codex'}))
        self.chat = runner.Chat(self.chat_id)
        self.stack.enter_context(patch.dict(runner.chats, {self.chat_id: self.chat}, clear=True))
        token = 'media-test-token'
        self.stack.enter_context(patch.object(runner, 'TOKENS', {token}))
        self.image_buffer = io.BytesIO()
        Image.new('RGB', (1200, 900), '#123456').save(self.image_buffer, 'PNG')
        self.original = self.image_buffer.getvalue()
        self.path = self.root / 'asset.bin'
        self.prefix = b'prefix-header-never-exposed'
        self.path.write_bytes(self.prefix + self.original)
        self.asset = SimpleNamespace(id=uuid.uuid4(), storage_key='asset.bin', mime_type='image/png',
                                     meta={'filename': 'Айліріс.png'}, kind='image')
        async def lookup(model, asset_id):
            return self.asset if model is Asset and asset_id == self.asset.id else None
        db = SimpleNamespace(get=AsyncMock(side_effect=lookup))
        async def db_dependency():
            yield db
        self.app = FastAPI()
        self.app.include_router(agent_chats.router)
        self.app.include_router(assets.router)
        self.app.dependency_overrides[get_db] = db_dependency
        storage = SimpleNamespace(path_of=lambda key: self.path, payload_offset=lambda key: len(self.prefix))
        self.stack.enter_context(patch.object(agent_chats, 'get_storage', return_value=storage))
        self.stack.enter_context(patch.object(assets, 'get_storage', return_value=storage))
        def runner_client():
            return httpx.AsyncClient(transport=httpx.ASGITransport(app=runner.app), base_url='http://runner',
                                     headers={'Authorization': f'Bearer {token}'})
        self.stack.enter_context(patch.object(agent_chats, '_runner', runner_client))
        self.stack.enter_context(patch.object(mcp_client, '_app', self.app))
        self.stack.enter_context(patch.object(mcp_client, 'get_settings', return_value=SimpleNamespace(api_token=token)))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()
        self.stack.close()

    async def test_mcp_show_in_chat_for_all_providers(self):
        self.assertIn('show_in_chat', [t.name for t in await mcp_server.list_tools()])
        for provider in ('claude', 'codex', 'agy'):
            self.chat.meta['provider'] = provider
            result = await tools.show_in_chat(self.chat_id, str(self.asset.id), 'Айліріс')
            self.assertTrue(result['shown'])
            event = self.chat.events_after(0)[-1]
            self.assertEqual(event['type'], 'media')
            self.assertEqual(event['asset_id'], str(self.asset.id))
            self.assertEqual(event['mime_type'], 'image/png')
            self.assertEqual(event['caption'], 'Айліріс')
            self.assertEqual(event['name'], 'Айліріс.png')
            self.assertNotIn('token', json.dumps(event))
            self.assertNotIn('storage_key', event)

    async def test_download_is_original_payload_without_storage_prefix(self):
        response = await self.client.get(f'/api/assets/{self.asset.id}/file?download=true')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.original)
        self.assertTrue(response.headers['content-disposition'].startswith('attachment;'))
        self.assertIn('%D0%90', response.headers['content-disposition'])
        self.assertEqual(Image.open(io.BytesIO(response.content)).size, (1200, 900))

    async def test_generated_attachment_preview_is_separate_from_original(self):
        self.chat.attachments_dir.mkdir()
        name = uuid.uuid4().hex + '.png'
        (self.chat.attachments_dir / name).write_bytes(self.original)
        url = f'/api/agent-chats/{self.chat_id}/attachments/{name}'
        preview = await self.client.get(url + '?preview=true')
        original = await self.client.get(url)
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.headers['content-type'], 'image/webp')
        self.assertEqual(Image.open(io.BytesIO(preview.content)).size, (768, 576))
        self.assertEqual(original.content, self.original)
        self.assertEqual(Image.open(io.BytesIO(original.content)).size, (1200, 900))
        download = await self.client.get(url + '?download=true&preview=true')
        self.assertEqual(download.content, self.original)
        self.assertTrue(download.headers['content-disposition'].startswith('attachment;'))

    async def test_video_audio_and_other_files_publish_without_image_conversion(self):
        for mime in ('video/mp4', 'audio/mpeg', 'application/pdf', 'model/gltf-binary'):
            self.asset.mime_type = mime
            self.asset.meta = {}
            result = await tools.show_in_chat(self.chat_id, str(self.asset.id), mime)
            self.assertTrue(result['shown'])
            self.assertEqual(self.chat.events_after(0)[-1]['mime_type'], mime)
            response = await self.client.get(f'/api/assets/{self.asset.id}/file', headers={'Range': 'bytes=2-8'})
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.content, self.original[2:9])

    async def test_invalid_asset_chat_and_missing_file_are_not_published(self):
        with self.assertRaisesRegex(RuntimeError, '404'):
            await tools.show_in_chat(self.chat_id, str(uuid.uuid4()))
        with self.assertRaises(ValueError):
            await tools.show_in_chat('../other-chat', str(self.asset.id))
        self.path.unlink()
        with self.assertRaisesRegex(RuntimeError, 'Asset file missing'):
            await tools.show_in_chat(self.chat_id, str(self.asset.id))
        self.assertEqual(self.chat.events_after(0), [])

    async def test_unknown_chat_does_not_publish(self):
        with self.assertRaisesRegex(RuntimeError, 'Chat not found'):
            await tools.show_in_chat('000000000000', str(self.asset.id))
        self.assertEqual(self.chat.events_after(0), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
