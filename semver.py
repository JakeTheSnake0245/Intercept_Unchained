"""Minimal semver compatibility shim.

This project vendors a tiny subset of the ``semver`` package API so
integrations like radiosonde_auto_rx can run even when the external
dependency is missing from the target Python environment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Iterable

_SEMVER_RE = re.compile(
    r"^\s*"
    r"(?P<major>0|[1-9]\d*)"
    r"(?:\.(?P<minor>0|[1-9]\d*))?"
    r"(?:\.(?P<patch>0|[1-9]\d*))?"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?"
    r"(?:\+(?P<build>[0-9A-Za-z.-]+))?"
    r"\s*$"
)


def _split_prerelease(value: str | None) -> list[int | str]:
    if not value:
        return []
    parts: list[int | str] = []
    for token in value.split("."):
        parts.append(int(token) if token.isdigit() else token)
    return parts


def _compare_identifiers(left: Iterable[int | str], right: Iterable[int | str]) -> int:
    left_parts = list(left)
    right_parts = list(right)
    for l_part, r_part in zip(left_parts, right_parts):
        if l_part == r_part:
            continue
        if isinstance(l_part, int) and isinstance(r_part, str):
            return -1
        if isinstance(l_part, str) and isinstance(r_part, int):
            return 1
        return -1 if l_part < r_part else 1
    if len(left_parts) == len(right_parts):
        return 0
    return -1 if len(left_parts) < len(right_parts) else 1


@dataclass(frozen=True)
class VersionInfo:
    major: int
    minor: int = 0
    patch: int = 0
    prerelease: str | None = None
    build: str | None = None

    @classmethod
    def parse(cls, version: str) -> VersionInfo:
        match = _SEMVER_RE.match(str(version))
        if not match:
            raise ValueError(f"{version!r} is not valid SemVer")
        groups = match.groupdict()
        return cls(
            major=int(groups["major"]),
            minor=int(groups["minor"] or 0),
            patch=int(groups["patch"] or 0),
            prerelease=groups["prerelease"],
            build=groups["build"],
        )

    @classmethod
    def isvalid(cls, version: str) -> bool:
        return _SEMVER_RE.match(str(version)) is not None

    @classmethod
    def is_valid(cls, version: str) -> bool:
        return cls.isvalid(version)

    def compare(self, other: str | VersionInfo) -> int:
        return compare(self, other)

    def match(self, expr: str) -> bool:
        return match(str(self), expr)

    def bump_major(self) -> VersionInfo:
        return VersionInfo(self.major + 1, 0, 0)

    def bump_minor(self) -> VersionInfo:
        return VersionInfo(self.major, self.minor + 1, 0)

    def bump_patch(self) -> VersionInfo:
        return VersionInfo(self.major, self.minor, self.patch + 1)

    def finalize_version(self) -> VersionInfo:
        return VersionInfo(self.major, self.minor, self.patch)

    def replace(self, **changes) -> VersionInfo:
        return replace(self, **changes)

    def __str__(self) -> str:
        value = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            value += f"-{self.prerelease}"
        if self.build:
            value += f"+{self.build}"
        return value


def parse(version: str) -> VersionInfo:
    return VersionInfo.parse(version)


def compare(left: str | VersionInfo, right: str | VersionInfo) -> int:
    left_ver = left if isinstance(left, VersionInfo) else parse(str(left))
    right_ver = right if isinstance(right, VersionInfo) else parse(str(right))

    left_core = (left_ver.major, left_ver.minor, left_ver.patch)
    right_core = (right_ver.major, right_ver.minor, right_ver.patch)
    if left_core != right_core:
        return -1 if left_core < right_core else 1

    if left_ver.prerelease == right_ver.prerelease:
        return 0
    if left_ver.prerelease is None:
        return 1
    if right_ver.prerelease is None:
        return -1
    return _compare_identifiers(
        _split_prerelease(left_ver.prerelease),
        _split_prerelease(right_ver.prerelease),
    )


def match(version: str | VersionInfo, expr: str) -> bool:
    version_info = version if isinstance(version, VersionInfo) else parse(str(version))
    expression = str(expr).strip()
    for operator in ("<=", ">=", "==", "!=", "<", ">"):
        if expression.startswith(operator):
            other = parse(expression[len(operator):].strip())
            result = compare(version_info, other)
            return {
                "<": result < 0,
                "<=": result <= 0,
                ">": result > 0,
                ">=": result >= 0,
                "==": result == 0,
                "!=": result != 0,
            }[operator]
    return compare(version_info, parse(expression)) == 0


def max_ver(left: str | VersionInfo, right: str | VersionInfo) -> VersionInfo:
    left_ver = left if isinstance(left, VersionInfo) else parse(str(left))
    right_ver = right if isinstance(right, VersionInfo) else parse(str(right))
    return left_ver if compare(left_ver, right_ver) >= 0 else right_ver


def min_ver(left: str | VersionInfo, right: str | VersionInfo) -> VersionInfo:
    left_ver = left if isinstance(left, VersionInfo) else parse(str(left))
    right_ver = right if isinstance(right, VersionInfo) else parse(str(right))
    return left_ver if compare(left_ver, right_ver) <= 0 else right_ver
