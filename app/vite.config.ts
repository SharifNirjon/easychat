// The onboarding app: built into dist/ and served by the API at /app (same origin as the API,
// so the session cookie needs no cross-site rules). `npm run dev` rebuilds on every change.
import { defineConfig } from 'vite';

export default defineConfig({
  base: '/app/',
  esbuild: { jsx: 'automatic', jsxImportSource: 'preact' },   // Preact without an extra plugin
  build: { outDir: 'dist', emptyOutDir: true, target: 'es2020' },
});
