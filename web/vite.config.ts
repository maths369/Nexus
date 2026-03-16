import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5175,
    proxy: {
      "/ws": {
        target: "http://127.0.0.1:8000",
        ws: true,
      },
      "/documents": {
        target: "http://127.0.0.1:8000",
      },
      "/health": {
        target: "http://127.0.0.1:8000",
      },
    },
  },
});
