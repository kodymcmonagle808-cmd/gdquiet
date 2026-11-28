-- Applied to the "gdquiet" Supabase project on 2026-11-28.
-- One row per user: their GD Quiet level profiles, synced between PCs.
create table public.gdquiet_level_sync (
  user_id uuid primary key default auth.uid() references auth.users (id) on delete cascade,
  levels jsonb not null default '{}'::jsonb,
  deleted jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now(),
  constraint gdquiet_level_sync_levels_is_object check (jsonb_typeof(levels) = 'object'),
  constraint gdquiet_level_sync_deleted_is_object check (jsonb_typeof(deleted) = 'object'),
  constraint gdquiet_level_sync_size_limit check (pg_column_size(levels) + pg_column_size(deleted) < 1000000)
);

comment on table public.gdquiet_level_sync is 'GD Quiet level profiles per user (keyed by level key), plus deletion tombstones, for syncing between devices.';

alter table public.gdquiet_level_sync enable row level security;

create policy "gdquiet_level_sync_select_own" on public.gdquiet_level_sync
  for select to authenticated using ((select auth.uid()) = user_id);

create policy "gdquiet_level_sync_insert_own" on public.gdquiet_level_sync
  for insert to authenticated with check ((select auth.uid()) = user_id);

create policy "gdquiet_level_sync_update_own" on public.gdquiet_level_sync
  for update to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);

revoke all on public.gdquiet_level_sync from anon;
grant select, insert, update on public.gdquiet_level_sync to authenticated;
