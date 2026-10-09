import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.verify_package import VerificationError, verify_installed, verify_wheel


class VerifyPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.package = self.root / "source" / "dockergo"
        (self.package / "display").mkdir(parents=True)
        (self.package / "__init__.py").write_bytes(b"__version__ = 'test'\n")
        (self.package / "discord.py").write_bytes(b"class DiscordNotifier: pass\n")
        (self.package / "display" / "ui.py").write_bytes(b"def render(): pass\n")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_verifies_wheel_python_modules(self):
        wheel_path = self.root / "dockergo.whl"
        with zipfile.ZipFile(wheel_path, "w") as wheel:
            for source_file in self.package.rglob("*.py"):
                relative = source_file.relative_to(self.package).as_posix()
                wheel.writestr(f"dockergo/{relative}", source_file.read_bytes())

        self.assertEqual(verify_wheel(self.package.parent, wheel_path), 3)

    def test_rejects_wheel_with_missing_module(self):
        wheel_path = self.root / "dockergo.whl"
        with zipfile.ZipFile(wheel_path, "w") as wheel:
            wheel.writestr("dockergo/__init__.py", b"__version__ = 'test'\n")

        with self.assertRaisesRegex(VerificationError, "missing dockergo/discord.py"):
            verify_wheel(self.package.parent, wheel_path)

    def test_rejects_installed_module_that_differs(self):
        installed = self.root / "installed" / "dockergo"
        (installed / "display").mkdir(parents=True)
        for source_file in self.package.rglob("*.py"):
            target = installed / source_file.relative_to(self.package)
            target.write_bytes(source_file.read_bytes())
        (installed / "discord.py").write_bytes(b"class OldNotifier: pass\n")

        with self.assertRaisesRegex(VerificationError, "differs from source:.*discord.py"):
            verify_installed(self.package.parent, installed)


if __name__ == "__main__":
    unittest.main()