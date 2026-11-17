from __future__ import annotations

import unittest

from mysearch.research.software_version import _apply_software_version_answer_override


class CurrentVersionEvidenceTests(unittest.TestCase):
    def answer(self, snippets, original=""):
        result = {"answer": original, "results": [
            {"url": f"https://www.python.org/downloads/{index}", "title": "Download Python", "snippet": snippet}
            for index, snippet in enumerate(snippets)
        ]}
        return _apply_software_version_answer_override(
            query="latest stable version of Python", mode="web", intent="factual", result=result,
        )["answer"]

    def test_source_and_month_do_not_imply_release_candidate(self):
        for text in ("Python 8.2.1 is the latest stable source release.",
                     "Python 8.2.1 is the latest stable release, published in March."):
            with self.subTest(text=text):
                self.assertEqual(self.answer([text]), "The latest stable version of Python is 8.2.1.")

    def test_prerelease_suffixes_and_development_are_rejected(self):
        for version in ("8.3.0rc1", "8.3.0b1", "8.3.0a1", "8.3.0-beta", "8.3.0 RC1",
                        "8.3.0-dev", "8.3.0-dev.1", "8.3.0.dev1", "8.3.0-1"):
            with self.subTest(version=version):
                self.assertEqual(self.answer([f"Python {version} is the latest release."]), "")
        self.assertEqual(self.answer(["The future development version is Python 8.3.0."]), "")

    def test_latest_does_not_leak_into_history_or_version_tables(self):
        latest = "Latest Python 8 Release - Python 8.2.1\n\n## Stable Releases\nPython 8.0.0 - Oct. 2, 2023"
        download = "# Download the latest version of Python\n\nDownload Python 8.2.2"
        self.assertEqual(self.answer([latest, download]), "The latest stable version of Python is 8.2.2.")
        for text in (
            "Latest release information.\n## Stable Releases\nPython 8.0.0",
            "Supported versions: Python 8.0 Python 8.1 Python 9.0",
            "Python 8.0.0 was the latest stable release in 2023.",
            "Python 8.0.0 is the latest LTS release.",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.answer([text]), "")

    def test_immediate_download_caption_only(self):
        self.assertEqual(self.answer([
            "Download the latest version of Python. Download Python 8.2.2.\nPython 9.0.0 is a preview."
        ]), "The latest stable version of Python is 8.2.2.")
        self.assertEqual(self.answer([
            "Download the latest version of Python.\nSee archived releases.\nDownload Python 8.0.0."
        ]), "")

    def test_a_historical_mention_cannot_override_an_existing_answer(self):
        original = "The current stable release needs live verification."
        self.assertEqual(self.answer(["Python 8.0.0 released in March 2023."], original), original)

    def test_download_link_caption_keeps_the_immediate_heading(self):
        caption = "[Download Python 8.2.2](https://www.python.org/downloads/release/python-822/)"
        self.assertEqual(self.answer(["Download the latest version of Python\n\n" + caption]),
                         "The latest stable version of Python is 8.2.2.")
        self.assertEqual(self.answer(["Download the latest version of Python\nArchived releases\n" + caption]), "")
