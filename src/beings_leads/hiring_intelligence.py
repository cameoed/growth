from __future__ import annotations

import hashlib
import html as html_lib
import json
import math
import re
import unicodedata
from collections import Counter, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

RoleConfidence = Literal["none", "medium", "high"]

MAX_HTML_CHARS = 4_000_000
MAX_DESCRIPTION_CHARS = 10_000
MAX_DESCRIPTION_EXCERPT = 1_000
MAX_JSONLD_NODES = 5_000
MAX_JSONLD_DEPTH = 12
MAX_STRUCTURAL_CARDS = 1_000
DEFAULT_MAX_POSTINGS = 250


@dataclass(frozen=True, slots=True)
class JobRoleClassification:
    """Deterministic role labels; no label implies hiring urgency or headcount growth."""

    role_families: tuple[str, ...]
    creative_subfamilies: tuple[str, ...]
    creative: bool
    beings_relevant: bool
    photo_video: bool
    growth_marketing: bool
    leadership: bool
    confidence: RoleConfidence
    matched_phrases: tuple[str, ...] = ()
    exclusion_reasons: tuple[str, ...] = ()
    description_upgrade: bool = False


@dataclass(frozen=True, slots=True)
class PublicCompensationRange:
    """A literal published range; never converted into available outsourcing budget."""

    minimum: float | None = None
    maximum: float | None = None
    currency: str = ""
    currency_symbol: str = ""
    period: str = ""
    literal: str = ""
    source_field: str = ""
    source_url: str = ""


@dataclass(frozen=True, slots=True)
class JobDeliverableExpectation:
    deliverable: str
    excerpt: str
    source_url: str
    quantity_min: int | None = None
    quantity_max: int | None = None
    period: str = ""
    channels: tuple[str, ...] = ()
    formats: tuple[str, ...] = ()
    workflow_stages: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NormalizedJobPosting:
    """A bounded public posting normalized without treating one posting as one hire."""

    stable_id: str
    provider: str
    provider_id: str
    title: str
    normalized_title: str
    job_url: str
    source_url: str
    source_urls: tuple[str, ...]
    location: str
    department: str
    description_excerpt: str
    description_hash: str
    posted_at: datetime | None
    valid_through: datetime | None
    current: bool
    state_reason: str
    compensation_published: bool
    classification: JobRoleClassification
    production_pressure_signals: tuple[str, ...] = ()
    external_capacity_signals: tuple[str, ...] = ()
    ai_workflow_signals: tuple[str, ...] = ()
    rights_risk_signals: tuple[str, ...] = ()
    compensation_ranges: tuple[PublicCompensationRange, ...] = ()
    deliverable_expectations: tuple[JobDeliverableExpectation, ...] = ()
    contract_signals: tuple[str, ...] = ()
    channels: tuple[str, ...] = ()
    formats: tuple[str, ...] = ()
    first_observed_at: datetime | None = None
    last_observed_at: datetime | None = None

    @property
    def role_families(self) -> tuple[str, ...]:
        return self.classification.role_families

    @property
    def creative_subfamilies(self) -> tuple[str, ...]:
        return self.classification.creative_subfamilies

    def as_dict(self) -> dict[str, Any]:
        return {
            "stable_id": self.stable_id,
            "provider": self.provider,
            "provider_id": self.provider_id,
            "title": self.title,
            "normalized_title": self.normalized_title,
            "job_url": self.job_url,
            "source_url": self.source_url,
            "source_urls": list(self.source_urls),
            "location": self.location,
            "department": self.department,
            "description_excerpt": self.description_excerpt,
            "description_hash": self.description_hash,
            "posted_at": self.posted_at.isoformat() if self.posted_at else None,
            "valid_through": self.valid_through.isoformat() if self.valid_through else None,
            "current": self.current,
            "state_reason": self.state_reason,
            "compensation_published": self.compensation_published,
            "role_families": list(self.role_families),
            "creative_subfamilies": list(self.creative_subfamilies),
            "creative": self.classification.creative,
            "beings_relevant": self.classification.beings_relevant,
            "photo_video": self.classification.photo_video,
            "growth_marketing": self.classification.growth_marketing,
            "leadership": self.classification.leadership,
            "classification_confidence": self.classification.confidence,
            "matched_phrases": list(self.classification.matched_phrases),
            "exclusion_reasons": list(self.classification.exclusion_reasons),
            "description_upgrade": self.classification.description_upgrade,
            "production_pressure_signals": list(self.production_pressure_signals),
            "external_capacity_signals": list(self.external_capacity_signals),
            "ai_workflow_signals": list(self.ai_workflow_signals),
            "rights_risk_signals": list(self.rights_risk_signals),
            "compensation_ranges": [asdict(item) for item in self.compensation_ranges],
            "deliverable_expectations": [asdict(item) for item in self.deliverable_expectations],
            "contract_signals": list(self.contract_signals),
            "channels": list(self.channels),
            "formats": list(self.formats),
            "first_observed_at": self.first_observed_at.isoformat()
            if self.first_observed_at
            else None,
            "last_observed_at": self.last_observed_at.isoformat()
            if self.last_observed_at
            else None,
        }


@dataclass(frozen=True, slots=True)
class HiringDemandSummary:
    """Current distinct-posting counts; ratios are unknown under partial coverage."""

    distinct_open_postings: int
    creative_core_count: int
    beings_relevant_count: int
    photo_video_count: int
    growth_marketing_count: int
    leadership_count: int
    recent_creative_30d: int
    creative_share_basis_points: int | None
    production_context_postings: int
    creative_subfamily_counts: tuple[tuple[str, int], ...]
    complete: bool
    counts_are_lower_bounds: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "distinct_open_postings": self.distinct_open_postings,
            "creative_core_count": self.creative_core_count,
            "beings_relevant_count": self.beings_relevant_count,
            "photo_video_count": self.photo_video_count,
            "growth_marketing_count": self.growth_marketing_count,
            "leadership_count": self.leadership_count,
            "recent_creative_30d": self.recent_creative_30d,
            "creative_share_basis_points": self.creative_share_basis_points,
            "production_context_postings": self.production_context_postings,
            "creative_subfamily_counts": dict(self.creative_subfamily_counts),
            "complete": self.complete,
            "counts_are_lower_bounds": self.counts_are_lower_bounds,
        }


_CREATIVE_PHRASES: dict[str, tuple[str, ...]] = {
    "photo_video": (
        "photographer",
        "photography producer",
        "photography manager",
        "photo producer",
        "photo editor",
        "photo retoucher",
        "videographer",
        "cinematographer",
        "director of photography",
        "video producer",
        "video editor",
        "video creator",
        "video production manager",
        "video production coordinator",
        "film producer",
        "film editor",
        "post production producer",
        "post production editor",
        "post production manager",
        "post production supervisor",
        "post production coordinator",
        "retoucher",
        "colorist",
        "ai video producer",
        "generative media producer",
    ),
    "motion_3d": (
        "motion designer",
        "motion graphics designer",
        "motion graphics artist",
        "motion graphics",
        "animator",
        "animation producer",
        "animation director",
        "animation designer",
        "3d artist",
        "3d designer",
        "3d animator",
        "cgi artist",
        "cgi producer",
        "vfx artist",
        "vfx producer",
        "vfx supervisor",
        "visual effects artist",
    ),
    "visual_brand": (
        "graphic designer",
        "visual designer",
        "brand designer",
        "marketing designer",
        "campaign designer",
        "creative designer",
        "illustrator",
        "art director",
    ),
    "creative_production_ops": (
        "creative producer",
        "integrated producer",
        "content producer",
        "studio producer",
        "creative project manager",
        "creative operations",
        "creative ops",
        "creative resource manager",
        "creative production manager",
        "creative production coordinator",
        "asset producer",
        "production artist",
    ),
    "performance_creative": (
        "creative strategist",
        "creative director",
        "associate creative director",
        "executive creative director",
        "group creative director",
        "chief creative officer",
        "performance creative",
        "paid social creative",
        "direct response creative",
        "ugc creative",
        "advertising creative",
        "creative lead",
        "generative ai creative",
        "ai creative producer",
    ),
    "copy_content": (
        "copywriter",
        "copy writer",
        "brand writer",
        "creative writer",
        "advertising writer",
        "content creator",
        "social content producer",
    ),
}

_GROWTH_PHRASES = (
    "growth marketing",
    "performance marketing",
    "paid media",
    "paid social",
    "user acquisition",
    "demand generation",
    "lifecycle marketing",
    "brand marketing",
    "content marketing",
    "social media marketing",
    "marketing manager",
    "marketing director",
    "head of marketing",
    "chief marketing officer",
)

_EXCLUSION_PHRASES = (
    "product designer",
    "product design",
    "ux designer",
    "ui designer",
    "ui ux",
    "user experience",
    "user interface",
    "service designer",
    "industrial designer",
    "cad designer",
    "interior designer",
    "fashion designer",
    "design engineer",
    "production engineer",
    "manufacturing engineer",
    "manufacturing production",
    "plant production",
    "production support",
    "content moderator",
    "content moderation",
    "content policy",
    "content engineer",
    "brand manager",
    "managing editor",
    "news editor",
    "medical editor",
    "scientific editor",
    "technical editor",
    "legal editor",
    "social worker",
    "social scientist",
    "media counsel",
    "media planner",
    "media buyer",
    "insurance producer",
    "agricultural producer",
    "oil producer",
    "gas producer",
    "game producer",
    "video game producer",
    "creative tools engineer",
    "creative recruiter",
    "design recruiter",
    "talent acquisition",
    "creative account manager",
    "creative account executive",
    "creative sales",
)

_NON_POSTING_TITLES = (
    "careers",
    "jobs",
    "open jobs",
    "open positions",
    "open roles",
    "job openings",
    "join our team",
    "join our talent community",
    "talent community",
    "general application",
    "submit your resume",
    "future opportunities",
    "job alerts",
    "stay connected",
)

_AMBIGUOUS_HEADS = frozenset(
    {
        "designer",
        "producer",
        "editor",
        "creator",
        "studio manager",
        "studio lead",
        "design lead",
        "design director",
        "executive producer",
    }
)

_CREATIVE_DEPARTMENTS = (
    "creative",
    "brand creative",
    "content studio",
    "creative studio",
    "marketing design",
    "creative production",
    "brand design",
    "visual design",
)

_NEGATIVE_DEPARTMENTS = (
    "engineering",
    "manufacturing",
    "plant operations",
    "supply chain",
    "legal",
    "medical",
    "clinical",
    "talent acquisition",
    "human resources",
    "sales",
)

_RESPONSIBILITY_GROUPS: dict[str, tuple[str, ...]] = {
    "capture": (
        "video shoot",
        "photo shoot",
        "shoot video",
        "shoot photo",
        "film content",
        "product photography",
        "video production",
    ),
    "post": (
        "edit video",
        "video editing",
        "photo editing",
        "retouch",
        "color grading",
        "post production",
    ),
    "motion_3d": ("motion graphics", "animation", "3d assets", "cgi", "vfx"),
    "campaign": (
        "ad creative",
        "paid social assets",
        "campaign assets",
        "creative variants",
        "asset versioning",
        "creative testing",
        "storyboard",
    ),
    "operations": (
        "production schedule",
        "production pipeline",
        "creative workflow",
        "external agencies",
        "production partners",
    ),
}

_PRODUCTION_PRESSURE_TERMS = (
    "always on",
    "fast paced",
    "high volume",
    "multiple concurrent",
    "multiple projects",
    "rapid turnaround",
    "tight deadline",
    "production pipeline",
    "content pipeline",
    "asset pipeline",
    "creative testing",
    "testing roadmap",
    "ad concepts",
    "creative refresh",
    "asset variations",
    "versioning",
    "localization",
    "launch calendar",
    "content cadence",
    "performance creative",
    "creative iteration",
)

_EXTERNAL_CAPACITY_TERMS = (
    "agency partners",
    "external agencies",
    "external partners",
    "external production partners",
    "outside agencies",
    "third party vendors",
    "contractors",
    "outsourced",
    "outsourcing",
    "white label",
    "overflow",
    "creator network",
    "freelance network",
    "freelancers",
    "production partners",
    "vendor roster",
)

_AI_WORKFLOW_TERMS = (
    "ai generated",
    "creative ai",
    "generative ai",
    "midjourney",
    "stable diffusion",
    "runway",
    "ai video",
    "ai creative tools",
    "generative video",
    "image generation",
    "sora",
    "veo",
    "kling",
    "pika",
    "luma",
    "comfyui",
    "synthetic media",
)

_RIGHTS_RISK_TERMS = (
    "brand safety",
    "copyright",
    "licensing",
    "likeness",
    "model release",
    "rights management",
    "usage rights",
)

_LEADERSHIP_RE = re.compile(
    r"(?:^|\s)(?:chief|director|head of|lead|manager|president|vice president|vp)(?:\s|$)"
)
_CLOSED_STATE_RE = re.compile(
    r"(?:^|\s)(?:closed|archived|expired|filled|inactive|cancelled|canceled)(?:\s|$)"
)
_CARD_MARKER_RE = re.compile(
    r"(?:^|[-_\s])(?:job|career|opening|position|vacancy)[-_\s]"
    r"*(?:card|item|listing|posting|row|result)(?:$|[-_\s])",
    re.I,
)
_JOB_PATH_RE = re.compile(r"(?:^|/)(?:careers?|jobs?|openings?|positions?|vacancies?)(?:/|$)", re.I)
_ACTION_LABEL_RE = re.compile(
    r"\b(?:apply|view (?:job|role|position)|job details|learn more)\b", re.I
)
_NEGATION_RE = re.compile(r"\b(?:no|not|never|without|not responsible for|no longer|avoid)\b", re.I)


def _utc(value: datetime | None = None) -> datetime:
    result = value or datetime.now(UTC)
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def _plain_text(value: Any, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    if value is None:
        return ""
    if isinstance(value, Mapping):
        preferred = [
            value.get(key)
            for key in (
                "name",
                "title",
                "value",
                "description",
                "addressLocality",
                "addressRegion",
                "addressCountry",
            )
            if value.get(key) not in (None, "", [], {})
        ]
        value = preferred or list(value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        text = " ".join(_plain_text(item, limit) for item in value)
    else:
        text = html_lib.unescape(str(value))
        text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def normalize_job_title(value: Any) -> str:
    """Return a conservative, phrase-matchable title representation."""

    text = unicodedata.normalize("NFKC", _plain_text(value, 500)).casefold()
    text = text.replace("&", " and ")
    text = re.sub(r"[\u2010-\u2015_/|,:;()\[\]{}]+", " ", text)
    text = re.sub(r"[^a-z0-9+#\s]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _has_phrase(text: str, phrase: str) -> bool:
    normalized = normalize_job_title(phrase)
    return bool(normalized and f" {normalized} " in f" {text} ")


def _matched(text: str, phrases: Iterable[str]) -> tuple[str, ...]:
    return tuple(phrase for phrase in phrases if _has_phrase(text, phrase))


def _responsibility_matches(description: str) -> dict[str, tuple[str, ...]]:
    matches: dict[str, tuple[str, ...]] = {}
    for family, phrases in _RESPONSIBILITY_GROUPS.items():
        accepted: list[str] = []
        for phrase in phrases:
            normalized = normalize_job_title(phrase)
            start = description.find(normalized)
            if start < 0:
                continue
            prefix = description[max(0, start - 40) : start]
            if _NEGATION_RE.search(prefix):
                continue
            accepted.append(phrase)
        if accepted:
            matches[family] = tuple(accepted)
    return matches


def classify_job_role(
    title: Any,
    *,
    department: Any = "",
    description: Any = "",
) -> JobRoleClassification:
    """Classify a public title using bounded phrases and explicit ambiguity defenses."""

    normalized_title = normalize_job_title(title)
    normalized_department = normalize_job_title(department)
    normalized_description = normalize_job_title(_plain_text(description))
    exclusions = _matched(normalized_title, _EXCLUSION_PHRASES)
    if exclusions:
        return JobRoleClassification(
            role_families=(),
            creative_subfamilies=(),
            creative=False,
            beings_relevant=False,
            photo_video=False,
            growth_marketing=False,
            leadership=bool(_LEADERSHIP_RE.search(f" {normalized_title} ")),
            confidence="none",
            exclusion_reasons=exclusions,
        )

    subfamilies: list[str] = []
    matched_phrases: list[str] = []
    for family, phrases in _CREATIVE_PHRASES.items():
        family_matches = _matched(normalized_title, phrases)
        if family_matches:
            subfamilies.append(family)
            matched_phrases.extend(family_matches)

    description_upgrade = False
    if not subfamilies:
        ambiguous = any(_has_phrase(normalized_title, head) for head in _AMBIGUOUS_HEADS)
        department_supported = bool(_matched(normalized_department, _CREATIVE_DEPARTMENTS))
        department_blocked = bool(_matched(normalized_department, _NEGATIVE_DEPARTMENTS))
        responsibility_matches = _responsibility_matches(normalized_description)
        if (
            ambiguous
            and department_supported
            and not department_blocked
            and len(responsibility_matches) >= 2
        ):
            description_upgrade = True
            matched_phrases.extend(
                phrase for phrases in responsibility_matches.values() for phrase in phrases
            )
            if responsibility_matches.keys() & {"capture", "post"}:
                subfamilies.append("photo_video")
            if "motion_3d" in responsibility_matches:
                subfamilies.append("motion_3d")
            if "campaign" in responsibility_matches:
                subfamilies.append("performance_creative")
            if "operations" in responsibility_matches or not subfamilies:
                subfamilies.append("creative_production_ops")

    growth_matches = _matched(normalized_title, _GROWTH_PHRASES)
    growth = bool(growth_matches)
    matched_phrases.extend(growth_matches)
    leadership = bool(_LEADERSHIP_RE.search(f" {normalized_title} "))
    creative = bool(subfamilies)
    photo_video = bool(set(subfamilies) & {"photo_video", "motion_3d"})
    beings_relevant = bool(set(subfamilies) - {"copy_content"})
    role_families: list[str] = []
    if creative:
        role_families.append("creative")
    if photo_video:
        role_families.append("photo_video_production")
    if growth:
        role_families.append("growth_marketing")
    if leadership:
        role_families.append("leadership")
    confidence: RoleConfidence = "high" if creative or growth else "none"
    if description_upgrade:
        confidence = "medium"
    return JobRoleClassification(
        role_families=tuple(role_families),
        creative_subfamilies=tuple(dict.fromkeys(subfamilies)),
        creative=creative,
        beings_relevant=beings_relevant,
        photo_video=photo_video,
        growth_marketing=growth,
        leadership=leadership,
        confidence=confidence,
        matched_phrases=tuple(dict.fromkeys(matched_phrases)),
        description_upgrade=description_upgrade,
    )


def _field(record: Mapping[str, Any], *paths: str) -> Any:
    for path in paths:
        value: Any = record
        for part in path.split("."):
            if not isinstance(value, Mapping):
                value = None
                break
            value = value.get(part)
        if value not in (None, "", [], {}):
            return value
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1_000
        try:
            return datetime.fromtimestamp(timestamp, tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None
    text = _plain_text(value, 100)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _utc(parsed)


def _canonical_url(raw: Any, base_url: str = "") -> str:
    text = _plain_text(raw, 2_000)
    if not text:
        return ""
    candidate = urljoin(base_url, text)
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    if parsed.username or parsed.password:
        return ""
    host = parsed.hostname.casefold().rstrip(".")
    default_port = (parsed.scheme.casefold() == "https" and port == 443) or (
        parsed.scheme.casefold() == "http" and port == 80
    )
    authority = host if port is None or default_port else f"{host}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parsed.scheme.casefold(), authority, path, parsed.query, ""))


def _provider_id(record: Mapping[str, Any]) -> str:
    value = _field(
        record,
        "id",
        "job_id",
        "jobId",
        "requisitionId",
        "identifier.value",
        "identifier.name",
        "identifier",
    )
    if isinstance(value, Mapping):
        value = value.get("value") or value.get("name") or value.get("propertyID")
    normalized = unicodedata.normalize("NFKC", _plain_text(value, 200)).casefold()
    return re.sub(r"\s+", " ", normalized).strip()


def _infer_provider(source_url: str, declared: str) -> str:
    normalized = normalize_job_title(declared).replace(" ", "_") or "generic"
    host = (urlsplit(source_url).hostname or "").casefold()
    if "greenhouse.io" in host:
        return "greenhouse"
    if "lever.co" in host:
        return "lever"
    if "ashbyhq.com" in host:
        return "ashby"
    return normalized[:50]


def _posting_location(record: Mapping[str, Any]) -> str:
    value = _field(record, "location.name", "location", "categories.location", "jobLocation")
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return ", ".join(dict.fromkeys(_posting_location({"location": item}) for item in value))[
            :500
        ]
    if isinstance(value, Mapping):
        address = value.get("address") if isinstance(value.get("address"), Mapping) else value
        values = [
            _plain_text(address.get(key), 200)
            for key in ("name", "addressLocality", "addressRegion", "addressCountry")
            if address.get(key)
        ]
        return ", ".join(dict.fromkeys(values))[:500]
    return _plain_text(value, 500)


def _posting_department(record: Mapping[str, Any]) -> str:
    return _plain_text(
        _field(
            record,
            "departments",
            "department",
            "categories.department",
            "team",
            "occupationalCategory",
        ),
        500,
    )


def _literal_matches(corpus: str, phrases: Iterable[str]) -> tuple[str, ...]:
    normalized = normalize_job_title(corpus)
    return _matched(normalized, phrases)


_CONTRACT_TERMS = (
    "freelance",
    "freelancer",
    "contractor",
    "independent contractor",
    "contract role",
    "contract position",
    "fixed term",
    "temporary",
    "production partner",
    "white label",
)
_CHANNEL_TERMS = (
    "paid social",
    "tiktok",
    "instagram",
    "youtube",
    "facebook",
    "meta ads",
    "linkedin",
    "email",
    "landing page",
    "amazon",
    "app store",
    "connected tv",
    "display ads",
)
_FORMAT_TERMS = (
    "short form video",
    "long form video",
    "vertical video",
    "motion graphics",
    "animation",
    "product photography",
    "product images",
    "static ads",
    "carousel",
    "ugc",
    "3d",
    "storyboards",
    "social video",
    "9:16",
    "16:9",
    "1:1",
)
_DELIVERABLE_GROUPS = {
    "video production": ("videos", "video", "footage", "reels"),
    "image production": ("product images", "photography", "photos", "images", "retouching"),
    "motion design": ("motion graphics", "animations", "animation"),
    "campaign creative": ("campaign assets", "ad creatives", "ad creative", "static ads"),
    "creative concepts": ("storyboards", "storyboard", "creative concepts", "scripts"),
    "localization": ("localized assets", "localised assets", "translations", "subtitles"),
}
_WORKFLOW_TERMS = {
    "concept": ("concept", "concepts", "storyboard", "storyboards", "ideation"),
    "editing": ("edit", "editing", "retouching", "post production"),
    "adaptation": ("adapt", "resize", "localize", "localise", "localization", "variants"),
    "briefing": ("brief", "briefing", "briefs"),
    "approval": ("approve", "approval", "approvals", "review"),
    "production": ("produce", "create", "deliver", "shoot", "design", "animate"),
}
_ACTION_RE = re.compile(
    r"\b(?:produce|create|deliver|design|edit|adapt|locali[sz]e|develop|storyboard|cut|build|"
    r"own|shoot|animate|retouch|translate|manage)\b",
    re.I,
)
_PAY_RE = re.compile(
    r"(?P<currency>USD|GBP|EUR|CAD|AUD|NZD|CHF|JPY|INR|US\$|CA\$|AU\$|[$£€])\s*"
    r"(?P<minimum>\d[\d,]*(?:\.\d+)?)\s*(?P<min_k>[kK])?"
    r"\s*(?:-|–|—|to)\s*"
    r"(?:(?:USD|GBP|EUR|CAD|AUD|NZD|CHF|JPY|INR|US\$|CA\$|AU\$|[$£€])\s*)?"
    r"(?P<maximum>\d[\d,]*(?:\.\d+)?)\s*(?P<max_k>[kK])?"
    r"(?:\s*(?:per|a|an|/)\s*(?P<period>hour|hr|day|week|month|year|annum))?",
    re.I,
)


def _affirmed_matches(text: str, phrases: Iterable[str]) -> tuple[str, ...]:
    normalized = normalize_job_title(text)
    accepted = []
    for phrase in phrases:
        for match in re.finditer(
            r"\b" + re.escape(normalize_job_title(phrase)) + r"\b", normalized
        ):
            prefix = normalized[max(0, match.start() - 70) : match.start()]
            if not re.search(
                r"\b(?:no|not|without|never|neither|do not|does not|don t|isn t)\b[^.;]{0,55}$",
                prefix,
            ):
                accepted.append(phrase)
                break
    return tuple(accepted)


def _contract_request_signals(
    title: str, description: str, employment_type: Any
) -> tuple[str, ...]:
    """External collaboration is context; the role must explicitly solicit contract work."""
    found = list(_affirmed_matches(title, _CONTRACT_TERMS))
    declared = normalize_job_title(employment_type)
    if declared in {"contract", "contractor", "temporary", "freelance", "fixed term"}:
        found.append(declared)
    for sentence in re.split(r"(?<=[.!?;])\s+|[\n•]", description):
        if not re.search(
            r"\b(?:seeking|looking for|hiring|engage|this is a|this role|this position|work as|"
            r"contract role|contract position|fixed.term|freelance opportunity)\b",
            sentence,
            re.I,
        ):
            continue
        found.extend(_affirmed_matches(sentence, _CONTRACT_TERMS))
    return tuple(dict.fromkeys(found))


def _number(value: Any, divisor: int = 1) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        result = float(str(value).replace(",", "")) / divisor
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and 0 <= result <= 1_000_000_000 else None


def _pay_period(value: Any) -> str:
    normalized = normalize_job_title(value)
    return {
        "hour": "hour",
        "hourly": "hour",
        "hr": "hour",
        "day": "day",
        "daily": "day",
        "week": "week",
        "weekly": "week",
        "month": "month",
        "monthly": "month",
        "year": "year",
        "yearly": "year",
        "annual": "year",
        "annually": "year",
        "annum": "year",
    }.get(normalized, "")


def extract_compensation_ranges(
    record: Mapping[str, Any], *, description: str = "", source_url: str = ""
) -> tuple[PublicCompensationRange, ...]:
    """Preserve separate public ranges and explicit units; ambiguous '$' remains ambiguous."""
    ranges: list[PublicCompensationRange] = []
    for field in (
        "baseSalary",
        "salaryRange",
        "compensation",
        "compensationTierSummary",
        "compensationTier",
        "pay_input_ranges",
        "pay_transparency",
    ):
        raw = record.get(field)
        values = raw if isinstance(raw, list) else [raw]
        for value in values[:10]:
            if not value:
                continue
            if isinstance(value, Mapping):
                nested = value.get("value") if isinstance(value.get("value"), Mapping) else value
                cents = "min_cents" in nested or "max_cents" in nested
                minimum = _number(
                    _field(nested, "min_cents", "minValue", "min", "minimum"), 100 if cents else 1
                )
                maximum = _number(
                    _field(nested, "max_cents", "maxValue", "max", "maximum"), 100 if cents else 1
                )
                if minimum is None and maximum is None:
                    exact = _number(nested.get("value"))
                    minimum = maximum = exact
                currency = _plain_text(
                    _field(value, "currency", "currencyCode", "currency_code", "currency_type"), 20
                ).upper()
                if not re.fullmatch(r"[A-Z]{3}", currency):
                    currency = ""
                period = _pay_period(_field(nested, "unitText", "period", "interval"))
                literal = json.dumps(value, ensure_ascii=False, default=str)[:1_000]
                if minimum is not None or maximum is not None:
                    # Invalid source ranges remain literal but never produce typed numeric claims.
                    if minimum is not None and maximum is not None and minimum > maximum:
                        minimum = maximum = None
                    ranges.append(
                        PublicCompensationRange(
                            minimum=minimum,
                            maximum=maximum,
                            currency=currency,
                            period=period,
                            literal=literal,
                            source_field=field,
                            source_url=source_url,
                        )
                    )
                text = _plain_text(value)
            else:
                text = _plain_text(value)
            ranges.extend(_compensation_in_text(text, source_url, field))
    ranges.extend(_compensation_in_text(description, source_url, "description"))
    unique: dict[tuple[Any, ...], PublicCompensationRange] = {}
    for item in ranges:
        key = (item.minimum, item.maximum, item.currency, item.currency_symbol, item.period)
        unique.setdefault(key, item)
    return tuple(list(unique.values())[:10])


def _compensation_in_text(text: str, source_url: str, field: str) -> list[PublicCompensationRange]:
    results = []
    for match in _PAY_RE.finditer(text[:MAX_DESCRIPTION_CHARS]):
        minimum, maximum = _number(match["minimum"]), _number(match["maximum"])
        if minimum is None or maximum is None:
            continue
        minimum *= 1_000 if match["min_k"] else 1
        maximum *= 1_000 if match["max_k"] else 1
        if minimum > maximum:
            continue
        symbol = match["currency"].upper()
        currency = {"US$": "USD", "CA$": "CAD", "AU$": "AUD", "£": "GBP", "€": "EUR"}.get(
            symbol, symbol if len(symbol) == 3 and symbol.isalpha() else ""
        )
        results.append(
            PublicCompensationRange(
                minimum=minimum,
                maximum=maximum,
                currency=currency,
                currency_symbol=symbol if not symbol.isalpha() else "",
                period=_pay_period(match["period"]),
                literal=match[0],
                source_field=field,
                source_url=source_url,
            )
        )
    return results[:10]


def extract_deliverable_expectations(
    description: str, *, source_url: str = ""
) -> tuple[JobDeliverableExpectation, ...]:
    """Extract cited responsibility sentences, not capabilities or invented production quotas."""
    expectations: list[JobDeliverableExpectation] = []
    sentences = re.split(r"(?<=[.!?;])\s+|[\n•]", description[:MAX_DESCRIPTION_CHARS])
    for sentence in sentences[:150]:
        sentence = sentence.strip()[:800]
        if not _ACTION_RE.search(sentence):
            continue
        if re.search(r"\b(?:no|not|without|never)\b.{0,35}" + _ACTION_RE.pattern, sentence, re.I):
            continue
        normalized = normalize_job_title(sentence)
        deliverables = [
            label for label, terms in _DELIVERABLE_GROUPS.items() if _matched(normalized, terms)
        ]
        if not deliverables:
            continue
        quantity = re.search(
            r"\b(\d{1,3})(?:\s*[-–]\s*(\d{1,3}))?\s+"
            r"(?:\w+\s+){0,3}(?:videos?|assets?|images?|ads?|reels?|concepts?|animations?)"
            r"\s+(?:per|each|a)\s+(day|week|month)\b",
            sentence,
            re.I,
        )
        for deliverable in deliverables[:3]:
            minimum = int(quantity[1]) if quantity else None
            maximum = int(quantity[2] or quantity[1]) if quantity else None
            if minimum is not None and maximum is not None and minimum > maximum:
                minimum = maximum = None
            expectations.append(
                JobDeliverableExpectation(
                    deliverable=deliverable,
                    excerpt=sentence,
                    source_url=source_url,
                    quantity_min=minimum,
                    quantity_max=maximum,
                    period=quantity[3].lower() if quantity else "",
                    channels=_affirmed_matches(sentence, _CHANNEL_TERMS),
                    formats=_affirmed_matches(sentence, _FORMAT_TERMS),
                    workflow_stages=tuple(
                        label
                        for label, terms in _WORKFLOW_TERMS.items()
                        if _matched(normalized, terms)
                    ),
                )
            )
        if len(expectations) >= 12:
            break
    return tuple(expectations[:12])


def merge_job_observation_history(
    current: Sequence[NormalizedJobPosting],
    history: Sequence[NormalizedJobPosting],
    *,
    observed_at: datetime | None = None,
) -> list[NormalizedJobPosting]:
    """Only extend sightings of the same job; missing listings never imply a hire or closure."""
    reference = _utc(observed_at)
    previous: dict[tuple[str, str], list[NormalizedJobPosting]] = {}
    for item in history:
        # Provider IDs may repeat across boards; a matching source host is also required.
        key = (item.stable_id, (urlsplit(item.source_url).hostname or "").casefold())
        previous.setdefault(key, []).append(item)
    result = []
    for item in current:
        key = (item.stable_id, (urlsplit(item.source_url).hostname or "").casefold())
        seen = [
            _utc(date)
            for candidate in [item, *previous.get(key, [])]
            for date in [candidate.first_observed_at, candidate.last_observed_at]
            if date is not None and _utc(date) <= reference
        ]
        result.append(
            replace(item, first_observed_at=min(seen + [reference]), last_observed_at=reference)
        )
    return result


def _stable_id(
    *,
    provider: str,
    provider_id: str,
    source_url: str,
    title: str,
    location: str,
    department: str,
) -> str:
    if provider_id:
        basis = f"provider:{provider}:{provider_id}"
    elif source_url:
        basis = f"url:{source_url}"
    else:
        basis = "fallback:" + "|".join(
            (
                normalize_job_title(title),
                normalize_job_title(location),
                normalize_job_title(department),
            )
        )
    return f"job:{hashlib.sha256(basis.encode()).hexdigest()[:24]}"


def normalize_job_posting(
    record: Mapping[str, Any],
    *,
    provider: str = "generic",
    fallback_url: str = "",
    now: datetime | None = None,
) -> NormalizedJobPosting | None:
    """Normalize a provider record or JSON-LD mapping without assuming it is current."""

    title = _plain_text(_field(record, "title", "text", "name", "jobTitle"), 300)
    normalized_title = normalize_job_title(title)
    if (
        not normalized_title
        or len(normalized_title) < 3
        or len(normalized_title.split()) > 30
        or normalized_title in _NON_POSTING_TITLES
    ):
        return None
    description = _plain_text(
        _field(
            record,
            "content",
            "descriptionPlain",
            "description",
            "descriptionHtml",
            "jobDescription",
        )
    )
    location = _posting_location(record)
    department = _posting_department(record)
    raw_job_url = _field(record, "absolute_url", "hostedUrl", "jobUrl", "url", "applyUrl", "@id")
    job_url = _canonical_url(raw_job_url, fallback_url) if raw_job_url else ""
    source_url = job_url or _canonical_url(fallback_url)
    provider_name = _infer_provider(source_url, provider)
    provider_id = _provider_id(record)
    posted_at = _parse_datetime(
        _field(
            record,
            "datePosted",
            "publishedAt",
            "published_at",
            "createdAt",
        )
    )
    valid_through = _parse_datetime(_field(record, "validThrough", "valid_through"))
    reference = _utc(now)
    state = normalize_job_title(_field(record, "status", "state", "jobStatus", "employmentStatus"))
    current = True
    state_reason = "listed_on_public_surface"
    if state and _CLOSED_STATE_RE.search(f" {state} "):
        current = False
        state_reason = f"explicit_{state.replace(' ', '_')[:80]}"
    if valid_through is not None and valid_through < reference:
        current = False
        state_reason = "past_valid_through"
    classification = classify_job_role(
        title,
        department=department,
        description=description,
    )
    description_hash = hashlib.sha256(description.encode()).hexdigest() if description else ""
    compensation_ranges = extract_compensation_ranges(
        record, description=description, source_url=source_url
    )
    compensation_published = any(
        _field(record, path) not in (None, "", [], {})
        for path in (
            "compensation",
            "compensationTierSummary",
            "salaryRange",
            "compensationTier",
            "baseSalary",
            "pay_input_ranges",
            "pay_transparency",
        )
    ) or bool(compensation_ranges)
    return NormalizedJobPosting(
        stable_id=_stable_id(
            provider=provider_name,
            provider_id=provider_id,
            source_url=job_url,
            title=title,
            location=location,
            department=department,
        ),
        provider=provider_name,
        provider_id=provider_id,
        title=title,
        normalized_title=normalized_title,
        job_url=job_url,
        source_url=source_url,
        source_urls=(source_url,) if source_url else (),
        location=location,
        department=department,
        description_excerpt=description[:MAX_DESCRIPTION_EXCERPT],
        description_hash=description_hash,
        posted_at=posted_at,
        valid_through=valid_through,
        current=current,
        state_reason=state_reason,
        compensation_published=compensation_published,
        classification=classification,
        production_pressure_signals=_literal_matches(description, _PRODUCTION_PRESSURE_TERMS),
        external_capacity_signals=_literal_matches(description, _EXTERNAL_CAPACITY_TERMS),
        ai_workflow_signals=_literal_matches(description, _AI_WORKFLOW_TERMS),
        rights_risk_signals=_literal_matches(description, _RIGHTS_RISK_TERMS),
        compensation_ranges=compensation_ranges,
        deliverable_expectations=extract_deliverable_expectations(
            description, source_url=source_url
        ),
        contract_signals=_contract_request_signals(
            title,
            description,
            _field(
                record, "employmentType", "employment_type", "categories.commitment", "commitment"
            ),
        ),
        channels=_affirmed_matches(description, _CHANNEL_TERMS),
        formats=_affirmed_matches(description, _FORMAT_TERMS),
        first_observed_at=reference,
        last_observed_at=reference,
    )


def _jsonld_nodes(payload: Any) -> Iterable[Mapping[str, Any]]:
    queue: deque[tuple[Any, int]] = deque([(payload, 0)])
    visited = 0
    while queue and visited < MAX_JSONLD_NODES:
        value, depth = queue.popleft()
        visited += 1
        if depth > MAX_JSONLD_DEPTH:
            continue
        if isinstance(value, Mapping):
            yield value
            queue.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            queue.extend((child, depth + 1) for child in value)


def _is_jobposting(node: Mapping[str, Any]) -> bool:
    raw_types = node.get("@type", ())
    types = [raw_types] if isinstance(raw_types, str) else raw_types
    return bool(
        isinstance(types, Sequence)
        and not isinstance(types, (str, bytes, bytearray))
        and any(normalize_job_title(value) == "jobposting" for value in types)
    )


def extract_jsonld_job_postings(
    html: str | bytes,
    page_url: str,
    *,
    now: datetime | None = None,
    max_postings: int = DEFAULT_MAX_POSTINGS,
) -> list[NormalizedJobPosting]:
    """Extract bounded Schema.org JobPosting nodes, including nested @graph values."""

    raw = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html
    soup = BeautifulSoup(raw[:MAX_HTML_CHARS], "html.parser")
    postings: list[NormalizedJobPosting] = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)})[:50]:
        encoded = script.string or script.get_text(" ", strip=True)
        if not encoded or len(encoded) > 1_000_000:
            continue
        try:
            payload = json.loads(encoded)
        except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
            continue
        for node in _jsonld_nodes(payload):
            if not _is_jobposting(node):
                continue
            posting = normalize_job_posting(
                node,
                provider="generic",
                fallback_url=page_url,
                now=now,
            )
            if posting is not None:
                postings.append(posting)
            if len(postings) >= max(0, max_postings):
                return deduplicate_job_postings(postings)
    return deduplicate_job_postings(postings)


def _attrs_text(node: Tag) -> str:
    values: list[str] = []
    for key in (
        "id",
        "class",
        "itemtype",
        "data-job-id",
        "data-job",
        "data-testid",
        "data-qa",
    ):
        value = node.get(key)
        if isinstance(value, Sequence) and not isinstance(value, str):
            values.extend(str(item) for item in value)
        elif value:
            values.append(str(value))
    return " ".join(values)


def _structural_title(card: Tag) -> tuple[str, Tag | None]:
    selectors = (
        "[itemprop='title']",
        "[data-job-title]",
        ".job-title",
        ".position-title",
        ".opening-title",
        "h2",
        "h3",
        "h4",
        "a[href]",
    )
    for selector in selectors:
        node = card.select_one(selector)
        if node is None:
            continue
        title = _plain_text(node.get_text(" ", strip=True), 300)
        normalized = normalize_job_title(title)
        if (
            normalized
            and normalized not in _NON_POSTING_TITLES
            and not _ACTION_LABEL_RE.fullmatch(title.strip())
        ):
            return title, node
    return "", None


def _structural_link(card: Tag, title_node: Tag | None, page_url: str) -> tuple[str, bool]:
    anchors: list[Tag] = []
    if title_node is not None:
        if title_node.name == "a" and title_node.get("href"):
            anchors.append(title_node)
        parent_anchor = title_node.find_parent("a", href=True)
        if parent_anchor is not None:
            anchors.append(parent_anchor)
    anchors.extend(card.find_all("a", href=True, limit=20))
    fallback = ""
    semantic = False
    for anchor in dict.fromkeys(anchors):
        url = _canonical_url(anchor.get("href"), page_url)
        if not url:
            continue
        fallback = fallback or url
        label = anchor.get_text(" ", strip=True)
        if _JOB_PATH_RE.search(urlsplit(url).path) or _ACTION_LABEL_RE.search(label):
            return url, True
    return fallback, semantic


def _structural_value(card: Tag, selectors: Sequence[str]) -> str:
    for selector in selectors:
        node = card.select_one(selector)
        if node is not None:
            value = node.get("datetime") or node.get("content") or node.get_text(" ", strip=True)
            if text := _plain_text(value, 500):
                return text
    return ""


def extract_structural_job_postings(
    html: str | bytes,
    page_url: str,
    *,
    now: datetime | None = None,
    max_postings: int = DEFAULT_MAX_POSTINGS,
) -> list[NormalizedJobPosting]:
    """Extract repeated job cards only when markup supplies explicit posting structure."""

    raw = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html
    soup = BeautifulSoup(raw[:MAX_HTML_CHARS], "html.parser")
    candidates: list[Tag] = []
    for node in soup.find_all(["article", "li", "div"], limit=MAX_STRUCTURAL_CARDS * 4):
        if not isinstance(node, Tag):
            continue
        attrs = _attrs_text(node)
        itemtype_job = "jobposting" in attrs.casefold()
        explicit_id = bool(node.get("data-job-id") or node.get("data-job"))
        card_marker = bool(_CARD_MARKER_RE.search(f" {attrs} "))
        if itemtype_job or explicit_id or card_marker:
            candidates.append(node)
        if len(candidates) >= MAX_STRUCTURAL_CARDS:
            break

    postings: list[NormalizedJobPosting] = []
    for card in candidates:
        title, title_node = _structural_title(card)
        if not title or len(card.get_text(" ", strip=True)) > 5_000:
            continue
        source_url, semantic_link = _structural_link(card, title_node, page_url)
        provider_id = _plain_text(card.get("data-job-id") or card.get("data-job"), 200)
        itemtype_job = "jobposting" in _attrs_text(card).casefold()
        if not (provider_id or itemtype_job or semantic_link):
            continue
        location = _structural_value(
            card,
            ("[itemprop='jobLocation']", ".job-location", ".location", "[data-location]"),
        )
        department = _structural_value(
            card,
            ("[itemprop='occupationalCategory']", ".department", ".team", "[data-team]"),
        )
        posted = _structural_value(
            card,
            ("[itemprop='datePosted']", "time[datetime]", "[data-date-posted]"),
        )
        valid = _structural_value(
            card,
            ("[itemprop='validThrough']", "[data-valid-through]"),
        )
        state = _plain_text(card.get("data-status") or card.get("data-state"), 100)
        record = {
            "id": provider_id,
            "title": title,
            "url": source_url or page_url,
            "location": location,
            "department": department,
            "description": card.get_text(" ", strip=True),
            "datePosted": posted,
            "validThrough": valid,
            "status": state,
        }
        posting = normalize_job_posting(
            record,
            provider="generic",
            fallback_url=page_url,
            now=now,
        )
        if posting is not None:
            postings.append(posting)
        if len(postings) >= max(0, max_postings):
            break
    return deduplicate_job_postings(postings)


def _dedupe_keys(posting: NormalizedJobPosting) -> tuple[str, ...]:
    keys: list[str] = []
    if posting.provider_id:
        keys.append(f"provider:{posting.provider}:{posting.provider_id}")
    if posting.job_url:
        keys.append(f"url:{posting.job_url}")
    if not keys:
        keys.append(
            "fallback:"
            + "|".join(
                (
                    posting.normalized_title,
                    normalize_job_title(posting.location),
                    normalize_job_title(posting.department),
                )
            )
        )
    return tuple(keys)


def _merge_postings(
    current: NormalizedJobPosting, incoming: NormalizedJobPosting
) -> NormalizedJobPosting:
    title = current.title if len(current.title) >= len(incoming.title) else incoming.title
    location = current.location or incoming.location
    department = current.department or incoming.department
    description = (
        current.description_excerpt
        if len(current.description_excerpt) >= len(incoming.description_excerpt)
        else incoming.description_excerpt
    )
    classification = classify_job_role(
        title,
        department=department,
        description=description,
    )
    posted_dates = [value for value in (current.posted_at, incoming.posted_at) if value]
    valid_dates = [value for value in (current.valid_through, incoming.valid_through) if value]
    current_state = current.current and incoming.current
    state_reason = (
        current.state_reason
        if not current.current
        else incoming.state_reason
        if not incoming.current
        else "listed_on_public_surface"
    )
    source_urls = tuple(dict.fromkeys((*current.source_urls, *incoming.source_urls)))
    provider_id = current.provider_id or incoming.provider_id
    provider = current.provider if current.provider_id else incoming.provider
    job_url = current.job_url or incoming.job_url
    source_url = job_url or current.source_url or incoming.source_url
    return replace(
        current,
        stable_id=_stable_id(
            provider=provider,
            provider_id=provider_id,
            source_url=job_url,
            title=title,
            location=location,
            department=department,
        ),
        provider=provider,
        provider_id=provider_id,
        title=title,
        normalized_title=normalize_job_title(title),
        job_url=job_url,
        source_url=source_url,
        source_urls=source_urls,
        location=location,
        department=department,
        description_excerpt=description,
        description_hash=current.description_hash or incoming.description_hash,
        posted_at=min(posted_dates) if posted_dates else None,
        valid_through=min(valid_dates) if valid_dates else None,
        current=current_state,
        state_reason=state_reason,
        compensation_published=(current.compensation_published or incoming.compensation_published),
        classification=classification,
        production_pressure_signals=tuple(
            dict.fromkeys(
                (*current.production_pressure_signals, *incoming.production_pressure_signals)
            )
        ),
        external_capacity_signals=tuple(
            dict.fromkeys((*current.external_capacity_signals, *incoming.external_capacity_signals))
        ),
        ai_workflow_signals=tuple(
            dict.fromkeys((*current.ai_workflow_signals, *incoming.ai_workflow_signals))
        ),
        rights_risk_signals=tuple(
            dict.fromkeys((*current.rights_risk_signals, *incoming.rights_risk_signals))
        ),
        compensation_ranges=tuple(
            dict.fromkeys((*current.compensation_ranges, *incoming.compensation_ranges))
        )[:10],
        deliverable_expectations=tuple(
            dict.fromkeys((*current.deliverable_expectations, *incoming.deliverable_expectations))
        )[:12],
        contract_signals=tuple(
            dict.fromkeys((*current.contract_signals, *incoming.contract_signals))
        ),
        channels=tuple(dict.fromkeys((*current.channels, *incoming.channels))),
        formats=tuple(dict.fromkeys((*current.formats, *incoming.formats))),
        first_observed_at=min(
            (value for value in (current.first_observed_at, incoming.first_observed_at) if value),
            default=None,
        ),
        last_observed_at=max(
            (value for value in (current.last_observed_at, incoming.last_observed_at) if value),
            default=None,
        ),
    )


def deduplicate_job_postings(
    postings: Iterable[NormalizedJobPosting],
) -> list[NormalizedJobPosting]:
    """Collapse provider/API and HTML renderings without equating postings with seats."""

    deduplicated: list[NormalizedJobPosting | None] = []
    key_to_index: dict[str, int] = {}
    for posting in postings:
        keys = _dedupe_keys(posting)
        matching = {key_to_index[key] for key in keys if key in key_to_index}
        if not matching:
            index = len(deduplicated)
            deduplicated.append(posting)
        else:
            index = min(matching)
            target = deduplicated[index]
            if target is None:  # pragma: no cover - key map invariants prevent this
                continue
            for duplicate_index in sorted(matching - {index}):
                duplicate = deduplicated[duplicate_index]
                if duplicate is None:
                    continue
                target = _merge_postings(target, duplicate)
                deduplicated[duplicate_index] = None
                for key, mapped_index in tuple(key_to_index.items()):
                    if mapped_index == duplicate_index:
                        key_to_index[key] = index
            deduplicated[index] = _merge_postings(target, posting)
        for key in keys:
            key_to_index[key] = index
    return [posting for posting in deduplicated if posting is not None]


def extract_job_postings(
    html: str | bytes,
    page_url: str,
    *,
    now: datetime | None = None,
    max_postings: int = DEFAULT_MAX_POSTINGS,
) -> list[NormalizedJobPosting]:
    """Combine JSON-LD and conservative structural extraction under one bounded API."""

    if max_postings <= 0:
        return []
    jsonld = extract_jsonld_job_postings(
        html,
        page_url,
        now=now,
        max_postings=max_postings,
    )
    structural = extract_structural_job_postings(
        html,
        page_url,
        now=now,
        max_postings=max_postings,
    )
    return deduplicate_job_postings([*jsonld, *structural])[:max_postings]


def summarize_job_postings(
    postings: Iterable[NormalizedJobPosting],
    *,
    complete: bool,
    now: datetime | None = None,
) -> HiringDemandSummary:
    """Summarize distinct current postings with source-dated, non-inferred velocity."""

    reference = _utc(now)
    current = [posting for posting in deduplicate_job_postings(postings) if posting.current]
    creative = [posting for posting in current if posting.classification.creative]
    recent_creative = [
        posting
        for posting in creative
        if posting.posted_at is not None
        and reference - timedelta(days=30) <= posting.posted_at <= reference + timedelta(days=1)
    ]
    subfamilies = Counter(family for posting in creative for family in posting.creative_subfamilies)
    share = (
        round(len(creative) / len(current) * 10_000)
        if complete and current
        else 0
        if complete
        else None
    )
    return HiringDemandSummary(
        distinct_open_postings=len(current),
        creative_core_count=len(creative),
        beings_relevant_count=sum(posting.classification.beings_relevant for posting in creative),
        photo_video_count=sum(posting.classification.photo_video for posting in creative),
        growth_marketing_count=sum(posting.classification.growth_marketing for posting in current),
        leadership_count=sum(posting.classification.leadership for posting in current),
        recent_creative_30d=len(recent_creative),
        creative_share_basis_points=share,
        production_context_postings=sum(
            bool(posting.production_pressure_signals) for posting in creative
        ),
        creative_subfamily_counts=tuple(sorted(subfamilies.items())),
        complete=complete,
        counts_are_lower_bounds=not complete,
    )


__all__ = [
    "HiringDemandSummary",
    "JobRoleClassification",
    "NormalizedJobPosting",
    "classify_job_role",
    "deduplicate_job_postings",
    "extract_job_postings",
    "extract_jsonld_job_postings",
    "extract_structural_job_postings",
    "normalize_job_posting",
    "normalize_job_title",
    "summarize_job_postings",
]
