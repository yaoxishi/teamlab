-- Approved Stage 4 retention: 7 days inactive, no remaining study/object data.
-- Apply only to a dedicated ConvoLab Supabase project, after 001/002/003.
-- Session_active stays read-only: PostgREST GET/RLS transactions cannot write.
begin;
create table if not exists public.convolab_identity_activity (
 owner_id uuid primary key references auth.users(id) on delete cascade,
 last_seen_at timestamptz not null
);
alter table public.convolab_identity_activity enable row level security;
alter table public.convolab_identity_activity force row level security;
revoke all on public.convolab_identity_activity from public,anon,authenticated;
grant all on public.convolab_identity_activity to service_role;

create or replace function public.convolab_identity_created()
returns trigger language plpgsql security definer set search_path='' as $$
begin
 if new.is_anonymous is true then
  insert into public.convolab_identity_activity(owner_id,last_seen_at)
   values(new.id,statement_timestamp()) on conflict(owner_id) do nothing;
 end if;
 return new;
end $$;
revoke all on function public.convolab_identity_created() from public,anon,authenticated,service_role;
drop trigger if exists convolab_identity_created on auth.users;
create trigger convolab_identity_created after insert on auth.users for each row execute function public.convolab_identity_created();
-- Preserve an existing activity clock on reapply. Missing historical clocks
-- receive a conservative seven-day grace period starting at migration time.
insert into public.convolab_identity_activity(owner_id,last_seen_at)
 select id,statement_timestamp() from auth.users where is_anonymous is true
 on conflict(owner_id) do nothing;

create or replace function public.convolab_touch()
returns boolean language plpgsql security definer set search_path='' as $$
declare owner uuid;
begin
 perform pg_catalog.pg_advisory_xact_lock(72453,3);
 owner := (select auth.uid());
 if owner is null or (select public.convolab_session_active()) is not true
   or not exists(select 1 from auth.users where id=owner and is_anonymous is true) then return false; end if;
 insert into public.convolab_identity_activity(owner_id,last_seen_at)
  values(owner,statement_timestamp()) on conflict(owner_id) do update set last_seen_at=excluded.last_seen_at;
 return true;
end $$;
revoke all on function public.convolab_touch() from public,anon,authenticated;
grant execute on function public.convolab_touch() to authenticated;

-- Same admission contract, with live anonymous identity and activity guards
-- inside the existing transaction lock. A deleted identity cannot consume a
-- new action or be recreated from an old trusted server observation.
create or replace function public.convolab_admit(p_owner uuid,p_nonce uuid,
 p_instance uuid,p_execution uuid,p_fence uuid,p_calls integer,p_available boolean)
returns jsonb language plpgsql security definer set search_path='' as $$
declare a public.convolab_start_actions; e uuid; result text;
begin
 perform pg_catalog.pg_advisory_xact_lock(72453,3);
 perform public.convolab_reconcile();
 if not exists(select 1 from auth.users where id=p_owner and is_anonymous is true) then
  raise exception 'Private identity unavailable' using errcode='42501';
 end if;
 insert into public.convolab_identity_activity(owner_id,last_seen_at)
  values(p_owner,statement_timestamp()) on conflict(owner_id) do update set last_seen_at=excluded.last_seen_at;
 select * into a from public.convolab_start_actions where owner_id=p_owner and nonce=p_nonce;
 if found then return jsonb_build_object('outcome','duplicate','execution_id',a.execution_id); end if;
 if p_calls is null or p_calls < 1 or p_calls > 300 then result := 'limit';
 else
  select id into e from public.convolab_executions where owner_id=p_owner and status in ('pending','running') limit 1;
  if found then result := 'existing';
  elsif p_available is not true or exists(select 1 from public.convolab_executions where status in ('pending','running')) then result := 'busy';
  else
   result := 'accepted'; e := p_execution;
   insert into public.convolab_executions(id,owner_id,status,save_status,instance_id,fence,lease_until)
    values(e,p_owner,'running','pending',p_instance,p_fence,statement_timestamp()+interval '120 seconds');
  end if;
 end if;
 insert into public.convolab_start_actions(owner_id,nonce,outcome,execution_id) values(p_owner,p_nonce,result,e);
 return jsonb_build_object('outcome',result,'execution_id',e);
end $$;
revoke all on function public.convolab_admit(uuid,uuid,uuid,uuid,uuid,integer,boolean) from public,anon,authenticated;
grant execute on function public.convolab_admit(uuid,uuid,uuid,uuid,uuid,integer,boolean) to service_role;

create or replace function public.convolab_cleanup_identities()
returns integer language plpgsql security definer set search_path='' as $$
declare candidate uuid; n integer := 0;
begin
 perform pg_catalog.pg_advisory_xact_lock(72453,3);
 -- Identity selection and removal are one DB transaction, not a stale list
 -- followed by a network delete. Waiting touch/start operations recheck after
 -- commit. Auth FKs remove sessions/activity/Start records with the identity.
 for candidate in
  select u.id from auth.users u join public.convolab_identity_activity a on a.owner_id=u.id
   where u.is_anonymous is true and a.last_seen_at <= statement_timestamp()-interval '7 days'
    and not exists(select 1 from public.convolab_executions e where e.owner_id=u.id)
    and not exists(select 1 from storage.objects o where o.bucket_id='convolab-private' and split_part(o.name,'/',1)=u.id::text)
   order by a.last_seen_at,u.id limit 100 for update of u,a
 loop
  delete from auth.users where id=candidate and is_anonymous is true;
  if found then n := n+1; end if;
 end loop;
 return n;
end $$;
revoke all on function public.convolab_cleanup_identities() from public,anon,authenticated;
grant execute on function public.convolab_cleanup_identities() to service_role;
commit;
