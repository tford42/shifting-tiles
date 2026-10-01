#!/usr/bin/env bash
# Start the shifting-tiles photo wall from the folder this file lives in.
# A file manager does not leave you in that folder when you click a script,
# so the path is taken from this file, not from the working directory.
# Photos in this folder and its subfolders are used. Esc or Q quits.

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python_script="$here/shifting_tiles.py"

if [[ ! -f "$python_script" ]]; then
    echo "Cannot find shifting_tiles.py next to this script:" >&2
    echo "  $python_script" >&2
    exit 1
fi

if command -v python3 >/dev/null 2>&1; then
    python=python3
elif [[ -x /usr/bin/python3 ]]; then
    python=/usr/bin/python3
else
    echo "python3 was not found. Install it, then try again." >&2
    exit 1
fi

cd "$here"
"$python" "$python_script" "$here"
status=$?

if [[ $status -ne 0 ]]; then
    message="Shifting Tiles did not start (exit $status).
If this is the first run, install Pillow and the window toolkit:
    pip install Pillow
    sudo apt install python3-tk"
    echo "$message" >&2
    if command -v zenity >/dev/null 2>&1; then
        zenity --error --title="Shifting Tiles" --text="$message" 2>/dev/null || true
    elif command -v notify-send >/dev/null 2>&1; then
        notify-send "Shifting Tiles" "Did not start. See the message about Pillow and python3-tk." || true
    fi
fi

exit $status
