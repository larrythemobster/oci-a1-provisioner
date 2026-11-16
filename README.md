# oci-a1-provisioner

Keeps asking Oracle Cloud for an Always Free Ampere A1 instance until it gets one.

If you've tried to create a `VM.Standard.A1.Flex` instance in a popular region, you've probably seen `Out of host capacity`. Capacity frees up at random times, so the only reliable approach is to keep retrying. This runs as a systemd service on any Linux box, retries the launch every couple of minutes, survives reboots, and stops once the instance is `RUNNING`. It can ping you on Discord when that happens.

Here's a server that has been at it since late September:

```
2026-11-16 19:17:20 INFO Attempt 42033
2026-11-16 19:17:20 INFO Out of host capacity (InternalError), retrying later.
2026-11-16 19:17:20 INFO Waiting 111.7s before the next attempt.
2026-11-16 19:19:12 INFO Attempt 42034
2026-11-16 19:19:13 INFO Out of host capacity (InternalError), retrying later.
2026-11-16 19:19:13 INFO Waiting 95.2s before the next attempt.
```

That's over 42,000 attempts with no instance yet, which is why the tip below exists.

> [!TIP]
> Upgrading your tenancy to Pay As You Go keeps Always Free resources free and usually gets A1 launches through much faster. If you upgrade, set a budget alert first.

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Setup](#setup)
- [Checking on it](#checking-on-it)
- [Configuration](#configuration)
- [Retry timing](#retry-timing)
- [Troubleshooting](#troubleshooting)
- [Updating and uninstalling](#updating-and-uninstalling)
- [Development](#development)

## How it works

On startup the provisioner:

1. Picks the first availability domain, the first VCN, and the first public subnet in that VCN.
2. Picks the newest Ubuntu 24.04 image for the shape. If there isn't one it tries Oracle Linux 9, then any image that supports the shape.
3. Checks your A1 core and memory quota and exits if the request doesn't fit.
4. If an instance with the same display name is already provisioning or running, it skips the launch loop and waits for that one.

Then it loops. Each attempt calls `LaunchInstance` with a public IPv4 address and your SSH public key. Capacity errors and other 5xx responses are retried. 429s get exponential backoff. Any other 4xx (bad config, missing permissions) stops the service for good, since retrying won't fix it. The attempt count and last error are saved to disk, so restarts pick up where they left off.

It does not rotate between availability domains or regions.

## Requirements

- Linux with systemd 247 or newer (for `LoadCredential`)
- Python 3.12 with `venv` and `pip`
- Root access
- An OCI account with an API signing key
- A VCN with a public subnet in your home region. The default one the console creates with your first instance works fine.

The provisioner doesn't need to run on OCI. A Raspberry Pi or a cheap VPS is enough.

## Setup

### 1. Create an API key

In the OCI console, open your profile (top right), go to **API keys**, and click **Add API key**. Choose **Generate API key pair**, download the private key, and click **Add**. OCI then shows a config snippet. Copy it. You'll need it in the next step.

### 2. Put the credentials in place

Everything lives in `/etc/oci-a1-provisioner`, owned by root, outside the git checkout. Create the directory and each file empty with the right permissions first, then edit it, so the contents never touch your shell history:

```sh
sudo install -d -o root -g root -m 0700 /etc/oci-a1-provisioner

sudo install -o root -g root -m 0600 /dev/null /etc/oci-a1-provisioner/oci-config
sudoedit /etc/oci-a1-provisioner/oci-config
```

Don't run `install ... /dev/null` on a file that already exists. It will empty it.

| File | Contents |
| --- | --- |
| `oci-config` | The snippet from step 1. Use [`examples/oci-config.example`](examples/oci-config.example) as a template and set `key_file=/etc/oci-a1-provisioner/oci-api-key.pem`. If the key is encrypted, put `pass_phrase` here too. |
| `oci-api-key.pem` | The private key you downloaded. |
| `ssh-public-key.pub` | The SSH **public** key to install on the new instance (`ssh-ed25519`, `ssh-rsa`, or `ecdsa-sha2-*`). Never your private key. |
| `discord-webhook` | Optional. A Discord webhook URL on one line. Leave it out or empty to skip notifications. |

### 3. Install

```sh
git clone https://github.com/larrythemobster/oci-a1-provisioner.git
cd oci-a1-provisioner
sudo bash install.sh
```

The installer:

- checks the credential files and fixes their ownership and permissions (it never overwrites them)
- creates an `oci-a1-provisioner` system user
- builds a virtualenv in `/opt/oci-a1-provisioner` with the pinned OCI SDK
- installs the systemd unit and the `oci-a1-status` command, then starts the service

To use a specific Python, pass an absolute path: `sudo PYTHON=/usr/bin/python3.12 bash install.sh`.

The service runs as the unprivileged user with `ProtectSystem=strict` and `ProtectHome=yes`. systemd hands it private copies of the credentials through `LoadCredential`, so the service user can't read `/etc/oci-a1-provisioner` directly.

To test the Discord webhook before the instance lands:

```sh
sudo /opt/oci-a1-provisioner/.venv/bin/python /opt/oci-a1-provisioner/provisioner.py --test-discord
```

## Checking on it

```sh
sudo oci-a1-status                              # attempts, last error, instance details
sudo journalctl -u oci-a1-provisioner.service -f
```

```
oci-a1-provisioner status
------------------------------------------------------------
Service:                 active (running)
Total launch attempts:   42035
Last attempt time:       2026-11-16T19:20:49.648005+00:00
Last status:             Out of host capacity (retrying)
Last OCI response/error: [500] InternalError: Out of host capacity.
------------------------------------------------------------
Instance:                not created yet
```

Once the instance is up, `oci-a1-status` prints its OCID and public IP. Log in as `ubuntu` (or `opc` on Oracle Linux).

| Path | What |
| --- | --- |
| `/var/lib/oci-a1-provisioner/state.json` | Attempt count, last status, instance details |
| `/var/log/oci-a1-provisioner/provisioner.log` | Log file (also in the journal) |
| `/var/backups/oci-a1-provisioner/` | Backups made by each install |

These contain your tenancy and instance OCIDs. Keep them out of git.

## Configuration

The instance request is set by constants at the top of [`provisioner.py`](provisioner.py). Edit them before you install, or edit and re-run `install.sh`.

| Constant | Default |
| --- | --- |
| `DISPLAY_NAME` | `alwaysfree-arm` |
| `SHAPE` | `VM.Standard.A1.Flex` |
| `OCPUS` | `2.0` |
| `MEMORY_IN_GBS` | `12.0` |
| `PREFER_OS` / `PREFER_UBUNTU_VERSION` | `Canonical Ubuntu` / `24.04` |
| `BACKUP_OS` / `BACKUP_OL_VERSION` | `Oracle Linux` / `9` |

Always Free covers up to 4 OCPUs and 24 GB across all your A1 instances. The default of 2 and 12 leaves room for a second one.

Environment variables take file paths, never secret values:

| Variable | Default |
| --- | --- |
| `OCI_CONFIG_FILE` | systemd credential, else `~/.oci/config` |
| `OCI_CONFIG_PROFILE` | `DEFAULT` |
| `OCI_A1_KEY_FILE` | systemd credential, else `key_file` from the config |
| `OCI_A1_SSH_PUBLIC_KEY_FILE` | systemd credential, else `/etc/oci-a1-provisioner/ssh-public-key.pub` |
| `DISCORD_WEBHOOK_FILE` | systemd credential, else `/etc/oci-a1-provisioner/discord-webhook` |
| `OCI_A1_STATE_DIR` | `/var/lib/oci-a1-provisioner` (also read by `oci-a1-status`) |
| `OCI_A1_LOG_FILE` | `/var/log/oci-a1-provisioner/provisioner.log` |

To set one for the service, run `sudo systemctl edit oci-a1-provisioner.service` and add it under `[Service]`:

```ini
[Service]
Environment=OCI_CONFIG_PROFILE=MY_PROFILE
```

The config, API key, and webhook files are rejected if anyone besides root or the running user can read them.

## Retry timing

| After | Wait |
| --- | --- |
| Out of host capacity, or any other 5xx | 90 to 120 s |
| 1st 429 in a row | 130 to 165 s |
| 2nd 429 in a row | 250 to 285 s |
| 3rd 429 in a row | 490 to 525 s |
| 4th and later 429 in a row | 970 to 1005 s |

The wait starts after the request returns. A non-429 response resets the throttle counter. There's no attempt limit. If the process crashes, systemd restarts it after 30 seconds. It stays stopped after exiting with status 3 (insufficient quota or a non-retryable OCI error) and after the instance is running.

## Troubleshooting

**`Insufficient A1 quota` in the status output.** Something else is already using your A1 cores or memory, or your tenancy's limit is lower than the request. Check **Governance > Limits, Quotas and Usage** in the console, or lower `OCPUS` and `MEMORY_IN_GBS`.

**`Permanent error (stopped)` in the status output.** OCI rejected the launch with a 4xx that isn't a 429. `Last OCI response/error` shows the code. Fix the cause, then `sudo systemctl start oci-a1-provisioner.service`.

**`Provisioner failed: No VCN found` or `No public subnet found` in the journal.** Create a VCN with the console's VCN wizard (it makes a public subnet), or create any instance once through the console and delete it.

**`Provisioner failed (ValueError)`, `(PermissionError)`, or `(FileNotFoundError)` in the journal.** A credential file is missing, malformed, or readable by someone other than root and the running user. The log leaves out details on purpose so secrets never end up in it. Re-run `install.sh` to fix ownership and permissions in `/etc/oci-a1-provisioner`. For files passed through environment variables, `chmod 600` them.

**`Provisioner failed (ServiceError)` at startup.** OCI rejected a discovery call. The usual cause is an API key, fingerprint, or user OCID that don't match, or a user without permission to list instances, images, and networks.

**`Another instance of oci-a1-provisioner is already running`.** Only one copy can run at a time. Stop the service before running `provisioner.py` by hand.

## Updating and uninstalling

To update, pull and re-run the installer. It builds the new virtualenv before it stops the old service, backs up the previous files and state to `/var/backups/oci-a1-provisioner/`, and keeps your attempt count. If anything fails after the old service stops, it puts the old files back and restarts it. It does not roll back `state.json`.

After rotating a credential, restart the service so systemd picks up the new copy:

```sh
sudo systemctl restart oci-a1-provisioner.service
```

To remove everything:

```sh
sudo systemctl disable --now oci-a1-provisioner.service
sudo rm /etc/systemd/system/oci-a1-provisioner.service /usr/local/bin/oci-a1-status
sudo systemctl daemon-reload
sudo rm -rf /opt/oci-a1-provisioner /var/lib/oci-a1-provisioner \
  /var/log/oci-a1-provisioner /var/backups/oci-a1-provisioner
sudo userdel oci-a1-provisioner
sudo rm -rf /etc/oci-a1-provisioner   # your credentials
```

## Development

The tests need Linux (they use `fcntl` and POSIX ACLs). They work on temporary files and never call OCI or Discord.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

## License

[MIT](LICENSE)
