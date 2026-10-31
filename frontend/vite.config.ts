import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const apiTarget = env.VITE_DEV_API_TARGET || "http://127.0.0.1:8765";
  return {
    plugins: [react()],
    server: {
      host: "127.0.0.1",
      port: Number(env.FRONTEND_PORT || 5173),
      proxy: {
        "/api": {
          target: apiTarget,
          changeOrigin: false,
        },
        "/media": {
          target: apiTarget,
          changeOrigin: false,
        },
      },
    },
  };
});
