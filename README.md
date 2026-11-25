# GD Quiet

Standalone Windows app (`GDQuiet.exe`) that reads your level percentage directly from Geometry Dash's window or screen (no Geode mod required), mutes **only the open windows you select**, and optionally **auto-deafens Discord** using your Discord Toggle Deafen hotkey.

## Features

- **No Geode Mod Needed:** Reads the percentage text directly from the top 10% of `GeometryDash.exe` (or drag your own box with **Pick Screen Region**).
- **Per-Window Mute Menu:** Pick exactly which open windows or audio apps get muted past your chosen percent, leaving manually muted apps untouched.
- **Discord Auto-Deafen:** Record your Discord **Toggle Deafen** hotkey so Discord automatically deafens above your threshold and undeafens on death, exit, or 100%.
- **Discord Login via Supabase:** Sign in with Discord inside `GDQuiet.exe` to generate your GD Quiet Account Number and save your threshold/live status.

## Usage

1. Download and run **`GDQuiet.exe`** from the Releases page.
2. In Geometry Dash **Graphics** settings, use **Windowed** or **Borderless** mode and make sure **Show Percentage** is enabled.
3. In `GDQuiet.exe`:
   - Check the open windows/apps you want muted.
   - Set **Mute at %** (default `50%`).
   - *(Optional)* Click **Pick Screen Region** if your percentage is placed somewhere other than the top-center of GD.
   - *(Optional)* Enable **Auto-Deafen Discord** and record your Discord Toggle Deafen keybind.
   - *(Optional)* Click **Sign in with Discord** to link your GD Quiet account via Supabase.

## Setup (Supabase, Discord & GitHub Secrets)

1. Run [`supabase/schema.sql`](supabase/schema.sql) in the Supabase **SQL Editor**.
2. In Supabase **Authentication → URL Configuration → Redirect URLs**, add `http://127.0.0.1:54321/callback` and `http://127.0.0.1:*/callback`.
3. Enable **Discord** in Supabase **Authentication → Sign In / Providers**, and add Supabase's callback URL to your Discord Developer Portal app under **OAuth2 → Redirects**.
4. Set `SUPABASE_URL` and `SUPABASE_PUBLISHABLE_KEY` (or `SUPABASE_ANON_KEY`) in GitHub **Settings → Secrets and variables → Actions** before releasing.
