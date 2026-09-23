# MT5 EA Execution Protocol

The EA uses a Supabase Auth access token as a bearer token for the backend API.
It must never contain the Supabase service-role key.

## Lifecycle

1. Sign in with Supabase Auth and retain the access and refresh tokens securely.
2. Subscribe to `public:trade_orders` through Supabase Realtime with the access token when a compatible client is available.
3. Treat each Realtime event as a notification, then fetch `GET /v1/ea/orders?client_id={installation_id}`.
4. For each order, call `POST /v1/ea/orders/{id}/claim?client_id=...`.
5. Execute only an order returned by the claim call.
6. Send the MT5 result to `POST /v1/ea/orders/{id}/execution`.

The claim operation is atomic. A second EA instance or a duplicate Realtime event
receives a conflict and must not execute the order.

## Reconnect behavior

After startup, token refresh, or any Realtime reconnect, call
`GET /v1/ea/orders?client_id={installation_id}`. The endpoint returns pending
orders plus orders previously claimed by that same installation. This recovery
request is required because Realtime delivery is at-least-once and events can
be missed while the EA is offline.

The reference MQL5 EA uses authenticated HTTPS polling because standard MQL5 has
no built-in WebSocket client. It polls at a configurable interval and uses the
same claim and recovery rules; a native Realtime WebSocket implementation can be
added later without changing the order or receipt contract.

The EA should persist processed order IDs locally and use `order_id` as its
idempotency key. Repeated execution receipt submissions are safe for the same
order and overwrite the existing receipt.

## Receipt statuses

Use `executed` for a filled order, `partial` for a partial fill, `rejected` when
the broker rejects it, and `failed` for a local or transport failure. Include the
MT5 account number, broker ticket when available, requested and fill prices,
volume, broker error details, and the raw broker response.

The backend creates a `trade_orders` row automatically when a `signals` row first
becomes `parse_status = 'parsed'`. The original signal remains unchanged for
audit purposes; execution history is stored in `trade_executions`.