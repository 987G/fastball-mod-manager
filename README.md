# Show Mod Manager

Show Mod Manager adds mods to MLB The Show 26 on PC, the game you built with Fastball. You pick mods from a list,
download them and press Play; the manager puts each mod in place and takes it out again when you switch it off.

## What you need

- Windows 10 or 11 (64-bit).
- Fastball 0.2.1, with the game installed by it on this PC.
- Fastball's setup folder (the folder with `Fastball.exe`), kept where it is: the manager builds the game with your
  mods using the tools in it.

## Download and start

1. Open the Releases page of this repository and download `ShowModManager-<version>.zip` from the newest release.
2. Unzip it into a folder of its own.
3. Start `Show Mod Manager.exe` in that folder. The manager opens in a window of its own.

Windows may say it does not know the app the first time: choose **More info**, then **Run anyway**. Nothing is
installed anywhere else; the manager keeps everything in its own folder.

## First run

1. Open **SETTINGS**.
2. Set **Setup folder** to your Fastball setup folder.
3. Check that **Game folder** is the folder Fastball installed the game into (the one with `mlb.exe`), and correct it
   if it is not.
4. Press **SAVE**, then go back to **HOME**.

HOME now shows the mods of this release. Tick the ones you want (click the box, or press Space), then press
**DOWNLOAD SELECTED**. The manager downloads them and the runtime they need.

## Play

Press **PLAY** at the bottom right. When the mods you have switched on change the game's code, the first Play builds
the game with them, which takes a few minutes; later Plays use that build until you change your mods.

## Switch mods off, get more, remove one

- On HOME, each mod has an ON / OFF switch, and its card has **TURN ON** / **TURN OFF**. A mod you switch off stays
  downloaded, and its files come out of the game at your next Play.
- **GET MODS** shows this release's list again: tick more mods and press **DOWNLOAD SELECTED**. Each mod shows its
  version, and a mod you have shows, for example, **v5 → v6** when this release updates it.
- To delete a mod, switch it off, press **GET MODS**, then press **REMOVE** beside it. The manager takes its files out
  of the game and deletes it from your mods folder.

## Updating

The manager checks GitHub for a newer release when it starts and when you open **GET MODS**, and shows a line on
HOME when there is one; **OPEN DOWNLOAD PAGE** beside it opens that release's page in your browser.

Close the manager (**SETTINGS**, **QUIT MANAGER**). Download the new `ShowModManager-<version>.zip` from the Releases
page and unzip it over the old manager folder, replacing the files there; your settings and mods stay. Start the
manager again and press **GET MODS**: it offers an update for each mod you have.

## Stop using the manager

To stop using the manager, remove every mod as above, close the manager (**SETTINGS**, **QUIT MANAGER**) and delete
its folder. The game Fastball installed keeps working as before.

## How a release is built

Everything a release is made from is in this repository:

- `manager/`: the manager itself (Python's standard library only), with its tests.
- `mods/`: each mod's files, as its zip holds them.
- `runtime/mods.patch`: the mods' changes to Fastball's runtime, and `runtime/sources.json`, which names the exact
  Fastball commit and the commit of every mod branch the patch was made from, and the manager's own commit.
- `tools/build_runtime.py` builds Fastball's runtime library with that patch, `tools/package.py` makes the release
  files, and `tools/check_release.py` checks that nothing published holds game files or personal information.

The runtime is built and packaged on the maintainer's PC, not by GitHub: building it needs headers made from a local
build of the game, and the game's files are never published. Each push runs the manager's tests and the content
check here on GitHub.

## Not affiliated

Show Mod Manager is a fan project. It is not made, approved or supported by the makers of MLB The Show, by MLB or by
Nintendo, and it is not part of Fastball. No game files are in this repository or its releases: the mods change the
game you built yourself from your own copy.

## Licence

MIT (see `LICENSE`). The work of others in the release files is listed in `THIRD_PARTY_NOTICES.md`.
