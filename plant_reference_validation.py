"""Validate plant reference search links and record taxonomy checks.

The utility uses RHS Plant Finder search results instead of constructing
unverified plant-page slugs. Network failures are recorded in the JSON report.
"""

import argparse
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlencode

import requests

from plant_data import (
    UNAVAILABLE_BOTANICAL_INFO,
    get_detailed_plant_profile,
    plant_reference_search_query,
)

PROJECT_ROOT = Path(__file__).resolve().parent
RHS_SEARCH_API = "https://lwapp-uks-prod-hortiinfo-01.azurewebsites.net/api/v1/plants/search/advanced"
USER_AGENT = "FloraSense-plant-reference-validator/1.0"
REQUIRED_PROFILE_FIELDS = (
    "common_name",
    "scientific_name",
    "family",
    "short_description",
    "botanical_description",
    "plant_type",
    "native_region",
    "sunlight",
    "watering",
    "soil",
    "temperature",
    "humidity",
    "maintenance",
    "propagation",
    "pruning",
    "pet_safety",
    "reference_sources",
)


def _clean_html(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", value or ""))).strip()


def _normalized_words(value):
    return re.findall(r"[a-z0-9]+", _clean_html(value).casefold())


def _rhs_result_matches(query, hits):
    expected = _normalized_words(query)
    if not expected:
        return []
    matches = []
    for hit in hits:
        botanical = _clean_html(hit.get("botanicalName", ""))
        common = _clean_html(hit.get("commonName", ""))
        botanical_words = _normalized_words(botanical)
        common_words = _normalized_words(common)
        if len(expected) == 1:
            matched = expected[0] in botanical_words or expected[0] in common_words
        else:
            matched = any(
                words[:len(expected)] == expected
                for words in (botanical_words, common_words)
            ) or all(word in botanical_words for word in expected)
        if matched:
            matches.append({
                "id": hit.get("id"),
                "botanical_name": botanical,
                "common_name": common,
                "description": _clean_html(hit.get("entityDescription", "")),
            })
    return matches


def _get_status(session, url, timeout):
    try:
        response = session.get(url, timeout=timeout, allow_redirects=True)
        return {
            "status_code": response.status_code,
            "final_url": response.url,
            "redirects": [
                {"status_code": item.status_code, "url": item.url}
                for item in response.history
            ],
            "error": None,
        }
    except requests.RequestException as error:
        return {
            "status_code": None,
            "final_url": None,
            "redirects": [],
            "error": f"{type(error).__name__}: {error}",
        }


def _taxonomic_query(profile):
    scientific_name = profile.get("scientific_name", "")
    if not scientific_name or scientific_name.startswith("Unavailable:"):
        return ""
    return plant_reference_search_query(
        profile.get("common_name", ""),
        scientific_name,
    )


def _audit_profile(session, labels, timeout):
    plant_label = labels[0]
    profile = get_detailed_plant_profile(plant_label)
    if profile is None:
        return {
            "labels": labels,
            "profile_found": False,
            "references": [],
            "native_region": UNAVAILABLE_BOTANICAL_INFO,
        }

    query = plant_reference_search_query(
        profile["common_name"],
        profile["scientific_name"],
    )
    rhs_url = f"https://www.rhs.org.uk/plants/search-results?{urlencode({'query': query})}"
    rhs_page = _get_status(session, rhs_url, timeout)

    rhs_api = {"status_code": None, "total_hits": None, "matches": [], "error": None}
    try:
        response = session.post(
            RHS_SEARCH_API,
            json={
                "startFrom": 0,
                "pageSize": 20,
                "keywords": query.casefold(),
                "includeAggregation": False,
                "semantic": True,
            },
            headers={
                "Origin": "https://www.rhs.org.uk",
                "Referer": rhs_url,
            },
            timeout=timeout,
        )
        rhs_api["status_code"] = response.status_code
        response.raise_for_status()
        search_data = response.json()
        rhs_api["total_hits"] = search_data.get("totalHit")
        rhs_api["matches"] = _rhs_result_matches(query, search_data.get("hits", []))
    except (requests.RequestException, ValueError) as error:
        rhs_api["error"] = f"{type(error).__name__}: {error}"

    references = []
    if rhs_page["status_code"] == 200:
        exact_matches = rhs_api["matches"]
        reference_label = f"RHS Plant Finder search for {query}"
        if not exact_matches:
            reference_label += " (search fallback)"
        references.append({
            "source": "Royal Horticultural Society Plant Finder",
            "label": reference_label,
            "url": rhs_url,
            "kind": "search",
            "http_status": rhs_page["status_code"],
            "matching_results": len(exact_matches),
            "verified_plant_match": bool(exact_matches),
        })

    scientific_query = _taxonomic_query(profile)
    gbif = {
        "query": scientific_query or None,
        "status_code": None,
        "match_type": None,
        "rank": None,
        "canonical_name": None,
        "accepted_name": None,
        "family": None,
        "confidence": None,
        "species_key": None,
        "native_distribution": [],
        "error": None,
    }
    if scientific_query:
        gbif_url = f"https://api.gbif.org/v1/species/match?{urlencode({'name': scientific_query})}"
        try:
            response = session.get(gbif_url, timeout=timeout, allow_redirects=True)
            gbif["status_code"] = response.status_code
            response.raise_for_status()
            match = response.json()
            gbif.update({
                "match_type": match.get("matchType"),
                "rank": match.get("rank"),
                "canonical_name": match.get("canonicalName"),
                "accepted_name": match.get("scientificName"),
                "family": match.get("family"),
                "confidence": match.get("confidence"),
                "species_key": (
                    match.get("acceptedUsageKey")
                    or match.get("usageKey")
                    or match.get("speciesKey")
                ),
            })
            if gbif["species_key"] and match.get("rank") == "SPECIES":
                distributions = session.get(
                    f"https://api.gbif.org/v1/species/{gbif['species_key']}/distributions",
                    params={"limit": 1000},
                    timeout=timeout,
                )
                distributions.raise_for_status()
                native = [
                    item.get("locality") or item.get("locationId")
                    for item in distributions.json().get("results", [])
                    if str(item.get("establishmentMeans", "")).casefold() == "native"
                ]
                gbif["native_distribution"] = sorted({value for value in native if value})
            if gbif["match_type"] in {"EXACT", "HIGHERRANK"}:
                references.append({
                    "source": "GBIF Backbone Taxonomy",
                    "label": f"GBIF accepted taxon: {gbif['canonical_name']}",
                    "url": gbif_url,
                    "kind": "taxonomy-api",
                    "http_status": gbif["status_code"],
                    "verified_plant_match": True,
                })
        except (requests.RequestException, ValueError) as error:
            gbif["error"] = f"{type(error).__name__}: {error}"

    native_values = gbif["native_distribution"]
    native_region = (
        "GBIF distribution records explicitly marked native: " + "; ".join(native_values)
        if native_values
        else UNAVAILABLE_BOTANICAL_INFO
    )
    local_profile = get_detailed_plant_profile(plant_label) or {}
    missing_profile_fields = [
        field for field in REQUIRED_PROFILE_FIELDS
        if not local_profile.get(field)
    ]
    unavailable_profile_fields = [
        field for field in REQUIRED_PROFILE_FIELDS
        if isinstance(local_profile.get(field), str)
        and local_profile[field].startswith("Unavailable:")
    ]
    return {
        "labels": labels,
        "profile_found": True,
        "common_name": profile["common_name"],
        "scientific_name": profile["scientific_name"],
        "family": profile["family"],
        "rhs_search": {
            **rhs_page,
            "query": query,
            "search_api": rhs_api,
        },
        "gbif_taxonomy": gbif,
        "native_region": native_region,
        "profile_completeness": {
            "required_fields": list(REQUIRED_PROFILE_FIELDS),
            "missing_fields": missing_profile_fields,
            "explicitly_unavailable_fields": unavailable_profile_fields,
        },
        "taxonomy_match_verified": gbif["match_type"] in {"EXACT", "HIGHERRANK"},
        "references": references,
    }


def validate_plant_references(workspace_path=PROJECT_ROOT, output_path=None, timeout=20):
    workspace = Path(workspace_path).resolve()
    mapping_paths = {
        "default_47": workspace / "checkpoints_47" / "class_mapping.json",
        "legacy_19": workspace / "checkpoints" / "class_mapping.json",
    }
    mappings = {}
    grouped_profiles = {}
    for mapping_name, mapping_path in mapping_paths.items():
        with mapping_path.open("r", encoding="utf-8") as mapping_file:
            mapping = json.load(mapping_file)["id2label"]
        labels = [mapping[str(index)] for index in range(len(mapping))]
        mappings[mapping_name] = {"class_count": len(labels), "labels": labels}
        for label in labels:
            profile = get_detailed_plant_profile(label)
            if profile is None:
                key = f"missing:{label.casefold()}"
            else:
                key = "|".join((profile["common_name"].casefold(), profile["scientific_name"].casefold()))
            grouped_profiles.setdefault(key, {"labels": [], "query_label": label})["labels"].append(label)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json, text/html"})
    records = []
    for grouped in grouped_profiles.values():
        labels = list(dict.fromkeys(grouped["labels"]))
        records.append(_audit_profile(session, labels, timeout))

    rhs_ok = sum(record.get("rhs_search", {}).get("status_code") == 200 for record in records)
    rhs_matches = sum(bool(record.get("rhs_search", {}).get("search_api", {}).get("matches")) for record in records)
    gbif_ok = sum(record.get("gbif_taxonomy", {}).get("status_code") == 200 for record in records)
    gbif_matches = sum(record.get("taxonomy_match_verified", False) for record in records)
    unresolved = [record["labels"] for record in records if not record.get("profile_found")]
    missing_fields = [
        {"labels": record["labels"], "fields": record["profile_completeness"]["missing_fields"]}
        for record in records
        if record.get("profile_completeness", {}).get("missing_fields")
    ]
    report = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "workspace": str(workspace),
        "source_policy": "RHS search URLs are used as explicit fallbacks; no RHS plant-page slugs are synthesized.",
        "mappings": mappings,
        "summary": {
            "unique_profiles": len(records),
            "rhs_search_pages_http_200": rhs_ok,
            "rhs_queries_with_matching_results": rhs_matches,
            "gbif_taxonomy_responses_http_200": gbif_ok,
            "gbif_accepted_taxonomy_matches": gbif_matches,
            "gbif_unresolved_or_broad_classes": len(records) - gbif_matches,
            "profiles_missing_from_local_catalog": len(unresolved),
            "profiles_with_missing_required_fields": len(missing_fields),
        },
        "missing_profile_fields": missing_fields,
        "plants": records,
    }
    destination = Path(output_path) if output_path else workspace / "plant_reference_validation_report.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report, destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=str(PROJECT_ROOT))
    parser.add_argument("--output", default=None)
    parser.add_argument("--timeout", type=float, default=20)
    args = parser.parse_args(argv)
    try:
        report, destination = validate_plant_references(args.workspace, args.output, args.timeout)
    except (OSError, ValueError, KeyError) as error:
        print(f"Validation could not complete: {error}", file=sys.stderr)
        return 2
    summary = report["summary"]
    print(f"Plant reference report: {destination}")
    print(
        f"Profiles: {summary['unique_profiles']}; RHS pages HTTP 200: "
        f"{summary['rhs_search_pages_http_200']}; RHS result matches: "
        f"{summary['rhs_queries_with_matching_results']}; GBIF API responses HTTP 200: "
        f"{summary['gbif_taxonomy_responses_http_200']}; accepted taxonomy matches: "
        f"{summary['gbif_accepted_taxonomy_matches']}; missing profiles/field gaps: "
        f"{summary['profiles_missing_from_local_catalog']}/"
        f"{summary['profiles_with_missing_required_fields']}"
    )
    return 0 if (
        summary["profiles_missing_from_local_catalog"] == 0
        and summary["profiles_with_missing_required_fields"] == 0
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
