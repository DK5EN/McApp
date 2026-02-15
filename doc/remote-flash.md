# Remote SD Card Flashing

Write a fresh SD card image to a Raspberry Pi over the network — without physically removing the card.

## Overview

Normally, re-imaging a Pi means: pull the SD card, insert into Mac, flash with Imager, re-insert. This document describes two methods to flash remotely while the Pi is running.

| Method | Risk | Extra Hardware | RAM Needed |
|--------|------|----------------|------------|
| Pivot-to-RAM | High | None | ~200 MB |
| USB Boot | Low | USB stick | Minimal |

---

## Method 1: Pivot to RAM

Move the running root filesystem into RAM, unmount the SD card, then `dd` a new image onto it.

### Prerequisites

- Pi accessible via SSH
- Stable network connection (Ethernet preferred — WiFi driver may not survive pivot)
- New image served over HTTP from your Mac (or any reachable host)

### Step 1: Serve the Image from Your Mac

```bash
# Download the image (or use an existing one)
# Example: Raspberry Pi OS Lite (64-bit)
cd ~/Downloads
python3 -m http.server 8000
```

Verify the image is reachable:

```bash
curl -sI http://your-mac.local:8000/raspios.img | head -5
```

### Step 2: Pivot Root to tmpfs

SSH into the Pi, then run each block in order:

```bash
ssh mcapp.local
```

Create and populate a tmpfs root:

```bash
sudo mkdir /tmp/tmproot
sudo mount -t tmpfs -o size=200M tmpfs /tmp/tmproot

# Copy essential directories
for d in bin sbin lib usr etc; do
  sudo cp -a /$d /tmp/tmproot/
done

# Create mount points
sudo mkdir -p /tmp/tmproot/{dev,proc,sys,run,tmp,mnt}

# Bind-mount virtual filesystems
sudo mount --bind /dev  /tmp/tmproot/dev
sudo mount --bind /proc /tmp/tmproot/proc
sudo mount --bind /sys  /tmp/tmproot/sys
```

Pivot root and unmount the SD card:

```bash
sudo pivot_root /tmp/tmproot /tmp/tmproot/mnt

# Unmount old root (the SD card)
sudo umount -l /mnt/boot/firmware 2>/dev/null
sudo umount -l /mnt
```

### Step 3: Write the New Image

```bash
curl -fsSL http://your-mac.local:8000/raspios.img \
  | sudo dd of=/dev/mmcblk0 bs=4M status=progress
```

### Step 4: Reboot

```bash
sudo reboot
```

### Risks and Limitations

- **No recovery if it fails.** If SSH drops or the write is interrupted, the SD card is corrupt and requires physical access.
- **RAM is limited.** Pi Zero 2W has 512 MB total. The 200 MB tmpfs plus running processes must fit.
- **Network must stay up.** If using WiFi, the driver may not work after pivot since kernel modules could be on the now-unmounted SD card. Use Ethernet if possible.
- **Post-flash config.** The new image won't have your SSH keys, WiFi config, or hostname. Pre-configure the image before serving, or use the bootstrap script after first boot.

---

## Method 2: USB Boot (Recommended)

Boot the Pi from a USB stick, then reimage the SD card as an unmounted block device. Much safer.

### One-Time Setup: Enable USB Boot

```bash
ssh mcapp.local
sudo raspi-config
# Advanced Options → Boot Order → USB Boot
sudo reboot
```

### Flash Workflow

1. Flash a minimal OS onto a USB stick (from your Mac, using Raspberry Pi Imager).
2. Insert the USB stick into the Pi and boot from it.
3. SSH in and write the new image to the SD card:

```bash
# SD card is /dev/mmcblk0, not mounted
curl -fsSL http://your-mac.local:8000/raspios.img \
  | sudo dd of=/dev/mmcblk0 bs=4M status=progress
```

4. Remove the USB stick and reboot into the fresh SD card.

### Why This Is Safer

- The SD card is never mounted during the write — no risk of corruption.
- If the write fails, you still have a working system on USB.
- No RAM constraints.

---

## Pre-Configuring the Image

A freshly flashed image won't have SSH enabled or WiFi configured. To avoid needing a monitor/keyboard for first boot, mount the image before serving and add the necessary files.

On your Mac:

```bash
# Mount the boot partition of the image (first partition)
hdiutil attach -nomount raspios.img
# Find the disk (e.g., /dev/disk4s1)
diskutil list | grep disk4
sudo mkdir /Volumes/bootfs
sudo mount -t msdos /dev/disk4s1 /Volumes/bootfs

# Enable SSH
touch /Volumes/bootfs/ssh

# Add WiFi config (for Pi OS Bookworm+)
cat > /Volumes/bootfs/custom.toml << 'EOF'
[system]
hostname = "mcapp"

[wifi]
ssid = "YourNetwork"
password = "YourPassword"
country = "DE"

[user]
name = "pi"
password_encrypted = ""  # Set via: echo 'yourpassword' | openssl passwd -6 -stdin
EOF

sudo umount /Volumes/bootfs
hdiutil detach /dev/disk4
```

After the Pi boots with the fresh image, run the McApp bootstrap to restore everything:

```bash
curl -fsSL https://raw.githubusercontent.com/DK5EN/McApp/main/bootstrap/mcapp.sh | sudo bash
```

---

## Quick Reference

```bash
# Mac: serve image
cd ~/Downloads && python3 -m http.server 8000

# Pi: pivot-to-RAM one-liner (copy-paste friendly)
sudo mkdir /tmp/tmproot && \
sudo mount -t tmpfs -o size=200M tmpfs /tmp/tmproot && \
for d in bin sbin lib usr etc; do sudo cp -a /$d /tmp/tmproot/; done && \
sudo mkdir -p /tmp/tmproot/{dev,proc,sys,run,tmp,mnt} && \
sudo mount --bind /dev /tmp/tmproot/dev && \
sudo mount --bind /proc /tmp/tmproot/proc && \
sudo mount --bind /sys /tmp/tmproot/sys && \
sudo pivot_root /tmp/tmproot /tmp/tmproot/mnt && \
sudo umount -l /mnt/boot/firmware 2>/dev/null; \
sudo umount -l /mnt && \
curl -fsSL http://your-mac.local:8000/raspios.img \
  | sudo dd of=/dev/mmcblk0 bs=4M status=progress && \
sudo reboot
```
