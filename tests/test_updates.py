import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from scripts import groovebox_update


class GrooveboxUpdateArchiveTests(unittest.TestCase):
    def create_archive(self, entries):
        archive_buffer = io.BytesIO()
        with tarfile.open(fileobj=archive_buffer, mode="w:gz") as archive:
            for name, content, member_type in entries:
                member = tarfile.TarInfo(name)
                if member_type == tarfile.DIRTYPE:
                    member.type = tarfile.DIRTYPE
                    archive.addfile(member)
                elif member_type == tarfile.SYMTYPE:
                    member.type = tarfile.SYMTYPE
                    member.linkname = content.decode("utf-8")
                    archive.addfile(member)
                else:
                    member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
        return archive_buffer.getvalue()

    def test_extract_archive_copies_application_and_skips_local_data(self):
        entries = [
            ("project/app.py", b"print('Groovebox')", tarfile.REGTYPE),
            ("project/Dockerfile", b"FROM python", tarfile.REGTYPE),
            ("project/compose.yaml", b"services: {}", tarfile.REGTYPE),
            ("project/cd_library/http.py", b"handler", tarfile.REGTYPE),
            ("project/static/index.html", b"<html></html>", tarfile.REGTYPE),
            ("project/data/settings.json", b"must stay local", tarfile.REGTYPE),
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "release"
            destination.mkdir()
            groovebox_update.extract_archive(
                self.create_archive(entries),
                destination,
            )

            self.assertEqual((destination / "app.py").read_bytes(), b"print('Groovebox')")
            self.assertFalse((destination / "data").exists())

    def test_extract_archive_rejects_traversal_and_symbolic_links(self):
        unsafe_archives = (
            self.create_archive(
                [("project/../../outside", b"unsafe", tarfile.REGTYPE)]
            ),
            self.create_archive(
                [("project/escape", b"../../outside", tarfile.SYMTYPE)]
            ),
        )
        for archive_content in unsafe_archives:
            with self.subTest(archive=archive_content[:8]):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    destination = Path(temporary_directory) / "release"
                    destination.mkdir()
                    with self.assertRaises(groovebox_update.UpdateError):
                        groovebox_update.extract_archive(
                            archive_content,
                            destination,
                        )


if __name__ == "__main__":
    unittest.main()
