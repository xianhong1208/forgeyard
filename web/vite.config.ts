import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The server serves the built UI from forgeyard/static. `npm run dev` proxies the API to a
// server on :7860 (the default port), which speaks HTTPS with its own CA: the dev proxy does not
// check that certificate (it only ever talks to this machine's own server).
export default defineConfig({
  plugins: [react()],
  build: { outDir: "../forgeyard/static", emptyOutDir: true },
  server: { proxy: { "/api": { target: "https://127.0.0.1:7860", secure: false } } },
  test: { environment: "jsdom", include: ["src/**/*.test.{ts,tsx}"] },
});
