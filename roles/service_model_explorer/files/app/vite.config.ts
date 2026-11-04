import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

// The catalogue snapshot is the caller's data, not part of the bundle: the role ships it to the
// host separately (model_explorer_catalogue_src), so refreshing it needs no rebuild. For
// `npm run dev`, point MODEL_EXPLORER_CATALOGUE at a snapshot and the dev server serves it at
// the same /catalogue.json path the deployed app fetches.
const CATALOGUE = process.env.MODEL_EXPLORER_CATALOGUE
  ? resolve(process.env.MODEL_EXPLORER_CATALOGUE)
  : undefined;

function catalogue(): Plugin {
  return {
    name: "catalogue",

    // Dev server only: serve the snapshot, so `npm run dev` and a deployed bundle read the same
    // shape from the same path.
    configureServer(server) {
      server.middlewares.use("/catalogue.json", (_req, res) => {
        if (!CATALOGUE || !existsSync(CATALOGUE)) {
          res.statusCode = 404;
          res.end("set MODEL_EXPLORER_CATALOGUE to a catalogue.json snapshot");
          return;
        }
        res.setHeader("Content-Type", "application/json");
        res.end(readFileSync(CATALOGUE, "utf8"));
      });
    },
  };
}

// `base: "./"` so the built bundle works at any mount path. The app is served as plain static
// files by nginx, with no build-time knowledge of its own hostname.
export default defineConfig({
  plugins: [react(), catalogue()],
  base: "./",
});
