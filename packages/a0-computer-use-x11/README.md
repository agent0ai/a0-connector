# a0-computer-use-x11

Legacy Xorg/X11 computer-use backend for `a0`.

The backend uses XTEST through `python-xlib` for mouse and keyboard input, and `mss` for screen capture. It is no longer embedded in the root `a0` remote host connector package; Linux host computer use uses the Wayland portal backend. X11/Xpra automation lives in Agent Zero Core's internal Docker Desktop tooling.
