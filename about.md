# GD Quiet

**GD Quiet** automatically mutes selected background windows/apps (such as YouTube, Spotify, or browsers) and optionally triggers your Discord Deafen hotkey once you pass your chosen percentage in a Geometry Dash level — then unmutes and undeafens as soon as you die, restart, beat the level, or exit.

## How It Works

1. Download and run **`GDQuiet.exe`** on Windows.
2. Click **Sign in with Discord (via Supabase)** inside `GDQuiet.exe` to get your **GD Quiet Account Number**.
3. Copy that Account Number into **GD Quiet**'s mod settings inside Geometry Dash.
4. In `GDQuiet.exe`:
   - Check only the open windows/apps you want muted.
   - Set the **Mute at %** threshold.
   - *(Optional)* Enable **Auto-Deafen Discord** and record your Discord Toggle Deafen keybind.

## Network & Privacy Disclosure

- If **GD Quiet Account Number** is blank or the mod is disabled, **zero** network requests are made.
- When an account number is configured, the mod asynchronously sends only your **account number**, **current level percentage**, **level name**, and **mute state** to the configured Supabase backend so your `GDQuiet.exe` companion app can mute your selected apps in real time.
- Source code for both the Geode mod and `GDQuiet.exe` is publicly available in the linked GitHub repository.
