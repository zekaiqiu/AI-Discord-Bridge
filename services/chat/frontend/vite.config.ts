import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// SPA build configuration.
//
// outDir points at ../static/ so `npm run build` lands the bundle exactly
// where Phase 3's FastAPI backend expects it (services/chat/static/). The
// Dockerfile's stage-1 also relies on this so it can `COPY --from=frontend
// /build/static /app/static` without any extra rename step.
//
// The dev proxy is for `npm run dev` only: it forwards /api and /healthz to
// a local uvicorn so a coder can iterate on the SPA without rebuilding the
// container. In production the SPA is served by the same FastAPI process,
// so /api requests are same-origin and the Cloudflare Access cookie is
// included automatically — no proxy needed.
export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    outDir: "../static",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: false,
      },
      "/healthz": {
        target: "http://localhost:8000",
        changeOrigin: false,
      },
    },
  },
});
