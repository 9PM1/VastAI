"""Offline tests for the fixer's matching: generic names, folder checks, Civitai word search, file-type guard."""
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("XTRA_WORKSPACE", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))
from xtra import downloads, fixer  # noqa: E402

KOLORS_VAE = {"name": "Kolors VAE", "type": "VAE", "base": "Kolors", "save_path": "vae/kolors", "filename": "diffusion_pytorch_model.safetensors",
              "url": "https://huggingface.co/Kwai-Kolors/Kolors/resolve/main/vae/diffusion_pytorch_model.safetensors", "size": "335MB"}
CN_SDXL = {"name": "xinsir/ControlNet++: All-in-one ControlNet", "type": "controlnet", "base": "SDXL", "save_path": "controlnet/SDXL/controlnet-union-sdxl-1.0",
           "filename": "diffusion_pytorch_model.safetensors", "size": "2.5GB",
           "url": "https://huggingface.co/xinsir/controlnet-union-sdxl-1.0/resolve/main/diffusion_pytorch_model.safetensors"}
CN_FLUX = {"name": "InstantX FLUX.1-dev ControlNet Union", "type": "controlnet", "base": "FLUX.1", "save_path": "controlnet/FLUX.1/InstantX-FLUX1-Dev-Union",
           "filename": "diffusion_pytorch_model.safetensors", "size": "6.6GB",
           "url": "https://huggingface.co/InstantX/FLUX.1-dev-Controlnet-Union/resolve/main/diffusion_pytorch_model.safetensors"}


class Names(unittest.TestCase):
    def test_generic(self):
        for n in ("diffusion_pytorch_model.safetensors", "diffusion_pytorch_model.fp16.safetensors", "model.safetensors",
                  "pytorch_lora_weights.safetensors", "ip-adapter.bin", "model-00001-of-00002.safetensors"):
            self.assertTrue(fixer.is_generic_name(n), n)
        for n in ("ae.safetensors", "krea2_turbo_fp8_scaled.safetensors", "control_v11p_sd15_canny.pth", "add_detail.safetensors"):
            self.assertFalse(fixer.is_generic_name(n), n)

    def test_filename_words(self):
        self.assertEqual(fixer.filename_words("RealisticSnapshotKrea2V2"), ["Realistic", "Snapshot"])
        self.assertEqual(fixer.filename_words("police-uniform_Krea2_V1.0"), ["police", "uniform"])
        self.assertEqual(fixer.filename_words("HyperDetailedPencil_H3"), ["Hyper", "Detailed", "Pencil"])

    def test_path_fits(self):
        self.assertFalse(fixer.path_fits("vae/diffusion_pytorch_model.safetensors", "controlnet"))
        self.assertTrue(fixer.path_fits("controlnet/SDXL/x/diffusion_pytorch_model.safetensors", "controlnet"))
        self.assertTrue(fixer.path_fits("diffusion_pytorch_model.safetensors", "controlnet"))  # top level says nothing
        self.assertTrue(fixer.path_fits("split_files/text_encoders/umt5.safetensors", "clip"))
        self.assertFalse(fixer.path_fits("split_files/vae/ae.safetensors", "diffusion_models"))

    def test_base_family(self):
        self.assertEqual(fixer.base_family("amanatsuIllustrious_v2.safetensors"), "sdxl")
        self.assertEqual(fixer.base_family("SDXL"), "sdxl")
        self.assertEqual(fixer.base_family("FLUX.1"), "flux")


class FindModel(unittest.TestCase):
    def setUp(self):
        self.saved = (fixer.manager_models, fixer.hf_index, fixer.civitai_find, fixer._hf_accessible)
        fixer.hf_index = lambda wait=False: {}
        fixer._hf_accessible = lambda url, token="": True
        self.civitai_calls = []
        fixer.civitai_find = lambda *a, **k: self.civitai_calls.append(a) or None

    def tearDown(self):
        fixer.manager_models, fixer.hf_index, fixer.civitai_find, fixer._hf_accessible = self.saved

    def test_generic_controlnet_never_gets_the_kolors_vae(self):
        fixer.manager_models = lambda: {"diffusion_pytorch_model.safetensors": [CN_SDXL, KOLORS_VAE, CN_FLUX]}
        found = fixer.find_model("diffusion_pytorch_model.safetensors", "controlnet", {}, [])
        self.assertIsNone(found["candidate"])  # never auto-downloaded
        urls = [p["url"] for p in found["possible"]]
        self.assertNotIn(KOLORS_VAE["url"], urls)
        self.assertEqual(set(urls), {CN_SDXL["url"], CN_FLUX["url"]})
        self.assertEqual(self.civitai_calls, [])  # no pointless Civitai search for a generic name

    def test_plan_ranks_suggestions_by_workflow_base(self):
        fixer.manager_models = lambda: {"diffusion_pytorch_model.safetensors": [CN_FLUX, CN_SDXL]}
        fixer.missing_nodes = lambda wf, reported=None: []
        wf = {"nodes": [{"type": "CheckpointLoaderSimple", "widgets_values": ["amanatsuIllustrious_v2.safetensors"]},
                        {"type": "ControlNetLoader", "widgets_values": ["diffusion_pytorch_model.safetensors"]}]}
        plan = fixer.plan(wf, [], {})
        cn = next(m for m in plan["models"] if m["category"] == "controlnet")
        self.assertIsNone(cn["fix"])
        self.assertEqual(cn["possible"][0]["url"], CN_SDXL["url"])
        self.assertTrue(cn["possible"][0]["fits_base"])
        self.assertIn("2 different models", cn["note"])

    def test_wrong_type_manager_entry_is_skipped_for_specific_names(self):
        vae = dict(KOLORS_VAE, filename="kolors_vae.safetensors")
        fixer.manager_models = lambda: {"kolors_vae.safetensors": [vae]}
        self.assertIsNone(fixer.find_model("kolors_vae.safetensors", "controlnet", {}, []))
        found = fixer.find_model("kolors_vae.safetensors", "vae", {}, [])
        self.assertEqual(found["candidate"]["url"], vae["url"])

    def test_library_alias(self):
        fixer.manager_models = lambda: {}
        lib = [{"id": "x1", "filename": "Realistic_Snapshot.safetensors", "aliases": ["RealisticSnapshotKrea2V2.safetensors"],
                "url": "https://civitai.com/api/download/models/1", "category": "loras", "source": "civitai"}]
        found = fixer.find_model("RealisticSnapshotKrea2V2.safetensors", "loras", {}, lib)
        self.assertEqual(found["origin"], "library")
        self.assertEqual(found["candidate"]["filename"], "RealisticSnapshotKrea2V2.safetensors")


def civ_model(mid, name, files):
    return {"id": mid, "name": name, "stats": {"downloadCount": mid},
            "modelVersions": [{"id": mid * 10, "name": "v1", "baseModel": "Illustrious", "images": [],
                               "files": [{"id": mid * 100 + i, "name": fn, "sizeKB": 1000, "primary": i == 0} for i, fn in enumerate(files)]}]}


class CivitaiFind(unittest.TestCase):
    def setUp(self):
        self.saved = (fixer.sources._get_json, fixer.sources.civitai_version)
        self.queries = []
        fixer.sources.civitai_version = lambda vid, token="", fid=None, nsfw=True: {"civitai": {"version_id": vid, "file_id": fid}, "name": str(vid), "category": "loras"}

    def tearDown(self):
        fixer.sources._get_json, fixer.sources.civitai_version = self.saved

    def search(self, results):
        def fake(url, headers=None, params=None):
            self.queries.append(params["query"])
            return {"items": results.get(params["query"], [])}
        fixer.sources._get_json = fake

    def test_finds_file_through_its_words(self):
        # Civitai's search matches model names: the raw file name finds nothing, its words find the model.
        self.search({"Realistic Snapshot": [civ_model(7, "Realistic Snapshot (Krea 2)", ["RealisticSnapshotKrea2V2.safetensors"])]})
        r = fixer.civitai_find("RealisticSnapshotKrea2V2.safetensors", "loras", {})
        self.assertEqual(r["confidence"], "exact")
        self.assertEqual(r["candidate"]["civitai"]["version_id"], 70)
        self.assertEqual(self.queries, ["RealisticSnapshotKrea2V2", "Realistic Snapshot"])  # stops at the exact match

    def test_same_file_name_in_two_models_asks(self):
        self.search({"Infrared": [civ_model(1, "Infrared Photos", ["Infrared.safetensors"]), civ_model(2, "Infrared", ["Infrared.safetensors"])]})
        r = fixer.civitai_find("Infrared.safetensors", "loras", {})
        self.assertEqual(r["confidence"], "possible")
        self.assertEqual({p["model_id"] for p in r["possible"]}, {1, 2})
        self.assertIn("2 different Civitai models", r["note"])

    def test_short_near_names_are_not_likely(self):
        self.search({"ARCHER2": [civ_model(3, "Archer - Clash of Clans", ["ARCHER.safetensors"])]})
        r = fixer.civitai_find("ARCHER2.safetensors", "loras", {})
        self.assertNotEqual((r or {}).get("confidence"), "likely")

    def test_unavailable_version_does_not_break_search(self):
        def broken(vid, token="", fid=None, nsfw=True):
            raise fixer.sources.SourceError("Not found")
        fixer.sources.civitai_version = broken
        self.search({"add_detail": [civ_model(4, "Detail Tweaker", ["add_detail.safetensors"])]})
        r = fixer.civitai_find("add_detail.safetensors", "loras", {})  # no exception
        self.assertEqual(r["confidence"], "possible")  # still offered, to try by hand


from xtra import util, workflows  # noqa: E402

NOTE = """## Model Links

**Video VAE**
- [minimax_h3_video_vae_int8_convrot.safetensors](https://huggingface.co/Kijai/MiniMax-H3-experimental/resolve/main/minimax_h3_video_vae_int8_convrot.safetensors?download=true)

**vae_approx (TAE)**
- [taeh3.safetensors](https://huggingface.co/Kijai/MiniMax-H3-TAE/resolve/main/vae_approx/taeh3.safetensors?download=true)

### Frame Interpolation
- [frame interpolation models](https://huggingface.co/Comfy-Org/frame_interpolation/tree/main/frame_interpolation) (optional)

## Requirements
- [ComfyUI-DaSiWa-Nodes](https://github.com/darksidewalker/ComfyUI-DaSiWa-Nodes)
"""
SG = "e0940f57-80b2-480e-8a38-36968dc1763d"


def stress_workflow():
    """The shape of DaSiWa's MiniMax H3 workflow: a settings subgraph overriding inner loaders,
    bypassed optional loaders, and the download links in a note."""
    return {
        "nodes": [
            {"id": 1, "type": "MarkdownNote", "widgets_values": [NOTE]},
            {"id": 2, "type": SG, "mode": 0, "widgets_values": ["euler", "MiniMaxH3/dasiwa_turbo_int8.safetensors", "MiniMaxH3/minimax_h3_video_vae_int8_convrot.safetensors"]},
            {"id": 3, "type": "ModelPreviewOverrideKJ", "widgets_values": [512, "taeh3.safetensors"]},
            {"id": 4, "type": "FrameInterpolationModelLoader", "mode": 4, "widgets_values": ["rife_v4.26.safetensors"]},
        ],
        "definitions": {"subgraphs": [{
            "id": SG, "name": "Checkpoint settings",
            "inputs": [{"name": "sampler_name", "type": "COMBO", "linkIds": [10]},
                       {"name": "unet_name", "type": "COMBO", "linkIds": [11]},
                       {"name": "vae_name", "type": "COMBO", "linkIds": [12]},
                       {"name": "model", "type": "MODEL", "linkIds": [13]}],
            "links": [{"id": 11, "origin_id": -10, "target_id": 20, "target_slot": 0},
                      {"id": 12, "origin_id": -10, "target_id": 21, "target_slot": 0}],
            "nodes": [
                {"id": 20, "type": "UNETLoader", "widgets_values": ["MiniMaxH3/minimax_h3_fl2va_pruned_int8_convrot.safetensors", "default"],
                 "properties": {"models": [{"name": "minimax_h3_fl2va_pruned_int8_convrot.safetensors", "directory": "diffusion_models",
                                            "url": "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors"}]}},
                {"id": 21, "type": "VAELoader", "widgets_values": ["MiniMaxH3/minimax_h3_video_vae_fp16.safetensors"]},
            ]}]},
    }


class WorkflowHints(unittest.TestCase):
    def test_subgraph_values_override_inner_loaders(self):
        refs = {r["name"]: r for r in workflows.extract(stress_workflow())}
        turbo = refs["MiniMaxH3/dasiwa_turbo_int8.safetensors"]
        self.assertEqual((turbo["category"], turbo["active"]), ("diffusion_models", True))
        self.assertTrue(refs["MiniMaxH3/minimax_h3_video_vae_int8_convrot.safetensors"]["active"])
        self.assertNotIn("MiniMaxH3/minimax_h3_video_vae_fp16.safetensors", refs)  # replaced by the subgraph's value
        fl2va = next(r for r in refs.values() if "fl2va" in r["name"])
        self.assertFalse(fl2va["active"])  # only listed in the template's model links

    def test_switched_off_nodes_are_optional(self):
        refs = {r["name"]: r for r in workflows.extract(stress_workflow())}
        self.assertFalse(refs["rife_v4.26.safetensors"]["active"])
        self.assertEqual(refs["rife_v4.26.safetensors"]["category"], "frame_interpolation")
        self.assertEqual(refs["taeh3.safetensors"]["category"], "vae_approx")

    def test_note_links_and_headings(self):
        h = workflows.hints(stress_workflow())
        files = {f["filename"]: f for f in h["files"]}
        self.assertEqual(files["taeh3.safetensors"]["category"], "vae_approx")
        self.assertEqual(files["minimax_h3_video_vae_int8_convrot.safetensors"]["category"], "vae")
        self.assertTrue(files["taeh3.safetensors"]["url"].endswith("/vae_approx/taeh3.safetensors"))  # no ?download=true
        self.assertEqual(h["folders"], [{"repo": "Comfy-Org/frame_interpolation", "folder": "frame_interpolation",
                                         "revision": "main", "category": "frame_interpolation"}])
        self.assertEqual(h["github"][0]["url"], "https://github.com/darksidewalker/ComfyUI-DaSiWa-Nodes")

    def test_plan_uses_notes_keeps_subfolders_and_skips_optional(self):
        saved = (fixer.manager_models, fixer.hf_index, fixer.civitai_find, fixer._repo_files, fixer.missing_nodes, fixer.openmodeldb_index)
        fixer.manager_models = lambda: {}
        fixer.hf_index = lambda wait=False: {}
        fixer.civitai_find = lambda *a, **k: None
        fixer.openmodeldb_index = lambda: {}
        fixer.missing_nodes = lambda wf, reported=None, hints=None: []
        fixer._repo_files = lambda repo, revision="main", folder="", token="": (
            [{"path": "frame_interpolation/rife_v4.26.safetensors", "size": 21_000_000,
              "url": "https://huggingface.co/Comfy-Org/frame_interpolation/resolve/main/frame_interpolation/rife_v4.26.safetensors"}]
            if repo == "Comfy-Org/frame_interpolation" else [])
        try:
            plan = fixer.plan(stress_workflow(), [], {})
        finally:
            (fixer.manager_models, fixer.hf_index, fixer.civitai_find, fixer._repo_files, fixer.missing_nodes, fixer.openmodeldb_index) = saved
        by = {m["filename"]: m for m in plan["models"]}
        vae = by["minimax_h3_video_vae_int8_convrot.safetensors"]
        self.assertEqual(vae["origin"], "workflow-note")
        self.assertEqual(vae["fix"]["candidate"]["filename"], "MiniMaxH3/minimax_h3_video_vae_int8_convrot.safetensors")
        rife = by["rife_v4.26.safetensors"]
        self.assertTrue(rife["optional"])
        self.assertEqual(rife["origin"], "workflow-note:Comfy-Org/frame_interpolation")
        self.assertEqual(plan["summary"]["models_optional"], 2)  # rife + the overridden fl2va
        self.assertEqual(plan["summary"]["unresolved"], ["dasiwa_turbo_int8.safetensors"])
        self.assertEqual(plan["summary"]["download_bytes"], 0 + (vae["fix"]["size"] or 0) + (by["taeh3.safetensors"]["fix"]["size"] or 0))

    def test_pack_from_notes(self):
        hints = workflows.hints(stress_workflow())
        p = fixer._pack_from_notes("DaSiWa_Watermark", hints, {})
        self.assertEqual(p["git_url"], "https://github.com/darksidewalker/ComfyUI-DaSiWa-Nodes")
        self.assertIsNone(fixer._pack_from_notes("SomethingElse", {"github": []}, {}))
        # Once the DaSiWa pack is installed, a DaSiWa-named type isn't blamed on another linked pack.
        two = {"github": hints["github"] + [{"url": "https://github.com/city96/ComfyUI-GGUF", "owner": "city96", "repo": "ComfyUI-GGUF"}]}
        installed = {"dasiwa": {"id": "dasiwa", "repository": "https://github.com/darksidewalker/ComfyUI-DaSiWa-Nodes", "state": "enabled"}}
        self.assertIsNone(fixer._pack_from_notes("Lable (DaSiWa)", two, installed))

    def test_display_only_nodes_are_not_missing(self):
        wf = {"nodes": [{"id": 1, "type": "Lable (DaSiWa)", "inputs": [], "outputs": [], "properties": {}},
                        {"id": 2, "type": "DaSiWa_Watermark", "inputs": [{"name": "image"}], "outputs": [{"name": "IMAGE"}]}]}
        self.assertEqual(list(fixer.workflow_node_types(wf)), ["DaSiWa_Watermark"])


class ModelPaths(unittest.TestCase):
    def test_subfolders_only_when_allowed_and_contained(self):
        p = util.model_path("vae", "MiniMaxH3/x.safetensors", subdirs=True)
        self.assertEqual((p.parent.name, p.name), ("MiniMaxH3", "x.safetensors"))
        for bad in ("../x.safetensors", "a/../../x.safetensors", "/etc/passwd", "a/b/c/d/e.safetensors"):
            with self.assertRaises(ValueError):
                util.model_path("vae", bad, subdirs=True)
        with self.assertRaises(ValueError):
            util.model_path("vae", "MiniMaxH3/x.safetensors")


class FileTypeGuard(unittest.TestCase):
    def write(self, keys):
        header = json.dumps({k: {"dtype": "F16", "shape": [1], "data_offsets": [0, 2]} for k in keys}).encode()
        f = tempfile.NamedTemporaryFile(suffix=".safetensors", delete=False)
        f.write(struct.pack("<Q", len(header)) + header + b"\0\0")
        f.close()
        return f.name

    def test_vae_detected(self):
        self.assertEqual(downloads.safetensors_kind(self.write(["decoder.conv_in.weight", "encoder.conv_out.bias", "quant_conv.weight"])), "vae")
        self.assertIsNone(downloads.safetensors_kind(self.write(["controlnet_cond_embedding.conv_in.weight", "down_blocks.0.weight"])))
        self.assertIsNone(downloads.safetensors_kind(self.write(["model.diffusion_model.x", "first_stage_model.decoder.x"])))


if __name__ == "__main__":
    unittest.main()
