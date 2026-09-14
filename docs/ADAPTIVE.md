# Smart Money Radar — primeira entrega do sistema adaptativo

Esta versão acrescenta uma base operacional de pesquisa: sinais imutáveis,
acompanhamento pós-sinal, simulação isolada, risco e estatísticas descritivas.
É experimental e precisa ser ativada explicitamente. Não faz ordens reais,
não promove modelos e não altera os pesos dos alertas atuais.

## O que está implementado

| Componente | Comportamento entregue |
|---|---|
| Registro de sinais | Snapshot permanente dos candidatos observados no ranking canônico, versão, componentes e qualidade |
| Outcome Tracker | Jobs persistentes de 5m, 15m, 30m, 1h, 4h, 12h, 24h, 3d e 7d |
| Paper Trading | Long, entrada posterior ao sinal, stop, dois alvos parciais, expiração, taxa e slippage |
| Risk Engine | Dimensionamento Decimal com limites de risco, capital e liquidez |
| Analytics | Resultados fechados, expectativa, Profit Factor, médias, mediana, sequências e grupos |
| Continuidade | Lock por banco, jobs retomáveis, transações e registro de falhas |
| Versionamento | Configuração experimental congelada junto à versão |
| Proteção temporal | Rejeita entrada futura, não preenche o passado com cotações recebidas depois do prazo |

O código está em `fomo_agent/learning/`. A migração 20 apenas acrescenta tabelas,
índices e triggers; mantém carteiras, trades e traduções já existentes.

## Ativação no Termux

No aplicativo Termux, entre em `~/smart-money-radar`. Antes de atualizar, faça
uma cópia consistente do SQLite, inclusive dados ainda no WAL. Este bloco usa
somente bibliotecas já presentes e não executa migrações:

```bash
cd ~/smart-money-radar && python - <<'PY'
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from fomo_agent.config import settings
source = settings.db_path.resolve()
folder = Path.home() / "radar-backups"
folder.mkdir(exist_ok=True)
target = folder / ("before-learning-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".db")
with target.open("xb"):
    pass
target.chmod(0o600)
src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30)
dst = sqlite3.connect(target)
try:
    src.backup(dst)
    assert dst.execute("PRAGMA quick_check").fetchone()[0] == "ok"
finally:
    dst.close()
    src.close()
print("Backup verificado:", target)
PY
```

Após confirmar a mensagem de backup:

```bash
cd ~/smart-money-radar && git pull --ff-only origin main
fomo-radar learning tick
fomo-radar learning report
```

Uma instalação feita com `pip install -e .` passa a usar o código atualizado.
Se a instalação antiga não for editável, execute `python -m pip install -e .`.
Nenhuma dependência Python nova foi adicionada.

O primeiro ciclo pode mostrar zero sinais: o ranking exige pelo menos duas
carteiras confiáveis compradoras. `unavailable > 0` indica falha ou ausência de
cotações para parte dos ativos; a saída do comando será 2. Isso não é validado
como um ciclo de preços completo. `--offline` não consulta provedores e não
deve ser usado para simular coleta ao vivo.

Para iniciar a rotina contínua:

```bash
bash scripts/learning_start.sh
tail -n 20 logs/learning.log
```

O worker usa o mesmo banco configurado no radar. Um lock exclusivo impede dois
workers de aprendizado nesse banco; uma segunda tentativa registra que o
primeiro já está ativo. O script apenas solicita a inicialização: confirme no
log que há ciclos concluídos. `nohup` não reinicia processos após reboot.

Mantenha as rotinas existentes de `track`, `holdings` e preços: o novo worker
não coleta transações das carteiras. Ele consulta preços dos sinais acompanhados
e registra suas próprias observações. Não envia Telegram nesta entrega.

Para observar detalhes e testar o cálculo de risco:

```bash
fomo-radar learning report --json
fomo-radar learning risk 5000 10 9.5 --fee-bps 0 --slip-bps 0
```

Resultado do exemplo sem custos: 100 unidades, posição de 1.000 e perda
planejada de 50. Todos os valores devem estar na mesma moeda. O risco planejado
não é perda máxima garantida: gaps e ausência de liquidez podem excedê-lo.
Backups seguintes podem usar `fomo-radar learning backup`.

## Universo, features e score

O worker lê os candidatos do ranking canônico da blockchain escolhida
(padrão `robinhood`), com janela de 24 horas e limite padrão de 40.
Ele registra a detecção no momento em que observa o ranking, sem atribuir um
horário retroativo ao sinal. Não captura ainda todas as categorias de alertas
(bursts, lançamentos, saídas) nem todas as oportunidades do mercado.

Uma nova detecção por ativo/versão é permitida após 24 horas. Se a primeira não
tiver preço e uma cotação válida aparecer, cria outra detecção, preservando a
anterior. Isso evita corrigir retroativamente um sinal que não tinha entrada.

Versão `onchain-research-v1`, score experimental:

- Compradores confiáveis: `min(buyers, 5) / 5 * 30`.
- Qualidade média das carteiras: `min(avg_score, 100) / 100 * 40`.
- Liquidez em USD: `min(liquidity, 100000) / 100000 * 30`.

Valores negativos são limitados a zero. Se faltar um componente, score total
fica desconhecido. Esses pesos são uma regra inicial explicitamente escolhida,
não pesos aprendidos ou comprovados. Confiança permanece como
`insufficient_validated_evidence`.

Snapshot inclui compradores, qualidade das carteiras, conviction, volume de
compras das carteiras acompanhadas, liquidez, capitalização, idade quando
disponível e o ranking original. Volume dessas carteiras não é volume total do
mercado. Acelerações, momentum, concentração, CVD, order book e contexto BTC
ficam `null` até existirem coletores e definições adequados; não são inventados.

## Tempo, qualidade e cobertura

O worker tenta observar até 40 ativos por ciclo, com intervalo padrão de 60s,
priorizando os menos recentemente consultados. Ativos sem resposta também
avançam na fila para não bloquear os demais. Mais ativos ou lentidão do provedor
podem impedir resolução de um minuto; o relatório mede essas lacunas.

O intervalo da fila é contado desde o início do ciclo, como o agendamento do
worker. O preço continua datado no recebimento da resposta. Isso evita que uma
consulta de 2s faça o ciclo seguinte enxergar apenas 58s transcorridos e pular
uma consulta inteira. Logs incluem `cycle_started_at` e `observed_at` para
verificar a duração da consulta. A correção não reabre simulações já marcadas
como incompletas nem preenche lacunas antigas. Depois de atualizar o código,
reinicie somente o processo `fomo_agent.cli learning run` para aplicar a mudança.

Cotação inicial precisa ter no máximo 120s. Os provedores atuais retornam preço
indicativo: o timestamp registrado é o recebimento da resposta, não um horário
de negócio garantido pela exchange. Respostas podem refletir caches do provedor.
Preços não são bid/ask executável. Isso limita a precisão do estudo.

Cada horizonte fecha após tolerância de 120s. Usa a cotação mais próxima do
instante desejado, dentro de ±120s, recebida até o fechamento. O desvio temporal
fica explícito. Sem cotação, resultado é `missing`; sem entrada, `no_entry`.
Nenhum preço consultado muito depois é usado como se fosse histórico conhecido.

Máxima, mínima, MFE, MAE e drawdown usam somente amostras até o horizonte.
Extremos reais entre amostras podem ser diferentes. A cobertura conta janelas
de um minuto observadas; gaps acima de 120s deixam `path_complete=false`.
O retorno no endpoint pode existir mesmo com caminho incompleto: ambos são
reportados separadamente. Não inferimos volume posterior a partir da diferença
entre dois volumes móveis de 24h.

## Contabilidade da simulação

Cada sinal elegível recebe uma simulação **isolada** de capital USD 1.000,
risco planejado 1%, score mínimo 70 e liquidez mínima USD 5.000.
Esses são parâmetros experimentais, não recomendação operacional.

Entrada: primeira cotação posterior à latência mínima de 1s, recebida dentro da
janela de entrada de 120s. Stop: 5% abaixo do preço do sinal. Alvos: +10% e +20%
em relação à entrada simulada, vendendo 50% em cada um. Expiração: 24h após a
entrada. Custos padrão: 10 bps de taxa por lado e 20 bps de slippage por lado.

Com `f=taxa/10000` e `s=slippage/10000`:

```text
entrada_executada = cotação_entrada × (1+s)
custo_unitário = entrada_executada × (1+f)
receita_unitária_no_stop = stop × (1-s) × (1-f)
risco_unitário = custo_unitário - receita_unitária_no_stop
quantidade = min(capital × risco% / risco_unitário,
                 capital / custo_unitário,
                 liquidez × 1% / custo_unitário)
PnL = soma das receitas líquidas das saídas - custo total da entrada
retorno% = PnL / custo total da entrada × 100
R realizado = PnL / perda planejada
```

O stop pode executar abaixo do nível planejado. Candles que cruzam stop e alvo
sem permitir identificar a ordem ficam `ambiguous`. Gaps de coleta ficam
`insufficient_data`, com fills conhecidos preservados e sem inventar fechamento.
TP observado em um ponto não comprova a ordem dos movimentos entre pontos.

Com custos zero, metade vendida a +10% e metade a +20% dá **+15%**, não +30%.
Taxas e slippage reduzem esse resultado. Não há financiamento, gas, MEV,
honeypot, falhas de venda ou estimativa de impacto específica por pool nesta
entrega; a execução é indicativa e não valida operações com dinheiro real.

O total de PnL soma experimentos independentes. Não representa uma carteira
com capital compartilhado, exposição simultânea e reinvestimento. Por isso
drawdown da carteira fica indisponível; drawdown do preço pós-sinal é outra métrica.

## Estatísticas e validação

Resultados fechados determinam win rate, ganho médio, perda média, retorno médio
e mediano, expectativa, Profit Factor, R realizado e sequências. Operações
abertas, inelegíveis, ambíguas e incompletas são contadas separadamente. Profit
Factor sem perdas fica indefinido, e amostra vazia nunca vira 0% de sucesso.

Há agrupamento por faixa de score, estratégia, versão e dia UTC. Exemplo
contábil testado: 40 ganhos de 120 e 60 perdas de 50 produzem lucro líquido
1.800, expectativa 18 por operação e Profit Factor 1,6.

Essas estatísticas são descritivas. Não há inferência de confiança alta,
descoberta de vantagem, seleção de modelo ou aprovação automática. Amostras
correlacionadas, seleção do ranking e exclusão dos caminhos incompletos podem
introduzir viés; a distribuição dos estados deve acompanhar qualquer análise.

## Próximas entregas da arquitetura

1. Cobertura de todas as categorias de sinal, rejeições e universo observado;
   identidade por contrato/rede/mercado e instrumentação de qualidade das fontes.
2. Binance Spot REST/WebSocket e features versionadas de trades, fluxo e livro;
   snapshots sincronizados, recuperação de gaps e confirmação entre mercados.
3. Portfolio paper com capital compartilhado, exposição correlacionada,
   execução por bid/ask, capacidade e custos específicos por mercado.
4. Analytics por contexto, categoria, hora, regime, horizonte e drawdown da carteira;
   dashboard e contexto estatístico nos alertas Telegram.
5. Casos semelhantes com normalização ajustada somente no treino, tamanho
   efetivo da amostra, intervalos de confiança e análise de estabilidade.
6. Descoberta e recalibração com divisões cronológicas, purga de labels
   sobrepostos, embargo, teste reservado e controle de múltiplas tentativas.
7. Registro de challengers, walk-forward, paper ao vivo comparável ao modelo
   vigente, aprovação explícita, rollback e detecção de degradação.
8. Nuvem e supervisão contínua quando retomarmos essa etapa. Capital real
   exige uma entrega separada e evidência líquida de custos fora da amostra.

Mais tempo de execução gera dados; melhora só existe quando demonstrada por
avaliação independente. Aprovação nos testes de software não demonstra lucro.
