import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The production bundle is static files served by FastAPI (§103);
// `npm run dev` proxies /api to the local workbench server.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
