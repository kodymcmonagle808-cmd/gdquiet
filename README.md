# GD Quiet

Standalone Windows app (`GDQuiet.exe`) that reads your level percentage directly from Geometry Dash's window or screen (no Geode mod required), mutes **only the open windows you select**, and optionally **auto-deafens Discord** using your Discord Toggle Deafen hotkey.

## Features

- **No Geode Mod Needed:** Reads the percentage text directly from the top 10% of `GeometryDash.exe` (or drag your own box with **Pick Screen Region**).
- **Per-Window Mute Menu:** Pick exactly which open windows or audio apps get muted past your chosen percent, leaving manually muted apps untouched.
- **Discord Auto-Deafen:** Record your Discord **Toggle Deafen** hotkey so Discord automatically deafens above your threshold and undeafens on death, exit, or 100%.

## Usage

1. Download and run **`GDQuiet.exe`** from the Releases page.
2. In Geometry Dash **Graphics** settings, use **Windowed** or **Borderless** mode and make sure **Show Percentage** is enabled.
3. In `GDQuiet.exe`:
   - Check the open windows/apps you want muted.
   - Set **Mute at %** (default `50%`).
   - *(Optional)* Click **Pick Screen Region** if your percentage is placed somewhere other than the top-center of GD.
   - *(Optional)* Enable **Auto-Deafen Discord** and record your Discord Toggle Deafen keybind.
