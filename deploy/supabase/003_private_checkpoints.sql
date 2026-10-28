-- Stage 4: immutable checkpoints and expiry. No Cron enabled by this migration.
begin;
alter table public.convolab_executions add column if not exists save_fault boolean not null default false;
create table if not exists public.convolab_checkpoints (
  object_key text primary key,
  execution_id uuid not null references public.convolab_executions(id) on delete cascade,
  sha256 text not null check (sha256 ~ '^[0-9a-f]{64}$'),
  state text not null check (state in ('staged','current','obsolete')),
  created_at timestamptz not null default now()
);
alter table public.convolab_checkpoints enable row level security;
alter table public.convolab_checkpoints force row level security;
revoke all on public.convolab_checkpoints from public,anon,authenticated;
grant all on public.convolab_checkpoints to service_role;
-- Removing a study row must not allow its old Start nonce to be reused.
alter table public.convolab_start_actions drop constraint if exists convolab_start_actions_execution_id_fkey;
alter table public.convolab_start_actions add constraint convolab_start_actions_execution_id_fkey
  foreign key(execution_id) references public.convolab_executions(id) on delete set null;

create or replace function public.convolab_reserve_checkpoint(p_execution uuid,p_owner uuid,p_instance uuid,p_fence uuid,p_key text,p_sha text)
returns boolean language plpgsql security definer set search_path='' as $$
begin
  perform 1 from public.convolab_executions where id=p_execution and owner_id=p_owner and instance_id=p_instance and fence=p_fence
    and status='running' and lease_until > statement_timestamp() for update;
  if not found or p_sha is null or p_sha !~ '^[0-9a-f]{64}$' or p_key is null or
    p_key !~ ('^'||p_owner::text||'/'||p_execution::text||'/checkpoints/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.zip$') then return false; end if;
  insert into public.convolab_checkpoints(object_key,execution_id,sha256,state) values(p_key,p_execution,p_sha,'staged');
  return true;
end $$;

create or replace function public.convolab_publish_checkpoint(p_execution uuid,p_owner uuid,p_instance uuid,p_fence uuid,p_key text,p_sha text)
returns boolean language plpgsql security definer set search_path='' as $$
begin
  perform 1 from public.convolab_executions where id=p_execution and owner_id=p_owner and instance_id=p_instance and fence=p_fence
    and status='running' and lease_until > statement_timestamp() for update;
  if not found then return false; end if;
  perform 1 from public.convolab_checkpoints where object_key=p_key and execution_id=p_execution and sha256=p_sha and state='staged' for update;
  if not found then return false; end if;
  update public.convolab_checkpoints set state='obsolete' where execution_id=p_execution and state='current';
  update public.convolab_checkpoints set state='current' where object_key=p_key;
  update public.convolab_executions set object_key=p_key,sha256=p_sha,
    save_status=case when save_fault then 'failed' else 'saved' end where id=p_execution;
  return true;
end $$;

create or replace function public.convolab_checkpoint_confirmed(p_execution uuid,p_owner uuid,p_instance uuid,p_fence uuid,p_key text,p_sha text)
returns boolean language sql volatile security definer set search_path='' as $$
 select exists(select 1 from public.convolab_executions where id=p_execution and owner_id=p_owner and instance_id=p_instance and fence=p_fence
   and status='running' and lease_until > statement_timestamp() and object_key=p_key and sha256=p_sha);
$$;

create or replace function public.convolab_save_failed(p_execution uuid,p_owner uuid,p_instance uuid,p_fence uuid)
returns boolean language plpgsql security definer set search_path='' as $$
begin
 update public.convolab_executions set save_fault=true,save_status='failed'
  where id=p_execution and owner_id=p_owner and instance_id=p_instance and fence=p_fence
   and status='running' and lease_until > statement_timestamp();
 return found;
end $$;

create or replace function public.convolab_cleanup_objects()
returns table(object_key text) language sql volatile security definer set search_path='' as $$
 -- Inventory includes late/orphan Storage objects even after a study row was
 -- removed. Never delete storage.objects directly: use the Storage API.
 select c.object_key from public.convolab_checkpoints c join public.convolab_executions e on e.id=c.execution_id
   where c.state='obsolete' or e.expires_at <= statement_timestamp()
 union
 select o.name from storage.objects o where o.bucket_id='convolab-private'
  and o.name ~ '^[0-9a-f-]{36}/[0-9a-f-]{36}/checkpoints/[0-9a-f-]{36}\.zip$'
  and not exists(select 1 from public.convolab_executions e
    where e.owner_id::text=split_part(o.name,'/',1) and e.id::text=split_part(o.name,'/',2)
    and (e.expires_at is null or e.expires_at > statement_timestamp()))
 limit 100;
$$;

create or replace function public.convolab_cleanup_ack(p_key text)
returns boolean language plpgsql security definer set search_path='' as $$
begin
 if not exists(select 1 from public.convolab_cleanup_objects() c where c.object_key=p_key) then return false; end if;
 -- The API has removed the actual object; absence is independently checked.
 if exists(select 1 from storage.objects where bucket_id='convolab-private' and name=p_key) then return false; end if;
 delete from public.convolab_checkpoints where object_key=p_key;
 return true;
end $$;

create or replace function public.convolab_cleanup_rows()
returns integer language plpgsql security definer set search_path='' as $$
declare n integer;
begin
 delete from public.convolab_executions e where expires_at <= statement_timestamp()
  and not exists(select 1 from public.convolab_checkpoints c where c.execution_id=e.id)
  and not exists(select 1 from storage.objects o where o.bucket_id='convolab-private' and
    split_part(o.name,'/',1)=e.owner_id::text and split_part(o.name,'/',2)=e.id::text);
 get diagnostics n=row_count;
 return n;
end $$;

create or replace function public.convolab_local_expired(p_execution uuid,p_owner uuid)
returns boolean language sql volatile security definer set search_path='' as $$
 select not exists(select 1 from public.convolab_executions where id=p_execution and owner_id=p_owner
  and (expires_at is null or expires_at > statement_timestamp()));
$$;

revoke all on function public.convolab_reserve_checkpoint(uuid,uuid,uuid,uuid,text,text),public.convolab_publish_checkpoint(uuid,uuid,uuid,uuid,text,text),
 public.convolab_checkpoint_confirmed(uuid,uuid,uuid,uuid,text,text),public.convolab_save_failed(uuid,uuid,uuid,uuid),
 public.convolab_cleanup_objects(),public.convolab_cleanup_ack(text),public.convolab_cleanup_rows(),public.convolab_local_expired(uuid,uuid) from public,anon,authenticated;
grant execute on function public.convolab_reserve_checkpoint(uuid,uuid,uuid,uuid,text,text),public.convolab_publish_checkpoint(uuid,uuid,uuid,uuid,text,text),
 public.convolab_checkpoint_confirmed(uuid,uuid,uuid,uuid,text,text),public.convolab_save_failed(uuid,uuid,uuid,uuid),
 public.convolab_cleanup_objects(),public.convolab_cleanup_ack(text),public.convolab_cleanup_rows(),public.convolab_local_expired(uuid,uuid) to service_role;
commit;
