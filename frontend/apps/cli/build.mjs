import {build} from 'esbuild';
import path from 'node:path';
import {createRequire} from 'node:module';

const require = createRequire(import.meta.url);
const reactPath = path.dirname(require.resolve('react/package.json'));

await build({
  entryPoints: ['src/cli.ts'],
  bundle: true,
  platform: 'node',
  format: 'esm',
  alias: {react: reactPath, 'react-devtools-core': './src/ink-devtools-stub.ts'},
  banner: {js: "import {createRequire} from 'node:module';const require=createRequire(import.meta.url);"},
  outdir: 'dist',
  outExtension: {'.js': '.mjs'},
  logLevel: 'info',
});
