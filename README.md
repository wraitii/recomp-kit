# Recomp Toolkit

Build native versions of supported classic PC games from your own copies.
Recomp Toolkit checks your game files, recompiles the game and packages it as
a native app for your computer. Launch each game's app directly, with its own
settings and game files. Your original copy stays untouched.

The desktop app handles setup and builds, with a library view for convenience.

## Get started

[Download Recomp Toolkit](https://github.com/wraitii/recomp-kit/releases/latest)
for Apple silicon Macs running macOS 14 or later. You'll need your own copy of
one of the games below and Apple's Command Line Tools (`xcode-select --install`).
The app includes the other build dependencies; GOG installer imports also need
`innoextract` 1.9 or later (`brew install innoextract` with Homebrew).

1. Open Recomp Toolkit and choose a game.
2. Choose your installed game folder, a ZIP of it, or a supported GOG installer.
   The app checks that your copy matches the required version.
3. Choose where to install the native app. Follow any preparation instructions,
   then choose **Install**. Building happens on your computer and can take
   several minutes or longer; leave room for the game files and temporary builds.
4. Open the finished game's app, or choose **Start game** in Recomp Toolkit.

## Games

The games below are playable in single player, with Spore still in development.
Networking is currently unsupported across the toolkit. Each game's page lists
the required release and preparation steps.

| Game | Status | Required version |
| --- | --- | --- |
| [Black & White](games/black-and-white/README.md) | Playable | CD with Fan Patch 1.42 |
| [Tom Clancy's Ghost Recon](games/ghost-recon/README.md) | Playable | GOG 1.4 |
| [Civilization IV: Beyond the Sword](games/civ4-bts/README.md) | Playable | GOG 3.19; additional DLL required |
| [4x4 Evolution 2](games/4x4-evo2/README.md) | Playable | PC 1.00.139 with matching renderer DLLs |
| [Railroad Tycoon 3](games/railroad-tycoon-3/README.md) | Playable | GOG 1.05 |
| [Spore](games/spore/README.md) | Work in progress | GOG Collection, Galactic Adventures 3.1.0.22 |

Performance is generally good, with occasional slowdowns still being worked on,
mostly in Civilization IV and Spore. Rare crashes from incorrect code mappings
are also possible.

## Your installed apps

**Game files.** You supply the games; downloads contain no commercial game files.
The app copies your files into the destination you choose. Keep the whole
installed folder together, including the game app, its Configure app,
`original/` and `.recomp-toolkit/`.

**Installers and patches.** Supported GOG Inno installers can be extracted
without running them. Keep accompanying BIN files beside the installer.
Other installers and disc images need preparation outside Recomp Toolkit;
select the resulting installed folder afterward. Apply any required patches
before importing, including Black & White's Fan Patch 1.42.

**Settings.** Open the installed game's Configure app to adjust its settings
and controller mappings. Launch the game directly whenever you want to play;
Recomp Toolkit doesn't need to be open.

**Trouble installing?** Check the required version on the game's page. If build
tools are missing, follow the app's instructions and choose **Check again**.
For a failed installation, expand **Installation details** and use **Try again**
after resolving the reported problem. Include the game version and installation
log when [reporting an issue](https://github.com/wraitii/recomp-kit/issues).

## How it works

Static recompilation translates the original game's 32-bit x86 Windows machine
code into native code ahead of time. The toolkit supplies the operating-system,
graphics and audio interfaces the game expects. The resulting app runs without
a CPU emulator or JIT, using your game's original data. Each port includes
translation metadata for the exact executable it supports.

## Development and porting

The desktop app is the recommended way to install supported games. To work on
the toolkit or bring another game to it, start with [Contributing](CONTRIBUTING.md):

- [Build from source](CONTRIBUTING.md#prerequisites), including the desktop app.
- [Port a new game](CONTRIBUTING.md#add-a-game).
- Explore the [architecture](docs/architecture.md), [code guide](docs/code-guide.md)
  and [translator](docs/ir.md).

The kit also has Linux and Windows builds, and iOS and Android targets. Their
build instructions are separate from the packaged macOS desktop app.
