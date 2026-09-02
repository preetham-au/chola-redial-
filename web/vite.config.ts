import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ command }) => ({
  // Nginx serves the built app under /redial/ and proxies /redial/api/ to the
  // backend. `api.ts` derives its API prefix from BASE_URL, so this one value
  // decides where BOTH the assets and the API calls point. It lives here rather
  // than in a `--base` flag because a forgotten flag ships a console whose every
  // request 404s against the wrong app. Dev stays at the root.
  base: command === 'build' ? '/redial/' : '/',
  plugins: [react()],
  server: {
    port: 5173,
    // Vite rejects requests whose Host header it does not recognise. ngrok
    // rewrites Host to a *.ngrok-free.app name, so the tunnel 403s without this.
    allowedHosts: ['.ngrok-free.dev', '.ngrok-free.app', '.ngrok.io', '.ngrok.app'],
    proxy: {
      '/api': {
        // 8000 is the port run.bat, the README and API_CONTRACT.md all name.
        // This said 8082, so `npm run dev` proxied every call to a closed port
        // and the console came up looking dead -- no campaigns, no buckets, no
        // error worth reading, just failed fetches against a server that was
        // running fine one port over.
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
}));
