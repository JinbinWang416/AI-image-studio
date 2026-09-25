# -*- coding: utf-8 -*-
"""备份与恢复测试（块 1-b）。"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import sys
import tempfile
import unittest
import zipfile
import io

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.security.backup import (  # noqa: E402
    KEY_ENV,
    BackupError,
    BackupManager,
    load_or_create_key,
)


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        # 造一份最小可备份的数据集
        sec = self.root / "data" / "security"
        sec.mkdir(parents=True)
        (sec / "security.json").write_text(
            json.dumps({"version": 1, "users": {"u1": {"login_name": "admin"}}}),
            encoding="utf-8",
        )
        (sec / "sessions.json").write_text('{"version":1,"sessions":{}}', encoding="utf-8")
        audit = self.root / "logs" / "audit"
        audit.mkdir(parents=True)
        (audit / "audit-2026-09-20.jsonl").write_text('{"action":"x"}\n', encoding="utf-8")
        cfg = self.root / "config"
        cfg.mkdir()
        (cfg / "settings.json").write_text('{"active_provider":"mock"}', encoding="utf-8")
        batch = self.root / "output" / "batch_20260101_000000" / "01_门店"
        batch.mkdir(parents=True)
        (batch / "_manifest.json").write_text('{"batch":"x"}', encoding="utf-8")

        # 用固定密钥，避免测试间互相影响
        os.environ[KEY_ENV] = base64.b64encode(b"k" * 32).decode()
        self.mgr = BackupManager(self.root, keep=3)

    def tearDown(self) -> None:
        os.environ.pop(KEY_ENV, None)
        self._tmp.cleanup()

    # ---------------------------------------------------------------- 创建
    def test_create_backup(self) -> None:
        info = self.mgr.create(label="manual")
        self.assertTrue(info.path.is_file())
        self.assertTrue(info.encrypted)
        self.assertTrue(info.path.name.endswith(".shsbak"))
        self.assertGreater(info.file_count, 0)

    def test_backup_is_encrypted(self) -> None:
        """备份文件里**不得出现明文**（含 API Key、账号名）。"""
        info = self.mgr.create()
        blob = info.path.read_bytes()
        self.assertNotIn(b"admin", blob)
        self.assertNotIn(b"active_provider", blob)
        self.assertNotIn(b"mock", blob)
        self.assertTrue(blob.startswith(b"SHSBAK01"))

    def test_list_and_get(self) -> None:
        info = self.mgr.create(label="manual")
        items = self.mgr.list()
        self.assertEqual(len(items), 1)
        self.assertEqual(self.mgr.get(info.id).id, info.id)
        self.assertIsNone(self.mgr.get("不存在的ID"))

    def test_public_excludes_secrets(self) -> None:
        info = self.mgr.create()
        pub = info.public()
        for k in pub:
            self.assertNotIn("key", k.lower())
        self.assertNotIn("nonce", json.dumps(pub))

    def test_prune_keeps_n(self) -> None:
        import time

        for i in range(5):
            self.mgr.create(label=f"m{i}")
            time.sleep(0.01)
        self.assertLessEqual(len(self.mgr.list()), 3)

    # ---------------------------------------------------------------- 恢复
    def test_restore_roundtrip(self) -> None:
        info = self.mgr.create()
        # 破坏数据
        (self.root / "data" / "security" / "security.json").write_text('{"users":{}}',
                                                                      encoding="utf-8")
        # 恢复
        res = self.mgr.restore(info.id, confirm=info.id)
        self.assertTrue(res.ok)
        data = json.loads((self.root / "data" / "security" / "security.json")
                          .read_text(encoding="utf-8"))
        self.assertIn("u1", data["users"], "恢复后账号应回来")
        self.assertEqual(data["users"]["u1"]["login_name"], "admin")

    def test_restore_creates_pre_restore_backup(self) -> None:
        """恢复前必须自动留一份回滚点。"""
        info = self.mgr.create()
        res = self.mgr.restore(info.id, confirm=info.id)
        self.assertTrue(res.pre_restore_backup, "未创建还原前备份")
        labels = [b.label for b in self.mgr.list()]
        self.assertIn("pre-restore", labels)

    def test_restore_requires_confirm(self) -> None:
        info = self.mgr.create()
        with self.assertRaises(BackupError):
            self.mgr.restore(info.id, confirm="")
        with self.assertRaises(BackupError):
            self.mgr.restore(info.id, confirm="wrong-id")
        with self.assertRaises(BackupError):
            self.mgr.restore(info.id, confirm=info.id + "x")

    def test_restore_unknown_id(self) -> None:
        with self.assertRaises(BackupError):
            self.mgr.restore("nope", confirm="nope")

    # ---------------------------------------------------------------- 完整性
    def test_tampered_backup_rejected(self) -> None:
        """篡改备份内容必须被 GCM 检出（AEAD 的价值）。"""
        info = self.mgr.create()
        blob = bytearray(info.path.read_bytes())
        blob[-1] ^= 0xFF                      # 翻转最后一字节
        info.path.write_bytes(bytes(blob))
        with self.assertRaises(BackupError):
            self.mgr.read_archive(info.id)

    def test_wrong_key_rejected(self) -> None:
        info = self.mgr.create()
        os.environ[KEY_ENV] = base64.b64encode(b"x" * 32).decode()
        with self.assertRaises(BackupError):
            self.mgr.read_archive(info.id)

    def test_not_our_file_rejected(self) -> None:
        self.mgr.dir.mkdir(parents=True, exist_ok=True)
        bad = self.mgr.dir / "backup_20260101_000000_fake.shsbak"
        bad.write_bytes(b"PK\x03\x04 not ours")
        with self.assertRaises(BackupError):
            self.mgr.read_archive(bad.stem)

    # ---------------------------------------------------------------- 安全
    def test_no_path_traversal_on_restore(self) -> None:
        """归档里带 ../ 的条目必须被拒绝（防备份被篡改后写任意路径）。"""
        evil = io.BytesIO()
        with zipfile.ZipFile(evil, "w") as z:
            z.writestr("../../evil.json", "{}")
            z.writestr("/absolute/evil.json", "{}")
            z.writestr("data/security/ok.json", '{"fine":true}')
        from app.security.backup import _encrypt

        key, _ = load_or_create_key(self.root)
        self.mgr.dir.mkdir(parents=True, exist_ok=True)
        p = self.mgr.dir / "backup_20260101_000001_evil.shsbak"
        p.write_bytes(_encrypt(evil.getvalue(), key))

        res = self.mgr.restore(p.stem, confirm=p.stem)
        self.assertIn("data/security/ok.json", res.restored_files)
        self.assertFalse((self.root.parent / "evil.json").exists())
        self.assertFalse(pathlib.Path("/absolute/evil.json").exists())

    def test_disallowed_extension_skipped(self) -> None:
        evil = io.BytesIO()
        with zipfile.ZipFile(evil, "w") as z:
            z.writestr("data/security/evil.exe", b"MZ")
        from app.security.backup import _encrypt

        key, _ = load_or_create_key(self.root)
        self.mgr.dir.mkdir(parents=True, exist_ok=True)
        p = self.mgr.dir / "backup_20260101_000002_bad.shsbak"
        p.write_bytes(_encrypt(evil.getvalue(), key))
        res = self.mgr.restore(p.stem, confirm=p.stem)
        self.assertEqual(res.restored_files, [])

    # ---------------------------------------------------------------- 审计保护
    def test_restore_does_not_overwrite_audit_log(self) -> None:
        """恢复**不得**覆盖审计日志。

        背景：审计是只追加的事实记录。早期实现把 logs/audit/ 一起还原，
        结果"执行恢复"这一动作反而抹掉了恢复点之后的审计痕迹
        （实测中 backup.create 的记录就是这样消失的）。
        """
        audit_file = self.root / "logs" / "audit" / "audit-2026-09-20.jsonl"
        audit_file.write_text('{"action":"before-backup"}\n', encoding="utf-8")
        info = self.mgr.create()

        # 备份之后又发生了新事件
        audit_file.write_text(
            '{"action":"before-backup"}\n{"action":"after-backup"}\n', encoding="utf-8"
        )

        res = self.mgr.restore(info.id, confirm=info.id)
        content = audit_file.read_text(encoding="utf-8")
        self.assertIn("after-backup", content, "审计日志被恢复操作覆盖了")
        self.assertFalse(
            any(f.startswith("logs/audit/") for f in res.restored_files),
            f"审计日志不该出现在恢复列表：{res.restored_files}",
        )

    def test_include_audit_flag_can_force(self) -> None:
        """显式开启时才覆盖审计（仅供人工排障）。"""
        from app.security.backup import BackupManager

        audit_file = self.root / "logs" / "audit" / "audit-2026-09-20.jsonl"
        audit_file.write_text('{"action":"v1"}\n', encoding="utf-8")
        info = self.mgr.create()
        audit_file.write_text('{"action":"v1"}\n{"action":"v2"}\n', encoding="utf-8")

        forced = BackupManager(self.root, include_audit=True)
        res = forced.restore(info.id, confirm=info.id)
        self.assertIn("logs/audit/audit-2026-09-20.jsonl", res.restored_files)
        self.assertNotIn("v2", audit_file.read_text(encoding="utf-8"))

    # ---------------------------------------------------------------- 密钥
    def test_key_file_created_when_no_env(self) -> None:
        os.environ.pop(KEY_ENV, None)
        # 用干净的 root，避免读到 setUp 里已生成的文件
        clean = self.root / "clean"
        clean.mkdir()
        (clean / "data" / "security").mkdir(parents=True)
        key, created = load_or_create_key(clean)
        self.assertTrue(created)
        self.assertEqual(len(key), 32)
        key2, created2 = load_or_create_key(clean)
        self.assertFalse(created2)
        self.assertEqual(key, key2)

    def test_corrupt_key_file_raises(self) -> None:
        os.environ.pop(KEY_ENV, None)
        p = self.root / "data" / "security" / ".backup_key"
        p.write_text("这不是base64!!!", encoding="utf-8")
        with self.assertRaises(BackupError):
            load_or_create_key(self.root)

    def test_bad_env_key_rejected(self) -> None:
        os.environ[KEY_ENV] = "不是base64"
        with self.assertRaises(BackupError):
            load_or_create_key(self.root)

    # ---------------------------------------------------------------- 新鲜度
    def test_freshness(self) -> None:
        self.assertTrue(self.mgr.freshness()["stale"])
        self.mgr.create()
        f = self.mgr.freshness()
        self.assertEqual(f["count"], 1)
        self.assertFalse(f["stale"], "刚创建的备份不应算过期")
        self.assertGreater(f["latest_kb"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
