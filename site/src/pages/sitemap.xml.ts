import type { APIRoute } from 'astro';
import { getLeaderboard, getSignals } from '../lib/api';

/**
 * The pages worth indexing, built from the same base URL every canonical tag uses.
 *
 * robots.txt pointed at a sitemap that did not exist, on a domain somebody else owns. Rather than
 * deleting the line, this makes it true: the feeds, plus the traders and tokens that actually have
 * something on their page. A trader nobody has scored and a token nobody holds would be a thin
 * page, and thin pages are worth less than no entry at all.
 */
export const GET: APIRoute = async ({ site }) => {
  const base = (site ?? new URL('https://fomoradar.app')).origin;
  const urls = ['/', '/fresh', '/exits', '/leaderboard', '/about'];

  const [board, signals] = await Promise.all([getLeaderboard('active', 60), getSignals(24, 40)]);
  for (const t of board?.traders ?? []) {
    urls.push(`/trader/${encodeURIComponent(t.handle ?? t.address)}`);
  }
  for (const s of signals?.signals ?? []) urls.push(`/token/${s.mint}`);

  const entries = [...new Set(urls)]
    .map((u) => `  <url><loc>${base}${u}</loc></url>`)
    .join('\n');

  const body = [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    entries,
    '</urlset>',
    '',
  ].join('\n');

  return new Response(body, {
    headers: { 'content-type': 'application/xml; charset=utf-8' },
  });
};
