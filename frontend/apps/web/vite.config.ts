import {defineConfig} from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    watch: {usePolling: process.env.VITE_USE_POLLING === '1', interval: 120},
    proxy: {'/api': 'http://127.0.0.1:3000', '/health': 'http://127.0.0.1:3000', '/version': 'http://127.0.0.1:3000'}
  }
});
