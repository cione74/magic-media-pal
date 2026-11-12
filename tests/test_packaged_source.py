"""A passing suite must not conceal stale application files in the image."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('packaged_source', Path(__file__).resolve().parents[1] / 'tools/verify-packaged-source.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class PackagedSourceTests(unittest.TestCase):
    def test_exact_image_required_and_empty_inventory_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source'; image = root / 'image'
            source.mkdir(); image.mkdir()
            with self.assertRaises(ValueError): module.verify(source, image)
            (source / 'app.py').write_text('candidate')
            (image / 'app.py').write_text('stale')
            with self.assertRaisesRegex(ValueError, 'app.py'): module.verify(source, image)
            (image / 'app.py').write_text('candidate')
            self.assertEqual(module.verify(source, image), 1)
            (image / 'app.py').unlink()
            with self.assertRaisesRegex(ValueError, 'app.py'): module.verify(source, image)
