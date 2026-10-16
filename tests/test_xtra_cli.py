"""Offline tests for tools/xtra.py: workflow sizing, real-cost ranking, slow and bad host handling."""
import importlib.util
import os
import tempfile
import time
import unittest
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ["XTRA_HOME"] = _tmp
_spec = importlib.util.spec_from_file_location("xtra", Path(__file__).resolve().parent.parent / "tools" / "xtra.py")
xtra = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(xtra)


def node(t, *values):
    return {"type": t, "widgets_values": list(values)}


def offer(oid, gpu, dlperf, dph, machine, mem_bw=1450, fee=0.003, ram=32768):
    return {"id": oid, "gpu_name": gpu, "dlperf": dlperf, "dph_total": dph, "machine_id": machine, "gpu_mem_bw": mem_bw,
            "inet_down_cost": fee, "gpu_ram": ram, "reliability2": 0.99}


class WorkflowNeeds(unittest.TestCase):
    def needs(self, nodes):
        return xtra.workflow_needs({"nodes": nodes}, sizes={})

    def test_wan_14b_fp8_video(self):
        n = self.needs([node("UNETLoader", "wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors", "default"),
                        node("UNETLoader", "wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors", "default"),
                        node("CLIPLoader", "umt5_xxl_fp8_e4m3fn_scaled.safetensors", "wan"),
                        node("VAELoader", "wan_2.1_vae.safetensors")])
        self.assertTrue(n["video"])
        self.assertEqual(n["min_vram_gb"], 20)
        self.assertEqual(n["comfortable_vram_gb"], 32)
        self.assertAlmostEqual(n["download_gb"], 34.0, delta=1)

    def test_sdxl_checkpoint(self):
        n = self.needs([node("CheckpointLoaderSimple", "juggernautXL_v9.safetensors"),
                        node("LoraLoader", "detail.safetensors", 1, 1)])
        self.assertFalse(n["video"])
        self.assertEqual(n["min_vram_gb"], 10)

    def test_gguf_quant_and_explicit_params(self):
        self.assertAlmostEqual(xtra.estimate_size_gb("flux1-dev-Q4_K_S.gguf", "diffusion")[0], 6.7, delta=0.1)
        self.assertEqual(xtra.estimate_size_gb("qwen_2.5_vl_7b_fp8_scaled.safetensors", "text")[0], 7.0)
        self.assertEqual(xtra.estimate_size_gb("t5xxl_fp16.safetensors", "text")[0], 9.6)
        self.assertEqual(xtra.estimate_size_gb("v1-5-pruned-emaonly-fp16.safetensors", "checkpoint")[0], 2.1)
        self.assertIsNone(xtra.estimate_size_gb("mystery.safetensors", "other")[0])

    def test_api_format_and_exact_sizes(self):
        wf = {"1": {"class_type": "UNETLoader", "inputs": {"unet_name": "x.safetensors"}},
              "2": {"class_type": "KSampler", "inputs": {"seed": 1}}}
        n = xtra.workflow_needs(wf, sizes={"x.safetensors": 23.8})
        self.assertEqual(n["files"][0]["how"], "exact")
        self.assertEqual(n["min_vram_gb"], 32)


class Ranking(unittest.TestCase):
    def setUp(self):
        self.offers = [
            offer(1, "RTX 5090", 200, 0.70, 11), offer(2, "RTX 5090", 199, 0.60, 12, fee=0.0156),
            offer(3, "RTX 5090", 99, 0.50, 13, mem_bw=712), offer(4, "RTX 5090", 201, 0.80, 14),
            offer(5, "RTX 5060 Ti", 38.4, 0.18, 15),
        ]

    def test_slow_hosts_hidden(self):
        ranked, hidden = xtra.rank_offers([dict(o) for o in self.offers], [], download_gb=50, hours=4)
        self.assertNotIn(3, [o["id"] for o in ranked])
        self.assertEqual(hidden["slow"], 1)
        ranked, _ = xtra.rank_offers([dict(o) for o in self.offers], [], allow_slow=True)
        self.assertIn(3, [o["id"] for o in ranked])

    def test_download_fee_counts(self):
        ranked, _ = xtra.rank_offers([dict(o) for o in self.offers], [], download_gb=50, hours=4)
        by = {o["id"]: o for o in ranked}
        self.assertAlmostEqual(by[2]["_fee"], 0.78, places=2)
        self.assertGreater(by[2]["_cost_h"], by[1]["_cost_h"])  # "cheaper" listing is dearer in practice

    def test_own_benchmark_overrides_estimate(self):
        ranked, _ = xtra.rank_offers([dict(o) for o in self.offers], [{"gpu": "NVIDIA GeForce RTX 5090", "images_per_hour": 12000, "dlperf": 200}])
        by = {o["id"]: o for o in ranked}
        self.assertTrue(by[1]["_measured"])
        self.assertAlmostEqual(by[1]["_ips"], 12000, delta=100)
        self.assertFalse(by[5]["_measured"])

    def test_speed_vs_value(self):
        value, _ = xtra.rank_offers([dict(o) for o in self.offers], [], mode="value")
        speed, _ = xtra.rank_offers([dict(o) for o in self.offers], [], mode="speed")
        self.assertEqual([o["id"] for o in value], [o["id"] for o in sorted(value, key=lambda o: -o["_ipd"])])
        self.assertNotEqual(value[0]["id"], 4)  # the fastest 5090 is also the dearest, so not the best value
        self.assertEqual(speed[0]["id"], 4)  # fastest healthy 5090
        by = {o["id"]: o for o in speed}
        self.assertAlmostEqual(by[4]["_ips"] / by[5]["_ips"], 1.35 / .38, delta=0.05)

    def test_bad_hosts_skipped(self):
        ranked, hidden = xtra.rank_offers([dict(o) for o in self.offers], [], bad={"11": {}})
        self.assertNotIn(1, [o["id"] for o in ranked])
        self.assertEqual(hidden["bad"], 1)


class BadHostMemory(unittest.TestCase):
    def test_remember_expire_and_forget(self):
        xtra.save_json(xtra.BAD_HOSTS_FILE, {"old": {"at": time.time() - 40 * 86400}})
        xtra.remember_bad(777, "RTX 5090", "power limited")
        shared = {"hosts": [{"machine_id": 888, "score": "poor", "warnings": ["x4 PCIe"], "at": time.time()},
                            {"machine_id": 999, "score": "good", "warnings": [], "at": time.time()}]}
        bad = xtra.bad_hosts(shared)
        self.assertEqual(set(bad), {"777", "888"})
        xtra.shared_data = lambda refresh=False: shared
        xtra.forget_bad("888")
        self.assertEqual(set(xtra.bad_hosts(shared)), {"777"})


class AutoSwap(unittest.TestCase):
    """rent --auto-swap with Vast faked: the first host fails the check, the second passes."""

    def run_rent(self, auto_swap, healths):
        calls, saved = [], {}
        offers = [dict(offer(i, "RTX 4090", 96, 0.5 + i / 100, 100 + i), _hourly=0.5, _fee=0.1, _cost_h=0.53, _ips=8900,
                       _ipd=16000, _perf=1.0, _measured=True, inet_down=1000, disk_bw=3000, geolocation="X") for i in (1, 2, 3)]
        fakes = {
            "template": lambda: {"hash_id": "h"},
            "shared_data": lambda refresh=False: {"benchmarks": [], "hosts": []},
            "search_offers": lambda a, machine_id=None, bad=None, shared=None: ([o for o in offers if str(o["machine_id"]) not in bad], {"slow": 0, "bad": 0}),
            "vast": lambda *args, **kw: calls.append(args) or ({"new_contract": 900 + len(calls)} if args[0] == "create" else ""),
            "wait_ready": lambda iid: {"id": iid},
            "wait_health": lambda inst: healths.pop(0),
            "dash": lambda *args, **kw: None,
            "show_status": lambda inst: None,
        }
        for k, v in fakes.items():
            saved[k] = getattr(xtra, k)
            setattr(xtra, k, v)
        try:
            xtra.save_json(xtra.BAD_HOSTS_FILE, {})
            args = ["rent", "--yes", "--gpu", "RTX_4090"] + (["--auto-swap"] if auto_swap else [])
            import sys
            argv, sys.argv = sys.argv, ["xtra"] + args
            try:
                xtra.main()
            finally:
                sys.argv = argv
        finally:
            for k, v in saved.items():
                setattr(xtra, k, v)
        return calls

    def test_swaps_poor_host_and_remembers_it(self):
        poor = {"score": "poor", "warnings": ["Power limited to 60%"]}
        calls = self.run_rent(True, [poor, {"score": "good", "warnings": []}])
        self.assertEqual([c[:3] for c in calls], [("create", "instance", "1"), ("destroy", "instance", "901"), ("create", "instance", "2")])
        self.assertIn("101", xtra.bad_hosts())

    def test_without_auto_swap_keeps_it(self):
        calls = self.run_rent(False, [{"score": "poor", "warnings": ["x4 PCIe"]}])
        self.assertEqual([c[0] for c in calls], ["create"])
        self.assertIn("101", xtra.bad_hosts())


if __name__ == "__main__":
    unittest.main()
