# pepump

Bot de *trailing take-profit* para un token de pump.fun / PumpSwap, operando
vía la Lightning Transaction API de PumpPortal.

⚠️ Herramienta de trading, no consejo financiero. Las memecoins de pump.fun
son extremadamente volátiles. Probá siempre primero con `live = false` y,
después, con montos chicos.

## Requisitos

- Python 3.11+ (`tomllib` viene incluido desde 3.11).
- Una API key de PumpPortal asociada a una wallet con al menos 0.02 SOL.
  Es **obligatoria siempre**, incluso con `live = false`: el precio sale
  únicamente de `subscribeTokenTrade`, que es un stream medido.
- Recomendado: un RPC de Solana propio (Helius, QuickNode, etc.). El
  endpoint público `api.mainnet-beta.solana.com` corta por rate limit en
  `getProgramAccounts`, que es lo que se usa para encontrar el pool de
  PumpSwap de un mint ya migrado.

```bash
pip install -r requirements.txt
```

## Configuración

```bash
cp config.toml.example config.toml
```

Edita `config.toml` (está en `.gitignore`, no se versiona). La API key se
puede dejar vacía en el archivo y pasarla por entorno:

```bash
export PUMPPORTAL_API_KEY="..."
```

El `mint` **no** va en el `.toml`: se pasa por línea de comandos.

## Uso

```bash
python run.py -m <MINT> -c config.toml      # modo según general.live
python run.py -m <MINT> -v                  # logging en DEBUG
```

`Ctrl+C` (o `SIGTERM`) hace un cierre ordenado: si hay una posición
abierta, la vende al precio más actual posible antes de salir.

## Estrategia

1. Se suscribe al feed de trades del mint **antes** de comprar y espera el
   primer trade real para fijar el precio de referencia.
2. Con `entry_dip_pct = 0` compra en esa referencia. Con `entry_dip_pct > 0`
   espera a que el precio baje ese porcentaje antes de entrar (sin timeout).
3. Mientras el precio no suba `activation_pct` % desde la entrada, solo
   vigila un stop-loss duro (`initial_stop_pct`).
4. Al superar `activation_pct`, el trailing-stop se arma: el nivel de venta
   sigue al máximo alcanzado, siempre `trailing_pct` % por debajo.
5. Si el precio retrocede hasta ese nivel, vende.

Cada orden cerrada se registra en el CSV de `general.trade_history_csv`
(`""` desactiva el historial).

## Fuentes de precio

El feed en vivo de PumpPortal es la fuente principal. Hay dos fallbacks
on-chain que se activan solos cuando ese feed deja de entregar trades:

- **Bonding curve** (`PumpCurveOnChainClient`): lee las reservas virtuales
  de la cuenta PDA de pump.fun para mints que todavía no migraron.
- **PumpSwap** (`PumpSwapOnChainClient`): lee las reservas del pool para
  mints ya migrados. `subscribeTokenTrade` no cubre esos mints y deja de
  mandar trades en silencio, así que el bot lo detecta por timeout
  (`stall_timeout_seconds`) y lo confirma on-chain antes de cambiar de
  fuente.

No se usan DexScreener ni Jupiter a propósito: desalinean el precio que el
bot "ve" del precio contra el que realmente se ejecuta la orden.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

No se pega a la red en ningún test: el websocket, la Lightning API y el RPC
de Solana están todos mockeados.
