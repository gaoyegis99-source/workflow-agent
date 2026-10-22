import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "/",
  plugins: [react()],
  build: {
    outDir: "../platform-api/src/platform_api/studio_static",
    emptyOutDir: true,
    sourcemap: false,
  },
  test: {
    environment: "node",
  },
});
