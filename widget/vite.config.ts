// Builds the widget as ONE self-contained script: dist/widget.js (IIFE, CSS inlined),
// plus dist/fonts/ (copied from public/) which widget.js loads from its own origin.
import { defineConfig, loadEnv } from 'vite';

export default defineConfig(({ mode }) => {
  // A widget built without an API base would silently call localhost from production sites.
  const env = { ...loadEnv(mode, process.cwd(), 'VITE_'), ...process.env };
  if (!env.VITE_API_BASE || !/^https?:\/\/[^/]+$/.test(env.VITE_API_BASE.replace(/\/$/, ''))) {
    throw new Error('VITE_API_BASE must be set to the API origin, e.g. https://api.example.com (see widget/.env.example)');
  }

  return {
    build: {
      lib: {
        entry: 'src/main.ts',
        name: 'EasychatWidget',
        formats: ['iife'],
        fileName: () => 'widget.js',
      },
      target: 'es2020',
      outDir: 'dist',
      emptyOutDir: true,
      sourcemap: false,
    },
  };
});
