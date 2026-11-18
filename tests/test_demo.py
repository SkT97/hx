"""The offline demo: deterministic fake agent, synthetic fixture, no network."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

import hx
from tests import support as S


class DemoTest(unittest.TestCase):
    def test_demo_runs_end_to_end_and_cleans_up(self) -> None:
        sb = S.Sandbox()
        self.addCleanup(sb.cleanup)
        runs = sb.runs_root()
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = hx.demo(runs_root=runs)
        out = buf.getvalue()

        self.assertEqual(code, 0, out)
        self.assertIn("status=ok", out)
        self.assertIn("synthetic", out.lower())
        self.assertIn("refused", out.lower(), "demo must show a blocked accept too")
        self.assertIn("applied", out.lower())
        # nothing left behind in the demo scratch area
        self.assertEqual(sorted(p.name for p in sb.root.iterdir()), ["runs"])

    def test_demo_is_deterministic(self) -> None:
        sb = S.Sandbox()
        self.addCleanup(sb.cleanup)
        runs = sb.runs_root()
        digests = []
        for _ in range(2):
            buf = io.StringIO()
            with redirect_stdout(buf):
                hx.demo(runs_root=runs)
            line = [ln for ln in buf.getvalue().splitlines() if "sha256=" in ln][0]
            digests.append(line.split("sha256=", 1)[1].split()[0])
        self.assertEqual(len(set(digests)), 1, digests)


if __name__ == "__main__":
    unittest.main()
