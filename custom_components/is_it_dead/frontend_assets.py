"""Keep HTTP content negotiation from selecting assets left by older installs."""
from pathlib import Path


def remove_legacy_compressed_panel(frontend_path: str) -> None:
    """Remove obsolete sidecars; this integration ships the canonical JS only.

    aiohttp prefers a .gz/.br sibling when a browser requests compression, even
    when that sibling predates the JS. Old manual installations left such files
    behind. Limit cleanup to our own panel asset, before registering HTTP routes.
    """
    for suffix in (".gz", ".br"):
        (Path(frontend_path) / ("is_it_dead_panel.js" + suffix)).unlink(missing_ok=True)
