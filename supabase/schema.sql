-- GD Quiet Supabase Schema (Auth with Discord + Account Linking + Realtime Sync)
-- Run this entire script once in the Supabase Dashboard -> SQL Editor.

create table if not exists public.gdquiet_accounts (
    user_id uuid primary key references auth.users(id) on delete cascade,
    account_number text unique not null,
    discord_id text not null default '',
    discord_username text not null default '',
    discord_avatar text not null default '',
    mute_at_percent double precision not null default 50.0
        check (mute_at_percent >= 0.0 and mute_at_percent <= 100.0),
    current_percent double precision not null default 0.0
        check (current_percent >= 0.0 and current_percent <= 100.0),
    is_playing boolean not null default false,
    should_mute boolean not null default false,
    level_name text not null default '',
    updated_at timestamptz not null default now()
);

alter table public.gdquiet_accounts enable row level security;

-- Authenticated users can read and update ONLY their own row.
drop policy if exists "gdquiet_select_own" on public.gdquiet_accounts;
create policy "gdquiet_select_own"
    on public.gdquiet_accounts
    for select
    to authenticated
    using (auth.uid() = user_id);

drop policy if exists "gdquiet_update_own" on public.gdquiet_accounts;
create policy "gdquiet_update_own"
    on public.gdquiet_accounts
    for update
    to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

-- Add table to Supabase Realtime publication if not already added.
do $$
begin
    if not exists (
        select 1
        from pg_publication_tables
        where pubname = 'supabase_realtime'
          and schemaname = 'public'
          and tablename = 'gdquiet_accounts'
    ) then
        alter publication supabase_realtime add table public.gdquiet_accounts;
    end if;
end
$$;

-- Helper: generate a unique 8-digit account number (10000000..99999999).
create or replace function public.generate_gdquiet_account_number()
returns text
language plpgsql
security definer
set search_path = public
as $$
declare
    v_candidate text;
    v_tries int := 0;
begin
    loop
        v_tries := v_tries + 1;
        v_candidate := (10000000 + floor(random() * 90000000)::bigint)::text;
        exit when not exists (
            select 1 from public.gdquiet_accounts where account_number = v_candidate
        );
        if v_tries > 50 then
            raise exception 'Failed to allocate unique GD Quiet account number';
        end if;
    end loop;
    return v_candidate;
end;
$$;

-- Authenticated RPC called by GDQuiet.exe after Discord login via Supabase.
create or replace function public.get_or_create_gdquiet_account()
returns public.gdquiet_accounts
language plpgsql
security definer
set search_path = public, auth
as $$
declare
    v_uid uuid := auth.uid();
    v_meta jsonb;
    v_discord_id text := '';
    v_discord_name text := '';
    v_discord_avatar text := '';
    v_row public.gdquiet_accounts;
begin
    if v_uid is null then
        raise exception 'Not authenticated';
    end if;

    select raw_user_meta_data
      into v_meta
      from auth.users
     where id = v_uid;

    v_meta := coalesce(v_meta, '{}'::jsonb);
    v_discord_id := coalesce(
        nullif(v_meta->>'provider_id', ''),
        nullif(v_meta->>'sub', ''),
        ''
    );
    v_discord_name := coalesce(
        nullif(v_meta->'custom_claims'->>'global_name', ''),
        nullif(v_meta->>'full_name', ''),
        nullif(v_meta->>'name', ''),
        nullif(v_meta->>'user_name', ''),
        'Discord User'
    );
    v_discord_avatar := coalesce(nullif(v_meta->>'avatar_url', ''), '');

    insert into public.gdquiet_accounts (
        user_id,
        account_number,
        discord_id,
        discord_username,
        discord_avatar
    )
    values (
        v_uid,
        public.generate_gdquiet_account_number(),
        v_discord_id,
        v_discord_name,
        v_discord_avatar
    )
    on conflict (user_id) do update
        set discord_id = excluded.discord_id,
            discord_username = excluded.discord_username,
            discord_avatar = excluded.discord_avatar,
            updated_at = now()
    returning * into v_row;

    return v_row;
end;
$$;

revoke all on function public.get_or_create_gdquiet_account() from public;
grant execute on function public.get_or_create_gdquiet_account() to authenticated;

-- Authenticated RPC called by GDQuiet.exe to update the mute threshold percent.
create or replace function public.update_gdquiet_mute_percent(p_mute_at_percent double precision)
returns public.gdquiet_accounts
language plpgsql
security definer
set search_path = public
as $$
declare
    v_uid uuid := auth.uid();
    v_clamped double precision := least(100.0, greatest(0.0, coalesce(p_mute_at_percent, 50.0)));
    v_row public.gdquiet_accounts;
begin
    if v_uid is null then
        raise exception 'Not authenticated';
    end if;

    update public.gdquiet_accounts
       set mute_at_percent = v_clamped,
           updated_at = now()
     where user_id = v_uid
    returning * into v_row;

    if not found then
        raise exception 'GD Quiet account not initialized';
    end if;

    return v_row;
end;
$$;

revoke all on function public.update_gdquiet_mute_percent(double precision) from public;
grant execute on function public.update_gdquiet_mute_percent(double precision) to authenticated;

-- Mod RPC: fetch threshold percent by account number (exposes no personal details).
create or replace function public.gdquiet_mod_sync(p_account_number text)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_code text := trim(coalesce(p_account_number, ''));
    v_percent double precision;
begin
    if v_code = '' then
        return jsonb_build_object('found', false);
    end if;

    select mute_at_percent
      into v_percent
      from public.gdquiet_accounts
     where account_number = v_code;

    if not found then
        return jsonb_build_object('found', false);
    end if;

    return jsonb_build_object(
        'found', true,
        'mute_at_percent', v_percent
    );
end;
$$;

revoke all on function public.gdquiet_mod_sync(text) from public;
grant execute on function public.gdquiet_mod_sync(text) to anon, authenticated;

-- Mod RPC: report current Geometry Dash level progress and mute state by account number.
create or replace function public.gdquiet_mod_report(
    p_account_number text,
    p_percent double precision,
    p_is_playing boolean,
    p_should_mute boolean,
    p_level_name text default ''
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_code text := trim(coalesce(p_account_number, ''));
    v_pct double precision := least(100.0, greatest(0.0, coalesce(p_percent, 0.0)));
    v_row public.gdquiet_accounts;
begin
    if v_code = '' then
        return jsonb_build_object('ok', false, 'error', 'empty_account_number');
    end if;

    update public.gdquiet_accounts
       set current_percent = v_pct,
           is_playing = coalesce(p_is_playing, false),
           should_mute = coalesce(p_should_mute, false),
           level_name = left(coalesce(p_level_name, ''), 120),
           updated_at = now()
     where account_number = v_code
    returning * into v_row;

    if not found then
        return jsonb_build_object('ok', false, 'error', 'account_not_found');
    end if;

    return jsonb_build_object(
        'ok', true,
        'mute_at_percent', v_row.mute_at_percent
    );
end;
$$;

revoke all on function public.gdquiet_mod_report(text, double precision, boolean, boolean, text) from public;
grant execute on function public.gdquiet_mod_report(text, double precision, boolean, boolean, text) to anon, authenticated;
