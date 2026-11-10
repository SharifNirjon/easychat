// Local dev: rebuild widget.js on every change and serve it, plus the demo host page.
//   http://localhost:5173/widget.js  -> dist/ (where the script tag points)
//   http://localhost:3000/           -> demo/ (stands in for the tenant's website)
import { createReadStream, existsSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { extname, join, normalize, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { build } from 'vite';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
const WIDGET_PORT = Number(process.env.WIDGET_PORT ?? 5173);
const DEMO_PORT = Number(process.env.DEMO_PORT ?? 3000);

const CONTENT_TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
};

/** Tiny static file server. Refuses paths that escape `dir`. */
function serveStatic(dir, port, label) {
  const base = resolve(dir);
  createServer((req, res) => {
    const urlPath = decodeURIComponent(new URL(req.url ?? '/', 'http://x').pathname);
    let filePath = normalize(join(base, urlPath));
    if (filePath !== base && !filePath.startsWith(base + sep)) {
      res.writeHead(403).end();
      return;
    }
    if (existsSync(filePath) && statSync(filePath).isDirectory()) filePath = join(filePath, 'index.html');
    if (!existsSync(filePath)) {
      res.writeHead(404).end('Not found');
      return;
    }
    res.writeHead(200, {
      'Content-Type': CONTENT_TYPES[extname(filePath)] ?? 'application/octet-stream',
      'Cache-Control': 'no-store', // always pick up the latest build
    });
    createReadStream(filePath).pipe(res);
  })
    .on('error', (err) => {
      const reason = err.code === 'EADDRINUSE' ? 'is already in use (another dev server?)' : err.message;
      console.error(`${label}: port ${port} ${reason}`);
      process.exit(1);
    })
    .listen(port, () => console.log(`${label}: http://localhost:${port}`));
}

// Vite's watch mode rebuilds dist/widget.js whenever src/ changes.
await build({ root: ROOT, build: { watch: {} } });

serveStatic(join(ROOT, 'dist'), WIDGET_PORT, 'widget.js');
serveStatic(join(ROOT, 'demo'), DEMO_PORT, 'demo page');
