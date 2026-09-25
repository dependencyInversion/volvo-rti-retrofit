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
     The hudiy role also puts a "Hudiy" shortcut on the desktop to start it
     again after quitting it (no-op while it's already running).
   - `install_uxplay` (hudiy builds only): installs
     [uxplay](https://github.com/antimof/UxPlay) (AirPlay mirroring) and
     enables it by default. Defaults to `true`; set `false` to skip it. Since
     livi is fully headless, uxplay is never installed there regardless of
     this setting. Adds a desktop shortcut on the hudiy desktop to start/stop
     the service - the icon is colored while uxplay is running and grey while
     stopped. Also integrates it into Hudiy - see
     [AirPlay in the car](#airplay-in-the-car) (`airplay_*` and
     `hudiy_wifi_*` vars).
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

## AirPlay in the car

With `install_uxplay` on (hudiy builds only), AirPlay mirroring runs on top of
Hudiy:

1. Connect the iPhone to Hudiy's Wi-Fi hotspot (`hotspot.ssid` in Hudiy's
   `main_configuration.json`). CarPlay isn't handed over automatically yet, so
   you have to join the hotspot on the phone yourself.
2. Tap **AirPlay** in Hudiy's bottom bar, or find it in the menu under the
   `airplay_menu_category` category. It starts uxplay if it isn't running and
   shows a toast with instructions.
3. On the phone, open Control Center > Screen Mirroring > **Volvo RTI**. The
   mirrored screen appears over Hudiy, a status icon shows up, and Hudiy's own
   media pauses.
4. To stop, end mirroring on the phone or tap **AirPlay** again. Hudiy's media
   is released again.

For a full-width picture on the wide RTI screen, mirror in landscape: turn the
iPhone's rotation lock off and use a landscape mount.

How it fits together:
- `airplay-bridge` (from `Raspberry Pi/hudiy-client`, systemd user unit)
  registers the `airplay_toggle` Hudiy action and watches uxplay's mirroring
  port to show the status icon and take Hudiy's audio focus.
- A labwc window rule in `~/.config/labwc/rc.xml` keeps Hudiy's window on
  labwc's always-on-bottom layer, so uxplay's fullscreen window stacks above it.
- **Wi-Fi when Hudiy isn't running** (`hudiy-wifi-fallback`, systemd user
  unit): Hudiy's own Wi-Fi settings stay untouched. Once Hudiy has been quit,
  the Pi joins known networks (e.g. home Wi-Fi) as usual. If none connects
  within `hudiy_wifi_fallback_timeout` seconds, it starts an access point with
  exactly Hudiy's hotspot settings, so the phone sees the same network either
  way. It is taken down again as soon as Hudiy starts.

Logs: `journalctl --user -u airplay-bridge -u hudiy-wifi-fallback -u uxplay`.
