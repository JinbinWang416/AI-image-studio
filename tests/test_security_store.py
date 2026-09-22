# -*- coding: utf-8 -*-
"""安全存储层测试（块 1-a）。

重点验证：**损坏时不会静默丢失账号**。
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.security.store import JsonStore, StorageFullError  # noqa: E402


class JsonStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _store(self, name: str = "security.json") -> JsonStore:
        return JsonStore(self.dir / name, default={"version": 1, "users": {}})

    # ---------------------------------------------------------------- 基础
    def test_missing_file_returns_default(self) -> None:
        s = self._store()
        self.assertEqual(s.load()["users"], {})

    def test_roundtrip(self) -> None:
        s = self._store()
        s.save({"version": 1, "users": {"u1": {"login_name": "admin"}}})
        self.assertIn("u1", s.load()["users"])

    def test_default_keys_filled(self) -> None:
        s = self._store()
        s.save({"users": {"u1": {}}})     # 少了 version
        self.assertIn("version", s.load())

    # ---------------------------------------------------------------- 自愈
    def test_corrupted_with_snapshot_recovers_accounts(self) -> None:
        """核心用例：写坏文件后，账号必须能从快照恢复，而不是凭空消失。"""
        s = self._store()
        # 建立两次正常写入 → 产生快照
        s.save({"version": 1, "users": {"admin": {"login_name": "admin", "roles": ["admin"]}}})
        s.save({"version": 1, "users": {
            "admin": {"login_name": "admin", "roles": ["admin"]},
            "staff": {"login_name": "staff", "roles": ["operator"]},
        }})
        self.assertTrue(s.snapshots(), "应已产生快照")

        # 模拟损坏：写入非法 JSON
        s.path.write_text('{"version": 1, "users": {"admin": {"login_nam', encoding="utf-8")

        data = s.load()
        self.assertIn("admin", data["users"], "损坏后账号丢失了！")
        self.assertIn("users", data)
        # 损坏原文必须被保留（证据）
        corrupts = list(self.dir.glob("security.json.corrupt.*"))
        self.assertTrue(corrupts, "未保留损坏文件证据")

    def test_corrupted_without_snapshot_keeps_evidence(self) -> None:
        """无快照时：保留损坏文件 + 返回默认值（但不静默）。"""
        s = JsonStore(self.dir / "fresh.json", default={"users": {}}, snapshot=False)
        s.path.write_text("{ 这不是合法 JSON", encoding="utf-8")
        data = s.load()
        self.assertEqual(data["users"], {})
        self.assertTrue(list(self.dir.glob("fresh.json.corrupt.*")), "未保留损坏证据")

    def test_empty_file_treated_as_fresh(self) -> None:
        """空文件（写入过程被中断）等同首次启动，不算损坏。"""
        s = self._store()
        s.path.write_text("", encoding="utf-8")
        self.assertEqual(s.load()["users"], {})
        self.assertFalse(list(self.dir.glob("*.corrupt.*")), "空文件不应被当成损坏")

    def test_recovery_writes_back(self) -> None:
        """恢复后主文件应是可解析的（后续读取走正常路径）。"""
        s = self._store()
        s.save({"version": 1, "users": {"admin": {"login_name": "admin"}}})
        s.save({"version": 1, "users": {"admin": {"login_name": "admin"}, "b": {}}})
        s.path.write_text("坏的", encoding="utf-8")
        s.load()
        raw = s.path.read_text(encoding="utf-8")
        json.loads(raw)          # 不应抛异常
        self.assertIn("admin", raw)

    # ---------------------------------------------------------------- 快照
    def test_snapshot_pruned(self) -> None:
        s = self._store()
        for i in range(JsonStore.SNAPSHOT_KEEP + 6):
            s.save({"version": 1, "users": {f"u{i}": {}}})
        self.assertLessEqual(len(s.snapshots()), JsonStore.SNAPSHOT_KEEP)

    def test_snapshots_are_valid_json(self) -> None:
        s = self._store()
        s.save({"version": 1, "users": {"x": {}}})
        s.save({"version": 1, "users": {"x": {}, "y": {}}})
        for snap in s.snapshots():
            json.loads(snap.read_text(encoding="utf-8"))

    def test_snapshot_disabled(self) -> None:
        s = JsonStore(self.dir / "nosnap.json", default={}, snapshot=False)
        s.save({"a": 1})
        s.save({"a": 2})
        self.assertEqual(s.snapshots(), [])

    # ---------------------------------------------------------------- 原子性
    def test_no_partial_file_on_write(self) -> None:
        """写入后不应残留 .tmp 文件。"""
        s = self._store()
        s.save({"version": 1, "users": {}})
        self.assertFalse(list(self.dir.glob("*.tmp")))

    def test_update_is_atomic(self) -> None:
        s = self._store()

        def mut(d):
            d["users"]["new"] = {"login_name": "new"}

        s.update(mut)
        self.assertIn("new", s.load()["users"])

    # ---------------------------------------------------------------- 并发
    def test_concurrent_updates_do_not_lose_data(self) -> None:
        """多线程并发写：每一条都不能丢。"""
        import threading

        s = self._store()

        def worker(n: int) -> None:
            s.update(lambda d: d["users"].__setitem__(f"u{n}", {"n": n}))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        users = s.load()["users"]
        self.assertEqual(len(users), 12, f"并发写入丢失数据：只有 {len(users)} 条")

    # ---------------------------------------------------------------- IO 容错
    def test_storage_full_raises_structured_error(self) -> None:
        """磁盘满必须抛结构化异常，而不是裸 OSError。"""
        s = self._store()
        # 用一个不可写路径模拟（把父目录换成文件）
        bad = JsonStore(self.dir / "afile" / "x.json", default={})
        (self.dir / "afile").write_text("我是文件不是目录", encoding="utf-8")
        with self.assertRaises((StorageFullError, Exception)) as ctx:
            bad.save({"a": 1})
        err = ctx.exception
        self.assertTrue(hasattr(err, "code"), "异常应带 code 属性")
        self.assertIn(err.code, ("storage_full", "storage_error"))

    def test_error_code_attributes(self) -> None:
        from app.security.store import StorageError, StorageFullError as SF

        self.assertEqual(SF("x").code, "storage_full")
        self.assertEqual(StorageError("x").code, "storage_error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
