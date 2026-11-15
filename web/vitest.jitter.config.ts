import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// `npm run test:jitter`: the tests of vite.config.ts, with every fake fetch answering late and out of order
// (src/test/fetchJitter.ts).
export default defineConfig({
  plugins: [react()],
  test: { environment: "jsdom", include: ["src/**/*.test.{ts,tsx}"], setupFiles: ["./src/test/fetchJitter.ts"] },
});
