# GD Quiet

Automatically mute **only the open windows you select** in `GDQuiet.exe` and optionally **auto-deafen Discord** once your Geometry Dash level progress passes your chosen percentage — then unmute and undeafen immediately when you die, restart, beat the level, exit, or close GD.

## How It Works

1. **`GDQuiet.exe`** (Windows desktop menu):
   - Sign in with **Discord through Supabase** to generate your 8-digit **GD Quiet Account Number**.
   - Check the specific open windows/apps you want muted.
   - Set your **Mute at %** threshold.
   - *(Optional)* Enable **Auto-Deafen Discord** and record your Discord **Toggle Deafen** hotkey.
2. **Geode Mod (`kodymcmonagle.gdquiet`)**:
   - Reads your live level percentage inside Geometry Dash.
   - Paste your **Account Number** from `GDQuiet.exe` into the mod settings in Geode — that's it.

## Setup (Supabase, Discord & GitHub Secrets)

### 1. Create the Supabase Project & Table
1. Create a project at [supabase.com](https://supabase.com).
2. Open **SQL Editor** and run [`supabase/schema.sql`](supabase/schema.sql).
3. In **Authentication → URL Configuration → Redirect URLs**, add:
   - `http://127.0.0.1:54321/callback`
   - `http://127.0.0.1:*/callback`

### 2. Enable Discord Sign-In in Supabase
1. Create an app in the [Discord Developer Portal](https://discord.com/developers/applications).
2. Copy your **Client ID** and **Client Secret** into **Supabase → Authentication → Providers → Discord**.
3. Copy the **Callback URL** shown by Supabase (`https://<your-project>.supabase.co/auth/v1/callback`) into **Discord Developer Portal → OAuth2 → Redirects**.

### 3. Add GitHub Repository Secrets
In your GitHub repo **Settings → Secrets and variables → Actions**, add:
- `SUPABASE_URL` — `https://<your-project>.supabase.co`
- `SUPABASE_ANON_KEY` — your project's Supabase `anon` / publishable key

Trigger the **Build and Release** workflow (or push a tag like `v2.0.0`) to build both `kodymcmonagle.gdquiet.geode` and `GDQuiet.exe` with your project config baked in.

## Submitting the `.geode` File to the Geode Index

1. Publish a GitHub Release containing the raw **`kodymcmonagle.gdquiet.geode`** file (not zipped).
2. Submit the direct download URL (`https://github.com/kodymcmonagle808-cmd/gdquiet/releases/download/v2.0.0/kodymcmonagle.gdquiet.geode`) either:
   - On [geode-sdk.org](https://geode-sdk.org) (log in with GitHub → profile → **Submit a new mod**), or
   - Via CLI: `geode index login` then `geode index mods create`.
3. Wait for a Geode Index moderator to review and approve your mod.
