from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

import level_sync  # noqa: E402
from level_sync import fingerprint, merge_levels  # noqa: E402


def lvl(name: str, pct: float, **extra) -> dict:
    return {"name": name, "mute_at_percent": pct, "enabled": True, **extra}


def baseline_of(levels: dict, t: float) -> dict:
    return {k: {"fp": fingerprint(p), "t": t} for k, p in levels.items()}


class MergeTests(unittest.TestCase):
    def test_edits_to_different_levels_on_two_pcs_both_survive(self) -> None:
        start = {"id:1": lvl("A", 50), "id:2": lvl("B", 50)}
        base = baseline_of(start, 100.0)
        # PC2 already pushed a change to B at t=150.
        remote = {"id:1": {**lvl("A", 50), "updated_at": 100.0}, "id:2": {**lvl("B", 80), "updated_at": 150.0}}
        # This PC edited A.
        local = {"id:1": lvl("A", 65), "id:2": lvl("B", 50)}
        levels, deleted, stamps = merge_levels(local, base, {}, remote, {}, now=200.0)
        self.assertEqual(levels["id:1"]["mute_at_percent"], 65)
        self.assertEqual(levels["id:2"]["mute_at_percent"], 80)
        self.assertEqual(stamps, {"id:1": 200.0, "id:2": 150.0})
        self.assertEqual(deleted, {})

    def test_same_level_newest_edit_wins(self) -> None:
        start = {"id:1": lvl("A", 50)}
        base = baseline_of(start, 100.0)
        remote = {"id:1": {**lvl("A", 90), "updated_at": 300.0}}
        local = {"id:1": lvl("A", 40)}  # edited locally, stamped "now"
        levels, _d, _s = merge_levels(local, base, {}, remote, {}, now=250.0)
        self.assertEqual(levels["id:1"]["mute_at_percent"], 90)
        levels, _d, _s = merge_levels(local, base, {}, remote, {}, now=400.0)
        self.assertEqual(levels["id:1"]["mute_at_percent"], 40)

    def test_deleting_a_level_syncs_and_does_not_come_back(self) -> None:
        start = {"id:1": lvl("A", 50), "id:2": lvl("B", 50)}
        base = baseline_of(start, 100.0)
        remote = {k: {**p, "updated_at": 100.0} for k, p in start.items()}
        local = {"id:2": lvl("B", 50)}  # A removed on this PC
        levels, deleted, _s = merge_levels(local, base, {}, remote, {}, now=200.0)
        self.assertNotIn("id:1", levels)
        self.assertEqual(deleted, {"id:1": 200.0})

        # Another PC that still has A (unchanged) drops it on its next sync.
        levels2, deleted2, _s = merge_levels(start, base, {}, {"id:2": remote["id:2"]}, deleted, now=300.0)
        self.assertNotIn("id:1", levels2)
        self.assertIn("id:1", deleted2)

    def test_edit_after_delete_brings_level_back(self) -> None:
        base = baseline_of({"id:1": lvl("A", 50)}, 100.0)
        local = {"id:1": lvl("A", 70)}  # edited at "now" after the cloud deletion
        levels, deleted, _s = merge_levels(local, base, {}, {}, {"id:1": 150.0}, now=200.0)
        self.assertEqual(levels["id:1"]["mute_at_percent"], 70)
        self.assertEqual(deleted, {})

    def test_first_sync_on_new_pc_takes_cloud_copy_and_uploads_new_levels(self) -> None:
        remote = {"id:1": {**lvl("A", 90), "updated_at": 100.0}}
        local = {"id:1": lvl("A", 10), "id:9": lvl("Local only", 55)}
        levels, _d, stamps = merge_levels(local, None, {}, remote, {}, now=500.0)
        self.assertEqual(levels["id:1"]["mute_at_percent"], 90)
        self.assertEqual(levels["id:9"]["mute_at_percent"], 55)
        self.assertEqual(stamps["id:9"], 500.0)

    def test_old_tombstones_expire(self) -> None:
        now = 10_000_000.0
        _l, deleted, _s = merge_levels({}, {}, {"id:old": now - level_sync.TOMBSTONE_TTL_S - 1}, {}, {}, now)
        self.assertEqual(deleted, {})

    def test_bad_remote_data_is_ignored(self) -> None:
        remote = {"id:1": "not a dict", "id:2": {**lvl("B", 5), "updated_at": "garbage"}}
        levels, _d, _s = merge_levels({}, {}, {}, remote, {"id:3": "x"}, now=1.0)
        self.assertEqual(list(levels), ["id:2"])


class FakeAuth:
    def __init__(self, signed_in: bool = True) -> None:
        self.signed_in = signed_in

    def access_token(self):
        return "token" if self.signed_in else None

    def user_id(self):
        return "11111111-2222-3333-4444-555555555555" if self.signed_in else None


class LevelSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        settings = patch.object(level_sync, "supabase_settings", return_value=("https://x.supabase.co", "key"))
        settings.start()
        self.addCleanup(settings.stop)
        self.cloud: dict = {"levels": {}, "deleted": {}}
        self.pushes = 0

        def fake_fetch(_url, _key, _token, _uid):
            return dict(self.cloud["levels"]), dict(self.cloud["deleted"]), bool(self.cloud["levels"])

        def fake_push(_url, _key, _token, _uid, levels, deleted):
            self.pushes += 1
            self.cloud = {"levels": levels, "deleted": deleted}

        for name, fn in (("fetch_remote", fake_fetch), ("push_remote", fake_push)):
            p = patch.object(level_sync, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)

    def make_pc(self, profiles: dict, state_dir: Path | None = None):
        store = {"profiles": profiles}

        def replace(new, expected):
            if store["profiles"] != expected:
                return False
            store["profiles"] = new
            return True

        sync = level_sync.LevelSync(FakeAuth(), state_dir or Path(self.tmp.name), lambda: store["profiles"], replace)
        return sync, store

    def test_two_pcs_converge(self) -> None:
        pc1_dir = Path(self.tmp.name) / "pc1"
        pc2_dir = Path(self.tmp.name) / "pc2"
        pc1_dir.mkdir()
        pc2_dir.mkdir()
        pc1, store1 = self.make_pc({"id:1": lvl("A", 60)}, pc1_dir)
        pc2, store2 = self.make_pc({"id:2": lvl("B", 70)}, pc2_dir)

        self.assertTrue(pc1.sync_once())
        self.assertTrue(pc2.sync_once())
        self.assertEqual(set(store2["profiles"]), {"id:1", "id:2"})
        self.assertTrue(pc1.sync_once())
        self.assertEqual(set(store1["profiles"]), {"id:1", "id:2"})
        self.assertEqual(pc1.to_state()["status"], "synced")

        # Edit on PC2, delete on PC1, then both sync.
        store2["profiles"] = {**store2["profiles"], "id:2": lvl("B", 85)}
        store1["profiles"] = {k: v for k, v in store1["profiles"].items() if k != "id:1"}
        with patch.object(level_sync.time, "time", return_value=level_sync.time.time() + 5):
            self.assertTrue(pc2.sync_once())
        with patch.object(level_sync.time, "time", return_value=level_sync.time.time() + 10):
            self.assertTrue(pc1.sync_once())
            self.assertTrue(pc2.sync_once())
        for store in (store1, store2):
            self.assertEqual(set(store["profiles"]), {"id:2"})
            self.assertEqual(store["profiles"]["id:2"]["mute_at_percent"], 85)
        self.assertIn("id:1", self.cloud["deleted"])

        # Nothing changed: no extra upload.
        pushes = self.pushes
        self.assertTrue(pc1.sync_once())
        self.assertEqual(self.pushes, pushes)

    def test_signed_out_and_errors_are_reported(self) -> None:
        sync, _store = self.make_pc({})
        sync._auth = FakeAuth(signed_in=False)
        self.assertFalse(sync.sync_once())
        self.assertEqual(sync.to_state()["status"], "signed_out")

        sync._auth = FakeAuth()
        with patch.object(level_sync, "fetch_remote", side_effect=level_sync.SyncError("Can't reach Supabase: offline")):
            self.assertFalse(sync.sync_once())
        st = sync.to_state()
        self.assertEqual(st["status"], "error")
        self.assertIn("offline", st["message"])

    def test_local_change_mid_sync_is_not_overwritten(self) -> None:
        self.cloud = {"levels": {"id:1": {**lvl("A", 99), "updated_at": 1.0}}, "deleted": {}}
        sync, store = self.make_pc({"id:5": lvl("E", 5)})
        orig_fetch = level_sync.fetch_remote

        def fetch_then_user_edits(*a):
            result = orig_fetch(*a)
            store["profiles"] = {**store["profiles"], "id:6": lvl("F", 6)}
            return result

        with patch.object(level_sync, "fetch_remote", side_effect=fetch_then_user_edits):
            self.assertFalse(sync.sync_once())
        self.assertIn("id:6", store["profiles"])
        self.assertTrue(sync.sync_once())
        self.assertEqual(set(store["profiles"]), {"id:1", "id:5", "id:6"})


if __name__ == "__main__":
    unittest.main()
