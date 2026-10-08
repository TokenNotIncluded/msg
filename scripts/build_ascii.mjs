// Rebuild only the pinned vendored subset; no network or dependency installation.
import { writeFile } from 'node:fs/promises';
const result = await Bun.build({
  entrypoints: ['vendor/ascii/msg-entry.ts'], target: 'browser', format: 'iife', minify: true,
});
if (!result.success) throw new Error(result.logs.join('\n'));
const banner = '/* ascii.rest by bas3line, MIT. Upstream fcb644403bfc25af0002e415d1006dd10b99ca34. See ascii-LICENSE.txt. */\n';
await writeFile('src/msg/data/ascii-runtime.js', banner + await result.outputs[0].text());
