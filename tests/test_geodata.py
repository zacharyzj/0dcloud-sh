from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from odcloud_sh.core import CoreManager, CoreError


class GeodataTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.manager = CoreManager(self.root)
        self.config = self.root / "subscription.yaml"

    def test_only_required_databases_download_and_cached_files_reused(self):
        self.config.write_text(yaml.safe_dump({"rules": ["GEOSITE,CN,DIRECT", "GEOIP,CN,DIRECT"]}))
        def download(url):
            if url.endswith("country.mmdb"):
                return b"x" * 200 + b"\xab\xcd\xefMaxMind.com"
            return b"\x0a" + b"x" * 200
        with patch("odcloud_sh.core._request", side_effect=download) as fetch:
            result = self.manager.install_geodata()
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual({d["name"] for d in result["databases"]}, {"GeoSite.dat", "Country.mmdb"})
            self.manager.install_geodata()
            self.assertEqual(fetch.call_count, 2)
        for name in ("GeoSite.dat", "Country.mmdb"):
            self.assertEqual((self.manager.data_dir / name).stat().st_mode & 0o777, 0o600)

    def test_geoip_dat_mode_and_invalid_download_is_not_saved(self):
        self.config.write_text(yaml.safe_dump({"rules": ["GEOIP,CN,DIRECT"], "geodata-mode": True}))
        with patch("odcloud_sh.core._request", return_value=b"<html>" * 100):
            with self.assertRaises(CoreError):
                self.manager.install_geodata()
        self.assertFalse((self.manager.data_dir / "GeoIP.dat").exists())
        with patch("odcloud_sh.core._request", return_value=b"\x0a" + b"x" * 200) as fetch:
            self.manager.install_geodata()
            self.assertTrue(fetch.call_args.args[0].endswith("/geoip.dat"))
        self.config.write_text(yaml.safe_dump({"rules": ["MATCH,DIRECT"]}))
        with patch("odcloud_sh.core._request") as fetch:
            self.assertEqual(self.manager.install_geodata(), {"databases": []})
            fetch.assert_not_called()
