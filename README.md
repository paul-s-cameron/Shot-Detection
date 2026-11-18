# Shot Detection

Find the cuts in movie strips in the Blender Video Sequence Editor (VSE) and split the strips there.

Shot Detection reads each clip once and remembers how much every frame differs from the one before. After that you can change the threshold as often as you like and see the cuts move at once, without waiting for the video to be read again. When the cuts look right, one click splits the strips.

It is a rewrite of [Detect Shots and Split Strips](https://github.com/tin2tin/Shot_Detection) by tin2tin. The detection itself is done by [PySceneDetect](https://www.scenedetect.com) by Brandon Castellano. See [What changed from the original](#what-changed-from-the-original).

## Disclosure: AI:W

My changes to this add-on are labelled **AI:W** under the [0rwx framework](https://0rwx.com/): AI writes the code and I read it.

*   The code I add to this fork is written by an AI model (Claude), not by hand.
*   I decide what gets built, read the changes, and test them in my own editing work in Blender before they are committed.
*   The label covers my changes only. The original add-on is [Detect Shots and Split Strips](https://github.com/tin2tin/Shot_Detection) by tin2tin.
*   I test on Windows in Blender 5.3. Expect bugs, and save your project before trying it.

## How it works

1.  **Analyse.** Select one or more movie strips and click `Analyse Selected`. Each clip is read once, in the background, one after another. Blender stays usable, and `Cancel` stops it.
2.  **Choose the cuts.** Drag `Threshold` and `Min Shot Length`. The number of cuts per strip updates at once, and timeline markers show where they are.
3.  **Split.** Click `Split at Cuts`. The selected movie strips are split, along with any selected strips that overlap them, such as their audio.

## Features

*   **Analyse once**: The slow part, reading the video, happens once per clip. Changing the settings afterwards is instant.
*   **Queue**: Every selected movie strip is analysed in turn, with a progress bar.
*   **Proxies**: Read Blender's 25%, 50%, 75% or 100% proxy, or the size the preview is set to, instead of the source file. Much faster on large footage. If the proxy does not exist, the source file is read and the add-on says why.
*   **Cut preview**: A cut count per strip, and optional timeline markers named `cut 1`, `cut 2` and so on. Only markers with those names are ever added or removed.
*   **Trimmed strips**: Only the part of a clip that the strip uses is read and split.
*   **Runs apart from Blender**: The video is read by a separate process, so a problem in the video library cannot take Blender down.

## Where the scores are kept

The `Keep Scores` setting decides what happens to the result of an analysis:

*   **Beside the Video** (default): a small file named `<video name>.shots.json` next to the source video. It is safe to delete; the video is simply read again next time.
*   **Add-on Folder**: in the add-on's own user folder, out of sight.
*   **This Session Only**: nothing is written. The video is read again after Blender restarts.

`Forget Analysis` deletes the scores of the selected strips, in memory and on disk.

## Installation

1.  **Blender version**: Blender **5.2** or newer.
2.  **Download**: [Download the add-on as a .zip file](https://github.com/paul-s-cameron/Shot-Detection/archive/refs/heads/main.zip). Do not unzip it.
3.  **Original add-on**: If you have tin2tin's Detect Shots and Split Strips enabled, disable it first.
4.  **Install the add-on**: In Blender, go to `Edit > Preferences > Add-ons`, open the menu in the top right corner and choose `Install from Disk...`. Select the downloaded `.zip` file.
5.  **Install the libraries**:
    *   Make sure `Preferences > System > Network > Allow Online Access` is on.
    *   In the Sequencer, open the Sidebar (`N` key) and go to the `Shots` tab.
    *   Click `Install / Repair`. This downloads PySceneDetect and OpenCV (about 180 MB installed). Blender stays usable while it runs, and progress is shown in the System Console (`Window > Toggle System Console`).

You do not need to run Blender as administrator. The libraries go into the extension's own user folder, which Blender keeps separately for each Blender version and removes when you uninstall the add-on. Blender's own libraries are left as they are.

## Where to find it

*   **Panel**: In the `Sequencer`, open the Sidebar (`N` key) and click the **Shots** tab.
*   **Menus**: `Analyse Shots` and `Split at Shot Cuts` are also at the bottom of the Strip menu and the strip context menu.

## What changed from the original

*   Detection is split into analysing (slow, once) and choosing the cuts (instant).
*   Proxies can be read instead of the source file.
*   A minimum shot length setting.
*   A progress bar and a Cancel button.
*   The libraries are installed into the add-on's own folder, without administrator rights.
*   It is a Blender extension, with a `blender_manifest.toml`.

The original splits as soon as detection finishes. Here splitting is a separate step, so you can check the cuts first.

## Modules used

This add-on relies on the following open-source libraries, installed by the `Install / Repair` button:

*   [PySceneDetect](https://github.com/Breakthrough/PySceneDetect): scene cut detection, by Brandon Castellano.
*   [OpenCV](https://opencv.org): reads the video frames for PySceneDetect.

## License and credits

GPL version 2 or later, the same as the original. Based on [Detect Shots and Split Strips](https://github.com/tin2tin/Shot_Detection) by tin2tin.
