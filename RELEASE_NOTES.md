**TENET Hub 1.3.1**: fixes packages wrongly saying they need a newer REVIVER.

Download **TenetHub.exe** below and run it (Windows 10 or 11, no installer). If you have TENET Hub 1.3.0 or newer, click the update banner or **Update and restart** in Settings instead.

## Fixed

- **"Needs REVIVER vN or newer" on a keyboard that has it:** if the keyboard wasn't answering yet when TENET Hub started, it was taken for the official firmware and packages were held back. TENET Hub now waits for the keyboard to answer before deciding which firmware it runs.
- **Packages tab stuck on an old status:** it now updates by itself when the keyboard connects or a package starts or stops.

## Everything from earlier versions

- One-click updates for TENET Hub itself, and stored animations that fail now say why (1.3.0)

- Stored animations with REVIVER v26: animation packages get a screen of their own and play even with the PC off
- Lighting tab: one colour or off, with a brightness slider
- Optional automatic updates for community packages
- Now Playing, community packages from other people's GitHub repos ([how to make your own](https://github.com/yuuusername/tenet-hub/blob/main/docs/community-packages.md))
- Screens list, PC stats with built-in temperature sensors, image screen with rotation, boot logo, keyboard clock sync, burn-in care

If the keyboard ever stops responding after a firmware update: unplug it, hold **Esc** while plugging it back in (it starts in update mode), then install the official firmware from the Firmware tab. Typing keeps working on every REVIVER version.
