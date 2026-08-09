"""Verisure OWA camera platform."""

import asyncio
import base64
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import DOMAIN, SIGNAL_CAMERA_STATE, VerisureHub
from .coordinators import CameraCoordinator
from .entity import camera_device_info
from .events import inject_ha_event
from .verisure_owa_api import Installation
from .verisure_owa_api.exceptions import VerisureOwaError
from .verisure_owa_api.models import ActivityCategory, CameraDevice

_LOGGER = logging.getLogger(__name__)

# Why `async_camera_image` is serving the placeholder, exposed as the
# `image_status` attribute.
IMAGE_STATUS_OK = "ok"
IMAGE_STATUS_NO_DATA = "no_data"  # coordinator hasn't fetched yet
IMAGE_STATUS_NO_FRAME = "no_frame"  # nothing held for this zone
IMAGE_STATUS_DECODE_FAILED = "decode_failed"  # base64 rejected
IMAGE_STATUS_NOT_JPEG = "not_jpeg"  # decoded, but not a JPEG

_PANEL_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

_PLACEHOLDER_IMAGE_PATH = Path(__file__).parent / "placeholder.jpg"
# Cache for the placeholder JPEG bytes — populated on first access via the
# event loop's executor to avoid sync file I/O during integration startup.
_PLACEHOLDER_IMAGE: bytes | None = None
_placeholder_lock: asyncio.Lock | None = None


def _normalise_panel_timestamp(value: str | None) -> str | None:
    """Return the panel's timestamp as ISO 8601 with an explicit offset.

    The panel sends naive ``YYYY-MM-DD HH:MM:SS`` in the installation's local
    time. Passing that through verbatim pushed the parsing onto every consumer
    — and browsers disagree about it: Chrome reads it as local time while
    Safari has historically rejected it outright, leaving the card to print
    the raw string. Stamping HA's timezone on it makes the value unambiguous
    and parseable everywhere.

    Anything that doesn't match the panel format is returned untouched rather
    than dropped, so an unexpected shape stays visible.
    """
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, _PANEL_TIME_FORMAT)
    except ValueError:
        return value
    return parsed.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE).isoformat()


async def _get_placeholder_image(hass: HomeAssistant) -> bytes:
    """Return the placeholder JPEG, reading the file once via the executor."""
    global _PLACEHOLDER_IMAGE, _placeholder_lock  # pylint: disable=global-statement
    cached = _PLACEHOLDER_IMAGE
    if cached is not None:
        return cached
    if _placeholder_lock is None:
        _placeholder_lock = asyncio.Lock()
    async with _placeholder_lock:
        if _PLACEHOLDER_IMAGE is None:
            loaded: bytes = await hass.async_add_executor_job(
                _PLACEHOLDER_IMAGE_PATH.read_bytes
            )
            _PLACEHOLDER_IMAGE = loaded
            return loaded
        return _PLACEHOLDER_IMAGE


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Verisure OWA camera entities.

    No API calls are made here.  Camera devices are discovered
    asynchronously after startup and added via the stored callback.
    The verisure_owa.capture_image entity service is registered
    globally in __init__.py via register_v5_entity_services — it
    dispatches to this platform's entities' async_manual_capture method.
    """
    entry_data = hass.data[DOMAIN][entry.entry_id]
    entry_data["camera_add_entities"] = async_add_entities


class VerisureCamera(CoordinatorEntity[CameraCoordinator], Camera):
    """A Verisure OWA camera entity.

    Subclass-controlled mode: `_mode = "thumbnail"` reads
    `coordinator.data.thumbnails` and exposes the `capturing` state attribute
    plus a SIGNAL_CAMERA_STATE listener; `_mode = "full"` reads
    `coordinator.data.full_images` instead.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _mode: str = "thumbnail"

    def __init__(
        self,
        coordinator: CameraCoordinator,
        hub: VerisureHub,
        installation: Installation,
        camera_device: CameraDevice,
    ) -> None:
        """Initialize the camera entity."""
        super().__init__(coordinator)
        Camera.__init__(self)
        self._client = hub
        self._installation = installation
        self._camera_device = camera_device
        self._zone_id = camera_device.zone_id
        suffix = "_full" if self._mode == "full" else ""
        self._attr_unique_id = (
            f"v4_securitas_direct.{installation.number}"
            f"_camera{suffix}_{camera_device.zone_id}"
        )
        if self._mode == "full":
            self._attr_name = "Full Image"
        self._attr_device_info = camera_device_info(
            installation, camera_device, hub.hass
        )
        self._frame_signature: tuple[Any, ...] = ()

    def _resolve_image(self) -> tuple[bytes | None, str]:
        """Return the current frame, and why it is or is not available.

        Four different conditions all end at the same placeholder JPEG, which
        left a user unable to tell a broken camera from one that simply has
        not captured recently.  The reason is surfaced as ``image_status`` so
        the difference is visible without reading the debug log.
        """
        data = self.coordinator.data
        if data is None:
            return None, IMAGE_STATUS_NO_DATA
        if self._mode == "full":
            image = data.full_images.get(self._zone_id)
            if image is None:
                return None, IMAGE_STATUS_NO_FRAME
            return image, IMAGE_STATUS_OK
        thumb = data.thumbnails.get(self._zone_id)
        if thumb is None or not thumb.image:
            return None, IMAGE_STATUS_NO_FRAME
        try:
            image_bytes = base64.b64decode(thumb.image)
        except (ValueError, TypeError):
            return None, IMAGE_STATUS_DECODE_FAILED
        if not image_bytes.startswith(b"\xff\xd8"):
            return None, IMAGE_STATUS_NOT_JPEG
        return image_bytes, IMAGE_STATUS_OK

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the relevant image for this mode, or a placeholder."""
        image, _status = self._resolve_image()
        if image is None:
            return await _get_placeholder_image(self.hass)
        return image

    @property
    def extra_state_attributes(self) -> dict[str, Any]:  # type: ignore[override]
        """Return extra state attributes.

        In full mode the timestamp is reported only when a full image is
        actually held.  The frontend card uses its presence as the test for
        "this camera has a full-resolution view" — reporting the thumbnail's
        timestamp regardless made it offer a view that could only ever render
        the placeholder, which is what a camera with no recent capture always
        has.  The value itself stays the thumbnail's: the full image is fetched
        from that frame's ``id_signal``, so they describe the same moment.
        """
        data = self.coordinator.data
        timestamp: str | None = None
        if data is not None and (
            self._mode != "full" or self._zone_id in data.full_images
        ):
            thumb = data.thumbnails.get(self._zone_id)
            if thumb is not None:
                timestamp = thumb.timestamp
        attrs: dict[str, Any] = {
            "image_timestamp": _normalise_panel_timestamp(timestamp),
            "image_status": self._resolve_image()[1],
        }
        if self._mode == "thumbnail":
            attrs["capturing"] = self._client.is_capturing(
                self._installation.number, self._camera_device.zone_id
            )
        return attrs

    async def async_manual_capture(self) -> None:
        """Request a new image capture and inject the activity event.

        Backs both the `verisure_owa.capture_image` entity service and the
        deprecated VerisureCaptureButton.  Errors from the hub layer are
        swallowed (already logged there) — we just skip the event injection.
        """
        try:
            _, thumbnail = await self._client.capture_image(
                self._installation, self._camera_device
            )
        except VerisureOwaError as err:
            _LOGGER.warning(
                "Failed to capture image from %s: %s",
                self._camera_device.name,
                err,
            )
            return
        id_signal = thumbnail.id_signal if thumbnail else None
        signal_type = thumbnail.signal_type if thumbnail else None
        await inject_ha_event(
            self.hass,
            self._installation,
            category=ActivityCategory.IMAGE_REQUEST,
            alias="Image request",
            device=self._camera_device.zone_id,
            device_name=self._camera_device.name,
            context=self._context,
            id_signal=id_signal,
            signal_type=signal_type,
        )

    def _current_frame_signature(self) -> tuple[Any, ...]:
        """Identify the frame this entity would serve right now."""
        data = self.coordinator.data
        if data is None:
            return ()
        thumb = data.thumbnails.get(self._zone_id)
        source = (thumb.id_signal, thumb.timestamp) if thumb else (None, None)
        if self._mode == "full":
            image = data.full_images.get(self._zone_id)
            return (*source, len(image) if image else 0)
        return source

    @callback
    def _handle_coordinator_update(self) -> None:
        """Rotate the access token only when this entity's frame really changed.

        Rotating makes the frontend re-fetch, which is what we want on a new
        frame — but HA keeps only two access tokens per camera (a deque of
        maxlen 2), so every rotation invalidates the one before last.

        A capture pushes two coordinator updates in quick succession: the fresh
        thumbnail first, then the full image its background task fetches. When
        both entities rotated on both updates, the token an already-open
        more-info dialog was streaming with fell out of the deque and HA
        rejected the stream with "401 Signature expired" — the full-image
        entity worst of all, since the second update is the one it causes.
        """
        signature = self._current_frame_signature()
        if signature != self._frame_signature:
            self._frame_signature = signature
            self.async_update_token()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Subscribe to camera state signal (thumbnail mode only)."""
        await super().async_added_to_hass()
        if self._mode == "thumbnail":
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass, SIGNAL_CAMERA_STATE, self._handle_state
                )
            )

    @callback
    def _handle_state(self, installation_number: str, zone_id: str) -> None:
        """Handle capturing state change — write state without rotating token."""
        if (
            installation_number != self._installation.number
            or zone_id != self._camera_device.zone_id
        ):
            return
        self.async_write_ha_state()


class VerisureCameraFull(VerisureCamera):
    """Full-resolution variant of VerisureCamera."""

    _mode = "full"
