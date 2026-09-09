import { defineConfig } from 'astro/config';
import node from '@astrojs/node';

// Server rendering, not a static build: a trader page has to exist for a wallet resolved five
// minutes ago, and a token page has to answer for an address nobody has ever asked about.
export default defineConfig({
  output: 'server',
  adapter: node({ mode: 'standalone' }),
  site: process.env.PUBLIC_SITE_URL || 'https://fomoradar.xyz',
  server: { port: Number(process.env.PORT ?? 4321), host: true },
  devToolbar: { enabled: false },
});
