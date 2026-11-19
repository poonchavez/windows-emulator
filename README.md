# Windows Emulator

This repository has three experiences:

- `index.html` is a browser-based visual simulation of Windows XP through Windows 8.1.
- `hosted.html` is a browser console for real QEMU desktops running on a host server.
- `EmulatorLauncher.java` starts real Windows installations in QEMU. This is the option to use when you want the original Windows interface and apps.

## Run hosted desktops in a browser

The browser user does not upload installation media. The **host operator** must provision a licensed, preinstalled qcow2 disk for each Windows version, as described in [`vm-images/README.md`](vm-images/README.md). The host streams the guest display to the browser. Each session gets a temporary copy-on-write overlay, which is deleted when the user stops the session.

Install QEMU and OVMF/EDK II firmware on the host; install `swtpm` if you want to expose TPM 2.0 to the guest. Start the built-in Python server (Python 3, no Python packages required):

```sh
python3 hosted_vm_server.py
```

Open <http://127.0.0.1:8090/hosted.html>. The noVNC client is loaded from jsDelivr, so the browser needs internet access for that script. QEMU VNC stays bound to loopback and the host serves its WebSocket through a random session token.

This is a local development prototype. It binds only to `127.0.0.1` by default and is not safe to expose as a public multi-user service. Public deployment requires HTTPS/WSS, authentication, per-user quotas and isolation, abuse protection, monitoring, and Windows image licensing.

## Run actual Windows

Install Java 11 or newer, QEMU, OVMF/EDK II UEFI firmware, and `swtpm` for your host operating system. Make sure `qemu-system-x86_64`, `qemu-img`, and `swtpm` are available on `PATH`. For example, on macOS with Homebrew:

```sh
brew install qemu swtpm
```

On Debian or Ubuntu, install `qemu-system-x86`, `ovmf`, and `swtpm`. Then compile and start the launcher:

```sh
javac EmulatorLauncher.java
java EmulatorLauncher
```

Choose a version, select a Windows ISO you are licensed to use, and select **Create VM and install**. QEMU stores each virtual disk, UEFI variable store, and persistent TPM state under `~/Windows-Emulator/uefi-tpm2/`. After Windows setup finishes, choose **Start installed VM** to boot that disk without the ISO. Virtual disks are sparse files and grow as Windows uses space. Existing disks from the earlier BIOS-only launcher are preserved in the old folder; install a fresh VM to use this UEFI configuration.

The launcher uses the host accelerator (HVF on macOS, KVM on Linux, WHPX on Windows), so the corresponding host virtualization support must be available. It connects QEMU to `swtpm` for a persistent TPM 2.0 device on each VM. Vista through Windows 8.1 use OVMF/EDK II UEFI firmware and require 64-bit installation media. Windows XP uses legacy BIOS for compatibility and cannot use TPM 2.0 as a native Windows security device. XP-era Windows may also lack current web browser and network security support even when the VM itself runs correctly.

The launcher does not include Microsoft Windows installation files or product keys. Microsoft Windows is proprietary software; obtain installation media and licenses through Microsoft or another authorized source. The emulator uses QEMU, OVMF, and swtpm, not a custom recreation of Windows. QEMU's TPM integration uses the swtpm emulator and QEMU's documented TPM device interface ([QEMU TPM documentation](https://www.qemu.org/docs/master/specs/tpm.html)); UEFI uses per-VM writable firmware variables based on the installed firmware template ([QEMU UEFI variable documentation](https://www.qemu.org/docs/master/devel/uefi-vars.html)).
