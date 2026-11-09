# Motor HMI Installation Guide

Nov 9, 2026 · Meena Salika

Install the packaged release on operator laptops, install from source on developer laptops, and on Windows install from source until the Windows installer has been built once on a Windows PC. Each install is independent: it creates its own admin account, keys and audit log on first start.

| Path | Use it for | The new laptop needs | Status |
| --- | --- | --- | --- |
| A: Linux packaged release | Operators who run motors | 64-bit Ubuntu/Debian, sudo rights. No Python. | Built and run on the development PC |
| B: Linux from source | Developers, tuning, debugging | Python 3.12, internet access for pip | Tested on the development PC |
| C: Windows 10/11 | Windows operators and developers | Python 3.12 (source) or Inno Setup 6 (installer) | Scripts written, not yet run on Windows |

The development PC is the laptop where the HMI was built, project folder `~/moteus_hmi`.

## 1. Before you start

Any 64-bit laptop from the last ten years is enough. What matters is the operating system version, admin rights for the one-time install, and a free USB port for each CAN adapter.

| Item | Linux | Windows |
| --- | --- | --- |
| Operating system | Ubuntu 24.04 LTS or newer for the packaged release; any 64-bit distro with Python 3.12 for a source install | Windows 10 or 11, 64-bit |
| Disk space | 320 MB packaged, about 1 GB from source | 320 MB installed, about 1 GB from source |
| Screen | 1500 × 940 or larger (the window's opening size) | Same |
| Rights | `sudo` once, for the install | Administrator once, for the installer |
| USB | One port per adapter: fdcanusb, PCAN-USB FD or the Nucleo's ST-LINK | Same |

### What to copy from the development PC

| File | Where it is on the development PC | Needed for |
| --- | --- | --- |
| `MotorHMI-1.0.0-linux-x86_64.tar.gz` and its `.sha256` | `~/moteus_hmi/dist/` after a release build (Option A, step 1) | Option A |
| `motor-hmi-1.0.0-src.tar.gz` | `~/moteus_hmi/dist/`, made with the command below | Options B and C |

Make the source archive on the development PC. It leaves out the virtual environments and build output, which hold paths that only work on this PC:

```bash
cd ~ && tar czf ~/moteus_hmi/dist/motor-hmi-1.0.0-src.tar.gz \
  --exclude=.venv --exclude=.build-venv --exclude=dist --exclude=.pytest_cache \
  --exclude=packaging/build --exclude=__pycache__ moteus_hmi
```

Copy the files with a USB stick, `scp` or a network share. **Do not copy the HMI's data folder** (`~/.local/share/moteus-hmi` on Linux, `%LOCALAPPDATA%\MotorHMI` on Windows). It holds the user accounts, the install key and the audit log, and every laptop must create its own (section 6).

## 2. Option A: Linux packaged release

The packaged release is a self-contained 121 MB archive: the new laptop needs no Python, and the app is installed read-only so operators cannot change it. Build it once on the development PC, then install it on as many laptops as you need.

**Which laptops it runs on.** The build needs glibc 2.38 or newer: Ubuntu 24.04 LTS or newer, Debian 13 or newer, Fedora 39 or newer. Ubuntu 22.04 and Debian 12 are too old; use Option B there.

### On the development PC

1. Build the release. The script runs the 80 automated tests first and stops if one fails:

   ```bash
   cd ~/moteus_hmi
   bash packaging/build_linux.sh
   ```

   It needs Python 3.12 with `venv` (`sudo apt install python3.12-venv`) or `uv`. A sourced ROS environment is fine: the script ignores `PYTHONPATH`.
2. Collect the two output files from `~/moteus_hmi/dist/`: `MotorHMI-1.0.0-linux-x86_64.tar.gz` and `MotorHMI-1.0.0-linux-x86_64.tar.gz.sha256`. Copy both to the new laptop, for example into `~/Downloads`.

### On the new laptop

1. Check the archive arrived intact, then unpack it and run the installer:

   ```bash
   cd ~/Downloads
   sha256sum -c MotorHMI-1.0.0-linux-x86_64.tar.gz.sha256    # must print: OK
   tar xzf MotorHMI-1.0.0-linux-x86_64.tar.gz
   cd MotorHMI-1.0.0-linux-x86_64
   sudo ./install.sh
   ```
2. Log out and back in once, so the new `dialout` group membership applies.
3. Start the HMI with `motor-hmi` in a terminal or **Motor HMI** in the applications menu. Start the Motor Simulator with `motor-hmi --simulator`, or with **▶ Launch Motor Simulator** inside the HMI.

**What `install.sh` does**

- Checks every file against `SHA256SUMS` and aborts on any mismatch.
- Installs `libxcb-cursor0` (apt) or `xcb-util-cursor` (dnf) if missing; Qt needs it to open windows.
- Copies the app to `/opt/motor-hmi`, owned by root and read-only for users.
- Adds the `motor-hmi` command (`/usr/local/bin`) and a menu entry.
- Installs the udev rule for fdcanusb and ST-LINK adapters and adds you to the `dialout` group.

Tested on 2026-11-09: release built (80 tests passed), archive and all 551 file checksums verified, `install.sh` run in an isolated test root, and the HMI and the simulator started from the release. The release has not yet been installed on a second physical laptop.

## 3. Option B: Linux from source

A source install runs the same code from a Python virtual environment. Use it on developer laptops and on Linux versions too old for the packaged release. Tested on 2026-11-09 by unpacking the 393 KB source archive into a clean folder: all 80 tests passed, with the system Python 3.12.3 and with a Python 3.12.13 downloaded by `uv`.

1. Install the system packages. On Ubuntu 24.04 or Debian 13:

   ```bash
   sudo apt install python3.12-venv libxcb-cursor0
   ```

   On Ubuntu 22.04, Debian 12 or any distro without Python 3.12, install `libxcb-cursor0` (Fedora: `xcb-util-cursor`) and `uv` instead. `uv` downloads Python 3.12 in step 3:

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
2. Unpack the source into your home folder:

   ```bash
   tar xzf ~/Downloads/motor-hmi-1.0.0-src.tar.gz -C ~
   cd ~/moteus_hmi
   ```
3. Create the virtual environment and install the pinned packages (about 900 MB on disk):

   ```bash
   python3.12 -m venv .venv
   . .venv/bin/activate
   pip install -r requirements.txt pytest
   ```

   With `uv`, the same step is:

   ```bash
   uv venv --python 3.12 .venv
   . .venv/bin/activate
   uv pip install -r requirements.txt pytest
   ```
4. Give your user access to the USB adapters, then log out and back in once:

   ```bash
   sudo cp packaging/linux/99-mjbots-fdcanusb.rules /etc/udev/rules.d/
   sudo udevadm control --reload-rules && sudo udevadm trigger
   sudo usermod -aG dialout $USER
   ```
5. Run the tests. The last line must read `80 passed`:

   ```bash
   PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 QT_QPA_PLATFORM=offscreen python -m pytest -q tests
   ```
6. Start the HMI, and the Motor Simulator in a second terminal:

   ```bash
   cd ~/moteus_hmi && . .venv/bin/activate
   python -m moteus_hmi          # the HMI
   python -m moteus_hmi.sim      # the Motor Simulator
   ```

**ROS laptops.** If ROS is sourced in the terminal, start with `env -u PYTHONPATH python -m moteus_hmi`; otherwise ROS's Python packages can load in place of the HMI's.

## 4. Option C: Windows 10/11

On Windows, install from source first; it needs only Python. Build the installer once the source install has passed its tests on that Windows PC. Neither path has been run on Windows yet, so run the tests in step 4 before connecting a motor.

### From source

1. Install Python 3.12 (64-bit) from python.org, or run `winget install Python.Python.3.12` in a terminal. In the python.org installer, tick **Add python.exe to PATH** and keep the **py launcher** option.
2. Copy `motor-hmi-1.0.0-src.tar.gz` to `Downloads` and unpack it in **PowerShell** (Windows 10 and 11 include `tar`):

   ```powershell
   tar xzf $HOME\Downloads\motor-hmi-1.0.0-src.tar.gz -C $HOME
   cd $HOME\moteus_hmi
   ```
3. Create the virtual environment and install the pinned packages. The requirements add `pywin32` on Windows automatically:

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt pytest
   ```

   If PowerShell refuses to run `Activate.ps1`, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, then activate again.
4. Run the tests. The last line must read `80 passed`:

   ```powershell
   $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"; $env:QT_QPA_PLATFORM = "offscreen"
   python -m pytest -q tests
   Remove-Item Env:QT_QPA_PLATFORM
   ```
5. Start the HMI, and the Motor Simulator in a second PowerShell window (activate `.venv` there too):

   ```powershell
   python -m moteus_hmi
   python -m moteus_hmi.sim
   ```

   When Windows Defender Firewall asks about Python, allow it on **Private networks**. The simulator bus is local UDP multicast and needs it.

### Building the Windows installer

1. On a Windows PC with Python 3.12, optionally install [Inno Setup 6](https://jrsoftware.org/isinfo.php) to also get a `setup.exe`.
2. In PowerShell, in the unpacked source folder:

   ```powershell
   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
   ```

   The script runs the tests first and stops if one fails.
3. It writes to `dist\`: `MotorHMI-1.0.0-win64.zip` and its `.sha256`, plus `MotorHMI-1.0.0-setup.exe` when Inno Setup is installed.
4. On each target laptop, run `MotorHMI-1.0.0-setup.exe` as administrator. It installs to `C:\Program Files\Motor HMI` and adds a Start-menu entry. Without the installer, unzip and run `app\MotorHMI.exe`.
5. Start the simulator with **▶ Launch Motor Simulator** in the HMI, or with `"C:\Program Files\Motor HMI\MotorHMI.exe" --simulator`.

An unsigned `MotorHMI.exe` makes SmartScreen show **Windows protected your PC**; choose **More info → Run anyway**. For production releases, sign the exe and the installer with your company's code-signing certificate (`signtool`), as the build script prints at the end.

## 5. Hardware setup on the new laptop

Only the adapter you will use needs setting up. moteus controllers need a CAN-FD adapter (1 Mbit/s nominal, 5 Mbit/s data). Classic CAN and slcan adapters do not work. The ST board talks over the Nucleo's USB serial port instead.

| Adapter | Linux | Windows | In the HMI (Controller / Interface) |
| --- | --- | --- | --- |
| mjbots fdcanusb / mjcanfd-usb | udev rule (installed in Option A, or Option B step 4); appears as `/dev/fdcanusb` | Plug in; appears as a COM port, driver built in | moteus r4.11 / *fdcanusb* or *Auto* |
| PEAK PCAN-USB FD | Built-in `peak_usb` driver; bring `can0` up with the command below | Install PEAK's driver package, which includes the PCAN-Basic library python-can uses | moteus r4.11 / *SocketCAN* (Linux) or *python-can* with `pcan` (Windows) |
| Nucleo + X-NUCLEO-IHM08M1 (ST-LINK) | udev rule; appears as `/dev/ttyACM0` | Usually appears as *STLink Virtual COM Port*; if not, install ST's ST-LINK driver (STSW-LINK009) | ST X-NUCLEO-IHM08M1 / *USB serial / ST-LINK*, 115200 baud |

Bring up a SocketCAN adapter on Linux (PCAN, or an fdcanusb running gs\_usb firmware). Repeat after every replug or reboot:

```bash
sudo ip link set can0 up type can bitrate 1000000 dbitrate 5000000 \
     sjw 10 dsjw 5 sample-point 0.666 dsample-point 0.666 restart-ms 1000 fd on
```

**Check the laptop sees the adapter**

- Linux: `ls -l /dev/fdcanusb /dev/ttyACM*` lists the serial devices; `ip -details link show can0` shows `can <FD>` and `state ERROR-ACTIVE` once the bus is up; `groups` must include `dialout`.
- Windows: Device Manager → *Ports (COM & LPT)* lists the fdcanusb or ST-LINK COM port; PCAN appears under *CAN-Hardware*.

Wiring, termination, unique controller IDs and the first power-up checklist are in `docs/HARDWARE.md` in the project folder and in the User Manual, section 5.

## 6. First start on the new laptop

The first start creates a new, empty security setup on that laptop. There are no default accounts: the person who installs it becomes the administrator.

1. Start the HMI. **First-time setup** opens. Enter an admin user name (`admin` is pre-filled) and a password of at least 10 characters, with an upper-case letter, a lower-case letter and a digit, not containing the user name.
2. A one-time **recovery code** appears. Write it down and store it offline. It is shown only once, and it is the only way back in if the user database is ever lost or changed outside the HMI.
3. Sign in as the administrator. In **Users & Audit → Add…**, create one account per person with the role Viewer, Operator, Engineer or Admin. New users choose their own password at first sign-in.
4. In **Safety & Alarms**, set the limits for each motor. They start at conservative defaults on every new laptop.
5. Choose the controller, interface and port in the connection panel, then **Connect**. Verify with the Motor Simulator first (section 7).

### Moving things from the old laptop

| What | How it moves |
| --- | --- |
| Controller parameters (gains, limits, calibration) | Nothing to move: after **Save to flash** they are stored in the controller. Optionally use **Parameters → Export** on the old laptop and **Import** on the new one. |
| HMI safety limits | Re-enter them in **Safety & Alarms**. Limits written with **Write soft limits + watchdog to controller** are already in the controller. |
| Users and passwords | Re-create them. Password hashes and the install key are not transferable by design. |
| Recordings and the audit log | For archiving, copy `recordings/`, `logs/` and `security/audit.jsonl` out of the old data folder. The new laptop starts its own audit chain. |

**Importing a backup from another laptop.** Each laptop signs backups with its own key, so the new laptop reports *Signature does not match* and asks you to type `IMPORT ANYWAY`. Do so only for a file you exported yourself, and check every value in the comparison before you write it to the controller.

## 7. Verify the installation

An installation is good when the HMI drives the Motor Simulator on that laptop through the full loop. That takes about five minutes and needs no hardware. Tick each item on the new laptop.

- [ ] Option B or C only: the test run ended with `80 passed`.
- [ ] Start the Motor Simulator: `motor-hmi --simulator` (packaged) or `python -m moteus_hmi.sim` (source). Two motor cards, CAN ID 1 and 2, and an amber *Waiting for the HMI* bar appear.
- [ ] Start the HMI and sign in. Set **Controller** *mjbots moteus r4.11*, **Interface** *Motor Simulator app (virtual CAN-FD bus)*, **Bus group** `239.74.163.2`, **Bus port** `43113`, **CAN prefix** `0`, **Node IDs** blank. Click **Connect**.
- [ ] The header shows **VIRTUAL CAN-FD · MOTOR SIMULATOR APP** and *Motors* lists ID 1 and ID 2. In the simulator the amber bar is gone and RX/TX count up.
- [ ] *Test & Control* → ID 1 → **Enable** → *Position* → `1.0` → **Send**. The simulator's *From HMI* column shows `+1.0000 rev` and the HMI's position plot moves to 1.0.
- [ ] In the simulator, set **Supply voltage** to `9 V`. The HMI shows **TRIPPED - Bus voltage … < 10.00 V**. Set `24 V` again, then **Reset fault**.
- [ ] Press **E-STOP** (or F12). The simulator shows mode **Stopped**. Reset the E-stop.
- [ ] *Firmware* → **Read version** shows firmware ABI 1.1.0 (0x010100).
- [ ] With hardware: connect the adapter (section 5), choose its interface, and **Connect**. Each motor appears in *Bus & Devices* with *Comm OK*. Then follow the first power-up checklist in `docs/HARDWARE.md`.

The full set of simulator experiments (load, noise, faults, cable cut) is in `docs/SIMULATOR.md` and the User Manual, section 6.

## 8. Troubleshooting, updating and uninstalling

Most installation problems are a missing system library, an old Linux version, or a USB permission that needs one log-out.

| Message or symptom | Cause | Fix |
| --- | --- | --- |
| `Could not load the Qt platform plugin "xcb"` | `libxcb-cursor0` missing | `sudo apt install libxcb-cursor0` (Fedora: `xcb-util-cursor`) |
| `version 'GLIBC_2.38' not found` when starting the packaged app | Linux older than Ubuntu 24.04 / Debian 13 | Use Option B on that laptop |
| `ensurepip is not available` | `python3-venv` not installed | `sudo apt install python3.12-venv`, or use `uv` |
| `sha256sum: … FAILED` or `checksum mismatch - aborting` | Archive damaged while copying | Copy the `.tar.gz` again and recheck |
| `Permission denied` on `/dev/fdcanusb` or `/dev/ttyACM0` | `dialout` group not active yet | Log out and back in; `groups` must list `dialout` |
| *No CAN-FD adapter found* | Adapter unplugged, driver missing, or wrong interface | Check section 5, or click **Use Motor Simulator** in that dialog |
| HMI connects but finds no motors on the simulator | Bus group, bus port or CAN prefix differ | Use the values in the simulator's top bar; CAN prefix `0` |
| *Motor HMI is already running for this user / data folder* | A second copy was started | Use the open window, or close it first |
| *The settings file was modified outside the HMI* | Settings copied from another laptop or edited by hand | Safe defaults were loaded: re-enter the settings and limits |
| *User database* error, then the Recovery dialog | User file copied from another laptop or changed | Enter the recovery code from section 6, step 2 |
| Import errors or wrong library versions, ROS laptop | ROS `PYTHONPATH` loads its packages first | Start with `env -u PYTHONPATH …` |
| Windows: `Activate.ps1 cannot be loaded` | PowerShell execution policy | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| Windows: HMI and simulator do not see each other | Firewall blocked Python's UDP traffic | Windows Security → Firewall → *Allow an app*: allow Python (or MotorHMI) on Private networks |
| Phone cannot open the mobile view link | Phone on another or guest Wi-Fi, or the laptop firewall blocks the port | Same Wi-Fi; Windows: allow MotorHMI or Python on Private networks; Linux: `sudo ufw allow 8443/tcp` |
| Phone cannot reach an HMI running in WSL2 | WSL2 has its own network address behind Windows | In `%UserProfile%\.wslconfig` set `networkingMode=mirrored` under `[wsl2]` (Windows 11), run `wsl --shutdown`, then in an administrator PowerShell: `New-NetFirewallHyperVRule -Name MotorHMI -DisplayName "Motor HMI mobile" -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -Protocol TCP -LocalPorts 8443` |

The HMI's own log is `logs/hmi.log` in the data folder.

### Updating to a new version

The data folder (users, settings, audit log, recordings) lives outside the program folder, so updates keep it.

- **Linux packaged:** build the new release, copy it, and run `sudo ./install.sh` from the new folder. It replaces `/opt/motor-hmi`.
- **Source:** rename the old `~/moteus_hmi`, unpack the new source archive, and repeat Option B steps 3 and 5.
- **Windows installer:** run the new `setup.exe`; it upgrades the existing installation in place.

### Uninstalling

Linux packaged release:

```bash
sudo rm -rf /opt/motor-hmi /usr/local/bin/motor-hmi \
  /usr/share/applications/motor-hmi.desktop /etc/udev/rules.d/99-mjbots-fdcanusb.rules
sudo udevadm control --reload-rules
```

Source install: delete `~/moteus_hmi`. Windows: *Settings → Apps → Motor HMI → Uninstall*, or delete the source folder.

Delete the data folder (`~/.local/share/moteus-hmi` or `%LOCALAPPDATA%\MotorHMI`) only when you no longer need its audit trail and recordings.
