# Setting up free code signing (SignPath Foundation)

The release workflow already supports SignPath. Until the steps below are done,
releases are published unsigned, exactly as before.

1. **Turn on two-factor authentication** for your GitHub account
   (GitHub → Settings → Password and authentication). SignPath requires it.
2. **Apply** at https://signpath.org/ ("Apply" / "Get started") for the
   GitHub project https://github.com/kodymcmonagle808-cmd/gdquiet. Point them to
   [CODE_SIGNING_POLICY.md](../CODE_SIGNING_POLICY.md) and the
   [MIT license](../LICENSE). Approval is manual and can take a while.
3. Once approved, in SignPath:
   - Note your **Organization ID**.
   - Create (or check) the project with slug `gdquiet` and a signing policy with
     slug `release-signing` (other names work too, see step 4).
   - Add the artifact configuration from
     [artifact-configuration.xml](artifact-configuration.xml).
   - Link the project to GitHub using the "Trusted Build System" for GitHub.com,
     as SignPath's onboarding describes.
   - Create an **API token** for a CI user that can submit signing requests.
4. In GitHub → this repo → Settings → Secrets and variables → Actions:
   - **Secret** `SIGNPATH_API_TOKEN` = the API token.
   - **Variable** `SIGNPATH_ORGANIZATION_ID` = your organization ID.
   - Only if your slugs differ: variables `SIGNPATH_PROJECT_SLUG` and
     `SIGNPATH_SIGNING_POLICY_SLUG`.
5. Push a version tag as usual. The build pauses at **Sign installer with
   SignPath** until you approve the request in SignPath (you have up to about
   4 hours), then publishes the signed `GDQuiet-Setup.exe`.

SmartScreen trusts signed files based on the certificate's reputation, so
warnings should drop off far faster than for unsigned builds, but Microsoft may
still warn for a short time on brand-new releases.
