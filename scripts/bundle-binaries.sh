#!/usr/bin/env bash
set -e

APP_ID="io.github.fastrizwaan.PopcornBox"
OUT_FILE="popcorn-box.tar.gz"

echo "Finding installation path for $APP_ID..."
INSTALL_PATH=$(flatpak info --show-location "$APP_ID" 2>/dev/null || true)

if [ -z "$INSTALL_PATH" ]; then
    echo "Error: Could not find $APP_ID. Ensure it is installed via flatpak."
    exit 1
fi

FILES_DIR="$INSTALL_PATH/files"

if [ ! -d "$FILES_DIR" ]; then
    echo "Error: Files directory $FILES_DIR does not exist."
    exit 1
fi

echo "Creating $OUT_FILE from $FILES_DIR..."
# We package the compiled binaries, libraries, Python source, and configuration
tar -czf "$OUT_FILE" -C "$FILES_DIR" bin lib share etc include

echo "Successfully created $OUT_FILE!"
ls -lh "$OUT_FILE"

echo ""
echo "Next steps:"
echo "1. Generate SHA256: sha256sum $OUT_FILE"
echo "2. Upload $OUT_FILE to your GitHub releases."
echo "3. Update the sha256 and url in your io.github.fastrizwaan.PopcornBox.json manifest."
