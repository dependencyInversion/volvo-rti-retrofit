# Ansible: provisioning the retrofit Raspberry Pi

Takes a fresh **Raspberry Pi OS Lite** install on a Pi 4/5 to a working state:
apt update+upgrade, `fish` as the default shell, and either **LIVI** or
**Hudiy** installed and configured.

## Prerequisites

- Control machine: `ansible` (>= 2.15).
- Target Pi: SSH access enabled, reachable over the network. (The `hudiy`
  role installs `python3-pexpect` on the Pi itself - required by the
  `ansible.builtin.expect` module used to answer the order number prompt.)

## Setup

1. Edit `inventory/hosts.ini` with your Pi's address and SSH user.
2. Edit `group_vars/raspberrypi/vars.yml`:
   - `target_app`: `livi` or `hudiy`.
   - `reboot_after_install`: reboots the Pi once install finishes, regardless
     of `target_app`. Defaults to `true`; set `false` to skip it.
   - LIVI users: adjust the `livi_*` headless-config vars as needed.
   - Hudiy users: set `hudiy_installer_local_path` if your
     `hudiy_installer.tar.gz` isn't at `~/Hudiy/hudiy_installer.tar.gz` on this
     machine.
3. Hudiy only - set up the secret order number via Ansible Vault:
   ```
   cp group_vars/raspberrypi/vault.yml.example group_vars/raspberrypi/vault.yml
   ansible-vault encrypt group_vars/raspberrypi/vault.yml
   ```
   Edit it later with `ansible-vault edit group_vars/raspberrypi/vault.yml`.
   `vault.yml` is gitignored as a safety net - it should never be committed
   unencrypted.

   > [!TIP]
   > Hudiy requires Raspberry Pi OS **64-bit, Debian 12 "bookworm", with
   > Desktop**. In Raspberry Pi Imager, pick **Raspberry Pi OS (other) >
   > Raspberry Pi OS (Legacy, 64-bit)** - not Lite. The `hudiy` role checks
   > this and fails with a clear message if the target doesn't match.

   > [!WARNING]
   > `group_vars` are loaded for the whole `raspberrypi` group up front,
   > regardless of `target_app` - so an encrypted `vault.yml` demands a
   > vault secret even on a LIVI-only run.
   >
   > If you're not running Hudiy right now, either decrypt it back, or just
   > rename it out of the way (e.g. a trailing dash: `vault.yml-`) - Ansible
   > only picks up exact `vault.yml`/`vault.yml.*` names, so a renamed file
   > is silently skipped without needing to delete it.

## Run

```
ansible-playbook site.yml --ask-become-pass --ask-vault-pass
```

`--ask-vault-pass` (or `--vault-password-file ...`) is required whenever an
encrypted `vault.yml` is present in `group_vars/raspberrypi/` - which, per the
warning above, is loaded for the whole `raspberrypi` group regardless of
`target_app`. Omitting it fails fast with:
```
[ERROR]: Attempting to decrypt but no vault secrets found.
```
If you're not running Hudiy right now, drop the flag and rename `vault.yml`
out of the way instead (see the warning above).

Dry run first with `--check --diff`.
