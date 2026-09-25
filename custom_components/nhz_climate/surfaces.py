"""Validated, persisted geometry for the S02 solar-surface model.

The model deliberately uses Open-Meteo's azimuth convention: zero points
south, negative angles point east, and positive angles point west.  Surface
IDs are configuration identities, not display names; callers must preserve an
ID when a user renames or edits a surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math
import re
from typing import Any, Mapping, Sequence


SURFACE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MIN_TILT_DEGREES = 0.0
MAX_TILT_DEGREES = 90.0
MIN_AZIMUTH_DEGREES = -180.0
MAX_AZIMUTH_DEGREES = 180.0


class SurfaceValidationError(ValueError):
    """A configured surface is incomplete or physically inconsistent."""


class SurfaceType(StrEnum):
    """Surface kinds supported by the initial unshaded solar model."""

    FACADE = "facade"
    ROOF = "roof"
    PV = "pv"


def _finite_number(value: Any, field: str) -> float:
    """Coerce a JSON config number without accepting booleans or NaN."""
    if isinstance(value, bool):
        raise SurfaceValidationError(f"{field} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise SurfaceValidationError(f"{field} must be a finite number") from exc
    if not math.isfinite(result):
        raise SurfaceValidationError(f"{field} must be a finite number")
    return result


def _required(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    labels = "/".join(keys)
    raise SurfaceValidationError(f"missing required field: {labels}")


@dataclass(frozen=True, slots=True)
class Surface:
    """One physical plane used for GTI and later incident-power calculation.

    ``gross_area_m2`` is the complete physical plane.  A facade records its
    included glazing separately in ``window_area_m2``; this is intentionally
    not subtracted here because GTI applies to the whole plane and S03 decides
    which area belongs to a downstream calculation.
    """

    surface_id: str
    surface_type: SurfaceType
    name: str
    tilt_deg: float
    azimuth_deg: float
    gross_area_m2: float
    window_area_m2: float = 0.0
    other_opening_area_m2: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.surface_id, str) or not SURFACE_ID_PATTERN.fullmatch(
            self.surface_id
        ):
            raise SurfaceValidationError(
                "surface_id must be a stable lower-case slug (1-64 characters)"
            )
        if not isinstance(self.surface_type, SurfaceType):
            try:
                object.__setattr__(self, "surface_type", SurfaceType(self.surface_type))
            except (TypeError, ValueError) as exc:
                allowed = ", ".join(member.value for member in SurfaceType)
                raise SurfaceValidationError(
                    f"surface_type must be one of: {allowed}"
                ) from exc
        if not isinstance(self.name, str) or not self.name.strip():
            raise SurfaceValidationError("name must not be empty")
        if len(self.name.strip()) > 120:
            raise SurfaceValidationError("name must not exceed 120 characters")

        for field in (
            "tilt_deg",
            "azimuth_deg",
            "gross_area_m2",
            "window_area_m2",
            "other_opening_area_m2",
        ):
            value = _finite_number(getattr(self, field), field)
            object.__setattr__(self, field, value)

        if not MIN_TILT_DEGREES <= self.tilt_deg <= MAX_TILT_DEGREES:
            raise SurfaceValidationError(
                f"tilt_deg must be between {MIN_TILT_DEGREES:g} and "
                f"{MAX_TILT_DEGREES:g} degrees"
            )
        if not MIN_AZIMUTH_DEGREES <= self.azimuth_deg <= MAX_AZIMUTH_DEGREES:
            raise SurfaceValidationError(
                "azimuth_deg must use Open-Meteo south-relative convention "
                "and be between -180 and 180 degrees"
            )
        # A zero-area placeholder is a valid, inert configuration during a
        # staged site setup. S03 must map it to exactly zero W/kWh. Negative
        # physical area remains invalid.
        if self.gross_area_m2 < 0:
            raise SurfaceValidationError("gross_area_m2 must not be negative")
        if not 0 <= self.window_area_m2 <= self.gross_area_m2:
            raise SurfaceValidationError(
                "window_area_m2 must be between zero and gross_area_m2"
            )
        if self.window_area_m2 + self.other_opening_area_m2 > self.gross_area_m2:
            raise SurfaceValidationError(
                "window_area_m2 plus other_opening_area_m2 must not exceed gross_area_m2"
            )
        if self.surface_type is not SurfaceType.FACADE and (
            self.window_area_m2 or self.other_opening_area_m2
        ):
            raise SurfaceValidationError(
                "window_area_m2 and other_opening_area_m2 are only applicable to a facade surface"
            )

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "Surface":
        """Build a surface from config-entry JSON, accepting legacy short keys.

        The serialised form emitted by :meth:`as_dict` is canonical.  The
        short aliases make this domain object usable by a config-flow form
        without coupling that form to this module's storage spelling.
        """
        if not isinstance(data, Mapping):
            raise SurfaceValidationError("surface configuration must be an object")
        raw_type = _required(data, "surface_type", "type")
        try:
            surface_type = SurfaceType(raw_type)
        except (TypeError, ValueError) as exc:
            allowed = ", ".join(member.value for member in SurfaceType)
            raise SurfaceValidationError(
                f"surface_type must be one of: {allowed}"
            ) from exc

        window_keys = ("window_area_m2", "window_area")
        other_opening_keys = ("other_opening_area_m2", "other_opening_area")
        if surface_type is SurfaceType.FACADE:
            window_area = _required(data, *window_keys)
        else:
            # A canonical non-facade has no window field.  Accept an explicit
            # zero during transition but reject a meaningful incompatible area.
            window_area = next((data[key] for key in window_keys if key in data), 0.0)
        other_opening_area = (
            next((data[key] for key in other_opening_keys if key in data), 0.0)
            if surface_type is SurfaceType.FACADE
            else 0.0
        )

        return cls(
            surface_id=_required(data, "surface_id", "id"),
            surface_type=surface_type,
            name=_required(data, "name"),
            tilt_deg=_required(data, "tilt_deg", "tilt"),
            azimuth_deg=_required(data, "azimuth_deg", "azimuth"),
            gross_area_m2=_required(data, "gross_area_m2", "gross_area", "area_m2"),
            window_area_m2=window_area,
            other_opening_area_m2=other_opening_area,
        )

    def as_dict(self) -> dict[str, str | float]:
        """Return the stable config-entry representation for this surface."""
        result: dict[str, str | float] = {
            "surface_id": self.surface_id,
            "surface_type": self.surface_type.value,
            "name": self.name.strip(),
            "tilt_deg": self.tilt_deg,
            "azimuth_deg": self.azimuth_deg,
            "gross_area_m2": self.gross_area_m2,
        }
        if self.surface_type is SurfaceType.FACADE:
            result["window_area_m2"] = self.window_area_m2
            result["other_opening_area_m2"] = self.other_opening_area_m2
        return result


def validate_surface_collection(values: Sequence[Surface | Mapping[str, Any]]) -> tuple[Surface, ...]:
    """Parse surfaces and reject duplicate persistent IDs before saving config."""
    surfaces = tuple(
        value if isinstance(value, Surface) else Surface.from_mapping(value)
        for value in values
    )
    ids = [surface.surface_id for surface in surfaces]
    if len(ids) != len(set(ids)):
        duplicates = sorted({surface_id for surface_id in ids if ids.count(surface_id) > 1})
        raise SurfaceValidationError(
            f"surface_id values must be unique: {', '.join(duplicates)}"
        )
    return surfaces
