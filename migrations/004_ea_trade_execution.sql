-- Durable execution contract for authenticated MT5/EA clients.

create table if not exists public.trade_orders (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  signal_id uuid not null references public.signals(id) on delete cascade,
  symbol text not null,
  signal_type text not null,
  direction text not null,
  order_type text not null default 'market',
  entry numeric,
  stop_loss numeric,
  take_profits jsonb not null default '[]'::jsonb,
  expiry_minutes integer,
  status text not null default 'pending',
  claimed_by text,
  claimed_at timestamptz,
  expires_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint trade_orders_signal_unique unique (signal_id),
  constraint trade_orders_status_check check (status in ('pending', 'claimed', 'executed', 'rejected', 'expired', 'failed')),
  constraint trade_orders_direction_check check (direction in ('buy', 'sell', 'call', 'put')),
  constraint trade_orders_order_type_check check (order_type in ('market', 'limit', 'stop'))
);

create index if not exists trade_orders_user_status_idx
  on public.trade_orders (user_id, status, created_at desc);

create table if not exists public.trade_executions (
  id uuid primary key default gen_random_uuid(),
  order_id uuid not null references public.trade_orders(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  mt5_account_id text not null,
  broker_ticket text,
  status text not null,
  requested_price numeric,
  fill_price numeric,
  volume numeric,
  error_code text,
  error_message text,
  raw_response jsonb not null default '{}'::jsonb,
  executed_at timestamptz,
  created_at timestamptz not null default now(),
  constraint trade_executions_order_unique unique (order_id),
  constraint trade_executions_status_check check (status in ('executed', 'rejected', 'failed', 'partial'))
);

create index if not exists trade_executions_user_created_idx
  on public.trade_executions (user_id, created_at desc);

alter table public.trade_orders enable row level security;
alter table public.trade_executions enable row level security;

drop policy if exists trade_orders_user_select on public.trade_orders;
create policy trade_orders_user_select on public.trade_orders
  for select using (auth.uid() = user_id);

drop policy if exists trade_executions_user_select on public.trade_executions;
create policy trade_executions_user_select on public.trade_executions
  for select using (auth.uid() = user_id);

do $$
begin
  if not exists (
    select 1 from pg_publication_tables
    where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'trade_orders'
  ) then
    alter publication supabase_realtime add table public.trade_orders;
  end if;
end;
$$;

create or replace function public.create_trade_order_from_signal()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if new.parse_status = 'parsed' and coalesce(new.symbol, '') <> '' and new.direction is not null then
    insert into public.trade_orders (
      user_id, signal_id, symbol, signal_type, direction, order_type,
      entry, stop_loss, take_profits, expiry_minutes, expires_at
    ) values (
      new.user_id,
      new.id,
      new.symbol,
      coalesce(new.signal_type, 'forex'),
      new.direction,
      coalesce(new.order_type, 'market'),
      new.entry,
      new.stop_loss,
      coalesce(new.take_profits, '[]'::jsonb),
      new.expiry_minutes,
      case
        when new.expiry_minutes is not null
        then now() + make_interval(mins => new.expiry_minutes)
        else null
      end
    )
    on conflict (signal_id) do nothing;
  end if;
  return new;
end;
$$;

drop trigger if exists signals_create_trade_order on public.signals;
create trigger signals_create_trade_order
after insert or update of parse_status, symbol, direction, order_type, entry, stop_loss,
  take_profits, expiry_minutes on public.signals
for each row execute function public.create_trade_order_from_signal();

-- The API uses the service role for this atomic claim operation. It is not
-- exposed to browser or EA clients.
create or replace function public.claim_trade_order(_order_id uuid, _user_id uuid, _client_id text)
returns setof public.trade_orders
language sql
security definer
set search_path = public
as $$
  update public.trade_orders
  set status = 'claimed', claimed_by = _client_id, claimed_at = now(), updated_at = now()
  where id = _order_id
    and user_id = _user_id
    and status = 'pending'
    and (expires_at is null or expires_at > now())
  returning *;
$$;

revoke all on function public.claim_trade_order(uuid, uuid, text) from public, anon, authenticated;
grant execute on function public.claim_trade_order(uuid, uuid, text) to service_role;