import { defineConfig } from "vite";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL(".", import.meta.url));

export default defineConfig({
  esbuild: { jsx: "automatic", jsxImportSource: "preact" },
  build: {
    emptyOutDir: false,
    outDir: resolve(root, "../src/assistant_agent/ui/assets"),
    rollupOptions: {
      input: {
        workspace: resolve(root, "src/workspace.js"),
        chat: resolve(root, "src/chat/main.jsx")
      },
      output: {
        entryFileNames: "[name].bundle.js",
        chunkFileNames: "[name].chunk.js",
        assetFileNames: assetInfo => {
          if (assetInfo.name && assetInfo.name.endsWith(".css")) return "[name].bundle.css";
          return "workspace.[name][extname]";
        }
      }
    }
  }
});
