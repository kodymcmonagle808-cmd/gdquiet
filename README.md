# GD Quiet

Standalone Windows app (`GDQuiet.exe`) that reads your level percentage directly from Geometry Dash's window or screen (no Geode mod required), mutes or ducks **only the open windows you select**, and optionally **auto-deafens Discord** using your Discord Toggle Deafen hotkey.

## Features

- **No Geode Mod Needed:** Reads the percentage text directly from the top 10% of `GeometryDash.exe` (or drag your own box with **Pick Screen Region**).
- **Per-Window Audio Controls:** Pick exactly which open windows or audio apps are affected past your chosen threshold, leaving manually muted apps untouched.
- **Mute or Duck Audio:** Mute selected apps or lower their volume to a configurable level, restoring their previous volume when the trigger ends.
- **Fade Before Threshold:** Optionally lower selected-app volume gradually before the threshold, with slow and fast fade ranges.
- **Discord Auto-Deafen:** Record your Discord **Toggle Deafen** hotkey so Discord automatically deafens above your threshold and undeafens on death, exit, or 100%.
- **Global % Checks Toggle Keybind:** Bind a global Windows keybind on **Overview** to pause or resume `%` watching and audio muting anytime, even while Geometry Dash is focused.
- **OBS Studio Overlay:** Add a local Browser Source to show live progress, mute state, and threshold, with controls to pause watching or adjust the threshold.
- **Per-Level Profiles (Beta):** Each level gets its own local threshold, app selection, and capture region. Profiles load automatically when GD Quiet reads the Eclipse `{levelName} ({levelID})` label, matching by level ID so levels with the same name stay separate. Manage them on the **Levels** tab: add a level by name, set its threshold, or switch it on or off.
- **OCR Diagnostics:** Preview the captured region, recognized text, scan time, and consecutive missed reads to help calibrate screen capture.
- **System Tray Controls:** Pause watching, toggle mute, reopen the window, or exit from the tray; optionally close the window to the tray.
- **Sign in with Discord & Level Sync:** Optional Discord sign-in through Supabase. While signed in, your saved levels sync between PCs through your account (each user can only access their own levels). The session is stored on this PC, encrypted to your Windows account.
- **Setup Wizard & Built-in Updater:** Distributed exclusively through `GDQuiet-Setup.exe` (accept terms, pick install folder/shortcuts), plus quick `%` position presets (**Top Center**, **Top Left**, **Top Right**, **Bottom Center**) and region box picker.

## Usage

1. Download and run **`GDQuiet-Setup.exe`** from the Releases page to walk through the Setup Wizard (accept terms, choose install folder/shortcuts, then launch GD Quiet). Future stable updates can be installed directly inside the app via **Check for Updates**. Beta updates are currently unavailable, so every install stays on stable releases.
2. In Geometry Dash **Graphics** settings, use **Windowed** or **Borderless** mode and make sure **Show Percentage** is enabled.
3. In `GDQuiet.exe`:
   - Check the open windows/apps whose audio you want changed.
   - Set the audio action threshold (default `50%`).
   - *(Optional)* Set **Toggle % Checks Hotkey** to turn `%` watching on or off mid-session with one keybind.
   - *(Optional)* Pick a **Quick % Spot** preset or click **Drag Box on Screen** / **Pick with 3s Delay** if your percentage is placed somewhere else.
   - *(Optional)* Enable **Auto-Deafen Discord** and follow the built-in setup tutorial.
   - *(Optional)* In **Settings → OBS Studio Overlay**, enable the local server and add the shown URL as a 560 × 300 OBS **Browser Source**. Right-click the source and choose **Interact** to use its controls. The overlay is served only from this computer.
   - *(Optional)* In **Settings → Audio Action**, choose **Mute** or **Duck**, then set the ducked volume. The app restores only volume changes it made.
   - *(Optional)* Set **Fade Audio Before Threshold** to **Slow** or **Fast** to lower selected-app volume before the threshold; the configured audio action takes over at the threshold.
   - *(Optional)* Use the **Screen Capture** page's OCR diagnostics to inspect recognized text and consecutive missed reads.
   - *(Optional)* Use the system tray menu to pause/resume, toggle mute, open the window, or exit. Enable **Close to System Tray** in Settings if you want the close button to hide the window instead of exiting.
   - *(Optional, beta)* Per-level profiles from Eclipse:
     1. In Geometry Dash (with Geode and the Eclipse mod menu), press **Tab** to open Eclipse.
     2. Go to **Labels**, choose **Add new label**, and type `{levelName} ({levelID})`. The ID keeps levels with the same name apart: when it's shown, GD Quiet matches the level by ID only. `{levelName}` on its own also works.
     3. In GD Quiet, turn on **Settings → Per-level profiles from Eclipse label (Beta)**. A **Levels** tab appears in the sidebar.
     4. Play a level: its name is read from the left 30% of the game window and its profile is created or loaded. When a level you've switched on is detected, watching turns on and that level's % and apps are used; when you move to a level that's switched off (or close the game), watching pauses again. If you turned watching on yourself, it's left alone. Turn on **Auto-add levels** to have new levels added automatically (switched off, with no % until you set one). On the **Levels** tab you can search, add levels by name and level ID, use **Test detection** to see what's being read, change each level's threshold, pick which apps each level mutes (click its app count), switch levels on or off, and drag a tighter box around the label if needed.
   - *(Optional)* Click **Sign in with Discord** in the sidebar to link your Discord account and sync your levels between PCs.
