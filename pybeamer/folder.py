from __future__ import annotations
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
	from .tracker import Tracker


class Folder:
	"""Represents a folder in the project tree."""

	def __init__(self, name: str, children: list[Folder | Tracker] | None = None):
		self._name = name
		self._children: list[Folder | Tracker] = children or []

	@property
	def name(self) -> str:
		return self._name

	@property
	def children(self) -> list[Folder | Tracker]:
		return self._children

	@property
	def folders(self) -> list[Folder]:
		return [c for c in self._children if isinstance(c, Folder)]

	@property
	def trackers(self) -> list[Tracker]:
		from .tracker import Tracker
		return [c for c in self._children if isinstance(c, Tracker)]

	def __repr__(self) -> str:
		return f'Folder(name={self._name}, children={len(self._children)})'

	def __str__(self) -> str:
		return self._name
