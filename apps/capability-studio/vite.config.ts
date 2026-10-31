import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "/",
  plugins: [react()],
  build: {
    // Generated bundles are deployment artifacts, never Python source files.
    outDir: "../../../runtime/work/capability-studio",
    emptyOutDir: true,
    sourcemap: false,
  },
  test: {
    environment: "node",
  },
});
