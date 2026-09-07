// Typed access to the FastAPI service. Every page renders on the server, so these calls go over
// loopback and never reach the visitor's browser.
const BASE = import.meta.env.API_BASE ?? process.env.API_BASE ?? 'http://127.0.0.1:8000';

export type Stats = {
  traders: number; scored: number; active: number; watch: number; dropped: number;
  fills: number; positions: number; open_pnl: number; tokens: number; updated_ts: number | null;
};

export type Signal = {
  mint: string; sym: string; liq: number | null; buyers: number; usd: number | null;
  first_ts: number; avg_score: number; conviction: number; who: string[]; scores: number[];
};

export type TraderRow = {
  handle: string | null; address: string; score: number; status: string;
  summary: string | null; fomo_pnl: number | null; style: string[]; red_flags: string[];
};

export type Position = { token: string; sym: string; pnl: number | null; cost: number | null };

export type Trader = {
  address: string; handle: string | null; chain: string; score: number | null; status: string;
  summary: string | null; model: string | null; style: string[]; red_flags: string[];
  stats: Record<string, number | null>; fomo_pnl: number | null;
  positions: Position[]; open_pnl: number | null;
  fills: { ts: number; side: string; usd: number | null; sym: string; source: string }[];
  bought_usd: number; sold_usd: number; hours: number;
  company: { handle: string; score: number; shared: number }[];
};

export type Holder = {
  handle: string | null; address: string; score: number | null; status: string;
  pnl: number | null; cost: number | null;
};

export type Token = {
  mint: string; symbol: string | null; is_quote: boolean; tracked: boolean;
  liquidity_usd: number | null; mcap_usd: number | null;
  holders: Holder[]; trusted_holders: number; avg_score: number | null; conviction: number;
  cohort_pnl: number | null; cohort_cost: number | null;
  flow: { handle: string | null; score: number | null; side: string; usd: number | null; ts: number }[];
  bought_usd: number; sold_usd: number; first_trusted_buy: number | null;
  trusted_buyers: number; hours: number;
};

/** Returns null on 404 or on a service that is down, so a page can say so instead of crashing. */
export async function get<T>(path: string): Promise<T | null> {
  try {
    const r = await fetch(`${BASE}${path}`, { headers: { accept: 'application/json' } });
    if (!r.ok) return null;
    return (await r.json()) as T;
  } catch {
    return null;
  }
}

export const getStats = () => get<Stats>('/api/stats');
export const getSignals = (hours = 24, limit = 40) =>
  get<{ hours: number; count: number; signals: Signal[] }>(`/api/signals?hours=${hours}&limit=${limit}`);
export const getLeaderboard = (status = 'active', limit = 60) =>
  get<{ status: string; count: number; traders: TraderRow[] }>(`/api/leaderboard?status=${status}&limit=${limit}`);
export const getTrader = (who: string) => get<Trader>(`/api/trader/${encodeURIComponent(who)}`);
export const getToken = (mint: string) => get<Token>(`/api/token/${encodeURIComponent(mint)}`);
export const search = (q: string) =>
  get<{ kind: string; handle?: string; address?: string; traders?: TraderRow[]; tokens?: { mint: string; symbol: string }[] }>(
    `/api/search?q=${encodeURIComponent(q)}`);

// ---------------------------------------------------------------- formatting

export function usd(v: number | null | undefined, dash = '—'): string {
  if (v === null || v === undefined) return dash;
  const a = Math.abs(v);
  if (a >= 1_000_000) return `$${(v / 1_000_000).toFixed(1)}M`;
  if (a >= 1_000) return `$${(v / 1_000).toFixed(0)}k`;
  return `$${v.toFixed(0)}`;
}

export function ago(ts: number | null | undefined): string {
  if (!ts) return '—';
  const d = Math.max(Math.floor(Date.now() / 1000) - ts, 0);
  if (d < 3600) return `${Math.floor(d / 60)}m`;
  if (d < 86400) return `${Math.floor(d / 3600)}h`;
  return `${Math.floor(d / 86400)}d`;
}

/** What a position is worth against what it cost. Null cost means fomo counts withdrawn profit. */
export function multiple(cost: number | null, pnl: number | null): string {
  if (!cost || cost <= 0 || pnl === null || pnl === undefined) return '—';
  return `${((cost + pnl) / cost).toFixed(1)}×`;
}

export const verdictClass = (status: string) =>
  status === 'active' ? 'follow' : status === 'watch' ? 'watch' : 'drop';

export const verdictLabel = (status: string) =>
  status === 'active' ? 'follow' : status === 'dropped' ? 'drop' : status;
