"""--json работает и до, и после подкоманды (агенты пишут `check --json`)."""
from __future__ import annotations

import contextlib
import io
import json
import unittest

from _ledger_env import LedgerCase, cli


class JsonFlagTest(LedgerCase):
    def _run(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(argv)
        return code, json.loads(buf.getvalue().strip().splitlines()[-1])

    def test_json_after_subcommand(self):
        code, p = self._run(["check", "--json"])
        self.assertIn("ok", p)
        code2, p2 = self._run(["--json", "check"])
        self.assertEqual(p.keys(), p2.keys())


if __name__ == "__main__":
    unittest.main()
