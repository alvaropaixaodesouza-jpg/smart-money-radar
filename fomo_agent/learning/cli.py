"""Explicitly enabled research commands, sharing the configured radar database."""
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import typer

from .. import db
from ..config import settings
from .analytics import report, render
from .math import position_size
from .runner import backup, tick, worker_lock
from .store import dumps

app = typer.Typer(help="Histórico, resultados e simulação experimental. Sem ordens reais.")


@app.command("backup")
def backup_command(out: Path | None = typer.Option(None, "--out")):
    """Copia o banco SQLite com WAL, sem migrar o original."""
    target = out or Path("backups") / ("radar-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".db")
    try:
        typer.echo(f"Backup verificado: {backup(settings.db_path, target)}")
    except (OSError, ValueError, RuntimeError) as e:
        typer.echo(f"Backup falhou: {type(e).__name__}", err=True)
        raise typer.Exit(1)


@app.command("tick")
def tick_command(chain: str = "robinhood", limit: int = typer.Option(40, min=1, max=100), offline: bool = False):
    """Executa um ciclo; saída 2 significa cotações indisponíveis."""
    try:
        with worker_lock(settings.db_path):
            conn = db.connect()
            try:
                stats = tick(conn, chain, limit, offline)
                typer.echo(dumps(stats))
            finally:
                conn.close()
    except (ValueError, RuntimeError, OSError) as e:
        typer.echo(f"Ciclo falhou: {type(e).__name__}: {e}", err=True)
        raise typer.Exit(1)
    if stats["unavailable"]:
        raise typer.Exit(2)


@app.command("run")
def run_command(interval: int = typer.Option(60, min=30, max=3600), chain: str = "robinhood",
                limit: int = typer.Option(40, min=1, max=100)):
    """Worker contínuo; retoma jobs existentes e impede duplicação de processo."""
    try:
        with worker_lock(settings.db_path):
            conn = db.connect()
            try:
                while True:
                    started = time.monotonic()
                    try:
                        stats = tick(conn, chain, limit)
                        typer.echo(dumps(stats))
                    except Exception as e:
                        logging.error("learning cycle failed: %s", type(e).__name__)
                    time.sleep(max(1, interval - (time.monotonic() - started)))
            finally:
                conn.close()
    except RuntimeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)


@app.command("report")
def report_command(as_json: bool = typer.Option(False, "--json")):
    """Métricas descritivas e cobertura; não promove modelos."""
    conn = db.connect()
    try:
        result = report(conn)
        typer.echo(dumps(result) if as_json else render(result))
    finally:
        conn.close()


@app.command("risk")
def risk_command(capital: str, entry: str, stop: str, risk: str = "1", fee_bps: str = "10", slip_bps: str = "20"):
    """Dimensionamento long sem alavancagem, na mesma moeda para todos os valores."""
    try:
        typer.echo(dumps(position_size(capital, risk, entry, stop, fee_bps, slip_bps)))
    except ValueError as e:
        raise typer.BadParameter(str(e))
