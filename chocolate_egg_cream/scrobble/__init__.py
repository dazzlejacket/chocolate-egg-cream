"""Where finished tracks get sent."""

from .listenbrainz import ListenBrainz
from .lastfm import LastFM

__all__ = ["LastFM", "ListenBrainz"]
