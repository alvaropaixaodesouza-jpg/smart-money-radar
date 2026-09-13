"""CLI: discover / new-tokens / track / score / report / run."""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Optional

import typer
import inspect as _inspect
import typer.core as _typer_core
import typer.rich_utils as _typer_rich
import typer.completion as _typer_completion


def _localizar_typer_ptbr() -> None:
    """Traduz somente a apresentação da CLI; comandos e opções não mudam."""

    # Painéis do Rich/Typer.
    _typer_rich.OPTIONS_PANEL_TITLE = "Opções"
    _typer_rich.COMMANDS_PANEL_TITLE = "Comandos"
    _typer_rich.ARGUMENTS_PANEL_TITLE = "Argumentos"
    _typer_rich.ERRORS_PANEL_TITLE = "Erro"

    # Metadados exibidos nas opções.
    if hasattr(_typer_rich, "DEFAULT_STRING"):
        _typer_rich.DEFAULT_STRING = "[padrão: {}]"
    if hasattr(_typer_rich, "ENVVAR_STRING"):
        _typer_rich.ENVVAR_STRING = "[var. ambiente: {}]"
    if hasattr(_typer_rich, "REQUIRED_LONG_STRING"):
        _typer_rich.REQUIRED_LONG_STRING = "[obrigatório]"
    if hasattr(_typer_rich, "ABORTED_TEXT"):
        _typer_rich.ABORTED_TEXT = "Interrompido."
    if hasattr(_typer_rich, "RICH_HELP"):
        _typer_rich.RICH_HELP = (
            "Use [blue]'{command_path} {help_option}'[/] para ver a ajuda."
        )

    # "Usage:" e descrição automática do --help.
    for cls in (_typer_core.TyperCommand, _typer_core.TyperGroup):
        if getattr(cls, "_fomo_ptbr_localizado", False):
            continue

        original_usage = cls.get_usage
        original_help = cls.get_help_option

        def criar_usage(original):
            def get_usage(self, ctx):
                texto = original(self, ctx)
                return texto.replace("Usage:", "Uso:", 1)
            return get_usage

        def criar_help(original):
            def get_help_option(self, ctx):
                option = original(self, ctx)
                if option is not None:
                    option.help = "Mostra esta mensagem e encerra."
                return option
            return get_help_option

        cls.get_usage = criar_usage(original_usage)
        cls.get_help_option = criar_help(original_help)
        cls._fomo_ptbr_localizado = True

    # Mantém --install-completion e --show-completion,
    # mas traduz suas descrições.
    traducoes_completion = {
        "Install completion for the current shell.":
            "Instala a conclusão automática no shell atual.",

        "Show completion for the current shell, to copy it or customize the installation.":
            "Mostra a conclusão automática do shell atual para copiar ou personalizar.",

        "Install completion for the specified shell.":
            "Instala a conclusão automática no shell especificado.",

        "Show completion for the specified shell, to copy it or customize the installation.":
            "Mostra a conclusão automática do shell especificado para copiar ou personalizar.",
    }

    for nome in (
        "_install_completion_placeholder_function",
        "_install_completion_no_auto_placeholder_function",
    ):
        fn = getattr(_typer_completion, nome, None)
        if fn is None:
            continue

        for param in _inspect.signature(fn).parameters.values():
            default = param.default

            if hasattr(default, "help") and default.help in traducoes_completion:
                default.help = traducoes_completion[default.help]


_localizar_typer_ptbr()

from . import db
from .config import settings

app = typer.Typer(
    help="FOMO Robinhood Radar: descubra, acompanhe e avalie traders da Robinhood Chain.",
    no_args_is_help=True)


def _setup(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _run(kind: str, fn, *args, **kw) -> dict | None:
    """Wrap a step in a `runs` row. Errors are recorded, not raised."""
    conn = db.connect()
    rid = db.run_start(conn, kind)
    try:
        stats = fn(conn, *args, **kw)
        db.run_finish(conn, rid, stats)
        return stats
    except Exception as e:  # noqa: BLE001
        logging.getLogger(kind).error("falha em %s: %s", kind, e)
        db.run_finish(conn, rid, error=repr(e)[:500])
        return None
    finally:
        conn.close()


@app.callback()
def main(verbose: bool = typer.Option(False, "-v", "--verbose")) -> None:
    _setup(verbose)


@app.command()
def init() -> None:
    """Cria o banco SQLite e mostra a configuração atual."""
    conn = db.connect()
    conn.close()
    typer.echo(f"banco: {settings.db_path.resolve()}")
    typer.echo(f"chave Helius: {'configurada' if settings.helius_api_key else 'AUSENTE'}")
    typer.echo(f"chave Anthropic: {'configurada' if settings.anthropic_api_key else 'AUSENTE'}")
    typer.echo(f"chave Codex: {'configurada' if settings.codex_api_key else 'não configurada (fonte Codex ignorada)'}")
    typer.echo(f"fontes de acompanhamento: {','.join(settings.track_sources)}")
    typer.echo(f"avaliador: {settings.scorer} (SCORER={settings.scorer_mode})")
    conn = db.connect()
    users, resolved = conn.execute(
        "SELECT COUNT(*), COUNT(resolved_at) FROM fomo_users"
    ).fetchone()
    conn.close()
    typer.echo(f"fomo: browser export only (Cloudflare blocks server calls) — "
               f"{users} usuários conhecidos, {resolved} com carteiras de execução. "
               f"Refresh: scripts/fomo_export.js -> cli fomo-import")
    typer.echo(f"new-token threshold: mcap>={settings.new_token_min_mcap_usd:,.0f} USD, age<={settings.new_token_max_age_hours}h, chains={','.join(settings.dex_chains)}, sources={','.join(settings.token_sources)}")
    if settings.codex_api_key:
        b = codex_budget(conn_counts=chain_counts())
        typer.echo(
            f"codex budget: ~{b['total']:,}/month of {settings.codex_monthly_request_cap:,} "
            f"(tokens {b['tokens']:,} + discovery ~{b['discovery']:,} + "
            f"acompanhamento={b['tracking']:,} para {b.get('codex_wallets', 0)} carteiras que o Codex precisa cobrir)"
            + ("  OVER BUDGET - raise the intervals" if b["total"] > settings.codex_monthly_request_cap else "")
        )


def chain_counts() -> dict[str, int]:
    conn = db.connect()
    try:
        return {r[0] or "solana": r[1] for r in conn.execute(
            "SELECT chain, COUNT(*) FROM traders WHERE status IN ('candidate','tracking','active','watch') GROUP BY chain")}
    finally:
        conn.close()


def codex_budget(new_tokens_per_day: int = 20, conn_counts: dict[str, int] | None = None) -> dict:
    """Rough monthly Codex request projection for the current config.

    Wallets on a chain served by a free source earlier in TRACK_SOURCES (today: trenches on
    Robinhood Chain) never reach Codex, so they are excluded from the tracking line.
    """
    from .pipeline.track import build_trackers, pick_tracker

    per_day = 86400
    tokens = round(per_day / max(settings.codex_min_interval_s, 1) * 30)
    discovery = round(min(new_tokens_per_day, settings.discover_tokens_per_pass * per_day / max(settings.new_tokens_interval, 1)) * 30)

    counts = conn_counts or {}
    on_codex = sum(counts.values())
    if counts:
        try:
            trackers = build_trackers()
            on_codex = sum(n for chain, n in counts.items()
                           if type(pick_tracker(trackers, chain)).__name__ == "Codex")
        except Exception:  # noqa: BLE001 - projection must never break `init`
            pass
    wallets_per_pass = min(settings.track_max_wallets_per_pass, on_codex) if counts else settings.track_max_wallets_per_pass
    tracking = round(per_day / max(settings.track_interval, 1) * wallets_per_pass * 30)
    return {"tokens": tokens, "discovery": discovery, "tracking": tracking,
            "codex_wallets": on_codex, "total": tokens + discovery + tracking}


@app.command()
def discover(
    leaderboard: bool = typer.Option(False, "--leaderboard", help="busca o ranking FOMO de 24h/7d/30d"),
    mint: Optional[str] = typer.Option(None, "--mint", help="busca no FOMO os holders com maior PnL de um token"),
    add: Optional[str] = typer.Option(None, "--add", help="adiciona manualmente uma carteira (não exige FOMO)"),
    handle: Optional[str] = typer.Option(None, "--handle"),
    chain: Optional[str] = typer.Option(None, "--chain", help="solana | base | robinhood | evm (detectada automaticamente se omitida)"),
    makers: bool = typer.Option(False, "--makers", help="com --mint: busca compradores recentes no Codex em vez dos holders FOMO"),
    trenches: bool = typer.Option(False, "--trenches", help="importa traders FOMO de robinhoodtrenches.com (gratuito, sem sessão)"),
    window: Optional[str] = typer.Option(None, "--window", help="com --trenches: 1h|24h|7d|30d|all"),
) -> None:
    """Descobre traders candidatos para acompanhamento."""
    from .pipeline import discover as d

    if add:
        conn = db.connect()
        created = d.add_manual(conn, add, handle, chain)
        conn.close()
        typer.echo(f"{'adicionada' if created else 'já conhecida'}: {add}")
    if trenches:
        typer.echo(_run("discover_trenches", d.discover_trenches, None, window))
    if leaderboard:
        typer.echo(_run("discover_leaderboard", d.discover_leaderboard))
    if mint:
        if makers:
            typer.echo(_run("discover_makers", d.discover_makers, mint, chain or "solana"))
        else:
            from .sources.fomo import NETWORKS

            net = next((n for n, c in NETWORKS.items() if c == (chain or "solana")), 1399811149)
            typer.echo(_run("discover_holders", d.discover_holders, mint, None, net))
    if not (add or leaderboard or mint or trenches):
        typer.echo("nada para fazer: use --trenches, --leaderboard, --mint [--makers] ou --add")


@app.command("fomo-check")
def fomo_check() -> None:
    """Verifica FOMO_SESSION na API ao vivo e mostra o que o ranking retorna."""
    from .sources.fomo import FomoClient, FomoError

    try:
        client = FomoClient()
    except FomoError as e:
        typer.echo(f"não configurado: {e}")
        raise typer.Exit(1)
    try:
        rows = client.leaderboard("7d", limit=5)
    except FomoError as e:
        typer.echo(f"FALHA: {e}")
        raise typer.Exit(1)
    typer.echo(f"OK, {len(rows)} linhas no ranking (7d). Melhores:")
    for r in rows:
        typer.echo(f"  {str(r.fomo_handle):20s} PnL7d={r.pnl_7d} operações={r.trades_cnt} volume={r.volume_usd}")
    if rows:
        addrs = client.execution_addresses(rows[0].fomo_user_id)
        typer.echo(f"carteiras de execução de {rows[0].fomo_handle}: {addrs or 'nenhuma encontrada nos swaps recentes'}")
        typer.echo(f"(o endereço de perfil {rows[0].profile_address} NÃO é a carteira que negocia on-chain)")
    typer.echo(f"requisições utilizadas: {client.requests}")


@app.command("trenches")
def trenches_status(
    window: str = typer.Option("24h", "--window", help="1h|24h|7d|30d|all"),
    tape: int = typer.Option(0, "--tape", help="também mostra as N execuções mais recentes"),
    closed: int = typer.Option(0, "--closed", help="também mostra as N posições encerradas mais recentes"),
) -> None:
    """Verifica a saúde e consulta robinhoodtrenches.com (traders FOMO na Robinhood Chain)."""
    from datetime import datetime, timezone

    from .sources.trenches import Trenches

    c = Trenches()
    s = c.status()
    typer.echo(f"rede={s.get('chain')} ({s.get('chain_id')}) carteiras={s.get('wallets')} "
               f"trades={s.get('trades')} lag={s.get('lag_seconds')}s source={s.get('source')}")
    o = c.overview(window)
    typer.echo(f"{window}: {o.get('fills')} operações, {o.get('active_traders')} traders ativos, "
               f"{o.get('tokens')} tokens, volume ${o.get('volume', 0):,.0f}, realized ${o.get('realized_pnl', 0):,.0f}")
    top = sorted(c.traders(window), key=lambda t: -(t.get("realized_pnl") or 0))[:10]
    typer.echo(f"\ntop realized PnL ({window}):")
    for t in top:
        typer.echo(f"  {str(t.get('handle')):20s} PnL={t.get('realized_pnl'):>12,.0f} "
                   f"acerto={t.get('win_rate')} operações={t.get('fills')} volume={t.get('volume'):>12,.0f} {t.get('address')}")
    for f in c.tape(tape)[:tape] if tape else []:
        when = datetime.fromtimestamp(f["ts"], tz=timezone.utc).strftime("%H:%M:%S")
        first = " PRIMEIRA POSIÇÃO" if f.get("new_position") else ""
        typer.echo(f"  {when} {f['side']:4s} {str(f.get('symbol')):12s} ${f.get('usd', 0):>10,.0f} {f.get('handle')}{first}")
    for p in c.closed(window, closed)[:closed] if closed else []:
        typer.echo(f"  encerrada {str(p.get('symbol')):12s} PnL={p.get('pnl_usd'):>10,.0f} "
                   f"({p.get('pnl_pct'):.0f}%) duração={p.get('hold_seconds', 0) / 3600:.1f}h {p.get('handle')}")


@app.command()
def receive(
    host: Optional[str] = typer.Option(None, "--host"),
    port: Optional[int] = typer.Option(None, "--port"),
) -> None:
    """Executa o endpoint local que recebe as coletas FOMO enviadas pela extensão do navegador."""
    from .receiver import serve

    serve(host, port)


@app.command("fomo-import")
def fomo_import(path: Path = typer.Argument(..., help="arquivo produzido por scripts/fomo_export.js")) -> None:
    """Importa ranking, holders e carteiras de execução exportados pelo navegador."""
    from .pipeline.discover import import_browser_export

    typer.echo(_run("fomo_import", import_browser_export, path))


@app.command("resolve")
def resolve_cmd(
    limit: Optional[int] = typer.Option(None, "--limit", help="quantos usuários FOMO processar"),
    chain: str = typer.Option("robinhood", "--chain"),
    handle: Optional[str] = typer.Option(None, "--handle", help="resolve somente este trader e mostra o ranking"),
) -> None:
    """Infere a carteira on-chain real dos traders FOMO usando tokens e horários negociados."""
    from .pipeline.resolve import maker_source, resolve_pending, resolve_user, user_windows

    if handle:
        conn = db.connect()
        u = conn.execute("SELECT * FROM fomo_users WHERE handle=?", (handle,)).fetchone()
        if u is None:
            typer.echo(f"nenhum usuário FOMO com o nome {handle!r} — importe primeiro uma exportação do navegador")
            raise typer.Exit(1)
        windows = user_windows(conn, u["user_id"], chain, settings.resolve_windows)
        typer.echo(f"{handle}: {len(windows)} janelas utilizáveis em {chain}")
        fetch, client = maker_source(chain)
        address, info = resolve_user(conn, fetch, u["user_id"], chain)
        typer.echo(f"resolvido: {address or 'nenhuma correspondência confiável'}  {info}")
        typer.echo(f"via {type(client).__name__}, {client.requests} requisições")
        conn.close()
        return
    typer.echo(_run("resolve", resolve_pending, None, chain, limit))


@app.command("fomo-resolve")
def fomo_resolve(limit: Optional[int] = typer.Option(None, "--limit")) -> None:
    """Registra os endereços informados pelo FOMO para cada usuário. São contas internas, NÃO carteiras de trading — use `resolve` para inferir a carteira que realmente negocia."""
    from .pipeline.discover import resolve_execution_wallets
    from .sources.fomo import FomoClient

    conn = db.connect()
    typer.echo(resolve_execution_wallets(conn, FomoClient(), limit))
    conn.close()


@app.command("new-tokens")
def new_tokens(
    dry_run: bool = typer.Option(False, "--dry-run", help="apenas mostra o que o DexScreener retorna"),
) -> None:
    """Consulta DexScreener + GeckoTerminal por tokens recentes de maior capitalização e inicia a descoberta de holders quando possível."""
    from .pipeline import new_tokens as nt
    from .pipeline.discover import safe_fomo

    if dry_run:
        for t in nt.fetch_new_tokens():
            age = (db.now() - t.created_at) / 3600 if t.created_at else None
            typer.echo(f"{t.source:13s} {t.chain:9s} {t.symbol or '?':10s} {t.mint}  mcap={t.mcap_usd or 0:>12,.0f}  liq={t.liquidity_usd or 0:>10,.0f}  age={age and f'{age:.1f}h'}")
        return
    typer.echo(_run("new_tokens", nt.poll_new_tokens, None, safe_fomo()))


@app.command("enrich-tokens")
def enrich_tokens_cmd(limit: int = typer.Option(300, "--limit")) -> None:
    """Obtém nome, preço, decimais e liquidez dos tokens conhecidos apenas pelo endereço (gratuito)."""
    from .pipeline.new_tokens import enrich_tokens

    typer.echo(_run("enrich_tokens", enrich_tokens, None, limit))



def _health_name_pt(name: str) -> str:
    return {
        "fomo collection": "Coleta FOMO",
        "holdings": "Posições das carteiras",
        "prices": "Preços dos tokens",
        "router": "Roteador on-chain",
        "fomoapi budget": "Orçamento da API FOMO",
        "on-chain tape": "Dados on-chain",
        "scoring queue": "Fila de avaliação",
        "wallet resolution": "Resolução de carteiras",
    }.get(name, name)


def _health_detail_pt(detail: str) -> str:
    d = str(detail)

    m = re.fullmatch(r"(\d+) fills in the last day", d)
    if m:
        return f"{m.group(1)} operações nas últimas 24h"

    if d == "nothing tracked on this chain yet":
        return "nenhuma carteira acompanhada nesta blockchain ainda"

    if d.startswith("could not ask the chain:"):
        return "não foi possível consultar a blockchain:" + d.split(":", 1)[1]

    if d == "no fills, but no transfers either - the cohort is simply still":
        return "não houve operações nem transferências; o grupo está sem atividade"

    m = re.fullmatch(
        r"no fills in a day while (\d+) transfers moved through these wallets\. "
        r"RPC_ROUTERS \((.*?)\) is probably stale - find the new one in a recent fomo trade\.",
        d,
    )
    if m:
        return (
            f"nenhuma operação em 24h, apesar de {m.group(1)} transferências nessas "
            f"carteiras; RPC_ROUTERS ({m.group(2)}) provavelmente está desatualizado"
        )

    m = re.match(r"last collection ([\d.]+)h ago", d)
    if m:
        idade = m.group(1).replace(".", ",")
        texto = f"última coleta há {idade}h"
        if "check `journalctl -u radar-fomo`" in d:
            texto += (
                " — verifique `journalctl -u radar-fomo`; uma chave rejeitada "
                "ou os créditos do mês esgotados podem causar isso"
            )
        return texto

    if d.startswith("never collected"):
        return (
            "nunca houve uma coleta válida — verifique "
            "`journalctl -u radar-fomo`; uma chave rejeitada ou os créditos "
            "do mês esgotados podem causar isso"
        )

    m = re.fullmatch(r"([\d.]+) of (\d+) credits used this month", d)
    if m:
        return f"{m.group(1)} de {m.group(2)} créditos usados neste mês"

    m = re.fullmatch(r"newest fill ([\d.]+)h ago", d)
    if m:
        return f"operação mais recente há {m.group(1).replace('.', ',')}h"

    if d == "no fills":
        return "nenhuma operação"

    m = re.fullmatch(r"balances read ([\d.]+)h ago", d)
    if m:
        return f"saldos lidos há {m.group(1).replace('.', ',')}h"

    if d == "never read":
        return "nunca lido"

    m = re.fullmatch(r"(\d+) tracked wallets waiting for a verdict", d)
    if m:
        return f"{m.group(1)} carteiras acompanhadas aguardando avaliação"

    m = re.fullmatch(r"(\d+) of (\d+) tokens priced", d)
    if m:
        return f"{m.group(1)} de {m.group(2)} tokens com preço"

    m = re.fullmatch(r"(\d+) fomo users still without an on-chain address", d)
    if m:
        return f"{m.group(1)} usuários FOMO ainda sem endereço on-chain"

    return d


@app.command("health")
def health_cmd(
    push: bool = typer.Option(False, "--push", help="envia o relatório para todos os assinantes do bot"),
    beat: bool = typer.Option(False, "--heartbeat", help="envia sinal para HEARTBEAT_URL enquanto todas as verificações estiverem OK"),
    quiet: bool = typer.Option(False, "--quiet", help="não mostra nada a menos que exista algum problema"),
) -> None:
    """Mostra problemas silenciosos: coletas desatualizadas, fluxo parado ou roteador alterado."""
    from .pipeline.health import heartbeat, report

    conn = db.connect()
    try:
        r = report(conn)
        if not (quiet and r["ok"]):
            for c in r["checks"]:
                nome = _health_name_pt(c["name"])
                detalhe = _health_detail_pt(c["detail"])
                typer.echo(f"{'OK ' if c['ok'] else 'FALHA'}  {nome:<24} {detalhe}")
        if beat:
            msg = heartbeat(r["ok"])
            if not quiet or not r["ok"]:
                typer.echo(f"sinal de atividade: {msg}")
        if push:
            from .bot import Telegram, fmt_health, subscribers

            tg, text = Telegram(), fmt_health(r)
            sent = 0
            for sub in subscribers(conn):
                try:
                    tg.send(sub["chat_id"], text)
                    sent += 1
                except Exception as e:  # noqa: BLE001 - one blocked chat must not stop the rest
                    logging.getLogger("health").warning("falha ao enviar: %s", e)
            typer.echo(f"enviado para {sent} assinantes")
    finally:
        conn.close()
    raise typer.Exit(0 if r["ok"] else 1)


@app.command("fomo-api")
def fomo_api_cmd(
    windows: str = typer.Option("24h,7d", "--windows", help="janelas do ranking, 1 crédito cada"),
    thesis_pages: int = typer.Option(None, "--thesis-pages", help="50 notas por página, 5 créditos cada"),
) -> None:
    """Coleta a parte do FOMO por HTTP em vez de usar o navegador."""
    from .pipeline.collect_api import collect

    got = _run("fomoapi", collect, tuple(w.strip() for w in windows.split(",") if w.strip()),
               thesis_pages)
    typer.echo(got)


@app.command("hot")
def hot_cmd(
    backtest: bool = typer.Option(False, "--backtest", help="reproduz a regra sobre todo o histórico"),
    mode: str = typer.Option("current", "--scores", help="current | strict | first: qual avaliação julga uma compra"),
    horizon: int = typer.Option(24, "--horizon", help="horas após um BURST para medir o resultado"),
    window: int = typer.Option(None, "--window", help="minutos (ao vivo)"),
    delta: float = typer.Option(None, "--delta", help="convicção acumulada dentro da janela (ao vivo)"),
) -> None:
    """Tokens em que várias carteiras confiáveis entraram em um BURST — ao vivo ou reproduzido para calibrar o limite."""
    from .pipeline import hot

    conn = db.connect()
    chain = settings.dex_chains[0] if settings.dex_chains else None
    try:
        if backtest:
            r = hot.backtest(conn, chain, horizon_s=horizon * 3600, mode=mode)
            typer.echo(f"cobertura dos scores: {r['days']} dias; "
                       f"{r['tokens_with_trusted_buys']} tokens com compras confiáveis; "
                       f"horizonte: {r['horizon_h']}h; modo dos scores: {r['mode']}")
            typer.echo(f"{'delta':>5} {'jan':>4} {'n':>2} {'BURST':>6} {'/dia':>5} {'med.':>5} {'s/d':>5} "
                       f"{'melhor':>8} {'>=2x':>5} {'>=3x':>5} {'último':>8} {'<0,5':>5}")
            for x in r["rows"]:
                f = lambda v, w: f"{v:>{w}}" if v is not None else f"{'-':>{w}}"
                typer.echo(f"{x['delta']:>5} {x['window_min']:>4} {x['min_wallets']:>2} {x['bursts']:>6} "
                           f"{f(x['per_day'],5)} {x['measured']:>5} {x['silent']:>5} "
                           f"{f(x['median_best'],8)} {f(x['p_best_2x'],5)} {f(x['p_best_3x'],5)} "
                           f"{f(x['median_last'],8)} {f(x['p_last_half'],5)}"
                           + (f"   <- {x['label']}" if x.get('label') else ""))
            return
        rows = hot.hot_now(conn, chain,
                           delta=settings.hot_delta if delta is None else delta,
                           window_s=(settings.hot_window_min if window is None else window) * 60,
                           min_wallets=settings.hot_min_wallets,
                           max_age_s=settings.hot_max_age_h * 3600)
        if not rows:
            typer.echo("nenhum BURST ativo agora")
        for h in rows:
            typer.echo(f"{h['sym']:<12} convicção +{h['conviction']:.2f} de {h['wallets']} carteiras "
                       f"em {h['window_s'] // 60}min, ${h['usd']:,.0f}  {h['mint']}")
    finally:
        conn.close()


@app.command("verify-fills")
def verify_fills_cmd(
    days: int = typer.Option(7, "--days", help="até quantos dias atrás buscar recibos"),
    per_min: int = typer.Option(20, "--per-min", help="limite de RPC disponível enquanto o monitor também está ativo"),
    limit: int = typer.Option(None, "--limit", help="número máximo de compras nesta execução"),
) -> None:
    """Busca o recibo de cada compra ainda não classificada e determina de quem foi a operação."""
    from .pipeline.provenance import verify

    got = _run("verify_fills", verify, days, None, per_min, limit)
    typer.echo(got)


@app.command("resize-fills")
def resize_fills_cmd() -> None:
    """Reavalia cada execução dimensionada usando o limite de poeira e a proporção atuais."""
    from .pipeline.provenance import refresh_medians, resize

    conn = db.connect()
    try:
        typer.echo({"medians": refresh_medians(conn), **resize(conn)})
    finally:
        conn.close()


@app.command("watch")
def watch_cmd(
    once: bool = typer.Option(False, "--once", help="executa um ciclo e encerra"),
) -> None:
    """Lê a blockchain a cada poucos segundos e envia um BURST no momento em que ele se forma."""
    from .pipeline.watch import run

    conn = db.connect()
    try:
        typer.echo(run(conn, once=once))
    finally:
        conn.close()


@app.command("digest")
def digest_cmd(
    hours: int = typer.Option(24, "--hours", help="janela de tempo coberta pelo resumo"),
    push: bool = typer.Option(False, "--push", help="envia para todos os assinantes do bot"),
) -> None:
    """Resume o dia em uma mensagem: entradas, saídas, novos participantes e problemas detectados."""
    from .bot import fmt_digest
    from .pipeline.digest import daily

    conn = db.connect()
    try:
        chain = settings.dex_chains[0] if settings.dex_chains else None
        text = fmt_digest(daily(conn, hours=hours, chain=chain))
        typer.echo(re.sub(r"<[^>]+>", "", text))
        if push:
            from .bot import Telegram, subscribers

            tg, sent = Telegram(), 0
            for chat in subscribers(conn):
                try:
                    tg.send(chat["chat_id"], text)
                    sent += 1
                except Exception as e:  # noqa: BLE001 - one blocked chat must not stop the rest
                    logging.getLogger("digest").warning("falha ao enviar: %s", e)
            typer.echo(f"enviado para {sent} assinantes")
    finally:
        conn.close()


@app.command("calibrate")
def calibrate_cmd(
    min_usd: float = typer.Option(100.0, "--min-usd", help="ignora posições menores que este valor"),
    json_out: bool = typer.Option(False, "--json", help="mostra os números brutos"),
) -> None:
    """Verifica se o score teve poder preditivo, medindo apenas posições abertas após a avaliação."""
    import json

    from .pipeline.calibrate import calibrate, report

    conn = db.connect()
    try:
        r = calibrate(conn, min_usd=min_usd)
        typer.echo(json.dumps(r, indent=2) if json_out else report(r))
    finally:
        conn.close()


@app.command("backfill")
def backfill_cmd(
    days: int = typer.Option(30, "--days", help="até quantos dias atrás percorrer"),
    max_requests: int = typer.Option(None, "--max-requests", help="interrompe após esta quantidade de chamadas RPC"),
) -> None:
    """Preenche o histórico anterior ao início do monitoramento. Gratuito, começa pelos dados mais recentes e pode ser retomado."""
    from .pipeline.backfill import backfill

    typer.echo(_run("backfill", backfill, days=days, max_requests=max_requests))


@app.command("browser")
def browser_cmd(
    seed: bool = typer.Option(False, "--seed", help="envia a sessão em espera para o navegador"),
    collect: bool = typer.Option(False, "--collect", help="força uma coleta agora sem esperar pelo próximo agendamento"),
) -> None:
    """Consulta o que o navegador coletor está vendo e, opcionalmente, entrega uma sessão em espera."""
    from . import browser

    if collect:
        typer.echo(f"coleta: {browser.collect_now()}")
        typer.echo("a coleta leva cerca de um minuto; acompanhe com `journalctl -u radar-receive`")
        raise typer.Exit(0)
    if seed:
        path = settings.seed_path
        if not path.exists():
            typer.echo(f"nenhuma sessão aguardando em {path}")
            raise typer.Exit(1)
        import json as _json

        wrote = browser.write_session(_json.loads(path.read_text(encoding="utf-8")))
        path.unlink(missing_ok=True)
        typer.echo(f"{wrote} gravado; arquivo temporário removido")
        time.sleep(8)
    st = browser.state()
    typer.echo(st)
    if st.get("restricted"):
        typer.echo("CONTA RESTRITA - o FOMO está recusando esta conta, não esta máquina. "
                   "A proxy will not help; the account itself has to be cleared or replaced.")
        raise typer.Exit(1)
    typer.echo("sessão autenticada" if st.get("hasToken") and not st.get("showsLogin")
               else "NOT signed in - the page still offers a login")


@app.command("holdings")
def holdings_cmd(limit: int = typer.Option(None, "--limit", help="pares carteira/token que serão relidos")) -> None:
    """Lê o que as carteiras acompanhadas realmente possuem. Gratuito e necessário para completar as posições."""
    from .pipeline.holdings import mark_holdings

    typer.echo(_run("holdings", mark_holdings, None, limit))


@app.command()
def track(
    address: Optional[str] = typer.Option(None, "--address", help="acompanha somente esta carteira"),
    chain: Optional[str] = typer.Option(None, "--chain", help="blockchain do --address (detectada automaticamente se omitida)"),
    limit: Optional[int] = typer.Option(None, "--limit", help="máximo de carteiras nesta execução"),
    show: bool = typer.Option(False, "--show", help="mostra as operações coletadas"),
) -> None:
    """Coleta swaps das carteiras acompanhadas usando as fontes definidas em TRACK_SOURCES."""
    from .pipeline import track as tr
    from .pipeline.discover import add_manual, guess_chain

    if address:
        conn = db.connect()
        row = db.get_trader(conn, address)
        if row is None:
            add_manual(conn, address, chain=chain)
            row = db.get_trader(conn, address)
        chain = chain or row["chain"] or guess_chain(address)
        tracker = tr.pick_tracker(tr.build_trackers(), chain, address)
        if tracker is None:
            typer.echo(f"nenhuma fonte configurada suporta a blockchain {chain!r} (TRACK_SOURCES={','.join(settings.track_sources)})")
            raise typer.Exit(1)
        n = tr.track_wallet(conn, tracker, address, chain)
        typer.echo(f"{address} [{chain}] via {type(tracker).__name__}: {n} novas operações")
        if show:
            for t in conn.execute(
                "SELECT ts, side, mint, token_amount, sol_amount, usd_value FROM trades WHERE address=? ORDER BY ts DESC LIMIT 15",
                (address,),
            ):
                typer.echo(f"  {t['ts']} {t['side']:4s} {t['mint'][:16]:16s} amt={t['token_amount']} sol={t['sol_amount']} usd={t['usd_value']}")
        conn.close()
        return
    typer.echo(_run("track", tr.track_all, None, limit))


@app.command()
def score(
    address: Optional[str] = typer.Option(None, "--address"),
    deep: bool = typer.Option(False, "--deep", help="revisão aprofundada dos N melhores traders ativos usando Sonnet"),
    force: bool = typer.Option(False, "--force", help="ignora o intervalo programado para nova avaliação"),
    limit: Optional[int] = typer.Option(None, "--limit"),
    show_context: bool = typer.Option(False, "--show-context", help="mostra o contexto JSON em vez de chamar a IA"),
    export: Optional[Path] = typer.Option(None, "--export", help="exporta contextos pendentes para um arquivo para avaliação no chat"),
    import_: Optional[Path] = typer.Option(None, "--import", help="importa avaliações produzidas no chat"),
    model_label: str = typer.Option("manual", "--model-label", help="identificador armazenado junto às avaliações importadas"),
    unscored: bool = typer.Option(False, "--unscored", help="com --export: somente carteiras ainda não avaliadas"),
    digest: int = typer.Option(0, "--digest", help="com --export: também mostra N carteiras em uma tabela compacta"),
    offset: int = typer.Option(0, "--offset", help="com --digest: ignora as primeiras N carteiras"),
) -> None:
    """Avalia traders com IA. Usa a API ou o fluxo exportar/importar quando SCORER=manual."""
    import json

    from .pipeline import score as sc

    if export:
        conn = db.connect()
        typer.echo(sc.export_contexts(conn, export, force=force, limit=limit,
                                      unscored_only=unscored))
        if digest:
            payload = json.loads(export.read_text(encoding="utf-8"))
            for line in sc.digest_lines(payload)[: (offset + digest) if digest else None][offset:]:
                typer.echo(line)
        conn.close()
        return
    if import_:
        conn = db.connect()
        typer.echo(sc.import_results(conn, import_, model_label))
        conn.close()
        return
    if address:
        conn = db.connect()
        if show_context:
            typer.echo(json.dumps(sc.build_context(conn, address), indent=1))
            return
        res, cost = sc.score_trader(conn, address, settings.deep_model if deep else None)
        conn.close()
        typer.echo(res.model_dump_json(indent=1) if res else "precisa de revisão")
        typer.echo(f"custo: ${cost:.4f}")
        return
    typer.echo(_run("score", sc.score_all, deep=deep, force=force, limit=limit))


@app.command()
def token(
    mint: str = typer.Argument(..., help="endereço do contrato do token"),
    hours: int = typer.Option(48, "--hours", help="janela de tempo da seção de fluxo"),
) -> None:
    """Mostra quem da lista de acompanhamento possui o token, quanto pagou e quem o negociou recentemente."""
    from .pipeline.analyze import analyze_token, format_token

    conn = db.connect()
    typer.echo(format_token(analyze_token(conn, mint, hours)))
    conn.close()


@app.command()
def trader(
    who: str = typer.Argument(..., help="nome do trader no FOMO ou endereço da carteira"),
    hours: int = typer.Option(168, "--hours", help="janela de tempo da seção de execuções"),
) -> None:
    """Analisa um trader: avaliação, posições abertas, execuções recentes e carteiras relacionadas."""
    from .pipeline.analyze import analyze_trader, format_trader

    conn = db.connect()
    a = analyze_trader(conn, who, hours)
    conn.close()
    if a is None:
        raise typer.BadParameter(f"nenhum trader corresponde a {who!r} (tente um nome do FOMO ou um endereço de carteira)")
    typer.echo(format_trader(a))


@app.command("serve")
def serve_cmd(
    host: Optional[str] = typer.Option(None, "--host"),
    port: Optional[int] = typer.Option(None, "--port"),
    reload: bool = typer.Option(False, "--reload", help="reinicia quando o código muda (desenvolvimento)"),
) -> None:
    """Executa a API HTTP usada pelo site e por clientes externos."""
    from .api import serve

    typer.echo(f"API disponível em http://{host or settings.api_host}:{port or settings.api_port}/docs")
    serve(host, port, reload)


@app.command("bot")
def bot_cmd(
    once: bool = typer.Option(False, "--once", help="faz uma consulta e uma transmissão e depois encerra"),
    check: bool = typer.Option(False, "--check", help="verifica o token e mostra a identidade do bot"),
) -> None:
    """Executa o bot do Telegram: responde consultas e envia sinais conforme acontecem."""
    from .bot import Telegram, broadcast, run

    tg = Telegram()
    if check:
        me = tg.me()
        typer.echo(f"@{me.get('username')} ({me.get('first_name')}) — token verificado")
        conn = db.connect()
        typer.echo(f"assinantes: {conn.execute('SELECT COUNT(*) FROM bot_subscribers WHERE active=1').fetchone()[0]}")
        conn.close()
        return
    conn = db.connect()
    try:
        typer.echo(run(conn, tg, once=once) if once else run(conn, tg))
    except KeyboardInterrupt:
        typer.echo("encerrado")
    finally:
        conn.close()


@app.command()
def report(
    hours: int = typer.Option(24, "--hours"),
    out: Optional[Path] = typer.Option(None, "--out", help="grava o Markdown em um arquivo"),
) -> None:
    """Gera um resumo em Markdown do estado atual."""
    from .pipeline.report import build_report

    conn = db.connect()
    md = build_report(conn, hours)
    conn.close()
    if out:
        out.write_text(md, encoding="utf-8")
        typer.echo(f"gravado: {out}")
    else:
        typer.echo(md)


@app.command()
def run(once: bool = typer.Option(False, "--once", help="executa uma passagem de todas as etapas e encerra")) -> None:
    """Loop contínuo: descobrir / novos tokens / resolver / acompanhar / avaliar / relatório."""
    from .pipeline import discover as d
    from .pipeline import holdings as hd
    from .pipeline import new_tokens as nt
    from .pipeline import resolve as rs
    from .pipeline import score as sc
    from .pipeline import track as tr
    from .pipeline.discover import safe_fomo
    from .pipeline.report import build_report

    fomo = safe_fomo()

    def do_report(conn):
        md = build_report(conn)
        Path("report.md").write_text(md, encoding="utf-8")
        return {"bytes": len(md)}

    steps = [
        ("discover_trenches", settings.discover_interval,
         lambda c: d.discover_trenches(c) if "trenches" in settings.track_sources else {"skipped": "trenches disabled"}),
        ("discover_leaderboard", settings.discover_interval, lambda c: d.discover_leaderboard(c, fomo) if fomo else {"skipped": "fomo not configured"}),
        ("new_tokens", settings.new_tokens_interval, lambda c: nt.poll_new_tokens(c, None, fomo)),
        # free on Robinhood Chain, and it is what turns collected fomo users into trackable wallets
        ("resolve", settings.discover_interval, lambda c: rs.resolve_pending(c)),
        ("track", settings.track_interval, lambda c: tr.track_all(c)),
        # bare contract addresses are useless on the page, and naming them costs nothing
        ("enrich_tokens", settings.track_interval, lambda c: nt.enrich_tokens(c)),
        # what a wallet holds is a free read, and without it every position is only as complete
        # as the fills we happened to watch
        ("holdings", settings.track_interval, lambda c: hd.mark_holdings(c)),
        ("score", settings.track_interval * 10, lambda c: sc.score_all(c)),
        ("report", settings.report_interval, do_report),
    ]
    last: dict[str, float] = {k: 0.0 for k, _, _ in steps}
    while True:
        now = time.monotonic()
        for kind, interval, fn in steps:
            if now - last[kind] >= interval:
                last[kind] = now
                _run(kind, fn)
        if once:
            break
        time.sleep(5)


from .learning.cli import app as learning_app
app.add_typer(learning_app, name="learning")

if __name__ == "__main__":
    app()
