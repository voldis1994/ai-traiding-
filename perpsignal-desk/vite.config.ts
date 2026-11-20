import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Production build is served by FastAPI at /desk/
export default defineConfig({
  plugins: [react()],
  base: "/desk/",
  build: {
    outDir: "../app/web/desk",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8765",
      "/admin": "http://127.0.0.1:8765",
    },
  },
});
