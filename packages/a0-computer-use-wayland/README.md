# a0-computer-use-wayland

Wayland portal backend wheel for `a0`.

The helper runs with `/usr/bin/python3`, independently of the CLI's Python
environment. Its native libraries must be installed for that system interpreter;
installing them with pip inside the CLI environment does not satisfy this requirement.

On Arch Linux and CachyOS:

```bash
sudo pacman -S python-dbus python-gobject python-pillow gst-python gtk3 at-spi2-core gst-plugin-pipewire xdg-desktop-portal
```

On Ubuntu and Debian:

```bash
sudo apt install python3-dbus python3-gi python3-pil python3-gst-1.0 gir1.2-gtk-3.0 gir1.2-atspi-2.0 gstreamer1.0-pipewire xdg-desktop-portal
```

Install the portal provider matching your desktop too, such as
`xdg-desktop-portal-kde` for KDE or `xdg-desktop-portal-gnome` for GNOME. Restart
`a0` after installing or repairing system packages. Computer Use still requires
the desktop portal's permission to capture or control the screen.

Backend detection checks real DBus, GI, GTK, GStreamer, Pillow, and AT-SPI imports
in isolated system Python before reporting support. This checks library loading;
portal availability and user approval are verified when starting a session.

System helpers ignore Python environment overrides and search system libraries
before CLI packages, so different Python minor versions can coexist. Startup
errors include the selected helper path and the last 4 KiB of stderr. For full
launch diagnostics:

```bash
A0_COMPUTER_USE_DEBUG=1 A0_COMPUTER_USE_DEBUG_LOG=/tmp/a0-computer-use.log a0
```

The packaged helper is `a0_computer_use_wayland/computer_use_helper.py`.
The compatibility helper in `agent_zero_cli/` is not the packaged backend's
launch target; use the path reported by the error or debug log when diagnosing it.
