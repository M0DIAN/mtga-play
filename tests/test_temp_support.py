import os
from pathlib import Path
import unittest
from unittest.mock import patch

from temp_support import test_directory


class TestDirectoryTests(unittest.TestCase):
    def test_exception_cleans_owned_child_and_preserves_sibling(self):
        with test_directory() as parent:
            root = Path(parent) / 'explicit-root'
            root.mkdir()
            sibling = root / 'keep.txt'
            sibling.write_text('unrelated', encoding='utf-8')
            with patch.dict(os.environ, {'MTGA_TEST_TMP': str(root)}):
                with self.assertRaisesRegex(RuntimeError, 'fixture setup failed'):
                    with test_directory() as directory:
                        child = Path(directory)
                        self.assertEqual(child.parent, root)
                        (child / 'Player.log').write_bytes(b'synthetic test data')
                        raise RuntimeError('fixture setup failed')
            self.assertFalse(child.exists())
            self.assertEqual(list(root.iterdir()), [sibling])
            self.assertEqual(sibling.read_text(encoding='utf-8'), 'unrelated')

    def test_unavailable_explicit_root_does_not_fall_back(self):
        with test_directory() as parent:
            blocked = Path(parent) / 'not-a-directory'
            blocked.write_text('keep', encoding='utf-8')
            for value, error in [(str(blocked), FileExistsError), ('', ValueError)]:
                with self.subTest(value=value), patch.dict(os.environ, {'MTGA_TEST_TMP': value}):
                    with self.assertRaises(error):
                        with test_directory():
                            self.fail('An unusable explicit root must not be ignored')
            self.assertEqual(list(Path(parent).iterdir()), [blocked])
            self.assertEqual(blocked.read_text(encoding='utf-8'), 'keep')

    def test_cleanup_denial_preserves_evidence_without_changing_permissions(self):
        with test_directory() as parent:
            root = Path(parent) / 'explicit-root'
            with patch.dict(os.environ, {'MTGA_TEST_TMP': str(root)}):
                with patch('os.unlink', side_effect=PermissionError(13, 'simulated cleanup denial')), \
                        patch('os.chmod') as chmod:
                    with self.assertRaisesRegex(PermissionError, 'simulated cleanup denial'):
                        with test_directory() as directory:
                            child = Path(directory)
                            (child / 'Player.log').write_bytes(b'synthetic test data')
                    chmod.assert_not_called()
                self.assertEqual((child / 'Player.log').read_bytes(), b'synthetic test data')


if __name__ == '__main__':
    unittest.main()
