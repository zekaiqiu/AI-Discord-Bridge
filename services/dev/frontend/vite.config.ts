import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Emit to ../static so the Dockerfile's COPY --from=frontend lands the
// build at /app/static, where FastAPI's StaticFiles mount picks it up.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../static",
    emptyOutDir: true,
    sourcemap: false,
    // Monaco is big; chunk it out so the initial HTML+CSS load is small.
    rollupOptions: {
      output: {
        manualChunks: {
          monaco: ["monaco-editor", "@monaco-editor/react"],
          react: ["react", "react-dom"],
        },
      },
    },
  },
  server: {
    port: 5174,
    proxy: {
      "/api": "http://localhost:8000",
      "/healthz": "http://localhost:8000",
    },
  },
});
