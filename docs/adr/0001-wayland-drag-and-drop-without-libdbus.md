# 1. Wayland drag-and-drop without libdbus

- Status: accepted (amended 2026-11-11, see Amendment)
- Date: 2026-10-22
- Tracking: #64

## Context

On native Wayland, dropping a file from a file manager onto a craft app does nothing. The cause is winit 0.30, which egui pins (`winit = "0.30.13"`): it never emits `DroppedFile` on Wayland. X11 and XWayland work.

#57 added `libdbus-1-dev` (and `libdbus-1-dev:riscv64` for the cross build) to the shared Linux build dependencies. That was groundwork for a winit prototype (rust-windowing/winit#4790, branch `mockydevs/winit:prototype/native-wayland-031`) that resolves Flatpak portal drops through libdbus.

Where the upstream pieces stand:

- **Plain Wayland drops** are in winit 0.31: rust-windowing/winit#1881 closed as completed 2026-08-03. 0.31 is in beta (beta.3 on 2026-09-04). The 0.31.0 milestone has no due date.
- **egui** is still on 0.30. Ports to 0.31 are in progress (emilk/egui#7731, emilk/egui#8767).
- **The 0.30 backport** (rust-windowing/winit#4787) was closed upstream without merging. It used zbus 5, not libdbus.
- **Flatpak drops** deliver an `application/vnd.portal.filetransfer` token instead of paths. The token must be exchanged through `org.freedesktop.portal.FileTransfer.RetrieveFiles` over D-Bus. 0.31 doesn't do this. #4790 proposes it, but its author says the dependency choice and the API are "proposals, not settled designs", and it notes that "an application-side resolver is another possible design".

What the craft apps already have:

- Every craft app on these shared workflows already depends on **zbus**, which is pure Rust and comes in through `rfd`.
- None of them link `libdbus-sys`.
- vectorcraft's D-Bus single-instance handoff (storytold/vectorcraft#3423) also uses zbus.

## Decision

1. **Revert #57.** No libdbus headers in `install-linux-deps` or `install-riscv64-cross`.
2. **Native drops come from upgrading to winit 0.31**, through the egui release that adopts it. We don't patch or fork winit.
3. **Flatpak drops use zbus.** Either winit resolves the portal token with zbus, or winit passes the raw token through and a shared resolver in craft-libs calls `RetrieveFiles` with zbus. We've said so on #4790.
4. **Until then, drag-and-drop on Wayland is a documented gap.** Users open files with File › Open.

## Consequences

- No C D-Bus library anywhere in the build or runtime.
  - libdbus would have had to be added to every build path: native, riscv64 cross, Nix and FreeBSD.
  - It would also have become a runtime dependency of every package: the nfpm deb and rpm, the Arch `depends=`, the AppImage and the Flatpak.
  - zbus needs none of that, and it parses data from outside the sandbox in memory-safe code.
- Wayland users can't drag files in for months. The chain is: winit 0.31 final, then an egui release, then an upgrade in each app.
- The 0.31 upgrade alone doesn't fix Flatpak. Flatpak needs the zbus resolver too, and #64 tracks both.
- If winit settles on libdbus for portal drops anyway, we revisit this decision. We'd prefer to raw-pass the token and resolve it in the app, so the dependency stays out.

## Alternatives considered

- **Keep #57 and adopt libdbus.** Rejected: adds a C dependency to every build and package, when zbus is already in the tree.
- **Patch or fork winit 0.30 with the #4787 backport.** Rejected: upstream closed it, and we'd carry a fork of the windowing stack.
- **Give the Flatpak the `--filesystem=host` permission so drops arrive as paths.** Rejected: it gives up the sandbox to work around a UI gap.
- **Force XWayland by unsetting `WAYLAND_DISPLAY`.** Possible stopgap, untested. It isn't a fix, because it loses native Wayland scaling and input.

## Amendment 2026-11-11

`libdbus-1-dev` is back in `install-linux-deps` (#186). The header is there for `deckcraft`: it pulls `scap` for screen recording (storytold/deckcraft#1838), and `scap`'s Linux build looks `libdbus-1` up through pkg-config. Point 1 of the decision no longer describes the action, and the first consequence -- no C D-Bus library in any build -- no longer holds for an app that builds `scap`.

The other points stand. Flatpak drops are still resolved with zbus, and nothing in this change chooses libdbus as the portal route.

If the shared action should stay free of the C library, the alternative is to install these three packages in `deckcraft`'s own release job, the way its `gate.yml` call already lists them. #186 is where that choice belongs.
