from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .discovery import normalize_apex
from .domain_policy import is_disallowed, require_allowed

DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}$", re.I)
BACKTICK_DOMAIN_RE = re.compile(r"`([a-z0-9][a-z0-9.-]*\.[a-z]{2,})`", re.I)


@dataclass(frozen=True)
class RegistrySource:
    apex: str
    category: str
    tier: str
    indexed_names: int | None = None
    expected_fit: str = ""
    notes: str = ""
    recommended: bool = False


def _category_for_heading(heading: str) -> str:
    lowered = heading.lower()
    if "agenc" in lowered:
        return "Agency"
    if "ecommerce" in lowered or "commerce" in lowered:
        return "Ecommerce"
    if "b2b" in lowered or "saas" in lowered or "marketing" in lowered:
        return "B2B / SaaS"
    return "Other"


def _tier_for_heading(heading: str, recommended: bool) -> str:
    lowered = heading.lower()
    if (
        recommended
        or "tier a" in lowered
        or "run first" in lowered
        or "enterprise commerce" in lowered
    ):
        return "A"
    if "tier b/c" in lowered:
        return "B/C"
    if "tier b" in lowered or "mid-market" in lowered:
        return "B"
    return "Research"


def parse_registry(path: str | Path) -> list[RegistrySource]:
    path = Path(path)
    if not path.exists():
        return []
    entries: dict[str, RegistrySource] = {}
    h2 = ""
    h3 = ""
    in_code = False
    recommended_section = False

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("## "):
            h2 = line[3:].strip()
            h3 = ""
            recommended_section = h2.lower() == "recommended starting set"
            continue
        if line.startswith("### "):
            h3 = line[4:].strip()
            continue
        if line.startswith("```"):
            in_code = not in_code
            continue

        heading = f"{h2} {h3}".strip()
        category = _category_for_heading(heading)
        tier = _tier_for_heading(h3, recommended_section)

        if in_code and recommended_section and DOMAIN_RE.fullmatch(line):
            apex = normalize_apex(line)
            entries[apex] = RegistrySource(
                apex=apex,
                category=category,
                tier="A",
                recommended=True,
                notes="Recommended starting set",
            )
            continue

        normalized_h2 = h2.casefold()
        dashboard_addition = normalized_h2 == "dashboard additions"
        if not line.startswith("|") or not (
            dashboard_addition
            or any(marker in normalized_h2 for marker in ("agency", "ecommerce", "b2b/saas"))
        ):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if not cells or cells[0].lower() in {"seed apex", "---"}:
            continue
        match = BACKTICK_DOMAIN_RE.search(cells[0])
        if not match:
            continue
        apex = normalize_apex(match.group(1))
        if dashboard_addition:
            entries[apex] = RegistrySource(
                apex=apex,
                category=cells[2] if len(cells) > 2 else "Other",
                tier="Research",
                notes=cells[3] if len(cells) > 3 else "Added from dashboard",
                recommended=False,
            )
            continue
        indexed_names = None
        if len(cells) > 1:
            count_text = cells[1].replace(",", "")
            if count_text.isdigit():
                indexed_names = int(count_text)
        expected_fit = cells[3] if len(cells) >= 5 else ""
        notes = cells[-1] if len(cells) >= 3 else ""
        prior = entries.get(apex)
        entries[apex] = RegistrySource(
            apex=apex,
            category=category,
            tier="A" if prior and prior.recommended else tier,
            indexed_names=indexed_names,
            expected_fit=expected_fit,
            notes=notes or (prior.notes if prior else ""),
            recommended=bool(prior and prior.recommended),
        )
    return sorted(
        (item for item in entries.values() if not is_disallowed(item.apex)),
        key=lambda item: (item.category, item.tier, item.apex),
    )


def append_registry_source(
    path: str | Path,
    apex: str,
    *,
    category: str = "Other",
    notes: str = "Added from the local dashboard",
) -> bool:
    path = Path(path)
    apex = normalize_apex(apex)
    require_allowed(apex)
    if any(source.apex == apex for source in parse_registry(path)):
        return False
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d")
    content = path.read_text(encoding="utf-8") if path.exists() else "# Prospecting domains\n"
    heading = "## Dashboard additions"
    table_header = (
        "\n\n## Dashboard additions\n\n"
        "Sources added through the localhost operations dashboard.\n\n"
        "| Seed apex | Added | Category | Notes |\n"
        "|---|---|---|---|\n"
    )
    category_cell = " ".join(category.replace("|", "/").split())
    notes_cell = " ".join(notes.replace("|", "/").split())
    row = f"| `{apex}` | {timestamp} | {category_cell} | {notes_cell} |\n"
    if heading not in content:
        content = content.rstrip() + table_header + row
    else:
        insertion = content.find("\n## ", content.find(heading) + len(heading))
        if insertion == -1:
            content = content.rstrip() + "\n" + row
        else:
            content = content[:insertion].rstrip() + "\n" + row + "\n" + content[insertion:]
    path.write_text(content, encoding="utf-8")
    return True
