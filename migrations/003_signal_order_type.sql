-- Preserve whether a signal is intended for market, limit, or stop execution.

alter table public.signals
  add column if not exists order_type text not null default 'market';

alter table public.signals
  drop constraint if exists signals_order_type_check;
alter table public.signals
  add constraint signals_order_type_check
  check (order_type in ('market', 'limit', 'stop'));