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
    // A second entry point, building-id-test.html, built a standalone MapLibre
    // page for checking building highlighting. It shipped ~10 KB of JS to
    // production for a check the main app can now do, and it carried its own
    // copy of the landmark table and of createCirclePolygon -- the landmark
    // copy with latitude and longitude in the opposite order to App.tsx's,
    // which is the kind of divergence duplicated tables always end at.
    build: {
      rollupOptions: {
        input: {
          main: path.resolve(__dirname, 'index.html'),
        },
        output: {
          // maplibre-gl is 1 MB of the 1.5 MB bundle and changes only when the
          // dependency is upgraded, so it is split out and cached on its own.
          //
          // It used to be split anyway, as a side effect: two entry points both
          // imported it, so Rollup hoisted it into a shared chunk. Dropping the
          // debug entry silently undid that and folded the megabyte back into
          // main, where every edit to a component would have invalidated it.
          // Stating the split makes it survive the next change to the inputs.
          manualChunks: {
            'maplibre-gl': ['maplibre-gl'],
          },
        },
      },
    },
  };
});
