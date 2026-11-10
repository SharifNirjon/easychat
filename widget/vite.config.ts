// Builds the widget as ONE self-contained file: dist/widget.js (IIFE, CSS inlined).
import { defineConfig } from 'vite';

export default defineConfig({
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
});
