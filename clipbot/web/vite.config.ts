import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// `npm run dev` proxies the API to a clipbot running on this machine (default :8080).
const api = process.env.CLIPBOT_DEV_API ?? "http://127.0.0.1:8080"

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  server: { proxy: { "/api": { target: api, changeOrigin: true } } },
  build: { chunkSizeWarningLimit: 1200 },
})
