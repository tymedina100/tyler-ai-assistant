# Deterministic TylerOS companion hosting

The companion is `tyleros_worker.py`, isolated from `group_bot.py` and all model
SDKs. `Dockerfile.tyleros` already packages it as an unprivileged stdlib-only
process. It polls canonical TylerOS jobs; it stores no parallel personal state.
Do not use the Telegram bot's start command or provider environment for it.

The repository describes Railway hosting for the group bot, but those documents
do not prove an existing service has spare capacity or permission for increased
resource usage. No authenticated Railway inventory was available during the
2026-09-09 inspection. Railway's current [plans](https://docs.railway.com/pricing/plans)
include limited free credit and metered resources. Do not assume an additional
always-running service is free or enable billing without Tyler's approval.

The available no-new-hosting-cost option is this Mac. A user LaunchAgent restarts
on failure and launches when the user logs in. It cannot work while the Mac is
asleep, shut down, offline, or logged out. No power settings are changed. Queued
jobs remain canonical on the server until an eligible worker claims them.
Use the existing always-on host only after confirming access and spending scope.

## Private configuration and preparation

Create a JSON file outside Git in a private directory, owned by the current user,
mode 600. Its allowed string keys are:

- `TYLEROS_URL`: final HTTPS origin without a path, query, or credentials.
- `TYLEROS_RUNTIME_CREDENTIAL`: the companion's generated `tylrt_` instance token.
- `TYLEROS_POLL_SECONDS`: optional, defaults to `30`, range `15`–`300`.
- `TYLEROS_VERCEL_PROTECTION_BYPASS`: optional Vercel automation bypass secret.

Never copy a system runtime token, database URL, human passphrase, or provider
key into this file. Do not pass credentials as shell arguments. The runner refuses
unknown keys, insecure remote HTTP, weak credential format, permissive modes, and
symlinked config files. It executes Python directly with an isolated environment;
AI and scheduler flags remain off.

On macOS, do not launch scripts or read configuration from Documents/Desktop/Downloads.
Background LaunchAgents can be denied access by TCC even when the same command
works in a terminal. The observed symptom is Python exiting 2 with “Operation not
permitted” opening the script. Use a private runtime directory under
`~/Library/Application Support/TylerOS/MobileCompanion` (mode 700), with this layout:

```text
MobileCompanion/
  tyleros_worker.py
  scripts/tyleros_launchd.py
  worker.json
```

Copy only the worker, helper, and private configuration there with file mode 600;
keep `scripts/` mode 700. No model SDKs, repository `.env`, or personal snapshots
are needed. When updating code, stop this service after active work completes,
copy the verified worker/helper, then restart it. Record the installed worker
hash with verification evidence so the runtime copy can be compared to source.
Use a Python executable outside Documents as well; the Xcode Python executable
was verified on this Mac.

Run the **copied** helper to prepare a plist in that same private directory:

```sh
python3 "$HOME/Library/Application Support/TylerOS/MobileCompanion/scripts/tyleros_launchd.py" prepare "$HOME/Library/Application Support/TylerOS/MobileCompanion/worker.json" --output /absolute/private/com.tyleros.mobile-companion.plist
plutil -lint /absolute/private/com.tyleros.mobile-companion.plist
```

Preparation does not install or start a service. Existing plist files are never
overwritten. Keep the private runtime copy, its Python executable, and the config in place:
the generated plist contains their absolute paths but no secrets. Both log streams
go to `/dev/null`; inspect authenticated fleet health and job results for health,
and use `launchctl print` for process state.

When ready to install the concrete reviewed configuration:

```sh
install -m 600 /absolute/private/com.tyleros.mobile-companion.plist "$HOME/Library/LaunchAgents/com.tyleros.mobile-companion.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.tyleros.mobile-companion.plist"
launchctl print "gui/$(id -u)/com.tyleros.mobile-companion"
```

Stop and remove only this service with:

```sh
launchctl bootout "gui/$(id -u)/com.tyleros.mobile-companion"
rm "$HOME/Library/LaunchAgents/com.tyleros.mobile-companion.plist"
```

Do not use `kickstart -k` while a run is executing. After configuration changes,
wait for current work to finish before restarting; verify the hosted fleet
identity and a real request → run → approval afterward.

## Protected Vercel previews

A protected preview challenges the worker before TylerOS sees its runtime token.
Vercel's [automation bypass](https://vercel.com/docs/deployment-protection/methods-to-bypass-deployment-protection/protection-bypass-automation)
is available on all plans. The optional worker setting sends it only in
`x-vercel-protection-bypass`, only to the configured HTTPS runtime origin. Runtime
Bearer authentication remains required. Redirects are refused so credential
headers cannot follow a redirect to another host. Upstream error bodies are not
printed because they can reflect secrets.

This bypass grants project-wide deployment access and belongs only in protected
server/worker or verification configuration. Never embed it in the iPhone app,
URLs, screenshots, or source. Do not disable deployment protection to avoid
configuring it. For one-off verification the authenticated Vercel CLI's `curl`
command can handle its own automation access; that does not automatically give
the independently running worker an equivalent credential.

Tests: `python3 -m unittest tests.test_tyleros_launchd tests.test_tyleros_worker tests.test_tyleros_worker_transport`.
