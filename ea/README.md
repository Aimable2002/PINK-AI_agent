# PINK AI MT5 EA

`PinkAI_EA.mq5` is the reference Expert Advisor client for the backend execution
contract. It signs in through Supabase Auth, polls the authenticated backend for
orders, atomically claims an order, executes it through `CTrade`, and reports the
broker result.

## Install

1. Open MetaEditor from MT5.
2. Copy `PinkAI_EA.mq5` into `MQL5/Experts/PINKAI/`.
3. Compile it and attach the resulting EA to one chart.
4. In MT5, open `Tools -> Options -> Expert Advisors` and allow WebRequest URLs:
   - the Supabase project URL;
   - the backend API URL.
5. Configure the EA inputs before enabling AutoTrading.

## Required inputs

- `InpBackendUrl`: backend origin, without a trailing slash.
- `InpSupabaseUrl`: Supabase project URL.
- `InpSupabaseAnonKey`: public Supabase anon key only.
- `InpEmail` and `InpPassword`: the user's Supabase Auth credentials.
- `InpClientId`: unique installation ID, for example `mt5-ACCOUNT-SERVER-01`.

The service-role key must never be entered into the EA.

## Execution behavior

The EA calls:

```text
GET  /v1/ea/orders?limit=50&client_id={installation_id}
POST /v1/ea/orders/{id}/claim?client_id={installation_id}
POST /v1/ea/orders/{id}/execution
```

The order list includes pending orders and orders already claimed by this same
installation. That allows the EA to recover after losing the network between a
successful broker submission and the receipt request. A claimed order is never
executed again; the EA only retries its receipt.

The EA uses the first take-profit level because one MT5 order has one TP in this
reference implementation. Multi-target position management should be added as a
separate, explicitly tested feature.

## Important limitations

- Standard MQL5 has no built-in WebSocket client, so this reference client uses
  HTTPS polling rather than a direct Supabase Realtime socket.
- Do not use the EA with a live account until the backend, migration, broker
  symbol mapping, volume rules, and rejection handling have been tested on demo.
- Password input is used only to obtain a Supabase session at runtime. A future
  device-code flow should replace password configuration for production use.
- `call` and `put` binary-option signals are rejected because this EA executes
  MT5 Forex-style buy/sell orders only.