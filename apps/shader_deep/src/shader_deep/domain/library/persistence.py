"""四库发布所需的最小持久化契约, 不绑定文件系统."""

from typing import Protocol


class LibraryPersistence(Protocol):
    """成功写入后返回, 失败抛出异常以阻止内存版本发布."""

    def save_version(self, revision: int, snapshot: dict[str, object]) -> None:
        """保存一个不可覆盖的完整版本."""
        ...

    def save_source(self, report_id: str, report: dict[str, object]) -> None:
        """保存不可变原始报告, 同内容重复写入可复用."""
        ...
