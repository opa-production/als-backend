import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Matches the origin already listed in the API's CORS_ORIGINS. Changing it
    // here means changing it there too, or every request fails in the browser
    // before it reaches the service.
    strictPort: true,
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
