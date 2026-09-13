#!/usr/bin/env python3
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB = BASE / "fomo_agent.db"
JSON_FILE = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE / "traders-resumos-portugues-import.json"

if not DB.exists():
    raise SystemExit(f"ERRO: banco não encontrado: {DB}")

if not JSON_FILE.exists():
    raise SystemExit(f"ERRO: arquivo de tradução não encontrado: {JSON_FILE}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = DB.with_name(f"{DB.name}.backup-antes-resumos-pt-{stamp}")
shutil.copy2(DB, backup)

data = json.loads(JSON_FILE.read_text(encoding="utf-8"))
if not isinstance(data, list):
    raise SystemExit("ERRO: JSON precisa conter uma lista.")

conn = sqlite3.connect(DB, timeout=15)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA busy_timeout=15000")

try:
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(traders)")}
    if "ai_summary_pt" not in columns:
        conn.execute("ALTER TABLE traders ADD COLUMN ai_summary_pt TEXT")

    atualizados = 0
    ausentes = []

    with conn:
        for item in data:
            address = str(item.get("address") or "").strip().lower()
            resumo = str(item.get("ai_summary_pt") or "").strip()

            if not address or not resumo:
                continue

            cur = conn.execute(
                """
                UPDATE traders
                SET ai_summary_pt = ?
                WHERE lower(address) = ?
                """,
                (resumo, address),
            )

            if cur.rowcount:
                atualizados += cur.rowcount
            else:
                ausentes.append({
                    "address": address,
                    "fomo_handle": item.get("fomo_handle"),
                })

    total_pt = conn.execute(
        """
        SELECT COUNT(*)
        FROM traders
        WHERE ai_summary_pt IS NOT NULL
          AND trim(ai_summary_pt) <> ''
        """
    ).fetchone()[0]

    total = conn.execute("SELECT COUNT(*) FROM traders").fetchone()[0]

    print("IMPORTAÇÃO CONCLUÍDA")
    print(f"Backup do banco: {backup.name}")
    print(f"Registros atualizados nesta execução: {atualizados}")
    print(f"Traders com resumo em português: {total_pt}/{total}")

    if ausentes:
        print(f"Avisos: {len(ausentes)} endereço(s) do JSON não foram encontrados no banco.")
        for item in ausentes[:10]:
            print(" -", item["fomo_handle"], item["address"])
        if len(ausentes) > 10:
            print(f" ... e mais {len(ausentes) - 10}")
finally:
    conn.close()
