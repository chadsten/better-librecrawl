// Lint gate: npm run lint and the pre-commit lint use it, so it must stay clean.
import { readdirSync, readFileSync } from 'node:fs';
import js from '@eslint/js';
import globals from 'globals';

const SCRIPT_DIR = 'web/static/js';

// The pages load web/static/js as classic <script> tags that share one global
// scope, so each file's top-level names (and window.X assignments) are globals
// for every other file. Read them from the files instead of listing them by hand.
const TOP_LEVEL_NAME = /^(?:(?:async\s+)?function\s*\*?|class|const|let|var)\s+([\w$]+)|^window\.([\w$]+)\s*=/gm;

// Read relative to this file, since eslint may run from another directory.
const scripts = readdirSync(`${import.meta.dirname}/${SCRIPT_DIR}`)
  .filter((name) => name.endsWith('.js'))
  .map((name) => {
    const path = `${SCRIPT_DIR}/${name}`;
    const source = readFileSync(`${import.meta.dirname}/${path}`, 'utf8');
    const names = [...source.matchAll(TOP_LEVEL_NAME)].map((m) => m[1] ?? m[2]);
    return { path, names };
  });

// Every script's names except the ones `path` declares itself, so a name two
// files both declare is still reported by no-redeclare. Plugins (not in
// SCRIPT_DIR) see them all.
function globalsFrom(path) {
  const others = scripts.filter((script) => script.path !== path).flatMap((script) => script.names);
  return Object.fromEntries(others.map((name) => [name, 'writable']));
}

export default [
  { ignores: ['node_modules/'] },
  js.configs.recommended,
  {
    files: ['web/static/**/*.js'],
    languageOptions: {
      sourceType: 'script',
      globals: {
        ...globals.browser,
        cytoscape: 'readonly', // loaded from a CDN in index.html
      },
    },
    rules: {
      // Top-level names are called from other scripts and HTML onclick handlers.
      'no-unused-vars': ['error', { vars: 'local' }],
    },
  },
  { files: ['web/static/plugins/*.js'], languageOptions: { globals: globalsFrom(null) } },
  ...scripts.map(({ path }) => ({ files: [path], languageOptions: { globals: globalsFrom(path) } })),
];
