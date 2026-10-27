import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from rate_limits import default_delay, five_hour_reset, read_five_hour_reset


class RateLimitTests(unittest.TestCase):
    def test_selects_five_hour_codex_window_and_adds_exactly_one_minute(self):
        result = {"rateLimitsByLimitId": {
            "other": {"primary": {"windowDurationMins": 300, "resetsAt": 9000}},
            "codex": {"primary": {"windowDurationMins": 300, "resetsAt": 2000, "usedPercent": 64},
                      "secondary": {"windowDurationMins": 10080, "resetsAt": 8000}}}}
        reset = five_hour_reset(result, now=1000)
        self.assertEqual(reset.resets_at, 2000)
        self.assertEqual(reset.send_at, 2060)
        self.assertEqual(reset.used_percent, 64)

    def test_legacy_single_bucket_and_secondary_window(self):
        reset = five_hour_reset({"rateLimits": {
            "primary": {"windowDurationMins": 10080, "resetsAt": 50000},
            "secondary": {"windowDurationMins": 300, "resetsAt": 2000}}}, now=1000)
        self.assertEqual(reset.send_at, 2060)

    def test_unavailable_expired_or_weekly_only_does_not_invent_a_default(self):
        for result in [{}, {"rateLimits": None}, {"rateLimits": {"primary": {
            "windowDurationMins": 10080, "resetsAt": 9000}}}, {"rateLimits": {"primary": {
            "windowDurationMins": 300, "resetsAt": 900}}}, {"rateLimits": {"primary": {
            "windowDurationMins": 300, "resetsAt": None}}}]:
            with self.subTest(result=result):
                self.assertIsNone(five_hour_reset(result, now=1000))

    def test_display_rounds_up_without_changing_absolute_deadline(self):
        self.assertEqual(default_delay(4600.5, 1000), "1:01")
        self.assertEqual(default_delay(4600, 1000), "1:00")
        self.assertEqual(default_delay(999, 1000), "0:00")

    def test_real_rpc_transport_only_reads_limits_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake_codex.py"
            calls = Path(directory) / "calls.jsonl"
            reset = int(time.time()) + 2000
            script.write_text(
                "import json,sys,pathlib\n"
                f"calls=pathlib.Path({str(calls)!r})\n"
                "for line in sys.stdin:\n"
                "    request=json.loads(line)\n"
                "    with calls.open('a',encoding='utf-8') as f:f.write(json.dumps(request)+'\\n')\n"
                "    if request['method']=='initialize':\n"
                "        print(json.dumps({'id':request['id'],'result':{}}),flush=True)\n"
                "    elif request['method']=='account/rateLimits/read':\n"
                "        print(json.dumps({'method':'account/updated','params':{}}),flush=True)\n"
                f"        print(json.dumps({{'id':request['id'],'result':{{'rateLimits':{{'primary':{{'windowDurationMins':300,'resetsAt':{reset},'usedPercent':64}}}}}}}}),flush=True)\n",
                encoding="utf-8")
            real_popen = subprocess.Popen
            processes = []

            def spawn(command, **options):
                self.assertEqual(command, ["fake-codex", "app-server", "--listen", "stdio://"])
                process = real_popen([sys.executable, str(script)], **options)
                processes.append(process)
                return process

            with patch("rate_limits.subprocess.Popen", side_effect=spawn):
                info = read_five_hour_reset("fake-codex", timeout=5)
            self.assertEqual(info.send_at, reset + 60)
            self.assertIsNotNone(processes[0].poll())
            self.assertTrue(processes[0].stdin.closed)
            self.assertTrue(processes[0].stdout.closed)
            methods = [json.loads(line)["method"] for line in calls.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(methods, ["initialize", "initialized", "account/rateLimits/read"])

    def test_rpc_timeout_cleans_up_without_blocking_the_ui(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "quiet_server.py"
            script.write_text("import sys\nfor line in sys.stdin: pass\n", encoding="utf-8")
            real_popen = subprocess.Popen
            processes = []

            def spawn(command, **options):
                process = real_popen([sys.executable, str(script)], **options)
                processes.append(process)
                return process

            with patch("rate_limits.subprocess.Popen", side_effect=spawn):
                with self.assertRaises(TimeoutError):
                    read_five_hour_reset("fake-codex", timeout=.1)
            self.assertIsNotNone(processes[0].poll())


if __name__ == "__main__":
    unittest.main()
