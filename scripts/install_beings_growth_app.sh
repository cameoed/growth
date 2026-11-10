#!/bin/zsh

set -eu

project_dir="$(cd -- "$(dirname -- "$0")/.." && pwd -P)"
source_app="$project_dir/macos/Beings Growth.app"
applications_dir="${BEINGS_GROWTH_APPLICATIONS_DIR:-$HOME/Applications}"
installed_app="$applications_dir/Beings Growth.app"

"$project_dir/scripts/build_beings_growth_app.sh"

if [[ ! -f "$source_app/Contents/Info.plist" || ! -f "$source_app/Contents/MacOS/BeingsGrowth" ]]; then
  print -u2 "Beings Growth application source is incomplete."
  exit 1
fi

/bin/mkdir -p "$applications_dir"
/usr/bin/ditto "$source_app" "$installed_app"
/bin/chmod 755 "$installed_app/Contents/MacOS/BeingsGrowth"
/usr/bin/codesign --force --deep --sign - "$installed_app"
/usr/bin/open "$installed_app"

print "$installed_app"
