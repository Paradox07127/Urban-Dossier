import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import path from 'path';
import { defineConfig, loadEnv } from 'vite';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '');

  return {
    plugins: [react(), tailwindcss()],
    define: {
      'process.env.GEMINI_API_KEY': JSON.stringify(env.GEMINI_API_KEY),
    },
    resolve: {
      alias: {
        '@': path.resolve(__dirname, '.'),
      },
    },
    server: {
      hmr: process.env.DISABLE_HMR !== 'true',
      port: 3000,
      host: '0.0.0.0',
      proxy: {
        '/api': {
          target: 'http://localhost:3456',
          changeOrigin: true,
        },
        '/tiles': {
          target: 'http://localhost:3456',
          changeOrigin: true,
        },
        '/metadata': {
          target: 'http://localhost:3456',
          changeOrigin: true,
        },
        '/fonts': {
          target: 'http://localhost:3456',
          changeOrigin: true,
        },
      },
    },
    build: {
      rollupOptions: {
        input: {
          main: path.resolve(__dirname, 'index.html'),
          buildingIdTest: path.resolve(__dirname, 'building-id-test.html'),
        },
      },
    },
  };
});
