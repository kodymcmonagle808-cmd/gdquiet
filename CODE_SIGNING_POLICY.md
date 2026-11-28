# Code signing policy

Free code signing provided by [SignPath.io](https://about.signpath.io/), certificate by [SignPath Foundation](https://signpath.org/).

## What is signed

Each release's installer, `GDQuiet-Setup.exe`, is signed. It is built from this
repository's source by the [Build and Release](.github/workflows/release.yml)
GitHub Actions workflow when a version tag is pushed, then submitted to SignPath
for signing. Nothing built outside that workflow is signed.

## Team roles

GD Quiet is maintained by a single developer, who holds every role:

| Role | Members |
|---|---|
| Committers and reviewers | [kodymcmonagle808-cmd](https://github.com/kodymcmonagle808-cmd) |
| Approvers | [kodymcmonagle808-cmd](https://github.com/kodymcmonagle808-cmd) |

Every signing request is approved manually in SignPath before the release is
published. All team members use multi-factor authentication for GitHub and
SignPath.

## Privacy policy

GD Quiet does not collect analytics or telemetry. Gameplay video, screenshots,
OCR text and audio never leave your PC. The program only contacts other
networked systems in these cases:

- **Update checks:** it asks the GitHub Releases API whether a newer version
  exists, and downloads the installer when you choose to update. Automatic
  checks can be turned off in Settings.
- **Discord sign-in (optional):** only if you choose "Sign in with Discord",
  it signs you in through Supabase and syncs your saved levels (names, level
  IDs, thresholds, app selections and capture settings) to your own account so
  they can be shared between your PCs. Only you can access them. Sign out to
  stop syncing.
- **OBS overlay (optional):** runs a web server on your own PC only
  (`127.0.0.1`); other computers cannot reach it.

The full details are in the [license and terms](installer/TERMS.txt) shown by the installer.

## License

GD Quiet is open source under the [MIT License](LICENSE).
