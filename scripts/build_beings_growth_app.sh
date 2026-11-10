#!/bin/zsh

set -eu

project_dir="$(cd -- "$(dirname -- "$0")/.." && pwd -P)"
source_file="$project_dir/macos/BeingsGrowth.swift"
app_dir="$project_dir/macos/Beings Growth.app"
app_binary="$app_dir/Contents/MacOS/BeingsGrowth"
logo_file="$project_dir/macos/BeingsGrowthIcon.png"
dashboard_logo="$project_dir/src/beings_leads/dashboard/beings-growth-logo.png"
resources_dir="$app_dir/Contents/Resources"
icon_file="$resources_dir/BeingsGrowth.icns"

/bin/mkdir -p "$app_dir/Contents/MacOS" "$resources_dir"
/usr/bin/xcrun swiftc \
  "$source_file" \
  -framework Cocoa \
  -framework WebKit \
  -o "$app_binary"

/bin/chmod 755 "$app_binary"
printf '%s' "$project_dir" > "$resources_dir/checkout-path"

if [[ ! -f "$logo_file" ]]; then
  print -u2 "Beings Growth app icon is missing: $logo_file"
  exit 1
fi

# Preserve the full blue star artwork, including its opaque background.

/usr/bin/ditto "$logo_file" "$dashboard_logo"
/usr/bin/ditto --norsrc --noextattr "$project_dir/src/beings_leads/dashboard" "$resources_dir/dashboard"

icon_work_dir="$(/usr/bin/mktemp -d)"
trap '/bin/rm -rf "$icon_work_dir"' EXIT
iconset="$icon_work_dir/BeingsGrowth.iconset"
/bin/mkdir -p "$iconset" "$resources_dir"

for size in 16 32 128 256 512; do
  /usr/bin/sips -z "$size" "$size" "$logo_file" --out "$iconset/icon_${size}x${size}.png" >/dev/null
  retina_size=$((size * 2))
  /usr/bin/sips -z "$retina_size" "$retina_size" "$logo_file" --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
done

/usr/bin/iconutil -c icns "$iconset" -o "$icon_file"
/usr/bin/touch "$app_dir"
