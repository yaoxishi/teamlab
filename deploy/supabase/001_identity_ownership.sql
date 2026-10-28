-- ConvoLab hosted Stage 2. Apply only to the owner-controlled staged project.
-- No study/key storage, object upload, cleanup schedule or retention of auth
-- records is introduced here. Actual Supabase application remains a Stage 6 gate.
begin;

create or replace function public.convolab_session_active()
returns boolean language sql stable security definer set search_path = '' as $$
  select exists (
    select 1 from auth.sessions as s
    where s.user_id = (select auth.uid())
      and s.id::text = (select auth.jwt() ->> 'session_id')
      and (s.not_after is null or s.not_after > now())
  );
$$;
revoke all on function public.convolab_session_active() from public, anon, authenticated;
grant execute on function public.convolab_session_active() to authenticated;

create table if not exists public.convolab_executions (
  id uuid primary key,
  owner_id uuid not null references auth.users(id) on delete cascade,
  status text not null check (status in ('pending','running','complete','failed','interrupted')),
  save_status text not null check (save_status in ('pending','saved','failed')),
  created_at timestamptz not null default now(),
  finished_at timestamptz,
  expires_at timestamptz,
  object_key text,
  sha256 text,
  check ((status in ('pending','running') and finished_at is null and expires_at is null)
      or (status in ('complete','failed','interrupted') and finished_at is not null and expires_at is not null
          and expires_at = finished_at + interval '24 hours')),
  check ((object_key is null and sha256 is null)
      or (object_key is not null and sha256 is not null and sha256 ~ '^[0-9a-f]{64}$'
          and object_key ~ ('^' || owner_id::text || '/' || id::text ||
              '/checkpoints/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.zip$')))
  , check (save_status <> 'saved' or object_key is not null)
);
create index if not exists convolab_executions_owner_created on public.convolab_executions(owner_id, created_at desc);
alter table public.convolab_executions enable row level security;
alter table public.convolab_executions force row level security;
revoke all on public.convolab_executions from public, anon, authenticated;
grant select on public.convolab_executions to authenticated;
grant all on public.convolab_executions to service_role;
drop policy if exists convolab_execution_read on public.convolab_executions;
create policy convolab_execution_read on public.convolab_executions for select to authenticated
  using (owner_id = (select auth.uid()) and (select public.convolab_session_active())
         and (expires_at is null or expires_at > now()));

-- The private bucket has no visitor SELECT/INSERT/UPDATE/DELETE permission,
-- including through broad pre-existing policies. SELECT also enables signed
-- URLs, which would bypass per-request ownership/expiry. Stage 4 must use an
-- authenticated owner-checking gateway, never visitor-generated signed links.
insert into storage.buckets(id, name, public) values ('convolab-private', 'convolab-private', false)
  on conflict (id) do update set public = false;
drop policy if exists convolab_private_read_boundary on storage.objects;
create policy convolab_private_read_boundary on storage.objects as restrictive for select to anon, authenticated
  using (bucket_id <> 'convolab-private');
drop policy if exists convolab_private_insert_boundary on storage.objects;
create policy convolab_private_insert_boundary on storage.objects as restrictive for insert to anon, authenticated
  with check (bucket_id <> 'convolab-private');
drop policy if exists convolab_private_update_boundary on storage.objects;
create policy convolab_private_update_boundary on storage.objects as restrictive for update to anon, authenticated
  using (bucket_id <> 'convolab-private') with check (bucket_id <> 'convolab-private');
drop policy if exists convolab_private_delete_boundary on storage.objects;
create policy convolab_private_delete_boundary on storage.objects as restrictive for delete to anon, authenticated
  using (bucket_id <> 'convolab-private');

commit;
