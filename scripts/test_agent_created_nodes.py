"""Tests for agent-created node tracking and conditional MCP deletion.

Run: backend/.venv/bin/python scripts/test_agent_created_nodes.py
"""
import asyncio
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend")]

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.routes import nodes as nodes_route
from app.core.ws_manager import ws_manager
from app.db.base import Base, get_db
from app.db.models import Node, NodeKind, NodeStatus, Project, Track
from app.main import create_app
from app.mcp import client as mcp_client
from app.mcp import tools as mcp_tools


class AgentCreatedNodeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

        self.app = create_app()

        async def override_get_db():
            async with self.session_factory() as session:
                yield session

        self.app.dependency_overrides[get_db] = override_get_db
        mcp_client.set_app(self.app)

        # Create a test project and track
        async with self.session_factory() as session:
            project = Project(name="Test Project", start_kind=NodeKind.workflow)
            session.add(project)
            await session.flush()
            track = Track(project_id=project.id)
            session.add(track)
            await session.commit()
            self.project_id = str(project.id)
            self.track_id = str(track.id)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_mcp_create_node_sets_flag_and_mcp_delete_succeeds(self):
        # 1. Create a node via MCP create_node
        node_res = await mcp_tools.create_node(
            track_id=self.track_id,
            step_index=0,
            node_type="native.crop",
            kind="workflow",
        )
        node_id = node_res["id"]
        self.assertTrue(node_res.get("is_agent_created"))

        # 2. Check get_node returns is_agent_created = True
        read = await mcp_tools.get_node(node_id)
        self.assertTrue(read.get("is_agent_created"))

        # 3. Agent can delete it freely via MCP delete_node
        del_res = await mcp_tools.delete_node(node_id)
        self.assertTrue(del_res["deleted"])

        # Verify node is gone
        async with self.session_factory() as session:
            node = await session.get(Node, uuid.UUID(node_id))
            self.assertIsNone(node)

    async def test_mcp_delete_refused_when_unmarked(self):
        # 1. Create node via MCP
        node_res = await mcp_tools.create_node(
            track_id=self.track_id,
            step_index=0,
            node_type="native.crop",
            kind="workflow",
        )
        node_id = node_res["id"]
        self.assertTrue(node_res.get("is_agent_created"))

        # 2. User reviews and unmarks the node (e.g. via UI / PATCH /api/nodes/{id})
        async with mcp_client.get_client() as client:
            patch_res = await client.patch(f"/api/nodes/{node_id}", json={"is_agent_created": False})
            self.assertEqual(patch_res.status_code, 200)
            self.assertFalse(patch_res.json()["is_agent_created"])

        # 3. Agent attempts delete_node -> should raise RuntimeError with 403 message
        with self.assertRaises(RuntimeError) as ctx:
            await mcp_tools.delete_node(node_id)
        self.assertIn("403", str(ctx.exception))
        self.assertIn("not marked as agent-created", str(ctx.exception))

        # Node should still exist
        async with self.session_factory() as session:
            node = await session.get(Node, uuid.UUID(node_id))
            self.assertIsNotNone(node)

        # 4. User can still delete it directly (require_agent_created=False)
        async with mcp_client.get_client() as client:
            user_del = await client.delete(f"/api/nodes/{node_id}")
            self.assertEqual(user_del.status_code, 204)

        async with self.session_factory() as session:
            node = await session.get(Node, uuid.UUID(node_id))
            self.assertIsNone(node)

    async def test_workflow_output_protection(self):
        # Workflow node created by agent
        wf_res = await mcp_tools.create_node(
            track_id=self.track_id,
            step_index=0,
            node_type="native.crop",
            kind="workflow",
        )
        wf_id = wf_res["id"]

        # Output asset node created, bound to workflow
        async with self.session_factory() as session:
            out_node = Node(
                track_id=uuid.UUID(self.track_id),
                step_index=1,
                kind=NodeKind.asset,
                node_type="asset.single",
                created_by_node_id=uuid.UUID(wf_id),
                is_agent_created=True,
            )
            session.add(out_node)
            await session.commit()
            out_id = str(out_node.id)

        # User unmarks the output node (user liked the picture and wants to keep it)
        async with mcp_client.get_client() as client:
            await client.patch(f"/api/nodes/{out_id}", json={"is_agent_created": False})

        # Agent tries to delete the workflow node -> refused because output is verified!
        with self.assertRaises(RuntimeError) as ctx:
            await mcp_tools.delete_node(wf_id)
        self.assertIn("403", str(ctx.exception))
        self.assertIn("verified/unmarked", str(ctx.exception))

        # Nobody can re-mark through the API (marking is the MCP client's call only)
        async with mcp_client.get_client() as client:
            res = await client.patch(f"/api/nodes/{out_id}", json={"is_agent_created": True})
            self.assertEqual(res.status_code, 422)

        # The person adopting the workflow adopts its outputs too
        async with mcp_client.get_client() as client:
            await client.patch(f"/api/nodes/{wf_id}", json={"is_agent_created": False})
        async with self.session_factory() as session:
            self.assertFalse((await session.get(Node, uuid.UUID(out_id))).is_agent_created)
        async with self.session_factory() as session:
            for nid in (wf_id, out_id):
                (await session.get(Node, uuid.UUID(nid))).is_agent_created = True
            await session.commit()

        # Now agent deletes the workflow node -> succeeds and cascades to output
        del_res = await mcp_tools.delete_node(wf_id)
        self.assertTrue(del_res["deleted"])

        async with self.session_factory() as session:
            wf = await session.get(Node, uuid.UUID(wf_id))
            out = await session.get(Node, uuid.UUID(out_id))
            self.assertIsNone(wf)
            self.assertIsNone(out)

    async def test_flag_comes_from_caller_not_body(self):
        # A plain (web page) client can't mark a node, even by asking to.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://internal",
            headers={"Authorization": mcp_client.get_client().headers["Authorization"]},
        ) as web:
            res = await web.post(
                "/api/nodes",
                json={"track_id": self.track_id, "step_index": 0, "kind": "workflow", "is_agent_created": True},
            )
            self.assertEqual(res.status_code, 201)
            self.assertFalse(res.json()["is_agent_created"])

    async def test_person_referencing_agent_asset_unmarks_it(self):
        src = await mcp_tools.create_node(track_id=self.track_id, step_index=0, node_type="asset.single", kind="asset")
        self.assertTrue(src["is_agent_created"])
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://internal",
            headers={"Authorization": mcp_client.get_client().headers["Authorization"]},
        ) as web:
            res = await web.post(
                "/api/nodes",
                json={
                    "track_id": self.track_id,
                    "step_index": 2,
                    "kind": "asset",
                    "node_type": "asset.refasset",
                    "inputs": [{"type": "explicit", "node_id": src["id"]}],
                },
            )
            self.assertEqual(res.status_code, 201)
        with self.assertRaises(RuntimeError):
            await mcp_tools.delete_node(src["id"])

    async def test_recipe_and_link_reference(self):
        # 1. Create a workflow node
        wf_res = await mcp_tools.create_node(
            track_id=self.track_id,
            step_index=0,
            node_type="native.crop",
            kind="workflow",
        )
        wf_id = wf_res["id"]

        # 2. Check recipe has is_agent_created
        recipe = await mcp_tools.get_project_recipe(self.project_id)
        step_nodes = [n for step in recipe["steps"] for n in step["nodes"]]
        wf_recipe = next((n for n in step_nodes if n["node_id"] == wf_id), None)
        self.assertIsNotNone(wf_recipe)
        self.assertTrue(wf_recipe.get("is_agent_created"))

        # 3. Create a reference node via link_reference_asset
        ref_res = await mcp_tools.link_reference_asset(
            track_id=self.track_id,
            step_index=1,
            source_node_id=wf_id,
            source_asset_id=str(uuid.uuid4()),
        )
        self.assertTrue(ref_res.get("is_agent_created"))
        del_ref = await mcp_tools.delete_node(ref_res["id"])
        self.assertTrue(del_ref["deleted"])

    async def test_delete_and_reroll_broadcasts(self):
        broadcast_events = []
        orig_broadcast = ws_manager.broadcast

        async def capture_broadcast(project_id: str, payload: dict):
            broadcast_events.append((project_id, payload))

        ws_manager.broadcast = capture_broadcast
        try:
            # 1. Create node
            node_res = await mcp_tools.create_node(
                track_id=self.track_id,
                step_index=0,
                node_type="native.crop",
                kind="workflow",
            )
            node_id = node_res["id"]
            self.assertTrue(any(ev[1].get("type") == "node" and ev[1].get("node_id") == node_id for ev in broadcast_events))

            # 2. Reroll node
            broadcast_events.clear()
            async with mcp_client.get_client() as client:
                reroll_res = await client.post(f"/api/nodes/{node_id}/reroll")
                self.assertEqual(reroll_res.status_code, 201)
                new_id = reroll_res.json()["id"]

            # Must broadcast old discarded and new queued
            old_discarded = [ev[1] for ev in broadcast_events if ev[1].get("node_id") == node_id and ev[1].get("status") == "discarded"]
            new_queued = [ev[1] for ev in broadcast_events if ev[1].get("node_id") == new_id and ev[1].get("status") == "queued"]
            self.assertTrue(len(old_discarded) >= 1)
            self.assertTrue(len(new_queued) >= 1)

            # 3. Delete new node
            broadcast_events.clear()
            del_res = await mcp_tools.delete_node(new_id)
            self.assertTrue(del_res["deleted"])
            deleted_ev = [ev[1] for ev in broadcast_events if ev[1].get("type") == "node_deleted" and ev[1].get("node_id") == new_id]
            self.assertTrue(len(deleted_ev) >= 1)
        finally:
            ws_manager.broadcast = orig_broadcast


if __name__ == "__main__":
    unittest.main()
