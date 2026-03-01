# Mo2BinaryHelper (ModBisect)

MO2 plugin that finds FPS-killing plugins via binary search. Drop it in your MO2 plugins folder, enter your good/bad FPS, and it narrows down which plugins are tanking performance.

## How It Works

1. Backs up your `plugins.txt` and `modlist.txt`
2. Splits your enabled plugins into halves
3. You test each half in-game and report FPS
4. Repeats until individual culprits are identified (groups costing >5 FPS)
5. Restore button returns everything to pre-bisection state

## Features

- **Full load order bisection** — no suspects file needed, reads your entire plugin list
- **Left pane sync** — disables mods in `modlist.txt` when their plugins are disabled, preventing crashes from orphaned BA2 archives
- **Master-aware grouping** — plugins with shared masters are grouped together so disabling one doesn't break another
- **Cascade detection** — plugins depending on 5+ other testable plugins are set aside to avoid cascading missing master issues
- **Preserves load order** — disabled plugins stay in `plugins.txt` (without `*` prefix) so positions never shift
- **Crash handling** — retry the same test or skip and treat as bad FPS
- **Safe restore** — restores `modlist.txt` first, then `plugins.txt`, so mods are enabled before their plugins come back

## Install

Copy `ModBisect.py` to your MO2 `plugins/` folder. It appears under **Tools > Mod Bisect Tool** in MO2.

## Usage

1. Open MO2, go to **Tools > Mod Bisect Tool**
2. Enter your **Good FPS** (what you get with mods disabled) and **Bad FPS** (what you get with full load order)
3. Click **Start** — the tool backs up your files and enables the first test group
4. Launch the game, note your FPS, come back and enter it
5. Repeat until all culprits are found
6. Click **Restore Original Files** when done

## Settings

In MO2's plugin settings for ModBisect, you can set `extra_excludes` — a comma-separated list of mod name patterns to never disable (e.g., framework mods that everything depends on).

By default, these are always excluded: `address library`, `high fps`, `addictol`.

## Requirements

- Mod Organizer 2
- PyQt6 (included with MO2)
