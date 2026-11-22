# ComfyUI Orchestrator Bridge for Unreal (5.8)

Install: put the `OrchestratorBridge` folder into your project's `Plugins/`
(create the folder if missing), open the project, accept the rebuild prompt
(needs Visual Studio with the "Game development with C++" workload). If the
project is Blueprint-only, add any C++ class once (Tools > New C++ Class) so
it can compile plugins.

Pairing (no token to copy anywhere):
1. Project Settings > Plugins > Orchestrator Bridge: set Server Url
   (e.g. `http://192.168.0.3:8000`).
2. Tools > Orchestrator Bridge > Connect / Pair. A notification shows a code.
3. On the site: Settings > Unreal / Blender bridge -- the request appears with
   the same code. Approve it. The editor gets its own device key, valid for a
   month (stored in Saved/Config, per user); after that, pair again.
   Revoking it on the site disconnects the editor at once.

- Orchestrator -> Unreal: the ⇪ button on an asset's full-size view. It lands
  in `/Game/Orchestrator/<project>/`; sending again replaces it. Unreal must
  be open and connected -- otherwise it isn't offered, nothing is queued.
- Unreal -> Orchestrator: Content Browser, right-click assets > Send to
  Orchestrator... Meshes go as GLB (FBX if that fails), animations as FBX with just the skeleton and the clip (no preview mesh), textures as PNG.
