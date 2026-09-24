"""业务库可以使用内存存储, 发布失败不会改变已接受状态."""

from __future__ import annotations

from copy import deepcopy
from unittest import TestCase

from shader_deep.domain.library.store import LibraryStore
from tests.unit_tests.domain.test_possibility_library import outline, report


class MemoryPersistence:
    def __init__(self) -> None:
        self.versions: dict[int, dict[str, object]] = {}
        self.sources: dict[str, dict[str, object]] = {}
        self.fail_versions = False

    def save_source(self, report_id: str, value: dict[str, object]) -> None:
        self.sources.setdefault(report_id, deepcopy(value))

    def save_version(self, revision: int, snapshot: dict[str, object]) -> None:
        if self.fail_versions:
            msg = "fixture persistence unavailable"
            raise OSError(msg)
        self.versions[revision] = deepcopy(snapshot)


class LibraryPersistenceTests(TestCase):
    def test_failed_publication_keeps_previous_library_and_allows_retry(self) -> None:
        persistence = MemoryPersistence()
        store = LibraryStore(outline(), persistence)
        store.add_report("first", report())
        accepted, revision = store.library, store.revision
        persistence.fail_versions = True
        with self.assertRaisesRegex(OSError, "persistence unavailable"):
            store.add_report("second", report())
        self.assertEqual(store.library, accepted)
        self.assertEqual(store.revision, revision)
        self.assertEqual(store.report_ids, ("first",))
        persistence.fail_versions = False
        identities = store.add_report("second", report())
        self.assertTrue(identities)
        self.assertEqual(store.revision, revision + 1)
        self.assertEqual(store.add_report("second", report()), identities)
        self.assertEqual(store.revision, revision + 1)
