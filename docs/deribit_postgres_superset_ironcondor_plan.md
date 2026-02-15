# Plan MVP: Deribit Downloader → PostgreSQL → Superset + Backtest Iron Condor

## 1) Qué estamos construyendo

- **Aplicación/feature**: pipeline analítico para ingerir snapshots/time-series de opciones de Deribit, persistirlos en PostgreSQL y explotarlos en Apache Superset, con módulo de investigación cuantitativa para estrategia **Iron Condor**.
- **Para quién**: equipo de trading/investigación cuantitativa.
- **Problema que resuelve**:
  - datos crudos dispersos y difíciles de consultar;
  - falta de trazabilidad histórica por instrumento/strike/expiry;
  - dificultad para comparar escenarios de backtest.
- **Cómo funciona (MVP)**:
  1. Downloader ya existente guarda CSV/parquet “raw”.
  2. Un proceso ETL normaliza columnas Deribit a modelo canónico.
  3. Carga incremental a PostgreSQL (tabla de ticks + agregados).
  4. Superset consume vistas SQL para dashboards de mercado y performance.
  5. Script monolítico ejecuta backtest de Iron Condor sobre dataset normalizado.
- **Conceptos principales**:
  - `quote_tick`: snapshot por `timestamp` + `instrument_name`.
  - `option_contract`: parse de `instrument_name` (underlying, expiry, strike, C/P).
  - `backtest_run`: corrida parametrizada (DTE, OTM, slippage, etc.).
  - `backtest_trade`: trade individual generado por la estrategia.

> Distillation/MVP: primero sólo ETH/BTC options, timeframe único, 1 estrategia (Iron Condor), 2 dashboards y 1 script de análisis reproducible.

---

## 2) Diseño de experiencia de usuario

### User stories (happy path)
1. Como analista, subo/ingiero datos Deribit y veo en Superset el estado de mercado por vencimiento y strike.
2. Como quant, lanzo un backtest Iron Condor con parámetros definidos y comparo resultados por buckets DTE/OTM.
3. Como lead, consulto KPIs agregados (win rate, media, p95 drawdown aproximado) por período.

### Alternative flows
- Si faltan `best_bid_price/best_ask_price`, usar primer nivel de `bids/asks`.
- Si un instrumento no parsea, marcarlo como inválido y enviarlo a cuarentena.
- Si llegan duplicados por `change_id`, deduplicar en staging.

### Impacto en UI (Superset)
- Menú “Market Microstructure”:
  - Heatmap strike vs expiry (OI/IV/mark).
  - Timeseries de `mark_iv`, `underlying_price`, `open_interest`.
- Menú “Iron Condor Research”:
  - Tabla de buckets (`dte_range`, `otm_pct_range`, `mean`, `std`, `count`).
  - Distribución de `pct_change` y top/bottom trades.

### Wireframe textual
- **Dashboard 1**: filtros globales (underlying, fecha, expiry range), KPIs arriba, heatmap y curva IV abajo.
- **Dashboard 2**: filtros de estrategia (max_entry_dte, exit_dte, slippage), tabla de buckets, histograma PnL, tabla de trades extremos.

---

## 3) Necesidades técnicas

### Modelo de datos PostgreSQL

```sql
create table if not exists deribit_option_ticks (
  ts timestamptz not null,
  change_id bigint,
  instrument_name text not null,
  underlying_symbol text not null,
  option_type text not null check (option_type in ('call','put')),
  expiration date not null,
  strike numeric(18,8) not null,
  index_price numeric(18,8),
  underlying_price numeric(18,8),
  best_bid_price numeric(18,8),
  best_ask_price numeric(18,8),
  bid_iv numeric(10,4),
  ask_iv numeric(10,4),
  mark_iv numeric(10,4),
  mark_price numeric(18,8),
  open_interest numeric(20,4),
  volume numeric(20,4),
  delta numeric(12,8),
  gamma numeric(12,8),
  vega numeric(12,8),
  theta numeric(12,8),
  rho numeric(12,8),
  settlement_period text,
  raw_payload jsonb,
  primary key (ts, instrument_name)
);

create index if not exists idx_ticks_underlying_ts
  on deribit_option_ticks (underlying_symbol, ts desc);
create index if not exists idx_ticks_expiry_strike
  on deribit_option_ticks (expiration, strike);
```

### ETL recomendado
- **Staging** (`COPY` masivo) → **normalización** → **upsert final**.
- Reglas:
  - `timestamp(ms)` → `timestamptz`.
  - parse `instrument_name` -> `underlying_symbol`, `expiration`, `strike`, `option_type`.
  - `bid/ask`: usar `best_bid/ask`; fallback a primer nivel de `bids/asks`.
  - `nan` string a NULL.

### Backtest técnico
- Librería: `optopsy` (`iron_condor(..., raw=True/False)`).
- Parámetros clave: `max_entry_dte`, `exit_dte`, `max_otm_pct`, `slippage`, `min_bid_ask`.
- Salidas:
  - agregadas por bucket (media, std, percentiles, count);
  - trades crudos con strikes de 4 patas, costo entrada/salida y `pct_change`.

### Dependencias
- PostgreSQL 14+
- Apache Superset
- Python: `pandas`, `sqlalchemy`, `psycopg2-binary`, `optopsy`

---

## 4) Testing y seguridad

### Testing
- Unit tests ETL:
  - parse `instrument_name`;
  - fallback de `bid/ask`;
  - tratamiento de NaN/string corrupto.
- Regression:
  - snapshot de outputs del backtest sobre dataset fijo.
- E2E:
  - ingesta → DB → vista Superset válida.

### Seguridad
- DB user de sólo escritura para ingesta y sólo lectura para Superset.
- Secrets por variables de entorno / vault (no hardcode).
- Auditoría mínima: log de corridas ETL/backtest con hash de dataset.

---

## 5) Plan de trabajo

### Estimación MVP (2–3 semanas)
1. **Día 1-2**: DDL + staging + índices + particionado opcional por día.
2. **Día 3-5**: ETL incremental robusto + dedupe + métricas de calidad.
3. **Día 6-8**: Vistas SQL para Superset + dashboards iniciales.
4. **Día 9-10**: Script de investigación Iron Condor + baseline de resultados.
5. **Día 11-12**: Testing, hardening y documentación.

### Riesgos principales
- Calidad/consistencia del feed (campos vacíos, bursts, duplicados).
- Volumen alto (coste de storage e índices).
- Sesgos de backtest por slippage/latencia.

### Definition of Done
- Requerido:
  - ETL incremental funcionando;
  - dashboard Superset operativo;
  - backtest reproducible Iron Condor.
- Opcional:
  - particionamiento avanzado + materialized views + alertas automáticas.

---

## 6) Ripple effects

- Actualizar documentación operativa (runbooks ETL y consultas estándar).
- Comunicar al equipo naming conventions y nuevas métricas.
- Integración con monitorización (errores ETL, lag de datos).

---

## 7) Contexto amplio y futuras extensiones

### Limitaciones actuales
- Modelo pensado para opciones vanilla Deribit; no contempla todavía estructura completa de order book por niveles profundos.
- Backtest de referencia no modela costes de ejecución complejos (fees dinámicas, impacto de mercado).

### Extensiones futuras
- Multi-estrategia (strangles, butterflies, calendars).
- Feature store para ML (superficie IV, skew term-structure).
- “Moonshot”: motor de simulación intradía con escenarios de ajuste dinámico de Iron Condor.

---

## ¿Qué resultados puede arrojar el backtest? (con ejemplos)

1. **Resultados agregados por buckets** (`dte_range`, `otm_pct_range`, opcional `delta_range`):
   - `count`, `mean`, `std`, `min`, percentiles, `max` de `pct_change`.
2. **Resultados de trades individuales**:
   - strikes de las 4 patas, DTE de entrada, coste neto, salida y retorno %.

### Ejemplo A (agrupado, escenario base)
- Bucket `dte_range=(14,21]`, `otm_pct_range=(0.05,0.10]`:
  - `count=320`, `mean=0.084`, `std=0.19`, `p25=-0.03`, `p50=0.06`, `p75=0.15`.

### Ejemplo B (stress slippage)
- Mismo bucket bajo `slippage="spread"`:
  - `count=320`, `mean=0.031`, `std=0.22`, `p25=-0.08`, `p50=0.02`, `p75=0.10`.

Interpretación: el edge aparente se comprime al modelar ejecución más conservadora.

---

## Script monolítico incluido

Se añadió `samples/deribit_iron_condor_study.py` para:
- transformar columnas Deribit al formato de `optopsy`;
- ejecutar Iron Condor en modo base y stress;
- imprimir ejemplos concretos de buckets y colas de PnL.
