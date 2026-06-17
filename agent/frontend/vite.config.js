import { defineConfig } from "vite";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL(".", import.meta.url));

export default defineConfig({
  build: {
    emptyOutDir: false,
    outDir: resolve(root, "../src/assistant_agent/ui/assets"),
    rollupOptions: {
      input: resolve(root, "src/workspace.js"),
      output: {
        entryFileNames: "workspace.bundle.js",
        chunkFileNames: "workspace.[name].js",
        assetFileNames: assetInfo => {
          if (assetInfo.name && assetInfo.name.endsWith(".css")) return "workspace.bundle.css";
          return "workspace.[name][extname]";
        }
      }
    }
  }
});
