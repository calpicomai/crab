"""Model provisioning and runtime networking are separate: tests load no models."""
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from brain.costmap import LocalCostmap
from brain.perception.backends.nanoowl import NanoOwlBackend
from brain.perception.backends.yolo import YoloBackend
from shared.health import InputHealth


class LocalModelTests(unittest.TestCase):
    def test_yolo_missing_local_weights_never_calls_library(self):
        library = types.ModuleType("ultralytics")
        library.YOLO = Mock()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {"ultralytics": library}):
            for weights in (str(Path(tmp) / "missing.pt"), "https://example.com/model.pt"):
                with self.assertRaises(FileNotFoundError):
                    YoloBackend(weights=weights).load()
        library.YOLO.assert_not_called()

    def test_yolo_local_weights_disable_network_install_and_telemetry(self):
        library = types.ModuleType("ultralytics")
        library.YOLO = Mock()
        library.utils = types.SimpleNamespace(ONLINE=False, AUTOINSTALL=False, SETTINGS={"sync": True})
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {"ultralytics": library}), patch.dict(os.environ):
            weights = Path(tmp) / "model.pt"
            weights.write_bytes(b"test checkpoint; fake loader")
            YoloBackend(weights=str(weights)).load()
            self.assertEqual(os.environ["YOLO_OFFLINE"], "true")
            self.assertEqual(os.environ["YOLO_AUTOINSTALL"], "false")
            library.YOLO.assert_called_once_with(str(weights.resolve()), task="detect")
            self.assertFalse(library.utils.SETTINGS["sync"])

    def test_yolo_prior_online_import_is_rejected(self):
        library = types.ModuleType("ultralytics")
        library.YOLO = Mock()
        library.utils = types.SimpleNamespace(ONLINE=True, AUTOINSTALL=False, SETTINGS={})
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {"ultralytics": library}), patch.dict(os.environ):
            weights = Path(tmp) / "model.pt"
            weights.write_bytes(b"fake")
            with self.assertRaisesRegex(RuntimeError, "restart"):
                YoloBackend(weights=str(weights)).load()
        library.YOLO.assert_not_called()

    def test_nanoowl_missing_engine_never_resolves_model(self):
        with patch("brain.perception.backends.nanoowl._cached_model") as cache:
            with self.assertRaises(FileNotFoundError):
                NanoOwlBackend(engine="/missing/crab.engine").load()
            cache.assert_not_called()

    def test_nanoowl_cache_lookup_is_local_only_and_missing_cache_stops_load(self):
        hub = types.ModuleType("huggingface_hub")
        hub.constants = types.SimpleNamespace(HF_HUB_OFFLINE=True)
        hub.snapshot_download = Mock(side_effect=FileNotFoundError("no local snapshot"))
        predictor = types.ModuleType("nanoowl.owl_predictor")
        predictor.OwlPredictor = Mock()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {
                "huggingface_hub": hub, "nanoowl.owl_predictor": predictor}), patch.dict(os.environ):
            engine = Path(tmp) / "owl.engine"
            engine.write_bytes(b"fake engine")
            with self.assertRaises(FileNotFoundError):
                NanoOwlBackend(engine=str(engine)).load()
            hub.snapshot_download.assert_called_once_with(repo_id="google/owlvit-base-patch32", local_files_only=True)
            self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")
            self.assertEqual(os.environ["TRANSFORMERS_OFFLINE"], "1")
            predictor.OwlPredictor.assert_not_called()

    def test_nanoowl_incomplete_snapshot_cannot_trigger_online_repair(self):
        hub = types.ModuleType("huggingface_hub")
        hub.constants = types.SimpleNamespace(HF_HUB_OFFLINE=True)
        predictor = types.ModuleType("nanoowl.owl_predictor")
        predictor.OwlPredictor = Mock()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {
                "huggingface_hub": hub, "nanoowl.owl_predictor": predictor}), patch.dict(os.environ):
            hub.snapshot_download = Mock(return_value=tmp)
            engine = Path(tmp) / "owl.engine"
            engine.write_bytes(b"fake engine")
            with self.assertRaisesRegex(FileNotFoundError, "cache incomplete"):
                NanoOwlBackend(engine=str(engine)).load()
            predictor.OwlPredictor.assert_not_called()

    def test_nanoowl_complete_cache_keeps_vendor_model_id_and_offline_contract(self):
        hub = types.ModuleType("huggingface_hub")
        hub.constants = types.SimpleNamespace(HF_HUB_OFFLINE=True)
        transformer_utils = types.ModuleType("transformers.utils")
        transformer_utils.is_offline_mode = Mock(return_value=True)
        predictor = types.ModuleType("nanoowl.owl_predictor")
        predictor.OwlPredictor = Mock()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {
                "huggingface_hub": hub, "nanoowl.owl_predictor": predictor,
                "transformers": types.ModuleType("transformers"),
                "transformers.utils": transformer_utils}), patch.dict(os.environ):
            hub.snapshot_download = Mock(return_value=tmp)
            for filename in ("config.json", "preprocessor_config.json", "tokenizer_config.json",
                             "vocab.json", "merges.txt", "model.safetensors", "owl.engine"):
                (Path(tmp) / filename).write_text("fake cached asset; mocked loader")
            engine = Path(tmp) / "owl.engine"
            backend = NanoOwlBackend(engine=str(engine))
            backend.load()
            predictor.OwlPredictor.assert_called_once_with("google/owlvit-base-patch32",
                                                         image_encoder_engine=str(engine.resolve()))
            self.assertTrue(backend._loaded)


class CameraCostmapTests(unittest.TestCase):
    def observation(self, *, stamp=100, provenance="real", source="yolo"):
        health = InputHealth(available=True, provenance=provenance, captured_at=stamp).model_dump()
        return {"width": 640, "height": 480,
                "inputs": {"camera": dict(health), "interpretation": dict(health)},
                "detections": [{"label": "pole", "score": .9, "box": [40, 0, 160, 460], "source": source}]}

    def test_stale_failed_unknown_or_unstamped_frames_do_not_write_occupancy(self):
        cases = [self.observation(stamp=90), self.observation(provenance="unknown"), self.observation(stamp=110)]
        unstamped = self.observation()
        unstamped.pop("inputs")
        cases.append(unstamped)
        failed = self.observation()
        failed["inputs"]["interpretation"]["failure_reason"] = "model failed"
        cases.append(failed)
        for observation in cases:
            costmap = LocalCostmap()
            self.assertFalse(costmap.integrate_camera(observation, now=101))
            self.assertEqual(max(costmap.conf), 0)
            self.assertFalse(costmap.snapshot()["camera_use"]["used"])

    def test_fresh_real_camera_can_mark_obstacles(self):
        costmap = LocalCostmap()
        self.assertTrue(costmap.integrate_camera(self.observation(), now=101))
        self.assertGreater(max(costmap.conf), 0)

    def test_simulation_requires_explicit_context_and_dummy_is_always_ignored(self):
        observation = self.observation(provenance="simulated", source="simblob")
        costmap = LocalCostmap()
        self.assertFalse(costmap.integrate_camera(observation, now=101))
        self.assertTrue(costmap.integrate_camera(observation, now=101, allow_simulated=True))
        self.assertGreater(max(costmap.conf), 0)
        costmap = LocalCostmap()
        self.assertFalse(costmap.integrate_camera(self.observation(provenance="simulated", source="dummy"),
                                                now=101, allow_simulated=True))
        self.assertEqual(max(costmap.conf), 0)
        self.assertIn("no usable", costmap.camera_use["ignored_reason"])


if __name__ == "__main__":
    unittest.main()
