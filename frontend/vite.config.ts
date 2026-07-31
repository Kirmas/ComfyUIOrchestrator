import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  resolve: {
    // Resolve bare `three` to its pre-minified build. model-viewer pulls three
    // in as one ~0.6 MB module, and parsing that single huge file is the most
    // expensive thing in our build; the minified copy is 0.34 MB and measured
    // 65 MB less peak RSS for the full `npm run build` (515 -> 450 MB). This
    // box had been dying about 56 MB short, so that margin is the whole point.
    // Emitted output is unchanged (1,070 vs 1,072 kB) -- it gets minified
    // either way.
    //
    // Exact-match regex, NOT a plain string key: a string alias also rewrites
    // deep imports like `three/examples/jsm/exporters/USDZExporter.js` and the
    // build fails with ENOTDIR. Absolute path because three's package exports
    // map doesn't expose ./build/three.module.min.js by name.
    alias: [
      {
        find: /^three$/,
        replacement: fileURLToPath(new URL("./node_modules/three/build/three.module.min.js", import.meta.url)),
      },
    ],
  },
  server: {
    port: 5173,
  },
});
