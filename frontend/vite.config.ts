import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: proxy API to the local service. Build output is copied into backend/adstudio/static at release time.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://127.0.0.1:8765" } },
  build: { outDir: "../backend/adstudio/static", emptyOutDir: true },
});
