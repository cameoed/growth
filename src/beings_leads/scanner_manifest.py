"""Validate exact company/scanner work lists for bounded internal scan batches."""
from .models import FRONTIER_SCANNER_IDS


def validate_manifest(value, hosts, *, reuse_core_only, respect_schedule, maximum=500):
    if not value:
        return {}
    if not isinstance(value, dict) or not reuse_core_only or not respect_schedule:
        raise ValueError('Scanner manifests require core reuse and scheduled freshness checks')
    allowed_hosts = {str(host).strip().lower().rstrip('.') for host in (hosts or [])}
    result = {}
    for raw_host, raw_ids in value.items():
        host = str(raw_host).strip().lower().rstrip('.')
        if host not in allowed_hosts or host in result or not isinstance(raw_ids, list):
            raise ValueError('Scanner manifest hosts must match explicit company hosts')
        if any(not isinstance(item, str) for item in raw_ids):
            raise ValueError('Scanner manifest IDs must be strings')
        ids = list(dict.fromkeys(item.strip() for item in raw_ids))
        if not ids or set(ids) - set(FRONTIER_SCANNER_IDS):
            raise ValueError('Scanner manifest contains empty or unknown scanners')
        result[host] = ids
    if (set(result) != allowed_hosts or len(result) > 25
            or sum(map(len, result.values())) > min(500, maximum)):
        raise ValueError('Scanner manifest requires every host, at most 25 companies and 500 pairs')
    return result
