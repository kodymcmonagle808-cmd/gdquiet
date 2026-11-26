# GD Quiet

Standalone Windows app (`GDQuiet.exe`) that reads your level percentage directly from Geometry Dash's window or screen (no Geode mod required), mutes **only the open windows you select**, and optionally **auto-deafens Discord** using your Discord Toggle Deafen hotkey.

## Features

- **No Geode Mod Needed:** Reads the percentage text directly from the top 10% of `GeometryDash.exe` (or drag your own box with **Pick Screen Region**).
- **Per-Window Mute Menu:** Pick exactly which open windows or audio apps get muted past your chosen percent, leaving manually muted apps untouched.
- **Discord Auto-Deafen:** Record your Discord **Toggle Deafen** hotkey so Discord automatically deafens above your threshold and undeafens on death, exit, or 100%.
- **Global % Checks Toggle Keybind:** Bind a global Windows keybind on **Overview** to pause or resume `%` watching and audio muting anytime, even while Geometry Dash is focused.
- **Built-in Updater & % Position Picker:** Update `GDQuiet.exe` directly from the sidebar button without redownloading manually, or choose quick `%` presets (**Top Center**, **Top Left**, **Top Right**, **Bottom Center**) / drag a box anywhere on screen.

## Usage

1. Download and run **`GDQuiet-Setup.exe`** from the Releases page to walk through the Setup Wizard (accept terms, choose install folder/shortcuts, then launch GD Quiet). Future updates can be installed directly inside the app via **Check for Updates**.
2. In Geometry Dash **Graphics** settings, use **Windowed** or **Borderless** mode and make sure **Show Percentage** is enabled.
3. In `GDQuiet.exe`:
   - Check the open windows/apps you want muted.
   - Set **Mute at %** (default `50%`).
   - *(Optional)* Set **Toggle % Checks Hotkey** to turn `%` watching on or off mid-session with one keybind.
   - *(Optional)* Pick a **Quick % Spot** preset or click **Drag Box on Screen** / **Pick with 3s Delay** if your percentage is placed somewhere else.
   - *(Optional)* Enable **Auto-Deafen Discord** and follow the built-in setup tutorial.
