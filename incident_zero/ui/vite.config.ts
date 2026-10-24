import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` proxies the API to a running `incident_zero up`.
const api = process.env.IZ_API ?? "http://127.0.0.1:8700";

export default defineConfig({
  base: "/",
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5190,
    proxy: { "/api": { target: api, changeOrigin: true } },
  },
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false },
});
