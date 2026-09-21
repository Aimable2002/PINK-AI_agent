-- Exact usage billing for every model, tool, search, forecast, and cache operation.

alter table public.profiles
  alter column quota_used type numeric using quota_used::numeric;

alter table public.usage_events
  alter column requests type numeric using requests::numeric,
  add column if not exists credits numeric not null default 0,
  add column if not exists operation_type text not null default 'run',
  add column if not exists provider text not null default 'backend',
  add column if not exists resource text not null default 'unknown',
  add column if not exists input_tokens integer not null default 0,
  add column if not exists output_tokens integer not null default 0,
  add column if not exists units numeric not null default 1,
  add column if not exists metadata jsonb not null default '{}'::jsonb,
  add column if not exists usage_events jsonb not null default '[]'::jsonb;

create index if not exists usage_events_user_created_at_idx
  on public.usage_events (user_id, created_at desc);

create or replace function public.charge_usage(_user_id uuid, _credits numeric)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  if _credits < 0 then
    raise exception 'credits cannot be negative';
  end if;

  update public.profiles
  set quota_used = quota_used + _credits
  where user_id = _user_id;

  if not found then
    raise exception 'profile not found for user %', _user_id;
  end if;
end;
$$;