# Plan maestro: Deribit Downloader → PostgreSQL → Superset → Backtest Iron Condor

## 1) Qué vamos a construir

### Aplicación/feature
Un pipeline de análisis **end-to-end** para opciones de Deribit que:
1. Ingesta snapshots/ticks del downloader actual.
2. Normaliza a formato compatible con `optopsy`.
3. Persiste datos crudos + datos analíticos en PostgreSQL.
4. Ejecuta backtest de `iron_condor`.
5. Publica vistas SQL consumibles por Apache Superset.

### Para quién
- Quant/devs que investigan estrategias sobre opciones crypto.
- Trading/research team que quiere dashboards en Superset con métricas de riesgo y retorno.

### Problema que resuelve
Hoy existe el downloader, pero faltan tres piezas críticas:
- Capa de almacenamiento durable y consultable (PostgreSQL).
- Capa de modelado para BI (vistas para Superset).
- Capa de investigación reproducible de estrategias (backtest estandarizado).

### Cómo funcionará
- Entrada: time series con columnas de Deribit (`timestamp`, `instrument_name`, `best_bid_price`, `best_ask_price`, `greeks.delta`, etc.).
- Transformación:
  - Parseo de `instrument_name` (símbolo, expiry, strike, tipo call/put).
  - Derivación de `quote_date` desde `timestamp` en ms.
  - Reconstrucción de `bid/ask` desde ladders `bids/asks` cuando no haya `best_*`.
- Salida:
  - Tabla `deribit_option_ticks` (granular).
  - Tabla `iron_condor_stats` (agregada por buckets DTE/OTM).
  - Tabla `iron_condor_trades` (trades raw del backtest).
  - Vistas `v_iron_condor_daily` y `v_iron_condor_backtest`.

### Conceptos principales y relaciones
- **Tick de opción**: estado en un instante para un contrato.
- **Contrato**: `underlying_symbol` + `expiration` + `strike` + `option_type`.
- **Combinación Iron Condor**: 4 patas (put long, put short, call short, call long).
- **Trade backtest**: combinación + reglas de entrada/salida + `pct_change`.
- **KPI agregado**: estadísticas por rangos (DTE y OTM).

### Distilling the model (MVP)
Para no sobrediseñar:
- Empezar sin streaming online (solo batch por CSV).
- Sin particionado avanzado al inicio.
- Sin capa API intermedia (Superset directo a PostgreSQL).

---

## 2) Diseño de UX (usuario analítico)

### User stories
1. **Researcher (happy flow):** “Cargo CSV diario, corro script, veo dashboard en Superset con IV/OI y performance de iron condors”.
2. **Researcher (alt flow):** “No hay PostgreSQL disponible; corro solo modo local y reviso CSV/parquet output”.
3. **Quant lead (alt flow):** “Necesito revisar trades individuales; consulto `iron_condor_trades` y cruzo con market regime”.

### Impacto UI
No hay UI nueva propia; la interfaz principal será Superset:
- Menú Dataset:
  - `market.deribit_option_ticks`
  - `market.v_iron_condor_daily`
  - `market.v_iron_condor_backtest`
- Dashboards recomendados:
  - Market structure diario (IV, OI, precio subyacente).
  - Backtest scoreboard por buckets DTE/OTM.

### Wireframe textual rápido
- **Dashboard 1: Market Pulse**
  - Serie temporal de `avg_mark_iv`.
  - Serie temporal de `avg_open_interest`.
  - Heatmap por strike vs día (futuro).
- **Dashboard 2: Iron Condor Lab**
  - Tabla de buckets con `count`, `mean`, `std`, `min`, `max`.
  - Filtros por `underlying_symbol` y fecha.

---

## 3) Necesidades técnicas

### Tablas propuestas

#### A) `market.deribit_option_ticks`
Campos mínimos:
- Identidad/tiempo: `timestamp`, `change_id`, `quote_date`.
- Contrato: `underlying_symbol`, `instrument_name`, `option_type`, `expiration`, `strike`.
- Mercado: `bid`, `ask`, `best_bid_price`, `best_ask_price`, `best_bid_amount`, `best_ask_amount`.
- Features: `underlying_price`, `index_price`, `mark_price`, `open_interest`, `mark_iv`, `bid_iv`, `ask_iv`, `delta`, `volume`.
- Operativa: `settlement_period`.

#### B) `market.iron_condor_stats`
Salida agregada de Optopsy:
- Buckets: `dte_range`, `otm_pct_range_leg1..4`.
- Métricas: `count`, `mean`, `std`, `min`, `25%`, `50%`, `75%`, `max`.

#### C) `market.iron_condor_trades`
Trades individuales:
- `underlying_symbol`, `expiration`, `dte_entry`, `strike_leg1..4`.
- `total_entry_cost`, `total_exit_proceeds`, `pct_change`.

### Algoritmo/librerías
- `pandas`: limpieza y transformación.
- `optopsy`: motor de backtesting (`iron_condor`).
- `sqlalchemy` + `psycopg2`: persistencia en PostgreSQL.
- Superset: capa BI.

### Patrón de diseño recomendado
- Funciones puras para transformación (`normalize_deribit_csv`).
- Separar “crear” de “usar”: función independiente para persistencia (`persist_to_postgres`).
- Modo local y modo con DB mediante flags de CLI.

### Dependencias nuevas
Opcionales (si se persiste):
- `sqlalchemy`
- `psycopg2-binary`
- `pyarrow` (parquet)

### Edge cases documentados
- `nan` como string.
- `best_bid_price/best_ask_price` en cero -> fallback al ladder `bids/asks`.
- `instrument_name` inválido -> descartar fila.
- Falta de conexión DB -> seguir en modo local.

---

## 4) Testing y seguridad

### Tests mínimos
- Unit (transformación):
  - parse de `instrument_name`.
  - parse de ladders `bids/asks`.
  - coerción robusta de floats.
- Regression:
  - comparar shape y columnas del dataset normalizado.
- Integración:
  - smoke run completo del script con `samples/data/sample_spx_data.csv`.

### Seguridad para ship
- Credenciales DB vía variable de entorno o secret manager.
- Principio de privilegios mínimos en PostgreSQL (rol de escritura y rol de lectura para Superset).
- Sanitización de inputs y validación de esquema.

---

## 5) Plan de trabajo (estimación MVP)

### Tiempo total estimado
**4–6 días hábiles** para MVP productivo.

### Milestones
1. **Día 1:** script monolítico batch + outputs locales.
2. **Día 2:** persistencia PostgreSQL + vistas base para Superset.
3. **Día 3:** dashboards iniciales + validación de métricas.
4. **Día 4:** hardening (errores, logs, retry, docs operativas).
5. **Día 5-6 (opcional):** particionado, índices, incremental loads.

### Migraciones/DDL
- Crear esquema `market`.
- Índices recomendados:
  - `(quote_date, underlying_symbol)`
  - `(expiration, strike, option_type)`
  - `(instrument_name, quote_date)`

### Riesgos y rutas alternativas
- **Mayor riesgo:** calidad y consistencia de datos de mercado.
- Ruta alternativa: validación estricta + quarantine table para filas corruptas.
- Riesgo adicional: volumen alto -> usar carga incremental por particiones temporales.

### Definition of Done
**Requerido**
- Script ejecuta fin-a-fin con input real.
- Datos persistidos en PostgreSQL.
- Superset puede consultar vistas.
- Backtest `iron_condor` produce tablas raw y agregadas.

**Opcional**
- Alertas automáticas.
- Feature store de regime labels.

---

## 6) Ripple effects

- Documentar proceso operativo (runbook): carga, fallback, troubleshooting.
- Notificar al equipo de investigación sobre nuevos datasets disponibles.
- Alinear naming conventions SQL para futuras estrategias (straddle, calendar, etc.).

---

## 7) Contexto amplio y evolución

### Limitaciones actuales
- Pipeline batch; sin ingestión streaming.
- Un único enfoque de estrategia (iron condor) en el monolito.

### Extensiones futuras
- Backtests multi-estrategia (iron butterfly, strangles, diagonals).
- Segmentación por regime (volatilidad implícita alta/baja, trending vs mean reverting).
- Optimización bayesiana de parámetros (`max_entry_dte`, `exit_dte`, `max_otm_pct`).

### Moonshot ideas
- Simulación walk-forward con recalibración diaria.
- Ensemble de estrategias con budget/risk parity.
- Motor de señal en tiempo casi real usando snapshots recientes.

---

## Qué resultados puede arrojar el backtest (ejemplos)

1. **Estadística agregada por buckets (DTE/OTM):**
   - Ejemplo real de salida: bucket `(14, 21]` con `mean ≈ 0.8619`, `std ≈ 0.0436`, `count = 2`.
   - Interpretación: en ese segmento específico, el retorno porcentual histórico del condor fue alto y relativamente estable (en muestra pequeña).

2. **Trades individuales (`raw=True`):**
   - Ejemplo: strikes `1900/1950/2000/2050`, `dte_entry=15`, `pct_change≈0.1012`.
   - Ejemplo: strikes `1900/1950/2000/2100`, `dte_entry=15`, `pct_change≈0.2241`.
   - Interpretación: permite analizar distribución de retornos, cola de pérdidas, sensibilidad por anchura de alas.

Estos dos niveles (agregado y raw) se complementan: uno sirve para priorizar zonas de parámetros y el otro para entender riesgo real y dispersión de outcomes.
