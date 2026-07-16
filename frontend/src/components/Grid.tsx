import { useEffect, useMemo, useRef, useState } from "react";
import { assetsApi, backendsApi, capabilitiesApi, nodesApi, nodeTemplatesApi, projectsApi, tracksApi } from "../api/endpoints";
import { useProjectWs } from "../api/useProjectWs";
import { useProjectStore } from "../state/projectStore";
import type { Asset, Backend, Capability, NodeItem, NodeKind, NodeTemplate, Project, Track } from "../types";
import { ArrowsOverlay, type Edge } from "./ArrowsOverlay";
import { CompareModal } from "./CompareModal";
import { NodeCell } from "./NodeCell";

// A workflow node's result always materializes as a *following* asset node
// (see worker/tasks.py), so the next free step after a workflow node is
// step+2 -- step+1 is reserved for that asset node even before it exists.
function nextStepIndexFor(nodes: NodeItem[]): number {
  if (nodes.length === 0) return 0;
  const last = nodes[nodes.length - 1];
  return last.kind === "workflow" ? last.step_index + 2 : last.step_index + 1;
}

// Mirrors _kind_for_step in backend/app/api/routes/nodes.py: once a
// project's start_kind is set, every column alternates asset/workflow from
// it regardless of which track it's in -- so a brand-new track's first
// column is never a free choice, it's whatever the pattern already says.
function kindForStep(startKind: NodeKind, stepIndex: number): NodeKind {
  const opposite: NodeKind = startKind === "asset" ? "workflow" : "asset";
  return stepIndex % 2 === 0 ? startKind : opposite;
}

// "pick cell..." may only target a settled asset cell with exactly one
// resolved output -- a chooser cell (node_type "asset.select", still showing
// several undecided candidates) has no single well-defined picture to grab,
// so letting it be picked meant silently grabbing whichever candidate
// happened to be "selected" or first in the list, never a choice the user
// actually made. Resolve the chooser (select ★ one) first, then it becomes
// pickable (node_type flips to "asset.single").
function isPickable(node: NodeItem, outputs: Asset[]): boolean {
  return node.kind === "asset" && node.node_type !== "asset.select" && outputs.length > 0;
}

export function Grid({ projectId }: { projectId: string }) {
  const { tracks, nodesById, outputsByNode, loadProject, applyProgressEvent, addTrack, addNode, removeTrack, refreshNodeOutputs, setTracks } =
    useProjectStore();
  const [templates, setTemplates] = useState<NodeTemplate[]>([]);
  const [backends, setBackends] = useState<Backend[]>([]);
  const [capabilities, setCapabilities] = useState<Capability[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [pickingFor, setPickingFor] = useState<{ nodeId: string; slotIndex: number } | null>(null);
  // Compare mode spans the whole project, not just one node's own candidates
  // -- compareFor is the anchor asset (from wherever "⇄" was clicked), and
  // clicking any other pickable asset-node cell completes the pair (same
  // click-to-complete gesture pickingFor already uses for input slots).
  const [compareFor, setCompareFor] = useState<{ nodeId: string; asset: Asset } | null>(null);
  const [comparePair, setComparePair] = useState<[Asset, Asset] | null>(null);
  // How many leading columns an as-yet-empty track should skip before its
  // first real cell -- purely a UI notion (never sent to the backend on its
  // own): a track only gets a step_index once a node actually exists at it,
  // so "skip" just moves where the "+ asset"/"+ step" button appears next,
  // and gets attached to the node the moment one is finally created there.
  // Irrelevant (and cleared) once the track has any node.
  const [emptyTrackSkip, setEmptyTrackSkip] = useState<Record<string, number>>({});
  const containerRef = useRef<HTMLDivElement>(null);
  const cellRefs = useRef(new Map<string, HTMLDivElement>()).current;

  const reloadProject = () => projectsApi.get(projectId).then(setProject);

  useEffect(() => {
    loadProject(projectId);
    reloadProject();
    nodeTemplatesApi.list().then(setTemplates);
    backendsApi.list().then(setBackends);
    capabilitiesApi.list().then(setCapabilities);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, loadProject]);

  useProjectWs(projectId, applyProgressEvent);

  const nodesByTrack = useMemo(() => {
    const map = new Map<string, NodeItem[]>();
    for (const node of Object.values(nodesById)) {
      if (!map.has(node.track_id)) map.set(node.track_id, []);
      map.get(node.track_id)!.push(node);
    }
    for (const list of map.values()) list.sort((a, b) => a.step_index - b.step_index);
    return map;
  }, [nodesById]);

  const maxStep = useMemo(() => {
    let max = 0;
    for (const node of Object.values(nodesById)) max = Math.max(max, node.step_index);
    return max;
  }, [nodesById]);

  // Widest column any track's own "+ step" button could land on -- each track's
  // button sits right after *its own* last cell, not a shared global column.
  // For a still-empty track, "skipped" columns push that button out further
  // still, so the grid needs to be wide enough to show it.
  const maxButtonStep = useMemo(() => {
    let max = 0;
    for (const track of tracks) {
      const trackNodes = nodesByTrack.get(track.id) ?? [];
      const base = nextStepIndexFor(trackNodes);
      const step = trackNodes.length === 0 ? base + (emptyTrackSkip[track.id] ?? 0) : base;
      max = Math.max(max, step);
    }
    return Math.max(max, maxStep);
  }, [tracks, nodesByTrack, maxStep, emptyTrackSkip]);

  const sortedTracks = useMemo(() => [...tracks].sort((a, b) => a.row_index - b.row_index), [tracks]);

  const edges = useMemo<Edge[]>(() => {
    const result: Edge[] = [];
    const trackByRow = new Map<number, Track>(tracks.map((t) => [t.row_index, t]));

    for (const node of Object.values(nodesById)) {
      const track = tracks.find((t) => t.id === node.track_id);
      const isFirstInTrack = (nodesByTrack.get(node.track_id) ?? [])[0]?.id === node.id;

      node.inputs.forEach((ref) => {
        if (ref.type === "explicit") {
          // Skip drawing a duplicate merge arrow when this *is* the track's
          // spawn link (already drawn as a "spawn" edge below).
          const isSpawnLink =
            isFirstInTrack && track?.spawned_from_node_id === ref.node_id && track?.spawned_from_output_id === ref.output_id;
          if (!isSpawnLink) result.push({ from: ref.node_id, to: node.id, kind: "merge" });
        } else if (ref.type === "track_below_prev") {
          if (!track) return;
          const below = trackByRow.get(track.row_index + 1);
          if (!below) return;
          const candidates = (nodesByTrack.get(below.id) ?? []).filter((n) => n.step_index <= node.step_index);
          const source = candidates[candidates.length - 1];
          if (source) result.push({ from: source.id, to: node.id, kind: "merge" });
        }
      });
    }

    for (const track of tracks) {
      if (!track.spawned_from_node_id) continue;
      const firstNode = (nodesByTrack.get(track.id) ?? [])[0];
      if (firstNode) result.push({ from: track.spawned_from_node_id, to: firstNode.id, kind: "spawn" });
    }

    return result;
  }, [nodesById, nodesByTrack, tracks]);

  const registerRef = (nodeId: string, el: HTMLDivElement | null) => {
    if (el) cellRefs.set(nodeId, el);
    else cellRefs.delete(nodeId);
  };

  const addTrackRow = async () => {
    const rowIndex = tracks.length === 0 ? 0 : Math.max(...tracks.map((t) => t.row_index)) + 1;
    const track = await tracksApi.create({ project_id: projectId, row_index: rowIndex });
    addTrack(track);
  };

  const deleteTrackRow = async (trackId: string) => {
    if (!confirm("Delete this whole track and every cell in it? This can't be undone.")) return;
    await tracksApi.remove(trackId);
    removeTrack(trackId);
  };

  // Pure layout nudge (e.g. lining a spawned branch up under its parent's
  // column) -- always +/-2 so asset/workflow kind alternation (keyed off
  // step_index parity) isn't disturbed. Doesn't account for track_below_prev
  // inputs on this track or its row_index neighbors (see shift_track's
  // docstring in tracks.py) -- fine for a purely cosmetic nudge, but a track
  // that feeds/is fed by one via "track below" can resolve differently after.
  const shiftTrack = async (trackId: string, delta: number) => {
    const updated = await tracksApi.shift(trackId, delta).catch((err) => {
      alert(err instanceof Error ? err.message : "Couldn't shift this track.");
      return null;
    });
    if (updated) for (const node of updated) addNode(node);
  };

  // row_index is both the visible "track N" label and, via track_below_prev
  // inputs (see the edges memo above), an adjacency link to the track right
  // below -- so a move always reindexes *every* track to a contiguous
  // 0..N-1 run rather than just swapping the two endpoints, or a merge
  // pointing at "row_index + 1" could end up skipping a row or landing on
  // the wrong one.
  const moveTrack = async (draggedId: string, targetId: string) => {
    if (draggedId === targetId) return;
    const ids = sortedTracks.map((t) => t.id);
    const fromIdx = ids.indexOf(draggedId);
    const toIdx = ids.indexOf(targetId);
    if (fromIdx === -1 || toIdx === -1) return;
    ids.splice(toIdx, 0, ids.splice(fromIdx, 1)[0]);

    const byId = new Map(tracks.map((t) => [t.id, t]));
    const reindexed = ids.map((id, idx) => ({ ...byId.get(id)!, row_index: idx }));
    setTracks(reindexed);

    const changed = reindexed.filter((t) => byId.get(t.id)!.row_index !== t.row_index);
    await Promise.all(changed.map((t) => tracksApi.update(t.id, { row_index: t.row_index })));
  };

  const moveTrackStep = (trackId: string, direction: -1 | 1) => {
    const idx = sortedTracks.findIndex((t) => t.id === trackId);
    const targetIdx = idx + direction;
    if (idx === -1 || targetIdx < 0 || targetIdx >= sortedTracks.length) return;
    moveTrack(trackId, sortedTracks[targetIdx].id);
  };

  const nextStepIndex = (trackId: string): number => nextStepIndexFor(nodesByTrack.get(trackId) ?? []);

  // Column kind is a project-wide pattern, not a per-node choice (see nodes.py's
  // create_node): only the very first node in the whole project picks it; every
  // add after that just sends a placeholder kind that the backend overrides
  // based on step_index parity. stepIndexOverride is for a track whose first
  // cell got pushed past column 0 by the "empty" skip button below.
  const addStep = async (trackId: string, firstKindChoice?: NodeKind, stepIndexOverride?: number) => {
    const node = await nodesApi.create({
      track_id: trackId,
      step_index: stepIndexOverride ?? nextStepIndex(trackId),
      kind: firstKindChoice ?? "workflow",
    });
    addNode(node);
    if (project?.start_kind == null) reloadProject();
    setEmptyTrackSkip((prev) => {
      if (!(trackId in prev)) return prev;
      const next = { ...prev };
      delete next[trackId];
      return next;
    });
  };

  // No leftovers: there's only one possible outcome for this cell, so it's
  // not really "a complex node becoming a simple one" in any sense worth
  // avoiding -- just flip its node_type in place. (See below for the case
  // that actually matters.)
  const settleSoleCandidate = async (sourceNode: NodeItem): Promise<void> => {
    const settledNode = await nodesApi.update(sourceNode.id, { node_type: "asset.single" });
    addNode(settledNode);
    await refreshNodeOutputs(sourceNode.id);
  };

  // When there ARE leftovers, this is a genuine fork: the cell that
  // generated N candidates now has to become two different things (one
  // settled result, one still-undecided picker for the rest) -- and rather
  // than mutating sourceNode's role in place to be whichever one is more
  // convenient (see the 2026-07-17 review: "чому мутуємо складнішу ноду в
  // простішу, а не навпаки"), neither identity gets repurposed:
  //   1. sourceNode itself relocates (same id, same job/asset history) into
  //      the new spawned track, keeping its "still needs choosing" role and
  //      its remaining (non-kept) candidates.
  //   2. A brand new "asset.single" node is created in the vacated original
  //      cell, holding only the kept asset.
  // Moving (PATCH track_id) rather than delete+recreate matters here: DELETE
  // /api/nodes/{id} cascades forward through the rest of the track (right
  // for the user's trash-icon action, wrong for this internal reshuffle).
  //
  // Returns the relocated picker node (or undefined if there were no
  // leftovers) -- NodeCell.tsx's "Select all" calls this repeatedly in a
  // tight loop to cascade every candidate into its own line (pick one, get
  // the leftover picker back, pick one from *that*, and so on), so it reads
  // live store state via getState() instead of this render's closed-over
  // tracks/nodesByTrack -- those go stale after the first iteration of a
  // cascade that never waits for Grid to re-render in between.
  const onSelectCandidate = async (sourceNode: NodeItem, kept: Asset, others: Asset[]): Promise<NodeItem | undefined> => {
    const originalTrackId = sourceNode.track_id;
    const originalStepIndex = sourceNode.step_index;
    let relocatedPicker: NodeItem | undefined;

    if (others.length === 0) {
      await settleSoleCandidate(sourceNode);
    } else {
      // Attribute the branch to whatever actually produced these candidates,
      // not to sourceNode itself: usually the preceding workflow cell in the
      // same track ("Create image"). But sourceNode may itself be a leftover
      // picker from an earlier split in a "Select all" cascade -- it has
      // nothing before it in its own (freshly created) track, so in that
      // case carry forward whatever cause its *own* track was already
      // spawned from, rather than re-deriving one from this intermediate
      // cell. That keeps every arrow in a cascade pointing back at the one
      // true source instead of chaining picker -> picker -> picker.
      const liveState = useProjectStore.getState();
      const precedingWorkflow = Object.values(liveState.nodesById).find(
        (n) => n.track_id === originalTrackId && n.step_index === originalStepIndex - 1 && n.kind === "workflow",
      );
      const ownTrack = liveState.tracks.find((t) => t.id === originalTrackId);
      const causeNodeId = precedingWorkflow?.id ?? ownTrack?.spawned_from_node_id ?? sourceNode.id;

      const liveTracks = liveState.tracks;
      const rowIndex = liveTracks.length === 0 ? 0 : Math.max(...liveTracks.map((t) => t.row_index)) + 1;
      const newTrack = await tracksApi.create({
        project_id: projectId,
        row_index: rowIndex,
        spawned_from_node_id: causeNodeId,
        spawned_from_output_id: kept.id,
      });
      addTrack(newTrack);

      // 1) Create the settled single-asset node in the vacated original cell...
      const settledNode = await nodesApi.create({
        track_id: originalTrackId,
        step_index: originalStepIndex,
        kind: "asset",
        node_type: "asset.single",
      });
      addNode(settledNode);
      await assetsApi.move(kept.id, settledNode.id);
      await refreshNodeOutputs(settledNode.id);

      // 2) ...then relocate sourceNode (still holding `others`) into the new
      // track. Only after the kept asset is safely off of it -- otherwise a
      // crash between these two steps would leave the kept asset attached
      // to a node about to be moved out from under the user's expectation
      // of "this cell now shows the picked image".
      const movedSource = await nodesApi.update(sourceNode.id, { track_id: newTrack.id });
      addNode(movedSource);
      await refreshNodeOutputs(movedSource.id);

      relocatedPicker = movedSource;
    }

    const workflowStepIndex = originalStepIndex + 1;
    const liveNodes = Object.values(useProjectStore.getState().nodesById);
    const hasNextStep = liveNodes.some((n) => n.track_id === originalTrackId && n.step_index === workflowStepIndex);
    if (!hasNextStep) {
      const workflowCell = await nodesApi.create({ track_id: originalTrackId, step_index: workflowStepIndex, kind: "workflow" });
      addNode(workflowCell);
    }

    return relocatedPicker;
  };

  const onCellClicked = async (node: NodeItem) => {
    if (pickingFor && pickingFor.nodeId !== node.id) {
      const outputs = await nodesApi.outputs(node.id).catch(() => []);
      const asset = outputs.find((a) => a.selected) ?? outputs[0];
      if (!asset) {
        alert("This cell has no outputs yet.");
        return;
      }
      const target = nodesById[pickingFor.nodeId];
      const inputs = [...target.inputs];
      inputs[pickingFor.slotIndex] = { type: "explicit", node_id: node.id, output_id: asset.id };
      const updated = await nodesApi.update(target.id, { inputs });
      addNode(updated);
      setPickingFor(null);
      return;
    }
    if (compareFor && compareFor.nodeId !== node.id) {
      const outputs = await nodesApi.outputs(node.id).catch(() => []);
      const asset = outputs.find((a) => a.selected) ?? outputs[0];
      if (!asset) {
        alert("This cell has no outputs yet.");
        return;
      }
      setComparePair([compareFor.asset, asset]);
      setCompareFor(null);
    }
  };

  const onStartCompare = (node: NodeItem, asset: Asset) => setCompareFor({ nodeId: node.id, asset });

  return (
    <div className="main-area" ref={containerRef}>
      <div className="grid-wrapper">
        <ArrowsOverlay edges={edges} cellRefs={cellRefs} containerRef={containerRef} deps={[nodesById, tracks]} />
        <div
          className="grid-canvas"
          style={{
            gridTemplateColumns: `120px repeat(${maxButtonStep + 2}, 260px)`,
            gridTemplateRows: `repeat(${Math.max(sortedTracks.length, 1)}, auto)`,
          }}
        >
          {sortedTracks.map((track, rowIdx) => (
            <div
              key={`label-${track.id}`}
              className="track-label"
              style={{ gridColumn: 1, gridRow: rowIdx + 1, display: "flex", alignItems: "center", justifyContent: "space-between" }}
            >
              track {track.row_index}
              <div style={{ display: "flex", gap: 4 }}>
                <button
                  onClick={() => moveTrackStep(track.id, -1)}
                  disabled={rowIdx === 0}
                  title="Move this track up"
                  style={{ fontSize: 10, padding: "1px 4px" }}
                >
                  ↑
                </button>
                <button
                  onClick={() => moveTrackStep(track.id, 1)}
                  disabled={rowIdx === sortedTracks.length - 1}
                  title="Move this track down"
                  style={{ fontSize: 10, padding: "1px 4px" }}
                >
                  ↓
                </button>
                <button
                  onClick={() => shiftTrack(track.id, -2)}
                  title="Shift this whole track 2 columns to the left (layout only, doesn't change generation)"
                  style={{ fontSize: 10, padding: "1px 4px" }}
                >
                  ←
                </button>
                <button
                  onClick={() => shiftTrack(track.id, 2)}
                  title="Shift this whole track 2 columns to the right (layout only, doesn't change generation)"
                  style={{ fontSize: 10, padding: "1px 4px" }}
                >
                  →
                </button>
                <button onClick={() => deleteTrackRow(track.id)} title="Delete this track and all its cells" style={{ fontSize: 10, padding: "1px 4px" }}>
                  ×
                </button>
              </div>
            </div>
          ))}

          {sortedTracks.map((track, rowIdx) => {
            const trackNodes = nodesByTrack.get(track.id) ?? [];
            const lastNodeId = trackNodes[trackNodes.length - 1]?.id;
            return trackNodes.map((node) => (
              <div key={node.id} style={{ gridColumn: node.step_index + 2, gridRow: rowIdx + 1 }}>
                <NodeCell
                  node={node}
                  templates={templates}
                  backends={backends}
                  capabilities={capabilities}
                  outputs={outputsByNode[node.id] ?? []}
                  pickingActive={
                    pickingFor !== null && pickingFor.nodeId !== node.id && isPickable(node, outputsByNode[node.id] ?? [])
                  }
                  isPickingSource={pickingFor?.nodeId === node.id}
                  compareActive={
                    compareFor !== null && compareFor.nodeId !== node.id && isPickable(node, outputsByNode[node.id] ?? [])
                  }
                  isComparingSource={compareFor?.nodeId === node.id}
                  isLastInTrack={node.id === lastNodeId}
                  registerRef={registerRef}
                  onStartPicking={(nodeId, slotIndex) => setPickingFor({ nodeId, slotIndex })}
                  onCellClicked={onCellClicked}
                  onSelectCandidate={onSelectCandidate}
                  onStartCompare={onStartCompare}
                />
              </div>
            ));
          })}

          {sortedTracks.map((track, rowIdx) => {
            const showStartChoice = project?.start_kind == null;
            // A workflow cell always grows its own next step on its own --
            // draft (choose a template, generate) or done (its output already
            // materialized the following asset cell) -- so "+ step" would
            // only ever add a premature, redundant slot after it. It's only
            // ever needed after a manually-filled asset cell (no generation
            // involved, so nothing auto-chains forward from it).
            const lastNode = (nodesByTrack.get(track.id) ?? []).slice(-1)[0];
            const canAddStep = !showStartChoice && lastNode?.kind === "asset";
            // A track with no cells yet (freshly added via "+ New track")
            // has nothing to auto-chain its first cell either -- same
            // problem as the asset case above, just for whichever column its
            // button currently sits on. Unlike the very first track in the
            // project, it's not a free choice: start_kind already fixes what
            // kind every column is, so show the one button that matches
            // instead of asking -- but "empty" lets the user push that
            // button (and the kind it offers) out to the next column, for
            // tracks that shouldn't start where the pattern says.
            const buttonStep = !showStartChoice && !lastNode ? nextStepIndex(track.id) + (emptyTrackSkip[track.id] ?? 0) : nextStepIndex(track.id);
            const emptyTrackKind = !showStartChoice && !lastNode ? kindForStep(project!.start_kind!, buttonStep) : null;
            const skipColumn = () => setEmptyTrackSkip((prev) => ({ ...prev, [track.id]: (prev[track.id] ?? 0) + 1 }));
            return (
              <div
                key={`add-${track.id}`}
                style={{ gridColumn: buttonStep + 2, gridRow: rowIdx + 1, alignSelf: "center", display: "flex", gap: 4 }}
              >
                {showStartChoice ? (
                  <>
                    <button onClick={() => addStep(track.id, "asset")} title="First column in this project: asset cells (manual upload)">
                      + start with asset
                    </button>
                    <button onClick={() => addStep(track.id, "workflow")} title="First column in this project: workflow cells">
                      + start with workflow
                    </button>
                  </>
                ) : canAddStep ? (
                  <button onClick={() => addStep(track.id)} title="Add a workflow cell after this manually-filled asset cell">
                    + step
                  </button>
                ) : emptyTrackKind === "asset" ? (
                  <>
                    <button onClick={() => addStep(track.id, "asset", buttonStep)} title="This column in this track: asset cell (manual upload)">
                      + asset
                    </button>
                    <button onClick={skipColumn} title="Leave this column blank for this track and offer the next one instead">
                      empty
                    </button>
                  </>
                ) : emptyTrackKind === "workflow" ? (
                  <>
                    <button onClick={() => addStep(track.id, "workflow", buttonStep)} title="This column in this track: workflow cell">
                      + step
                    </button>
                    <button onClick={skipColumn} title="Leave this column blank for this track and offer the next one instead">
                      empty
                    </button>
                  </>
                ) : null}
              </div>
            );
          })}
        </div>

        <div style={{ marginTop: 16 }}>
          <button onClick={addTrackRow}>+ New track</button>
          {pickingFor && (
            <button style={{ marginLeft: 8 }} onClick={() => setPickingFor(null)}>
              Cancel picking
            </button>
          )}
          {compareFor && (
            <button style={{ marginLeft: 8 }} onClick={() => setCompareFor(null)}>
              Cancel comparing
            </button>
          )}
        </div>
      </div>

      {comparePair && <CompareModal left={comparePair[0]} right={comparePair[1]} onClose={() => setComparePair(null)} />}
    </div>
  );
}
