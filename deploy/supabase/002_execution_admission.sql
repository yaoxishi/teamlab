-- Stage 3: trusted admission only. No research prompts, keys or outputs here.
-- Apply after 001. Service-only RPCs use database time and transaction locks.
begin;
alter table public.convolab_executions add column if not exists instance_id uuid;
alter table public.convolab_executions add column if not exists fence uuid;
alter table public.convolab_executions add column if not exists lease_until timestamptz;
create table if not exists public.convolab_start_actions (
  owner_id uuid not null references auth.users(id) on delete cascade,
  nonce uuid not null,
  outcome text not null check(outcome in ('accepted','existing','busy','limit')),
  execution_id uuid references public.convolab_executions(id),
  created_at timestamptz not null default now(),
  primary key(owner_id, nonce)
);
alter table public.convolab_start_actions enable row level security;
alter table public.convolab_start_actions force row level security;
revoke all on public.convolab_start_actions from public, anon, authenticated;
grant all on public.convolab_start_actions to service_role;

create or replace function public.convolab_reconcile()
returns integer language plpgsql security definer set search_path = '' as $$
declare n integer;
begin
  perform pg_catalog.pg_advisory_xact_lock(72453, 3);
  -- Expiry is durable abandonment evidence, not a lost browser connection.
  -- Expired owners cannot renew, dispatch, or publish a terminal outcome.
  update public.convolab_executions set status='interrupted',
    finished_at=statement_timestamp(), expires_at=statement_timestamp()+interval '24 hours',
    save_status=case when object_key is null then 'failed' else save_status end
    where status in ('pending','running') and lease_until <= statement_timestamp();
  get diagnostics n = row_count;
  return n;
end $$;

create or replace function public.convolab_admit(p_owner uuid, p_nonce uuid,
  p_instance uuid, p_execution uuid, p_fence uuid, p_calls integer, p_available boolean)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare a public.convolab_start_actions; e uuid; result text;
begin
  perform pg_catalog.pg_advisory_xact_lock(72453, 3);
  perform public.convolab_reconcile();
  select * into a from public.convolab_start_actions where owner_id=p_owner and nonce=p_nonce;
  if found then
    return jsonb_build_object('outcome','duplicate','execution_id',a.execution_id);
  end if;
  if p_calls is null or p_calls < 1 or p_calls > 300 then result := 'limit';
  else
    select id into e from public.convolab_executions
      where owner_id=p_owner and status in ('pending','running') limit 1;
    if found then result := 'existing';
    elsif p_available is not true or exists(select 1 from public.convolab_executions where status in ('pending','running')) then
      result := 'busy';
    else
      result := 'accepted'; e := p_execution;
      insert into public.convolab_executions(id,owner_id,status,save_status,instance_id,fence,lease_until)
        values(e,p_owner,'running','pending',p_instance,p_fence,statement_timestamp()+interval '120 seconds');
    end if;
  end if;
  insert into public.convolab_start_actions(owner_id,nonce,outcome,execution_id) values(p_owner,p_nonce,result,e);
  return jsonb_build_object('outcome',result,'execution_id',e);
end $$;

create or replace function public.convolab_renew(p_execution uuid,p_owner uuid,p_instance uuid,p_fence uuid)
returns boolean language plpgsql security definer set search_path = '' as $$
begin
  update public.convolab_executions set lease_until=statement_timestamp()+interval '120 seconds'
    where id=p_execution and owner_id=p_owner and instance_id=p_instance and fence=p_fence
      and status='running' and lease_until > statement_timestamp();
  return found;
end $$;

create or replace function public.convolab_dispatch(p_execution uuid,p_owner uuid,p_instance uuid,p_fence uuid)
returns boolean language sql volatile security definer set search_path = '' as $$
  select exists(select 1 from public.convolab_executions
    where id=p_execution and owner_id=p_owner and instance_id=p_instance and fence=p_fence
      and status='running' and lease_until > statement_timestamp());
$$;

create or replace function public.convolab_finish(p_execution uuid,p_owner uuid,p_instance uuid,p_fence uuid,p_status text)
returns boolean language plpgsql security definer set search_path = '' as $$
begin
  if p_status not in ('complete','failed') or p_status is null then return false; end if;
  update public.convolab_executions set status=p_status,finished_at=statement_timestamp(),
    expires_at=statement_timestamp()+interval '24 hours',
    save_status=case when object_key is null then 'failed' else save_status end
    where id=p_execution and owner_id=p_owner and instance_id=p_instance and fence=p_fence
      and status='running' and lease_until > statement_timestamp();
  return found;
end $$;

revoke all on function public.convolab_reconcile() from public,anon,authenticated;
revoke all on function public.convolab_admit(uuid,uuid,uuid,uuid,uuid,integer,boolean) from public,anon,authenticated;
revoke all on function public.convolab_renew(uuid,uuid,uuid,uuid) from public,anon,authenticated;
revoke all on function public.convolab_dispatch(uuid,uuid,uuid,uuid) from public,anon,authenticated;
revoke all on function public.convolab_finish(uuid,uuid,uuid,uuid,text) from public,anon,authenticated;
grant execute on function public.convolab_reconcile(),
  public.convolab_admit(uuid,uuid,uuid,uuid,uuid,integer,boolean),
  public.convolab_renew(uuid,uuid,uuid,uuid), public.convolab_dispatch(uuid,uuid,uuid,uuid),
  public.convolab_finish(uuid,uuid,uuid,uuid,text) to service_role;
commit;
