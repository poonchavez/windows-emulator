# Host-provisioned Windows VM images

Place a **preinstalled, licensed, shut down** QEMU qcow2 disk at one path per version:

```text
vm-images/
  xp/disk.qcow2
  windows-vista/disk.qcow2
  windows-7/disk.qcow2
  windows-8/disk.qcow2
  windows-8-1/disk.qcow2
```

The browser user does not upload an ISO. The server makes a temporary copy-on-write qcow2 overlay for each session, so the host's base image is not modified and user changes disappear when the session stops. Keep licensed OS images out of source control; this repository intentionally contains none.

Use a BIOS-installed disk for Windows XP. Vista through Windows 8.1 profiles start with OVMF UEFI and therefore need disks installed in 64-bit UEFI mode. The server searches common OVMF paths; set `OVMF_CODE` and `OVMF_VARS` to a matching firmware pair if they are installed elsewhere. If `swtpm` is installed on the host, sessions also receive an ephemeral TPM 2.0 device.

Images should be `qcow2` and match the QEMU machine configuration. Make a clean shutdown before copying an installed disk. Do not copy a running VM's disk.
