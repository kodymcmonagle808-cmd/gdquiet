# GD Quiet

Standalone Windows app (`GDQuiet.exe`) that reads your level percentage directly from Geometry Dash's window or screen (no Geode mod required), mutes or ducks **only the open windows you select**, and optionally **auto-deafens Discord** using your Discord Toggle Deafen hotkey.

## Features

- **No Geode Mod Needed:** Reads the percentage text directly from the top 10% of `GeometryDash.exe` (or drag your own box with **Pick Screen Region**).
- **Per-Window Audio Controls:** Pick exactly which open windows or audio apps are affected past your chosen threshold, leaving manually muted apps untouched.
- **Mute or Duck Audio:** Mute selected apps or lower their volume to a configurable level, restoring their previous volume when the trigger ends.
- **Discord Auto-Deafen:** Record your Discord **Toggle Deafen** hotkey so Discord automatically deafens above your threshold and undeafens on death, exit, or 100%.
- **Global % Checks Toggle Keybind:** Bind a global Windows keybind on **Overview** to pause or resume `%` watching and audio muting anytime, even while Geometry Dash is focused.
- **OBS Studio Overlay:** Add a local Browser Source to show live progress, mute state, and threshold, with controls to pause watching or adjust the threshold.
- **Per-Level Profiles:** After confirming a detected level, save a per-level threshold, app selection, and capture region locally; matching saved profiles load when you confirm that level again.
- **OCR Diagnostics:** Preview the captured region, recognized text, scan time, and consecutive missed reads to help calibrate screen capture.
- **System Tray Controls:** Pause watching, toggle mute, reopen the window, or exit from the tray; optionally close the window to the tray.
- **Level Name Detection (Beta):** Optional, opt-in OCR reads level names only when visible on a pause screen. It never pauses the game; confirmations and corrections stay on this device.
- **Setup Wizard & Built-in Updater:** Distributed exclusively through `GDQuiet-Setup.exe` (accept terms, pick install folder/shortcuts), plus quick `%` position presets (**Top Center**, **Top Left**, **Top Right**, **Bottom Center**) and region box picker.

## Usage

1. Download and run **`GDQuiet-Setup.exe`** from the Releases page to walk through the Setup Wizard (accept terms, choose install folder/shortcuts, then launch GD Quiet). Future stable updates can be installed directly inside the app via **Check for Updates**. Prerelease updates are hidden unless you opt in under **Settings → Beta Updates**.
2. In Geometry Dash **Graphics** settings, use **Windowed** or **Borderless** mode and make sure **Show Percentage** is enabled.
3. In `GDQuiet.exe`:
   - Check the open windows/apps whose audio you want changed.
   - Set the audio action threshold (default `50%`).
   - *(Optional)* Set **Toggle % Checks Hotkey** to turn `%` watching on or off mid-session with one keybind.
   - *(Optional)* Pick a **Quick % Spot** preset or click **Drag Box on Screen** / **Pick with 3s Delay** if your percentage is placed somewhere else.
   - *(Optional)* Enable **Auto-Deafen Discord** and follow the built-in setup tutorial.
   - *(Optional)* In **Settings → OBS Studio Overlay**, enable the local server and add the shown URL as a 560 × 300 OBS **Browser Source**. Right-click the source and choose **Interact** to use its controls. The overlay is served only from this computer.
   - *(Optional)* In **Settings → Audio Action**, choose **Mute** or **Duck**, then set the ducked volume. The app restores only volume changes it made.
   - *(Optional)* Confirm a detected level to create or load its local profile. Adjust threshold, selected apps, and capture region as usual, then choose **Save Current Settings** on Overview.
   - *(Optional)* Use the **Screen Capture** page's OCR diagnostics to inspect recognized text and consecutive missed reads.
   - *(Optional)* Use the system tray menu to pause/resume, toggle mute, open the window, or exit. Enable **Close to System Tray** in Settings if you want the close button to hide the window instead of exiting.
   - *(Beta only)* After opting in to beta updates and installing `v4.0.0-beta.1`, use **Settings → Experimental Features (Beta)** to turn on level detection. It is off by default and only scans while screen watching is active; confirm or correct each detected title in GD Quiet. Feedback and profiles are saved locally.
