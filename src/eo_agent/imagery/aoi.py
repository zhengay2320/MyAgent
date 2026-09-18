from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from hashlib import sha256
from itertools import pairwise
from numbers import Real
from typing import Any

from eo_agent.imagery.schemas import (
    AOIRef,
    AOISource,
    MultiPolygonGeometry,
    PolygonGeometry,
)

EARTH_RADIUS_M = 6_371_008.8
DEFAULT_RECOMMENDED_MIN_KM2 = 25.0
DEFAULT_RECOMMENDED_MAX_KM2 = 100.0

Position = tuple[float, float]
Ring = list[Position]
PolygonCoordinates = list[Ring]
MultiPolygonCoordinates = list[PolygonCoordinates]


class AOIValidationError(ValueError):
    """Raised when supplied GeoJSON cannot safely define an AOI."""


def normalize_geojson(
    geojson: Mapping[str, Any] | str,
    *,
    aoi_id: str | None = None,
    name: str | None = None,
    source: AOISource | str = AOISource.PASTED,
    version: int = 1,
    max_vertices: int = 10_000,
    max_ring_vertices: int = 5_000,
    max_features: int = 100,
    recommended_min_area_km2: float = DEFAULT_RECOMMENDED_MIN_KM2,
    recommended_max_area_km2: float = DEFAULT_RECOMMENDED_MAX_KM2,
    hard_max_area_km2: float | None = None,
) -> AOIRef:
    """Validate and canonicalize a WGS84 Polygon/MultiPolygon GeoJSON AOI.

    This implementation intentionally has no Shapely/pyproj dependency so that the
    offline UI can validate its small trial AOIs. It rejects self-intersections and
    overlapping MultiPolygon members, computes geodesic-like spherical area itself,
    and never asks an LLM to supply area or bounds.
    """

    if isinstance(geojson, str):
        try:
            document = json.loads(geojson)
        except json.JSONDecodeError as exc:
            raise AOIValidationError(f"GeoJSON 不是有效 JSON：{exc.msg}") from exc
    elif isinstance(geojson, Mapping):
        document = dict(geojson)
    else:
        raise AOIValidationError("AOI 必须是 GeoJSON 对象或 JSON 字符串")

    if not isinstance(document, dict):
        raise AOIValidationError("GeoJSON 顶层必须是对象")
    _validate_crs(document)
    if version < 1:
        raise AOIValidationError("AOI version 必须大于等于 1")
    if max_vertices < 4 or max_ring_vertices < 4 or max_features < 1:
        raise AOIValidationError("AOI 复杂度限制配置无效")
    if recommended_min_area_km2 < 0:
        raise AOIValidationError("建议最小面积不能为负")
    if recommended_max_area_km2 <= recommended_min_area_km2:
        raise AOIValidationError("建议面积上限必须大于下限")

    warnings: list[str] = []
    raw_geometries, inferred_name = _extract_geometries(document, max_features=max_features)
    polygons: MultiPolygonCoordinates = []
    for geometry_index, raw_geometry in enumerate(raw_geometries):
        geometry_type = raw_geometry.get("type")
        raw_coordinates = raw_geometry.get("coordinates")
        if geometry_type == "Polygon":
            polygons.append(
                _normalize_polygon(
                    raw_coordinates,
                    path=f"geometry[{geometry_index}]",
                    warnings=warnings,
                    max_ring_vertices=max_ring_vertices,
                )
            )
        elif geometry_type == "MultiPolygon":
            if not _is_sequence(raw_coordinates) or not raw_coordinates:
                raise AOIValidationError(f"geometry[{geometry_index}] MultiPolygon 为空")
            for polygon_index, raw_polygon in enumerate(raw_coordinates):
                polygons.append(
                    _normalize_polygon(
                        raw_polygon,
                        path=f"geometry[{geometry_index}].polygon[{polygon_index}]",
                        warnings=warnings,
                        max_ring_vertices=max_ring_vertices,
                    )
                )
        else:
            raise AOIValidationError("仅支持 Polygon 或 MultiPolygon，不接受包络框代替几何")

    if not polygons:
        raise AOIValidationError("AOI 几何为空")
    vertex_count = sum(len(ring) for polygon in polygons for ring in polygon)
    if vertex_count > max_vertices:
        raise AOIValidationError(
            f"AOI 顶点数 {vertex_count} 超过复杂度上限 {max_vertices}，请简化几何"
        )
    _validate_multipolygon_relationships(polygons)

    area_m2 = sum(_polygon_area_m2(polygon) for polygon in polygons)
    area_km2 = area_m2 / 1_000_000
    if not math.isfinite(area_km2) or area_km2 <= 1e-8:
        raise AOIValidationError("AOI 面积为空或退化，无法用于检索")
    if hard_max_area_km2 is not None and area_km2 > hard_max_area_km2:
        raise AOIValidationError(
            f"AOI 面积 {area_km2:.3f} km² 超过配置上限 {hard_max_area_km2:.3f} km²"
        )

    within_recommended = recommended_min_area_km2 <= area_km2 <= recommended_max_area_km2
    if area_km2 < recommended_min_area_km2:
        warnings.append(
            f"AOI 面积 {area_km2:.3f} km² 小于建议试验范围 "
            f"{recommended_min_area_km2:g}–{recommended_max_area_km2:g} km²"
        )
    elif area_km2 > recommended_max_area_km2:
        warnings.append(
            f"AOI 面积 {area_km2:.3f} km² 超过普通模式建议上限 "
            f"{recommended_max_area_km2:g} km²；开始检索前应由用户调整或明确允许"
        )

    canonical_geometry: dict[str, Any]
    if len(polygons) == 1:
        canonical_geometry = {"type": "Polygon", "coordinates": polygons[0]}
        geometry_model = PolygonGeometry.model_validate(canonical_geometry)
    else:
        canonical_geometry = {"type": "MultiPolygon", "coordinates": polygons}
        geometry_model = MultiPolygonGeometry.model_validate(canonical_geometry)
        if len(raw_geometries) > 1:
            warnings.append(
                f"FeatureCollection 的 {len(raw_geometries)} 个对象已作为一个 MultiPolygon AOI；"
                "未执行 dissolve"
            )

    canonical = json.dumps(
        canonical_geometry,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    geometry_hash = sha256(canonical.encode("utf-8")).hexdigest()
    bbox = _bbox(polygons)
    parsed_source = AOISource(source)
    is_synthetic = parsed_source == AOISource.SYNTHETIC_MOCK
    if is_synthetic:
        warnings.append("该 AOI 是合成测试几何，并非行政边界，不得用于真实 GEE 请求")

    return AOIRef(
        aoi_id=aoi_id or f"aoi_{geometry_hash[:16]}",
        version=version,
        name=name or inferred_name or "用户研究区",
        geometry=geometry_model,
        bbox=bbox,
        area_km2=area_km2,
        vertex_count=vertex_count,
        geometry_hash=geometry_hash,
        source=parsed_source,
        is_synthetic=is_synthetic,
        within_recommended_area=within_recommended,
        requires_area_adjustment=area_km2 > recommended_max_area_km2,
        warnings=_deduplicate(warnings),
    )


def geometry_as_geojson(aoi: AOIRef) -> dict[str, Any]:
    """Return a provider-safe WGS84 geometry mapping without AOI metadata."""

    return aoi.geometry.model_dump(mode="json")


def _validate_crs(document: Mapping[str, Any]) -> None:
    crs = document.get("crs")
    if crs is None:
        return
    if not isinstance(crs, Mapping):
        raise AOIValidationError("GeoJSON crs 字段格式无效")
    properties = crs.get("properties")
    crs_name = properties.get("name") if isinstance(properties, Mapping) else None
    allowed = {
        "EPSG:4326",
        "urn:ogc:def:crs:EPSG::4326",
        "urn:ogc:def:crs:OGC:1.3:CRS84",
        "OGC:CRS84",
    }
    if crs_name not in allowed:
        raise AOIValidationError("仅接受 WGS84 (EPSG:4326/CRS84) GeoJSON")


def _extract_geometries(
    document: dict[str, Any],
    *,
    max_features: int,
) -> tuple[list[dict[str, Any]], str | None]:
    kind = document.get("type")
    inferred_name: str | None = None
    if kind == "Feature":
        geometry = document.get("geometry")
        if not isinstance(geometry, Mapping):
            raise AOIValidationError("Feature 缺少非空 geometry")
        properties = document.get("properties")
        if isinstance(properties, Mapping) and isinstance(properties.get("name"), str):
            inferred_name = properties["name"][:200]
        return [dict(geometry)], inferred_name
    if kind == "FeatureCollection":
        features = document.get("features")
        if not _is_sequence(features) or not features:
            raise AOIValidationError("FeatureCollection 必须包含至少一个 Feature")
        if len(features) > max_features:
            raise AOIValidationError(
                f"FeatureCollection 对象数 {len(features)} 超过上限 {max_features}"
            )
        geometries: list[dict[str, Any]] = []
        for index, feature in enumerate(features):
            if not isinstance(feature, Mapping) or feature.get("type") != "Feature":
                raise AOIValidationError(f"features[{index}] 不是有效 Feature")
            geometry = feature.get("geometry")
            if not isinstance(geometry, Mapping):
                raise AOIValidationError(f"features[{index}] 缺少非空 geometry")
            geometries.append(dict(geometry))
        return geometries, None
    if kind in {"Polygon", "MultiPolygon"}:
        return [document], None
    raise AOIValidationError("仅支持 Polygon、MultiPolygon、Feature 或 FeatureCollection")


def _normalize_polygon(
    raw_polygon: Any,
    *,
    path: str,
    warnings: list[str],
    max_ring_vertices: int,
) -> PolygonCoordinates:
    if not _is_sequence(raw_polygon) or not raw_polygon:
        raise AOIValidationError(f"{path} Polygon 为空")
    rings: PolygonCoordinates = []
    for ring_index, raw_ring in enumerate(raw_polygon):
        ring = _normalize_ring(
            raw_ring,
            path=f"{path}.ring[{ring_index}]",
            warnings=warnings,
            max_ring_vertices=max_ring_vertices,
        )
        signed = _planar_signed_area(ring)
        should_be_positive = ring_index == 0
        if (signed > 0) != should_be_positive:
            ring = list(reversed(ring))
        rings.append(ring)

    outer = rings[0]
    for hole_index, hole in enumerate(rings[1:], start=1):
        if not _point_in_ring(hole[0], outer, include_boundary=False):
            raise AOIValidationError(f"{path}.ring[{hole_index}] 不完全位于外环内部")
        if _rings_intersect(outer, hole):
            raise AOIValidationError(f"{path}.ring[{hole_index}] 与外环相交")
    for first in range(1, len(rings)):
        for second in range(first + 1, len(rings)):
            if _rings_intersect(rings[first], rings[second]):
                raise AOIValidationError(f"{path} 内环彼此相交")
            if _point_in_ring(rings[first][0], rings[second], include_boundary=True):
                raise AOIValidationError(f"{path} 内环发生嵌套")
            if _point_in_ring(rings[second][0], rings[first], include_boundary=True):
                raise AOIValidationError(f"{path} 内环发生嵌套")
    if _polygon_area_m2(rings) <= 0:
        raise AOIValidationError(f"{path} 内环面积不能大于或等于外环")
    return rings


def _normalize_ring(
    raw_ring: Any,
    *,
    path: str,
    warnings: list[str],
    max_ring_vertices: int,
) -> Ring:
    if not _is_sequence(raw_ring) or not raw_ring:
        raise AOIValidationError(f"{path} 为空")
    if len(raw_ring) > max_ring_vertices:
        raise AOIValidationError(
            f"{path} 顶点数 {len(raw_ring)} 超过单环上限 {max_ring_vertices}"
        )
    ring: Ring = []
    for coordinate_index, raw_position in enumerate(raw_ring):
        position = _normalize_position(raw_position, f"{path}[{coordinate_index}]")
        if not ring or position != ring[-1]:
            ring.append(position)
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0])
        warnings.append(f"{path} 未闭合，已按 GeoJSON 规则补上首点")
    if len(ring) < 4 or len(set(ring[:-1])) < 3:
        raise AOIValidationError(f"{path} 至少需要三个不同顶点并闭合")
    if len(ring) > max_ring_vertices:
        raise AOIValidationError(f"{path} 闭合后超过单环顶点上限 {max_ring_vertices}")
    if abs(_planar_signed_area(ring)) <= 1e-15:
        raise AOIValidationError(f"{path} 面积为零")
    if _ring_self_intersects(ring):
        raise AOIValidationError(f"{path} 存在自相交")
    return ring


def _normalize_position(raw_position: Any, path: str) -> Position:
    if not _is_sequence(raw_position) or len(raw_position) != 2:
        raise AOIValidationError(f"{path} 必须是二维 [longitude, latitude]")
    longitude, latitude = raw_position
    if isinstance(longitude, bool) or isinstance(latitude, bool):
        raise AOIValidationError(f"{path} 坐标不能是布尔值")
    if not isinstance(longitude, Real) or not isinstance(latitude, Real):
        raise AOIValidationError(f"{path} 坐标必须是数字")
    longitude = float(longitude)
    latitude = float(latitude)
    if not math.isfinite(longitude) or not math.isfinite(latitude):
        raise AOIValidationError(f"{path} 坐标必须是有限数字")
    if not -180 <= longitude <= 180:
        raise AOIValidationError(f"{path} 经度超出 [-180, 180]")
    if not -90 <= latitude <= 90:
        raise AOIValidationError(f"{path} 纬度超出 [-90, 90]")
    return longitude, latitude


def _polygon_area_m2(polygon: PolygonCoordinates) -> float:
    outer = _spherical_ring_area_m2(polygon[0])
    holes = sum(_spherical_ring_area_m2(ring) for ring in polygon[1:])
    return outer - holes


def _spherical_ring_area_m2(ring: Ring) -> float:
    total = 0.0
    for (lon1, lat1), (lon2, lat2) in pairwise(ring):
        lon1_rad = math.radians(lon1)
        lon2_rad = math.radians(lon2)
        delta_lon = lon2_rad - lon1_rad
        if delta_lon > math.pi:
            delta_lon -= 2 * math.pi
        elif delta_lon < -math.pi:
            delta_lon += 2 * math.pi
        total += delta_lon * (2 + math.sin(math.radians(lat1)) + math.sin(math.radians(lat2)))
    return abs(total) * EARTH_RADIUS_M**2 / 2


def _planar_signed_area(ring: Ring) -> float:
    return sum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in pairwise(ring)
    ) / 2


def _bbox(polygons: MultiPolygonCoordinates) -> tuple[float, float, float, float]:
    positions = [position for polygon in polygons for ring in polygon for position in ring]
    return (
        min(position[0] for position in positions),
        min(position[1] for position in positions),
        max(position[0] for position in positions),
        max(position[1] for position in positions),
    )


def _validate_multipolygon_relationships(polygons: MultiPolygonCoordinates) -> None:
    for first_index, first in enumerate(polygons):
        for second_index in range(first_index + 1, len(polygons)):
            second = polygons[second_index]
            if _rings_intersect(first[0], second[0]):
                raise AOIValidationError(
                    f"MultiPolygon 成员 {first_index} 与 {second_index} 边界相交"
                )
            if _point_in_polygon(first[0][0], second) or _point_in_polygon(second[0][0], first):
                raise AOIValidationError(
                    f"MultiPolygon 成员 {first_index} 与 {second_index} 发生重叠或包含"
                )


def _point_in_polygon(point: Position, polygon: PolygonCoordinates) -> bool:
    if not _point_in_ring(point, polygon[0], include_boundary=True):
        return False
    return not any(_point_in_ring(point, hole, include_boundary=True) for hole in polygon[1:])


def _point_in_ring(point: Position, ring: Ring, *, include_boundary: bool) -> bool:
    inside = False
    x, y = point
    for first, second in pairwise(ring):
        if _point_on_segment(point, first, second):
            return include_boundary
        x1, y1 = first
        x2, y2 = second
        if (y1 > y) != (y2 > y):
            intersection_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < intersection_x:
                inside = not inside
    return inside


def _ring_self_intersects(ring: Ring) -> bool:
    segment_count = len(ring) - 1
    for first_index in range(segment_count):
        first = (ring[first_index], ring[first_index + 1])
        for second_index in range(first_index + 1, segment_count):
            if second_index in {first_index - 1, first_index, first_index + 1}:
                continue
            if first_index == 0 and second_index == segment_count - 1:
                continue
            second = (ring[second_index], ring[second_index + 1])
            if _segments_intersect(*first, *second):
                return True
    return False


def _rings_intersect(first: Ring, second: Ring) -> bool:
    return any(
        _segments_intersect(first_start, first_end, second_start, second_end)
        for first_start, first_end in pairwise(first)
        for second_start, second_end in pairwise(second)
    )


def _segments_intersect(a: Position, b: Position, c: Position, d: Position) -> bool:
    first = _orientation(a, b, c)
    second = _orientation(a, b, d)
    third = _orientation(c, d, a)
    fourth = _orientation(c, d, b)
    if first * second < 0 and third * fourth < 0:
        return True
    return (
        (first == 0 and _point_on_segment(c, a, b))
        or (second == 0 and _point_on_segment(d, a, b))
        or (third == 0 and _point_on_segment(a, c, d))
        or (fourth == 0 and _point_on_segment(b, c, d))
    )


def _orientation(a: Position, b: Position, c: Position) -> int:
    cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    if math.isclose(cross, 0, abs_tol=1e-12):
        return 0
    return 1 if cross > 0 else -1


def _point_on_segment(point: Position, start: Position, end: Position) -> bool:
    if _orientation(start, end, point) != 0:
        return False
    return (
        min(start[0], end[0]) - 1e-12 <= point[0] <= max(start[0], end[0]) + 1e-12
        and min(start[1], end[1]) - 1e-12 <= point[1] <= max(start[1], end[1]) + 1e-12
    )


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _deduplicate(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
