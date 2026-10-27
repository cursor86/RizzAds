"""Builds the launch matrix from approved creative assets and fails loudly when it is incomplete.

Meta matrix:   3 angles (one body each) x 3 headlines x 3 visuals (all images or all videos).
Google matrix: 3 angles (one description each) x up to 5 headlines x 3 YouTube videos (horizontal, vertical, square).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.errors import DeployValidationError
from app.models import AngleTag, AssetKind, CreativeAsset, Orientation

META_HEADLINE_MAX_CHARS = 40
GOOGLE_HEADLINE_MAX_CHARS = 40
GOOGLE_DESCRIPTION_MAX_CHARS = 90
GOOGLE_MAX_HEADLINES = 5


@dataclass(frozen=True)
class MetaBundle:
    bodies: dict[AngleTag, CreativeAsset]
    headlines: list[CreativeAsset]
    visuals: list[CreativeAsset]


@dataclass(frozen=True)
class GoogleBundle:
    descriptions: dict[AngleTag, CreativeAsset]
    headlines: list[CreativeAsset]
    videos: dict[Orientation, CreativeAsset]


def _first_per_angle(assets: list[CreativeAsset], kind: AssetKind) -> dict[AngleTag, CreativeAsset]:
    chosen: dict[AngleTag, CreativeAsset] = {}
    for asset in assets:
        if asset.kind == kind and asset.angle not in chosen:
            chosen[asset.angle] = asset
    missing = [angle.value for angle in AngleTag if angle not in chosen]
    if missing:
        raise DeployValidationError(f"No approved {kind.value} asset for angle(s): {', '.join(missing)}")
    return chosen


def build_meta_bundle(assets: list[CreativeAsset]) -> MetaBundle:
    bodies = _first_per_angle(assets, AssetKind.BODY)

    headlines = [a for a in assets if a.kind == AssetKind.HEADLINE]
    if len(headlines) < 3:
        raise DeployValidationError(f"Meta launch needs 3 approved headlines, found {len(headlines)}")
    headlines = headlines[:3]
    for headline in headlines:
        if not headline.text or len(headline.text) > META_HEADLINE_MAX_CHARS:
            raise DeployValidationError(f"Headline {headline.id} must be 1-{META_HEADLINE_MAX_CHARS} characters")

    images = [a for a in assets if a.kind == AssetKind.IMAGE]
    videos = [a for a in assets if a.kind == AssetKind.VIDEO]
    if len(images) >= 3:
        visuals = images[:3]
    elif len(videos) >= 3:
        visuals = videos[:3]
    else:
        raise DeployValidationError(
            f"Meta launch needs 3 approved visuals of one type, found {len(images)} images and {len(videos)} videos"
        )
    for visual in visuals:
        if not visual.media_url:
            raise DeployValidationError(f"Visual {visual.id} has no media_url to upload from")

    return MetaBundle(bodies=bodies, headlines=headlines, visuals=visuals)


def build_google_bundle(assets: list[CreativeAsset]) -> GoogleBundle:
    descriptions = _first_per_angle(assets, AssetKind.BODY)
    for description in descriptions.values():
        if not description.text or len(description.text) > GOOGLE_DESCRIPTION_MAX_CHARS:
            raise DeployValidationError(
                f"Body asset {description.id} must be 1-{GOOGLE_DESCRIPTION_MAX_CHARS} characters for Google"
            )

    headlines = [a for a in assets if a.kind == AssetKind.HEADLINE][:GOOGLE_MAX_HEADLINES]
    if not headlines:
        raise DeployValidationError("Google launch needs at least 1 approved headline")
    for headline in headlines:
        if not headline.text or len(headline.text) > GOOGLE_HEADLINE_MAX_CHARS:
            raise DeployValidationError(f"Headline {headline.id} must be 1-{GOOGLE_HEADLINE_MAX_CHARS} characters")

    videos: dict[Orientation, CreativeAsset] = {}
    for asset in assets:
        if asset.kind == AssetKind.VIDEO and asset.orientation and asset.google_youtube_video_id:
            videos.setdefault(asset.orientation, asset)
    missing = [o.value for o in Orientation if o not in videos]
    if missing:
        raise DeployValidationError(
            f"Google launch needs an approved YouTube video for each orientation; missing: {', '.join(missing)}"
        )

    return GoogleBundle(descriptions=descriptions, headlines=headlines, videos=videos)
