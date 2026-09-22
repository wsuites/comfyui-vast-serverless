import { defineConfig } from 'tsup'

export default defineConfig({
  entry: { cv: 'src/index.ts' },
  outDir: 'dist',
  format: ['esm'],
  target: 'node22',
  platform: 'node',
  clean: true,
  splitting: false,
  sourcemap: true,
  dts: false,
  // The Astro entry is resolved and imported at runtime from a path the CLI
  // computes; bundling must never try to follow it.
  external: ['node:*'],
})
