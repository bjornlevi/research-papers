import { build } from 'esbuild';
import { readFile, readdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

const result = await build({
  entryPoints: ['web/paper-editor.js'],
  bundle: true,
  minify: true,
  format: 'iife',
  legalComments: 'eof',
  metafile: true,
  banner: { js: '/* Third-party notices: /paper-editor-licenses.txt */' },
  outfile: 'src/research_library/assets/paper-editor.js',
});
const packages = new Set();
for (const input of Object.keys(result.metafile.inputs)) {
  const parts = input.split('/');
  const index = parts.lastIndexOf('node_modules');
  if (index < 0) continue;
  packages.add(parts.slice(0, index + (parts[index + 1].startsWith('@') ? 3 : 2)).join('/'));
}
const notices = [];
for (const folder of [...packages].sort()) {
  const info = JSON.parse(await readFile(join(folder, 'package.json'), 'utf8'));
  const license = (await readdir(folder)).find(name => /^licen[cs]e(?:\.md|\.txt)?$/i.test(name));
  if (!license) throw new Error(`Missing license for ${info.name}`);
  notices.push(`${info.name} ${info.version}\n\n${await readFile(join(folder, license), 'utf8')}`);
}
await writeFile('src/research_library/assets/paper-editor-licenses.txt',
  notices.join('\n\n--------------------\n\n'));
