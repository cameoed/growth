"""Installation policy carried by the public source archive."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
USER_MISSION_MARKER = "config/require-user-mission"


def requires_user_mission(root: Path = ROOT) -> bool:
    return (root / USER_MISSION_MARKER).is_file()
