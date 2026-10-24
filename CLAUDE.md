# CLAUDE.md

Bot de *trailing take-profit* para tokens de Solana (pump.fun, PumpSwap,
Raydium CPMM, Meteora y cualquier otro vía Jupiter). Varias posiciones en un
mismo proceso, con panel web. El README explica el uso; aquí va lo que hace
falta para trabajar en el código.

## Dinero real: solo lecturas

La wallet de `config.toml` tiene SOL de verdad.

- **Nunca** enviar órdenes (`/execute` de Jupiter, Lightning API de
  PumpPortal), arrancar el bot en modo real ni abrir/cerrar posiciones reales
  (web, `ctl.py`, `-m`), aunque sea "para probar".
- **Sí** se puede leer para investigar: RPC (`getTransaction`,
  `getAccountInfo`, `getSignaturesForAddress`...), cotizaciones de Jupiter
  (`/order` **sin** `taker`), `pepump.db`. Los scripts de investigación van al
  scratchpad, no al repo.
- `config.toml` lleva las claves (API keys, clave privada): no mostrarlas en
  la salida ni copiarlas a ningún sitio. Está en `.gitignore`.

## Comandos

```bash
.venv/bin/python -m pytest -q        # tests (sin red: todo con dobles)
.venv/bin/ruff check .
.venv/bin/mypy                       # config en pyproject.toml
node --check pepump/web/static/app.js
```

Los tres primeros tienen que pasar antes de cada commit. El usuario arranca el
bot con `python run.py --web` (panel en http://127.0.0.1:8080) y abre las
posiciones desde ahí.

## Git

- Se trabaja y se hace commit en `dev`. El merge a `main` lo hace el usuario:
  ni push ni PR salvo que lo pida.
- Mensajes en español, `feat:` / `fix:` / `refactor:` + descripción en
  minúscula, y un cuerpo que explique el porqué (con el caso real si lo hay).

## Arquitectura

- `run.py`: arranque (CLI, logging, web, retomar posiciones). `ctl.py`:
  controla por HTTP un bot que ya corre con `--web`.
- `manager.py`: `PositionManager`, un `TrailingTakeProfitBot` por mint, la
  vista previa del formulario y el historial.
- `bot.py`: ciclo de vida de una posición (entrada, vigilar el precio, vender,
  persistir). `strategy.py`: la regla del trailing, sin I/O.
- `executor.py`: compra/venta (real o simulada) y `Position` con sus PnL.
- Precio: `feed.py` y `trade_stream.py` (websocket compartido de PumpPortal) y
  `onchain/fallback.py`, que elige la fuente on-chain (curva de pump.fun,
  PumpSwap, Raydium CPMM, Meteora DLMM/DAMM v2, Jupiter) y decide por dónde
  van las órdenes (`pool_override`).
- Órdenes: `pumpportal.py` (Lightning API, firma PumpPortal) o `jupiter.py`
  (firmamos nosotros). `onchain/txinfo.py` lee el fill real de la tx.
- `store.py`: SQLite (`pepump.db`) para retomar posiciones y guardar los
  precios del gráfico. `history.py`: CSV de operaciones cerradas.
- `web/`: FastAPI en el mismo loop que el manager. El frontend es JS sin
  build (`static/app.js`, `chart.js`, `style.css`).

## Cosas que no se ven en el código a primera vista

- **Dos precios de entrada.** `entry_price` es lo que se pagó (comisiones
  incluidas) y da el PnL neto; `market_entry_price` es el de mercado y da el
  PnL de mercado. Los umbrales de la estrategia usan uno u otro según
  `strategy_price_basis`.
- **El rent de las cuentas de token no cuenta en el PnL** de ninguna
  posición: se recupera aparte cerrando las cuentas vacías
  (`token_accounts.py`).
- **Precio por Jupiter = cotización de venta** de los tokens de la posición,
  no un precio "de pizarra". Por eso el PnL neto arranca en negativo: es el
  coste de comprar y vender al momento. La vista previa lo calcula
  (`round_trip_pct`) y la web avisa antes de comprar si pasa del 5 %.
- **Token-2022 con impuesto de transferencia.** El `outAmount` de compra de
  Jupiter no lo descuenta; `JupiterClient.buy_tokens` sí.
- **Jupiter (plan gratuito): una petición cada ~2 s para toda la cuenta.**
  Todo pasa por `_PriorityLimiter`; las órdenes adelantan a las consultas de
  precio. Cada consulta nueva hace más lento el refresco de las posiciones.
- **Las claves viajan en URLs** (Lightning API `?api-key=`, muchos RPC) y los
  errores de requests/httpx las incluyen. `redact.py` las tapa en los
  handlers de log y en `end_reason`; un texto de excepción que se guarde o
  se mande a la web por otro camino tiene que pasar por `redact()`.
- Lo que no se sabe si entró (timeout, 5xx) es `TradeOutcomeUnknown`, no
  `RuntimeError`: el executor mira la wallet antes de darlo por fallido.

## Estilo

- Español en comentarios, docstrings, logs, mensajes de la web y tests
  (nombres de test incluidos: `test_buy_tokens_descuenta_...`). Los
  identificadores van en inglés, salvo variables locales sueltas.
- Comentarios que explican el porqué, a menudo con el caso real que lo motivó
  (`caso real BTZK...WwGp: ...`). Mantener esa densidad.
- `Optional[...]` / `List[...]` de `typing` (ruff ignora UP006/UP007 a
  propósito). Líneas de hasta 120 caracteres. Python 3.11+.
- Cada bug real arreglado lleva un test que lo reproduce.
