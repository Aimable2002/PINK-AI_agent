-- Phase 1 signal contract: normalized Telegram signals and trading ensembles.
-- Apply this migration to the Supabase project before enabling the new workers.

alter table public.signals
  add column if not exists signal_type text,
  add column if not exists symbol text,
  add column if not exists direction text,
  add column if not exists entry numeric,
  add column if not exists take_profits jsonb not null default '[]'::jsonb,
  add column if not exists stop_loss numeric,
  add column if not exists expiry_minutes integer,
  add column if not exists normalized_signal jsonb not null default '{}'::jsonb,
  add column if not exists parse_status text not null default 'pending',
  add column if not exists model_reasoning text;

alter table public.trading_signals
  add column if not exists signal jsonb not null default '{}'::jsonb,
  add column if not exists model_forecasts jsonb not null default '[]'::jsonb,
  add column if not exists outcome_status text not null default 'pending',
  add column if not exists outcome_direction text,
  add column if not exists outcome_price numeric,
  add column if not exists outcome_at timestamptz;

alter table public.signals
  drop constraint if exists signals_signal_type_check;
alter table public.signals
  add constraint signals_signal_type_check
  check (signal_type is null or signal_type in ('forex', 'binary_option'));

alter table public.signals
  drop constraint if exists signals_parse_status_check;
alter table public.signals
  add constraint signals_parse_status_check
  check (parse_status in ('pending', 'parsed', 'rejected'));

alter table public.trading_signals
  drop constraint if exists trading_signals_outcome_status_check;
alter table public.trading_signals
  add constraint trading_signals_outcome_status_check
  check (outcome_status in ('pending', 'won', 'lost', 'neutral', 'expired'));

create index if not exists signals_user_created_at_idx
  on public.signals (user_id, created_at desc);

create index if not exists trading_signals_user_created_at_idx
  on public.trading_signals (user_id, created_at desc);
