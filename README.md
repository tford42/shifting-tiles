# Shifting Tiles

Fullscreen photo wall. Photos from a folder and its subfolders are cropped into a dense grid. Every few seconds a block of tiles slides away and new photos slide in, in the style of the macOS Shifting Tiles screensaver. Order is random.

## Requirements

Python 3.8 or newer, tkinter, and Pillow.

```
pip install -r requirements.txt
```

On Debian or Ubuntu, if the window toolkit is missing:

```
sudo apt install python3-tk
```

HEIC and HEIF photos are included only when `pillow-heif` is installed (`pip install pillow-heif`).

## Run

Choose a folder when asked:

```
python3 shifting_tiles.py
```

Or pass the folder on the command line:

```
python3 shifting_tiles.py ~/Pictures
```

`shifting_tiles.sh` starts the wall using the folder that script lives in, including subfolders, as the photo source. Esc or Q quits.

Do not commit personal photos into this repo. Point the script at a photo folder on your own machine.
